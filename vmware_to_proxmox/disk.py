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


def parse_vmx(content: str) -> dict[str, object]:
    """Parse a .vmx file (text content) and return a normalised hardware spec dict.

    Returns keys:
        name          (str)  — displayName
        memory_mb     (int)  — memsize in MB
        cpu_count     (int)  — numvcpus
        guest_os      (str)  — guestOS value
        firmware      (str)  — 'efi' or 'bios'
        disk_files    (list[str])  — all *.vmdk references in disk order
        scsi_type     (str)  — e.g. 'lsilogic', 'pvscsi'
        networks      (list[dict]) — [{adapter, network_name, mac}]
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

    firmware_raw = raw.get("firmware", "bios").lower()
    firmware = "efi" if firmware_raw in {"efi", "uefi"} else "bios"

    # Collect disk files in slot order (scsi0:0, scsi0:1, scsi1:0 …)
    disk_entries: list[tuple[str, str]] = []
    for key, val in raw.items():
        m = re.match(
            r'^(scsi|ide|sata|nvme)(\d+):(\d+)\.filename$', key, re.IGNORECASE
        )
        if m and val.lower().endswith(".vmdk"):
            slot_key = (int(m.group(2)), int(m.group(3)))
            disk_entries.append((slot_key, val))  # type: ignore[arg-type]
    disk_entries.sort(key=lambda t: t[0])
    disk_files = [Path(v).name for _, v in disk_entries]

    # SCSI controller type
    scsi_type = raw.get("scsi0.virtualdev", raw.get("scsi0.devicetype", "lsilogic"))

    # Network adapters
    networks: list[dict[str, str]] = []
    for key, val in raw.items():
        m = re.match(r'^ethernet(\d+)\.networkname$', key, re.IGNORECASE)
        if m:
            idx = m.group(1)
            adapter = raw.get(f"ethernet{idx}.virtualdev", "vmxnet3")
            mac = raw.get(f"ethernet{idx}.address", raw.get(f"ethernet{idx}.generatedaddress", ""))
            networks.append({"index": idx, "network_name": val, "adapter": adapter, "mac": mac})
    networks.sort(key=lambda n: int(n["index"]))

    return {
        "name": name,
        "memory_mb": memory_mb,
        "cpu_count": cpu_count,
        "guest_os": guest_os,
        "firmware": firmware,
        "disk_files": disk_files,
        "scsi_type": scsi_type,
        "networks": networks,
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
