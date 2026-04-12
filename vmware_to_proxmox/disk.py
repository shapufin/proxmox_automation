from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .models import DiskFormat


class DiskConversionError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# VMX parsing
# ---------------------------------------------------------------------------

_VMX_DISK_RE = re.compile(
    r'^(scsi|ide|sata|nvme)\d+:\d+\.fileName\s*=\s*"([^"]+)"', re.IGNORECASE
)


_VMX_DISK_KEY_RE = re.compile(r'^(scsi|ide|sata|nvme)(\d+):(\d+)\.filename$', re.IGNORECASE)


_VMX_DISK_TYPE_RE = re.compile(r'^(scsi|ide|sata|nvme)(\d+):(\d+)\.deviceType$', re.IGNORECASE)


_VMX_DISK_MODE_RE = re.compile(r'^(scsi|ide|sata|nvme)(\d+):(\d+)\.mode$', re.IGNORECASE)


_VMX_RDM_DEVICE_RE = re.compile(r'^scsi(\d+):(\d+)\.fileName\s*=\s*".*\.vmdk"', re.IGNORECASE)


def _split_vmx_disk_reference(value: str) -> tuple[str, str]:
    """Return (datastore, file_name) extracted from a VMX disk reference.

    VMware commonly stores VMDK paths as:
    - "[datastore1] folder/vm.vmdk"
    - "folder/vm.vmdk"
    - "vm.vmdk"
    """
    raw = (value or "").strip()
    datastore = ""
    if raw.startswith("[") and "]" in raw:
        datastore, _, remainder = raw[1:].partition("]")
        raw = remainder.strip()
    return datastore.strip(), Path(raw).name


def parse_vmx(content: str) -> dict[str, object]:
    """Parse a .vmx file (text content) and return a normalised hardware spec dict.

    Returns keys:
        name          (str)  — displayName
        memory_mb     (int)  — memsize in MB
        cpu_count     (int)  — numvcpus
        guest_os      (str)  — guestOS value
        guest_os_full_name (str) — guestFullName if available
        firmware      (str)  — 'efi' or 'bios'
        disk_files    (list[str])  — all *.vmdk references in disk order
        scsi_type     (str)  — e.g. 'lsilogic', 'pvscsi'
        networks      (list[dict]) — [{adapter, network_name, mac, virtual_dev}]
        cpu_hotplug_enabled (bool) — vcpu.hotadd setting
        memory_hotplug_enabled (bool) — mem.hotadd setting
        annotation    (str) — annotation/description
        raw           (dict[str, str])  — every parsed key=value pair
    """
    raw: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        raw[key.strip().lower()] = value.strip().strip('"')

    memory_mb = int(raw.get("memsize", 0) or 0)
    cpu_count = int(raw.get("numvcpus", 1) or 1)
    name = raw.get("displayname", "")
    guest_os = raw.get("guestos", "")
    guest_os_full_name = raw.get("guestfullname", guest_os)

    firmware_raw = raw.get("firmware", "bios").lower()
    firmware = "efi" if firmware_raw in {"efi", "uefi"} else "bios"

    # Collect disk files in slot order (scsi0:0, scsi0:1, scsi1:0 …)
    disk_entries: list[tuple[tuple[int, int], dict[str, object]]] = []
    for key, val in raw.items():
        m = _VMX_DISK_KEY_RE.match(key)
        if m and val.lower().endswith(".vmdk"):
            slot_key = (int(m.group(2)), int(m.group(3)))
            controller_type = m.group(1).lower()
            datastore, file_name = _split_vmx_disk_reference(val)
            
            # Extract device type (scsi-hardDisk, ide-hardDisk, etc.)
            device_type_key = f"{controller_type}{slot_key[0]}:{slot_key[1]}.deviceType"
            device_type = raw.get(device_type_key, f"{controller_type}-hardDisk")
            
            # Extract disk mode (persistent, independent-persistent, independent-nonpersistent)
            mode_key = f"{controller_type}{slot_key[0]}:{slot_key[1]}.mode"
            backing_mode = raw.get(mode_key, "persistent")
            
            # Detect RDM devices (Raw Device Mapping)
            is_rdm = backing_mode.startswith("independent") or "rdm" in device_type.lower()
            
            # Extract LUN ID for RDM devices (if available)
            lun_id = None
            if is_rdm:
                lun_key = f"{controller_type}{slot_key[0]}:{slot_key[1]}.lun"
                lun_val = raw.get(lun_key, "")
                if lun_val:
                    try:
                        lun_id = int(lun_val)
                    except ValueError:
                        pass
            
            disk_entries.append((
                slot_key,
                {
                    "label": f"Hard disk {slot_key[0] + 1}",
                    "file_name": file_name,
                    "path": val,
                    "datastore": datastore,
                    "controller_type": controller_type,
                    "controller": slot_key[0],
                    "unit_number": slot_key[1],
                    "backing_type": "rdm" if is_rdm else "file",
                    "device_type": device_type,
                    "backing_mode": backing_mode,
                    "lun_id": lun_id,
                    "is_rdm": is_rdm,
                    "thin_provisioned": True,
                },
            ))
    disk_entries.sort(key=lambda t: t[0])
    disk_files = [str(item[1]["file_name"]) for item in disk_entries]

    # SCSI controller type
    scsi_type = raw.get("scsi0.virtualdev", raw.get("scsi0.devicetype", "lsilogic"))

    # Network adapters
    networks: list[dict[str, str]] = []
    for key, val in raw.items():
        m = re.match(r'^ethernet(\d+)\.networkname$', key, re.IGNORECASE)
        if m:
            idx = m.group(1)
            virtual_dev = raw.get(f"ethernet{idx}.virtualdev", "vmxnet3")
            adapter = virtual_dev  # Use virtual_dev as the adapter type
            mac = raw.get(f"ethernet{idx}.address", raw.get(f"ethernet{idx}.generatedaddress", ""))
            networks.append({"index": idx, "network_name": val, "adapter": adapter, "mac": mac, "virtual_dev": virtual_dev})
    networks.sort(key=lambda n: int(n["index"]))

    # Hotplug settings
    cpu_hotplug_enabled = raw.get("vcpu.hotadd", "false").lower() == "true"
    memory_hotplug_enabled = raw.get("mem.hotadd", "false").lower() == "true"

    # Annotation
    annotation = raw.get("annotation", "")

    return {
        "name": name,
        "memory_mb": memory_mb,
        "cpu_count": cpu_count,
        "guest_os": guest_os,
        "guest_os_full_name": guest_os_full_name,
        "firmware": firmware,
        "memory_gb": round(memory_mb / 1024, 2) if memory_mb else 0,
        "disk_files": disk_files,
        "disks": [item[1] for item in disk_entries],
        "scsi_type": scsi_type,
        "networks": networks,
        "cpu_hotplug_enabled": cpu_hotplug_enabled,
        "memory_hotplug_enabled": memory_hotplug_enabled,
        "annotation": annotation,
        "raw": raw,
    }


