from __future__ import annotations

import json
import os
import posixpath
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Optional

import paramiko
from pyVim.connect import Disconnect, SmartConnect, SmartConnectNoSSL
from pyVmomi import vim

from .models import VmwareDiskSpec, VmwareNicSpec, VmwareVmSpec


class VmwareClientError(RuntimeError):
    pass


class VmwareClient:
    def __init__(self, host: str, username: str, password: str, port: int = 443, ssh_port: int = 22, allow_insecure_ssl: bool = True) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.ssh_port = ssh_port
        self.allow_insecure_ssl = allow_insecure_ssl
        self._service_instance = None
        self._content = None

    @property
    def content(self):
        if self._content is None:
            raise VmwareClientError("VMware client is not connected")
        return self._content

    def connect(self) -> None:
        connector = SmartConnectNoSSL if self.allow_insecure_ssl else SmartConnect
        self._service_instance = connector(
            host=self.host,
            user=self.username,
            pwd=self.password,
            port=self.port,
        )
        self._content = self._service_instance.RetrieveContent()

    def close(self) -> None:
        if self._service_instance is not None:
            Disconnect(self._service_instance)
        self._service_instance = None
        self._content = None

    def __enter__(self) -> "VmwareClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _collect(self, view_type):
        container = self.content.viewManager.CreateContainerView(self.content.rootFolder, [view_type], True)
        try:
            return list(container.view)
        finally:
            container.Destroy()

    def list_vms(self) -> list[str]:
        return [vm.name for vm in self._collect(vim.VirtualMachine)]

    def get_vm_by_name(self, name: str) -> VmwareVmSpec:
        for vm in self._collect(vim.VirtualMachine):
            if vm.name == name:
                return self._vm_to_spec(vm)
        raise VmwareClientError(f"VM '{name}' was not found on {self.host}")

    def _vm_to_spec(self, vm: vim.VirtualMachine) -> VmwareVmSpec:
        config = vm.config
        hardware = config.hardware
        disks: list[VmwareDiskSpec] = []
        nics: list[VmwareNicSpec] = []
        has_vtpm = False
        has_pci_passthrough = False

        for device in hardware.device:
            if isinstance(device, vim.vm.device.VirtualDisk):
                backing = device.backing
                file_name = getattr(backing, "fileName", "") or ""
                label = getattr(device.deviceInfo, "label", f"disk{len(disks)}")
                disks.append(
                    VmwareDiskSpec(
                        label=label,
                        file_name=file_name,
                        capacity_bytes=int(getattr(device, "capacityInBytes", 0) or 0),
                        backing_type=backing.__class__.__name__,
                        controller_key=getattr(device, "controllerKey", None),
                        unit_number=getattr(device, "unitNumber", None),
                        thin_provisioned=bool(getattr(backing, "thinProvisioned", False)),
                    )
                )
            elif isinstance(device, vim.vm.device.VirtualEthernetCard):
                label = getattr(device.deviceInfo, "label", f"nic{len(nics)}")
                backing = getattr(device, "backing", None)
                nics.append(
                    VmwareNicSpec(
                        label=label,
                        network_name=str(getattr(backing, "deviceName", "") or getattr(getattr(device, "deviceInfo", None), "summary", "") or ""),
                        mac_address=getattr(device, "macAddress", ""),
                        adapter_type=device.__class__.__name__,
                        vlan_id=None,
                    )
                )
            elif isinstance(device, vim.vm.device.VirtualTPM):
                has_vtpm = True
            elif isinstance(device, vim.vm.device.VirtualPCIPassthrough):
                has_pci_passthrough = True

        firmware = getattr(config, "firmware", "bios") or "bios"
        guest_id = getattr(config, "guestId", "") or ""
        power_state = str(getattr(vm.runtime, "powerState", "poweredOff")).split(".")[-1]
        snapshot_root = getattr(vm, "snapshot", None)

        return VmwareVmSpec(
            name=vm.name,
            moid=getattr(vm, "_moId", vm._GetMoId() if hasattr(vm, "_GetMoId") else vm.name),
            guest_id=guest_id,
            power_state=power_state,
            firmware=firmware,
            memory_mb=int(getattr(config.hardware, "memoryMB", 0) or 0),
            cpu_count=int(getattr(config.hardware, "numCPU", 1) or 1),
            annotation=str(getattr(config, "annotation", "") or ""),
            datastore=str(getattr(config.files, "vmPathName", "") or ""),
            disks=disks,
            nics=nics,
            has_snapshots=bool(snapshot_root),
            has_vtpm=has_vtpm,
            has_pci_passthrough=has_pci_passthrough,
        )

    def validate_supported(self, vm: VmwareVmSpec) -> list[str]:
        warnings: list[str] = []
        if not vm.is_linux:
            raise VmwareClientError(f"VM '{vm.name}' is not identified as Linux (guestId={vm.guest_id!r})")
        if vm.power_state.lower() not in {"poweredoff", "powered_off", "off"}:
            raise VmwareClientError(f"VM '{vm.name}' must be powered off before migration")
        if vm.has_snapshots:
            raise VmwareClientError(f"VM '{vm.name}' still has snapshots; consolidate or remove them first")
        if vm.has_vtpm:
            warnings.append("vTPM detected; Proxmox cannot migrate vTPM state from VMware")
        if vm.has_pci_passthrough:
            warnings.append("PCI passthrough detected; manual reconfiguration may be required")
        if not vm.disks:
            raise VmwareClientError(f"VM '{vm.name}' has no virtual disks")
        return warnings

    def datastore_path(self, vm: VmwareVmSpec) -> str:
        if not vm.disks:
            return ""
        first_disk = vm.disks[0].file_name
        return os.path.dirname(first_disk)

    def remote_esxi_path(self, vmware_disk_path: str) -> str:
        if vmware_disk_path.startswith("[") and "]" in vmware_disk_path:
            datastore, rel_path = vmware_disk_path[1:].split("]", 1)
            datastore = datastore.strip()
            rel_path = rel_path.strip().lstrip("/")
            return posixpath.join("/vmfs/volumes", datastore, rel_path)
        return vmware_disk_path

    def disk_source_paths(self, vm: VmwareVmSpec) -> list[str]:
        return [disk.file_name for disk in vm.disks]

    def download_file(self, remote_path: str, local_path: Path) -> Path:
        remote_path = self.remote_esxi_path(remote_path)
        if not remote_path.startswith("/vmfs/volumes/"):
            raise VmwareClientError(f"Remote disk path looks invalid: {remote_path}")
        transport = paramiko.Transport((self.host, self.ssh_port if self.ssh_port else 22))
        try:
            transport.connect(username=self.username, password=self.password)
            sftp = paramiko.SFTPClient.from_transport(transport)
            try:
                local_path.parent.mkdir(parents=True, exist_ok=True)
                sftp.get(remote_path, str(local_path))
            finally:
                sftp.close()
        finally:
            transport.close()
        return local_path

    def download_vm_disks(self, vm: VmwareVmSpec, target_dir: Path) -> list[Path]:
        target_dir.mkdir(parents=True, exist_ok=True)
        downloaded: list[Path] = []
        for disk in vm.disks:
            local_path = target_dir / Path(disk.file_name).name
            downloaded.append(self.download_file(disk.file_name, local_path))
        return downloaded

    def export_manifest(self, vm: VmwareVmSpec, target_path: Path) -> Path:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(json.dumps(asdict(vm), indent=2, sort_keys=True), encoding="utf-8")
        return target_path