# ---------------------------------------------------------------------------
# Archive detection
# ---------------------------------------------------------------------------

_ARCHIVE_SUFFIXES: dict[str, str] = {
    ".zip":  "zip",
    ".7z":   "7z",
    ".tar":  "tar",
    ".tgz":  "tar.gz",   # .tgz is always tar+gzip
    # .gz alone is NOT included — a bare .gz is not necessarily a tarball;
    # .tar.gz is already matched by the multi-suffix check in detect_archive_type.
    ".bz2":  "tar.bz2",
    ".xz":   "tar.xz",
    ".zst":  "tar.zst",
}


def detect_archive_type(filename: str) -> Optional[str]:
    """Return a canonical archive type string or None if not an archive.

    Possible return values: 'zip', '7z', 'tar', 'tar.gz', 'tar.bz2', 'tar.xz', 'tar.zst'
    """
    lower = filename.lower()
    # Multi-suffix check first (.tar.gz, .tar.bz2 …)
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
        if lower.endswith(suffix):
            return suffix.lstrip(".")
    p = Path(lower)
    return _ARCHIVE_SUFFIXES.get(p.suffix)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def ensure_qemu_img() -> None:
    if shutil.which("qemu-img") is None:
        raise DiskConversionError("qemu-img was not found on the Proxmox host")


def detect_disk_format(path: Path) -> str | None:
    ensure_qemu_img()
    proc = subprocess.run(["qemu-img", "info", "--output", "json", str(path)], text=True, capture_output=True)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout or "{}").get("format")
    except json.JSONDecodeError:
        return None


def convert_disk(source: Path, target: Path, target_format: DiskFormat, source_format: str = "vmdk") -> Path:
    ensure_qemu_img()
    target.parent.mkdir(parents=True, exist_ok=True)
    args = ["qemu-img", "convert", "-p", "-f", source_format, "-O", target_format.value, str(source), str(target)]
    proc = subprocess.run(args, text=True, capture_output=True)
    if proc.returncode != 0:
        raise DiskConversionError(
            f"qemu-img conversion failed:\nCMD: {' '.join(args)}\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
        )
    return target


def qemu_info(path: Path) -> dict[str, object]:
    ensure_qemu_img()
    proc = subprocess.run(["qemu-img", "info", "--output", "json", str(path)], text=True, capture_output=True)
    if proc.returncode != 0:
        raise DiskConversionError(proc.stderr or proc.stdout)
    import json

    return json.loads(proc.stdout or "{}")


def resize_disk(path: Path, size_gb: int, shrink_ok: bool = False) -> Path:
    """Resize a disk image to the specified size in GB.

    Args:
        path: Path to the disk image
        size_gb: Target size in gigabytes
        shrink_ok: Allow shrinking (default False for safety)

    Returns:
        The path to the resized disk

    Raises:
        DiskConversionError: If resize fails or would shrink without shrink_ok=True
    """
    ensure_qemu_img()

    # Get current size
    info = qemu_info(path)
    current_bytes = int(info.get("virtual-size", 0))
    current_gb = current_bytes / (1024 ** 3)

    target_bytes = size_gb * (1024 ** 3)

    # Safety check: prevent accidental shrinking unless explicitly allowed
    if target_bytes < current_bytes and not shrink_ok:
        raise DiskConversionError(
            f"Refusing to shrink disk from {current_gb:.2f} GB to {size_gb} GB. "
            f"Set shrink_ok=True to allow shrinking."
        )

    # Build resize command
    size_spec = f"{size_gb}G"
    args = ["qemu-img", "resize", str(path), size_spec]

    # Add shrink flag if needed (for certain formats that support it)
    if shrink_ok and target_bytes < current_bytes:
        args.insert(3, "--shrink-preallocated")

    proc = subprocess.run(args, text=True, capture_output=True)
    if proc.returncode != 0:
        raise DiskConversionError(
            f"qemu-img resize failed:\nCMD: {' '.join(args)}\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
        )

    return path


def get_disk_size_gb(path: Path) -> float:
    """Return the virtual disk size in GB."""
    try:
        info = qemu_info(path)
        bytes_size = int(info.get("virtual-size", 0))
        return bytes_size / (1024 ** 3)
    except Exception:
        return 0.0
