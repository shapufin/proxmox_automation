from __future__ import annotations

import json
import logging
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .config import AppConfig
from .disk import DiskConversionError, convert_disk, detect_archive_type, detect_disk_format, qemu_info, sha256_file
from .guest import GuestRemediator
from .models import DiskFormat, FirmwareMode, MigrationResult, MigrationTarget, VmwareDiskSpec, VmwareNicSpec, VmwareVmSpec
from .proxmox import ProxmoxClient, ProxmoxClientError
from .vmware import VmwareClient, VmwareClientError


@dataclass(slots=True)
class DiskImportRecord:
    source: str
    local_path: str
    converted_path: str
    volume_id: str
    slot: str
    target_storage: str = ""
    source_datastore: str = ""


@dataclass(slots=True)
class MigrationPlan:
    vm_name: str
    vmid: int
    storage: str
    bridge: str
    disk_format: DiskFormat
    firmware: FirmwareMode
    warnings: list[str] = field(default_factory=list)
    nics: list[dict[str, str]] = field(default_factory=list)
    disks: list[dict[str, str]] = field(default_factory=list)


class MigrationEngine:
    def __init__(self, config: AppConfig, logger: Optional[logging.Logger] = None) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.vmware = VmwareClient(
            host=config.vmware.host,
            username=config.vmware.username,
            password=config.vmware.password,
            port=config.vmware.port,
            ssh_port=config.vmware.ssh_port,
            allow_insecure_ssl=config.vmware.allow_insecure_ssl,
        )
        self.proxmox = ProxmoxClient(
            config.proxmox.node,
            ssh_enabled=config.proxmox.ssh_enabled,
            ssh_host=config.proxmox.ssh_host,
            ssh_port=config.proxmox.ssh_port,
            ssh_username=config.proxmox.ssh_username,
            ssh_private_key=config.proxmox.ssh_private_key,
            ssh_password=config.proxmox.ssh_password,
            api_host=config.proxmox.api_host,
            api_user=config.proxmox.api_user,
            api_token_name=config.proxmox.api_token_name,
            api_token_value=config.proxmox.api_token_value,
            api_verify_ssl=config.proxmox.api_verify_ssl,
        )
        self.remediator = GuestRemediator()

    def _resolve_firmware(self, vm: VmwareVmSpec) -> FirmwareMode:
        if self.config.proxmox.boot_firmware != FirmwareMode.AUTO:
            return self.config.proxmox.boot_firmware
        source = (vm.firmware or "").lower()
        if source in {"efi", "uefi"}:
            return FirmwareMode.UEFI
        return FirmwareMode.BIOS

    def _resolve_bridge(self, vm_network_name: str) -> str:
        bridge = self.config.bridge_for_network(vm_network_name)
        if not self.proxmox.bridge_exists(bridge):
            known = ", ".join(sorted(item.name for item in self.proxmox.list_bridges()))
            raise ProxmoxClientError(f"Bridge '{bridge}' is not present on Proxmox. Known bridges: {known}")
        return bridge

    def _resolve_storage(self, preferred: Optional[str] = None) -> str:
        storage = self.proxmox.choose_storage(preferred or self.config.proxmox.default_storage)
        return storage.storage

    @staticmethod
    def _map_lookup(mapping: Optional[dict[str, str]], *keys: object) -> Optional[str]:
        if not mapping:
            return None
        for key in keys:
            if key is None:
                continue
            text = str(key).strip()
            if not text:
                continue
            if text in mapping and mapping[text]:
                return str(mapping[text]).strip() or None
        return None

    def _resolve_disk_storage(
        self,
        disk: Optional[VmwareDiskSpec],
        index: int,
        storage_override: Optional[str] = None,
        disk_storage_map: Optional[dict[str, str]] = None,
    ) -> str:
        datastore = getattr(disk, "datastore", "") if disk is not None else ""
        mapped = self._map_lookup(
            disk_storage_map,
            getattr(disk, "path", "") if disk is not None else "",
            getattr(disk, "file_name", "") if disk is not None else "",
            getattr(disk, "label", "") if disk is not None else "",
            f"disk-{index}",
            f"scsi{index}",
            datastore,
        )
        preferred = mapped or storage_override or (self.config.storage_for_datastore(datastore) if datastore else None)
        return self._resolve_storage(preferred)

    def _resolve_network_bridge(self, network_name: str, bridge_override: Optional[str] = None) -> str:
        if bridge_override:
            return self._resolve_bridge(bridge_override)
        return self._resolve_bridge(self.config.bridge_for_network(network_name))

    def _resolve_nic_bridge(
        self,
        nic: Optional[VmwareNicSpec],
        index: int,
        bridge_override: Optional[str] = None,
        nic_bridge_map: Optional[dict[str, str]] = None,
    ) -> str:
        network_name = getattr(nic, "network_name", "") if nic is not None else ""
        mapped = self._map_lookup(
            nic_bridge_map,
            getattr(nic, "label", "") if nic is not None else "",
            network_name,
            f"nic-{index}",
            str(index),
        )
        return self._resolve_bridge(mapped or bridge_override or self.config.bridge_for_network(network_name))

    def inventory(self) -> dict[str, object]:
        self.proxmox.ensure_prerequisites()
        with self.vmware:
            vms = self.vmware.list_vms()
            proxmox_storages = [asdict(item) for item in self.proxmox.list_storages()]
            proxmox_bridges = [asdict(item) for item in self.proxmox.list_bridges()]
        return {
            "vmware_vms": vms,
            "proxmox_storages": proxmox_storages,
            "proxmox_bridges": proxmox_bridges,
        }

    def build_plan(self, vm_name: str, storage: Optional[str] = None, bridge: Optional[str] = None, disk_format: Optional[DiskFormat] = None) -> MigrationPlan:
        self.proxmox.ensure_prerequisites()
        with self.vmware:
            vm = self.vmware.get_vm_by_name(vm_name)
            warnings = self.vmware.validate_supported(vm)
        return self._plan_from_vm(vm, warnings, storage, bridge, disk_format)

    @staticmethod
    def _minimal_vm_spec(name: str, vmx_specs: Optional[dict] = None) -> VmwareVmSpec:
        """Build a minimal VmwareVmSpec when no manifest.json is available.
        Optionally enriched from parsed .vmx data."""
        specs = vmx_specs or {}
        nics: list[VmwareNicSpec] = []
        for net in specs.get("networks", []):
            nics.append(VmwareNicSpec(
                label=f"Network adapter {net.get('index', 0)}",
                mac_address=str(net.get("mac", "")),
                network_name=str(net.get("network_name", "VM Network")),
                adapter_type=str(net.get("adapter", "vmxnet3")),
            ))
        # Build disk specs from vmx disk metadata when available.
        disks: list[VmwareDiskSpec] = []
        for idx, disk in enumerate(specs.get("disks", [])):
            if isinstance(disk, dict):
                disks.append(VmwareDiskSpec(
                    label=str(disk.get("label", f"Hard disk {idx + 1}")),
                    file_name=str(disk.get("file_name", disk.get("path", f"disk-{idx + 1}.vmdk"))),
                    capacity_bytes=int(disk.get("capacity_bytes", 0) or 0),
                    backing_type=str(disk.get("backing_type", "file")),
                    controller_key=int(disk.get("controller", 0) or 0),
                    unit_number=int(disk.get("unit_number", 0) or 0),
                    thin_provisioned=bool(disk.get("thin_provisioned", False)),
                ))
        if not disks:
            for idx, fname in enumerate(specs.get("disk_files", [])):
                disks.append(VmwareDiskSpec(
                    label=f"Hard disk {idx + 1}",
                    file_name=fname,
                    capacity_bytes=0,  # Unknown from VMX alone; will infer later
                ))
        return VmwareVmSpec(
            name=name or specs.get("name", "vm"),
            moid=name or specs.get("name", "vm"),
            guest_id=str(specs.get("guest_os", "")),
            power_state="poweredOff",
            firmware=str(specs.get("firmware", "bios")),
            memory_mb=int(specs.get("memory_mb", 0) or 0),
            cpu_count=int(specs.get("cpu_count", 1) or 1),
            annotation="",
            datastore="",
            disks=disks,
            nics=nics,
            has_snapshots=False,
            has_vtpm=False,
            has_pci_passthrough=False,
        )

    def _vm_from_manifest(self, manifest_path: Optional[Path], vmx_specs: Optional[dict] = None, fallback_name: str = "") -> VmwareVmSpec:
        """Load VM spec from manifest_path. Falls back to a minimal spec if file is absent."""
        if manifest_path is None or not str(manifest_path) or not manifest_path.exists() or manifest_path.is_dir():
            self.logger.warning(
                "manifest.json not found at %s — building minimal spec%s",
                manifest_path,
                " from .vmx" if vmx_specs else " (no vmx data)",
            )
            return self._minimal_vm_spec(fallback_name, vmx_specs)
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if "vmware" in payload:
            payload = payload["vmware"]
        disks = [
            VmwareDiskSpec(**disk) for disk in payload.get("disks", [])
        ]
        nics = [
            VmwareNicSpec(**nic) for nic in payload.get("nics", [])
        ]
        vm = VmwareVmSpec(
            name=payload["name"],
            moid=str(payload.get("moid", payload["name"])),
            guest_id=str(payload.get("guest_id", "")),
            power_state=str(payload.get("power_state", "poweredOff")),
            firmware=str(payload.get("firmware", "bios")),
            memory_mb=int(payload.get("memory_mb", 0)),
            cpu_count=int(payload.get("cpu_count", 1)),
            annotation=str(payload.get("annotation", "")),
            datastore=str(payload.get("datastore", "")),
            disks=disks,
            nics=nics,
            has_snapshots=bool(payload.get("has_snapshots", False)),
            has_vtpm=bool(payload.get("has_vtpm", False)),
            has_pci_passthrough=bool(payload.get("has_pci_passthrough", False)),
        )
        return self._apply_vmx_specs(vm, vmx_specs)

    @staticmethod
    def _apply_vmx_specs(vm: VmwareVmSpec, vmx_specs: Optional[dict] = None) -> VmwareVmSpec:
        specs = vmx_specs or {}
        if not specs:
            return vm
        if specs.get("name") and not vm.name:
            vm.name = str(specs.get("name", vm.name))
        if specs.get("memory_mb"):
            vm.memory_mb = int(specs.get("memory_mb", vm.memory_mb) or vm.memory_mb)
        if specs.get("cpu_count"):
            vm.cpu_count = int(specs.get("cpu_count", vm.cpu_count) or vm.cpu_count)
        if specs.get("guest_os"):
            vm.guest_id = str(specs.get("guest_os", vm.guest_id) or vm.guest_id)
        if specs.get("firmware"):
            vm.firmware = str(specs.get("firmware", vm.firmware) or vm.firmware)
        if specs.get("networks") and not vm.nics:
            vm.nics = [
                VmwareNicSpec(
                    label=f"Network adapter {net.get('index', 0)}",
                    mac_address=str(net.get("mac", "")),
                    network_name=str(net.get("network_name", "VM Network")),
                    adapter_type=str(net.get("adapter", "vmxnet3")),
                )
                for net in specs.get("networks", [])
                if isinstance(net, dict)
            ]
        if specs.get("disks") and not vm.disks:
            vm.disks = [
                VmwareDiskSpec(
                    label=str(disk.get("label", f"Hard disk {idx + 1}")),
                    file_name=str(disk.get("file_name", disk.get("path", ""))),
                    capacity_bytes=int(disk.get("capacity_bytes", 0) or 0),
                    backing_type=str(disk.get("backing_type", "file")),
                    controller_key=int(disk.get("controller", 0) or 0),
                    unit_number=int(disk.get("unit_number", 0) or 0),
                    thin_provisioned=bool(disk.get("thin_provisioned", False)),
                )
                for idx, disk in enumerate(specs.get("disks", []))
                if isinstance(disk, dict)
            ]
        return vm

    def load_local_manifest(self, manifest_path: Optional[Path]) -> VmwareVmSpec:
        return self._vm_from_manifest(manifest_path)

    def _plan_from_vm(self, vm: VmwareVmSpec, warnings: list[str], storage: Optional[str], bridge: Optional[str], disk_format: Optional[DiskFormat]) -> MigrationPlan:
        target_storage = self._resolve_storage(storage)
        target_bridge = bridge or (self.config.bridge_for_network(vm.nics[0].network_name) if vm.nics else self.config.proxmox.default_bridge)
        target_bridge = self._resolve_bridge(target_bridge)
        target_format = disk_format or self.config.target_format()
        firmware = self._resolve_firmware(vm)
        vmid = self.proxmox.next_vmid()

        nics = []
        for nic in vm.nics:
            nics.append(
                {
                    "name": nic.label,
                    "source_network": nic.network_name,
                    "target_bridge": self._resolve_network_bridge(nic.network_name, bridge),
                    "mac": nic.mac_address if self.config.proxmox.preserve_mac else "generated",
                }
            )

        disks = []
        for disk in vm.disks:
            datastore = getattr(disk, "datastore", "") or ""
            disk_storage = self._resolve_storage(
                storage or (self.config.storage_for_datastore(datastore) if datastore else None)
            )
            disks.append(
                {
                    "label": disk.label,
                    "source_path": disk.file_name,
                    "target_format": target_format.value,
                    "capacity_bytes": str(disk.capacity_bytes),
                    "target_storage": disk_storage,
                    "source_datastore": datastore,
                }
            )

        return MigrationPlan(
            vm_name=vm.name,
            vmid=vmid,
            storage=target_storage,
            bridge=target_bridge,
            disk_format=target_format,
            firmware=firmware,
            warnings=warnings,
            nics=nics,
            disks=disks,
        )

    def _collect_remote_local_disk_paths(
        self,
        remote_path: str,
        diagnostics: list[str],
        visited: Optional[set[str]] = None,
    ) -> list[Path]:
        visited = visited or set()
        normalised = remote_path.rstrip("/")
        if normalised in visited:
            return []
        visited.add(normalised)

        try:
            listing = self.proxmox.list_remote_dir(remote_path)
        except Exception as exc:  # noqa: BLE001
            diagnostics.append(f"remote_list_failed path={remote_path!r} error={exc}")
            return []

        files = listing.get("files", []) if isinstance(listing, dict) else []
        folders = listing.get("folders", []) if isinstance(listing, dict) else []
        diagnostics.append(
            f"remote_list_ok path={remote_path!r} files={len(files)} folders={len(folders)}"
        )

        collected: list[Path] = []
        for f in files:
            if not isinstance(f, dict):
                continue
            name = str(f.get("name", ""))
            remote_child = str(f.get("path") or "").strip()
            if name.lower().endswith(".vmdk") and remote_child:
                collected.append(Path(remote_child))

        for folder in folders:
            folder_path = ""
            if isinstance(folder, dict):
                folder_path = str(folder.get("path") or "").strip()
            elif folder:
                folder_path = str(folder).strip()
            if folder_path:
                collected.extend(self._collect_remote_local_disk_paths(folder_path, diagnostics, visited))

        return collected

    def _resolve_local_disk_paths(
        self,
        disk_paths: Iterable[Path],
        manifest_vm: Optional[VmwareVmSpec] = None,
        diagnostics: Optional[list[str]] = None,
    ) -> list[Path]:
        diagnostics = diagnostics if diagnostics is not None else []
        collected: dict[str, Path] = {}
        extras: list[Path] = []
        unresolved: list[str] = []

        for raw_path in disk_paths:
            path = Path(raw_path)
            diagnostics.append(
                f"input_path path={str(path)!r} exists={path.exists()} is_dir={path.is_dir()} is_file={path.is_file()}"
            )

            if not path.exists() and not self.proxmox.ssh_enabled:
                diagnostics.append(
                    f"path_not_visible_locally_and_ssh_disabled path={str(path)!r}"
                )

            if path.is_dir():
                diagnostics.append(f"local_directory_detected path={str(path)!r}")
                for child in sorted(path.rglob("*")):
                    if child.is_file():
                        collected.setdefault(child.name, child)
                continue

            if path.is_file():
                diagnostics.append(f"local_file_detected path={str(path)!r}")
                collected.setdefault(path.name, path)
                continue

            remote_candidates = self._collect_remote_local_disk_paths(str(path), diagnostics)
            if remote_candidates:
                diagnostics.append(
                    f"remote_directory_resolved path={str(path)!r} candidate_count={len(remote_candidates)}"
                )
                for candidate in remote_candidates:
                    collected.setdefault(candidate.name, candidate)
                continue

            if path.suffix.lower() == ".vmdk":
                diagnostics.append(f"treating_unresolved_file_candidate path={str(path)!r}")
                collected.setdefault(path.name, path)
            else:
                unresolved.append(str(path))
                diagnostics.append(f"unresolved_path_skipped path={str(path)!r}")

        if manifest_vm and manifest_vm.disks:
            ordered: list[Path] = []
            used: set[str] = set()
            for disk in manifest_vm.disks:
                disk_name = Path(disk.file_name).name
                match = collected.get(disk_name)
                if match is not None:
                    ordered.append(match)
                    used.add(disk_name)
            for name, candidate in sorted(collected.items()):
                if name not in used:
                    extras.append(candidate)
            if diagnostics:
                diagnostics.append(
                    f"manifest_disk_order_applied matched={len(ordered)} extras={len(extras)} unresolved={len(unresolved)}"
                )
            return ordered + extras

        if diagnostics:
            diagnostics.append(
                f"path_resolution_complete collected={len(collected)} unresolved={len(unresolved)}"
            )
        return [candidate for _, candidate in sorted(collected.items())]

    def resolve_local_disk_paths(self, disk_paths: Iterable[Path], manifest_vm: Optional[VmwareVmSpec] = None) -> list[Path]:
        return self._resolve_local_disk_paths(disk_paths, manifest_vm)

    def migrate_local_disks(
        self,
        vm_name: str,
        manifest_path: Optional[Path],
        disk_paths: Iterable[Path],
        storage: Optional[str] = None,
        bridge: Optional[str] = None,
        disk_format: Optional[DiskFormat] = None,
        dry_run: Optional[bool] = None,
        start_after_import: bool = True,
        write_manifest: bool = True,
        vmx_specs: Optional[dict] = None,
        vmid: Optional[int] = None,
        disk_storage_map: Optional[dict[str, str]] = None,
        nic_bridge_map: Optional[dict[str, str]] = None,
    ) -> MigrationResult:
        self.proxmox.ensure_prerequisites()
        dry_run = self.config.migration.dry_run if dry_run is None else dry_run
        disk_paths = list(disk_paths)
        vm = self._vm_from_manifest(manifest_path, vmx_specs=vmx_specs, fallback_name=vm_name)
        if vm_name and vm.name != vm_name:
            vm.name = vm_name
        warnings = self.vmware.validate_supported(vm)
        target_storage = self._resolve_storage(storage)
        target_format = disk_format or self.config.target_format()
        firmware = self._resolve_firmware(vm)
        vmid = vmid or self.proxmox.next_vmid()
        path_diagnostics: list[str] = []
        source_paths = self._resolve_local_disk_paths(disk_paths, vm, path_diagnostics)
        self.logger.info(
            "Local migration path resolution for %s: %s candidate(s) -> %s resolved disk(s)",
            vm.name,
            len(disk_paths),
            len(source_paths),
        )
        for line in path_diagnostics:
            self.logger.info("Local migration path diagnostic for %s: %s", vm.name, line)
        if source_paths:
            self.logger.info("Resolved local source disks for %s: %s", vm.name, ", ".join(str(path) for path in source_paths))
        else:
            self.logger.warning(
                "No local disk paths were supplied or discovered for %s; diagnostics=%s",
                vm.name,
                path_diagnostics,
            )

        if dry_run:
            self.logger.info(
                "Dry-run local migration for %s: disk_count=%s, manifest=%s, storage=%s, format=%s",
                vm.name,
                len(source_paths),
                manifest_path,
                target_storage,
                target_format.value,
            )
            return MigrationResult(
                name=vm.name,
                vmid=vmid,
                target_storage=target_storage,
                disk_format=target_format,
                firmware=firmware,
                warnings=warnings,
                details={
                    "dry_run": True,
                    "source_mode": "local",
                    "disk_count": len(source_paths),
                    "path_resolution": path_diagnostics,
                },
            )

        if not source_paths:
            raise ValueError(
                "No local disk paths were supplied or discovered; "
                f"diagnostics={path_diagnostics}"
            )

        self.logger.info(
            "Live local migration for %s will import %s disk(s) into storage %s",
            vm.name,
            len(source_paths),
            target_storage,
        )
        target_dir = Path(tempfile.mkdtemp(prefix=f"pve-local-{vm.name}-"))
        import_records: list[DiskImportRecord] = []
        migration_commands: list[str] = []
        remediation_path = target_dir / f"{vm.name}.remediation.sh"

        try:
            self.logger.info("Importing local disks for %s from %s", vm.name, ", ".join(str(p) for p in source_paths))
            if write_manifest and manifest_path is not None and manifest_path.exists() and manifest_path.is_file():
                (target_dir / f"{vm.name}.manifest.json").write_text(manifest_path.read_text(encoding="utf-8"), encoding="utf-8")
            elif write_manifest:
                generated_manifest = {
                    "vmware": asdict(vm),
                    "target": {
                        "storage": target_storage,
                        "format": target_format.value,
                        "firmware": firmware.value,
                        "vmid": vmid,
                    },
                }
                (target_dir / f"{vm.name}.manifest.json").write_text(
                    json.dumps(generated_manifest, indent=2, sort_keys=True),
                    encoding="utf-8",
                )

            proxmox_name = self.proxmox.create_vm(
                vmid=vmid,
                name=vm.name,
                memory_mb=vm.memory_mb,
                cores=vm.cpu_count,
                sockets=1,
                bios="ovmf" if firmware == FirmwareMode.UEFI else "seabios",
                scsihw=self.config.proxmox.scsi_controller,
                agent=True,
            )
            if proxmox_name != vm.name:
                self.logger.warning(
                    "Proxmox adjusted VM name for %s to %s to satisfy naming rules",
                    vm.name,
                    proxmox_name,
                )

            if firmware == FirmwareMode.UEFI and self.config.proxmox.create_efi_disk:
                self.proxmox.add_efi_disk(vmid, target_storage, target_format)

            for index, nic in enumerate(vm.nics):
                bridge_name = self._resolve_nic_bridge(nic, index, bridge, nic_bridge_map)
                mac = nic.mac_address if self.config.proxmox.preserve_mac else ""
                self.proxmox.add_network(vmid, index, bridge_name, macaddr=mac, model="virtio")

            vmx_disk_specs = list(vm.disks)
            for index, source_path in enumerate(source_paths):
                disk_spec = vmx_disk_specs[index] if index < len(vmx_disk_specs) else None
                source_format = detect_disk_format(source_path) or source_path.suffix.lower().lstrip(".") or "vmdk"
                converted_path = target_dir / f"{source_path.stem}.{target_format.value}"
                if source_format == target_format.value:
                    converted_path = source_path
                else:
                    try:
                        convert_disk(source_path, converted_path, target_format, source_format=source_format)
                        qemu_info(converted_path)
                    except DiskConversionError:
                        converted_path = source_path

                disk_target_storage = self._resolve_disk_storage(disk_spec, index, target_storage, disk_storage_map)
                volume_id = self.proxmox.import_disk(vmid, converted_path, disk_target_storage, target_format)
                slot = f"scsi{index}"
                attach_command = self.proxmox.attach_disk(vmid, volume_id, slot=slot)
                migration_commands.append(
                    f"qm importdisk {vmid} {converted_path} {disk_target_storage} --format {target_format.value}"
                )
                migration_commands.append(attach_command)
                import_records.append(
                    DiskImportRecord(
                        source=str(source_path),
                        local_path=str(source_path),
                        converted_path=str(converted_path),
                        volume_id=volume_id,
                        slot=slot,
                        target_storage=disk_target_storage,
                        source_datastore=getattr(disk_spec, "datastore", "") if disk_spec is not None else "",
                    )
                )

            self.proxmox.set_boot_order(vmid, "scsi0")

            if self.config.migration.guest_remediation:
                self.remediator.write_script(
                    remediation_path,
                    vm,
                    rewrite_fstab=self.config.migration.guest_rewrite_fstab,
                    install_qemu_agent=self.config.migration.guest_install_qemu_agent,
                )

            if start_after_import:
                self.proxmox.start_vm(vmid)

            return MigrationResult(
                name=vm.name,
                vmid=vmid,
                target_storage=target_storage,
                disk_format=target_format,
                firmware=firmware,
                warnings=warnings,
                details={
                    "source_mode": "local",
                    "manifest": str(manifest_path),
                    "staging_dir": str(target_dir),
                    "remediation_script": str(remediation_path),
                    "path_resolution": path_diagnostics,
                    "migration_commands": migration_commands,
                    "disks": [asdict(item) for item in import_records],
                },
            )
        except Exception:
            if self.config.migration.rollback_on_failure:
                try:
                    self.proxmox.destroy_vm(vmid)
                except Exception as rollback_error:
                    self.logger.error("Rollback failed for VMID %s: %s", vmid, rollback_error)
            raise

    def migrate_vm(
        self,
        vm_name: str,
        storage: Optional[str] = None,
        bridge: Optional[str] = None,
        disk_format: Optional[DiskFormat] = None,
        dry_run: Optional[bool] = None,
        start_after_import: bool = True,
        write_manifest: bool = True,
        vmid: Optional[int] = None,
        disk_storage_map: Optional[dict[str, str]] = None,
        nic_bridge_map: Optional[dict[str, str]] = None,
    ) -> MigrationResult:
        self.proxmox.ensure_prerequisites()
        dry_run = self.config.migration.dry_run if dry_run is None else dry_run
        with self.vmware:
            vm = self.vmware.get_vm_by_name(vm_name)
            warnings = self.vmware.validate_supported(vm)
        return self._migrate_vmware(
            vm,
            warnings,
            storage,
            bridge,
            disk_format,
            dry_run,
            start_after_import,
            write_manifest,
            vmid=vmid,
            disk_storage_map=disk_storage_map,
            nic_bridge_map=nic_bridge_map,
        )

    def _migrate_vmware(
        self,
        vm: VmwareVmSpec,
        warnings: list[str],
        storage: Optional[str],
        bridge: Optional[str],
        disk_format: Optional[DiskFormat],
        dry_run: bool,
        start_after_import: bool,
        write_manifest: bool,
        vmid: Optional[int] = None,
        disk_storage_map: Optional[dict[str, str]] = None,
        nic_bridge_map: Optional[dict[str, str]] = None,
    ) -> MigrationResult:
        target_storage = self._resolve_storage(storage)
        target_format = disk_format or self.config.target_format()
        firmware = self._resolve_firmware(vm)
        vmid = vmid or self.proxmox.next_vmid()
        network_bridge_override = bridge

        if dry_run:
            return MigrationResult(
                name=vm.name,
                vmid=vmid,
                target_storage=target_storage,
                disk_format=target_format,
                firmware=firmware,
                warnings=warnings,
                details={"dry_run": True, "source_mode": "vmware"},
            )

        target_dir = Path(tempfile.mkdtemp(prefix=f"pve-migrate-{vm.name}-"))
        manifest_path = target_dir / f"{vm.name}.manifest.json"
        import_records: list[DiskImportRecord] = []
        migration_commands: list[str] = []
        remediation_path = target_dir / f"{vm.name}.remediation.sh"

        try:
            self.logger.info("Downloading disks for %s into %s", vm.name, target_dir)
            with self.vmware:
                self.vmware.export_manifest(vm, manifest_path)
                downloaded = self.vmware.download_vm_disks(vm, target_dir)

            if write_manifest:
                manifest_path.write_text(json.dumps({"vmware": asdict(vm), "target": {"storage": target_storage, "format": target_format.value, "firmware": firmware.value, "vmid": vmid}}, indent=2, sort_keys=True), encoding="utf-8")

            self.proxmox.create_vm(
                vmid=vmid,
                name=vm.name,
                memory_mb=vm.memory_mb,
                cores=vm.cpu_count,
                sockets=1,
                bios="ovmf" if firmware == FirmwareMode.UEFI else "seabios",
                scsihw=self.config.proxmox.scsi_controller,
                agent=True,
            )

            if firmware == FirmwareMode.UEFI and self.config.proxmox.create_efi_disk:
                self.proxmox.add_efi_disk(vmid, target_storage, target_format)

            for index, nic in enumerate(vm.nics):
                bridge_name = self._resolve_nic_bridge(nic, index, network_bridge_override, nic_bridge_map)
                mac = nic.mac_address if self.config.proxmox.preserve_mac else ""
                self.proxmox.add_network(vmid, index, bridge_name, macaddr=mac, model="virtio")

            for index, disk_path in enumerate(downloaded):
                disk_spec = vm.disks[index] if index < len(vm.disks) else None
                source_format = detect_disk_format(disk_path) or Path(disk_path).suffix.lower().lstrip(".") or "vmdk"
                converted_path = target_dir / f"{disk_path.stem}.{target_format.value}"
                try:
                    convert_disk(disk_path, converted_path, target_format, source_format=source_format)
                    qemu_info(converted_path)
                    source_for_import = converted_path
                except DiskConversionError:
                    source_for_import = disk_path
                    converted_path = disk_path

                disk_target_storage = self._resolve_disk_storage(disk_spec, index, target_storage, disk_storage_map)
                volume_id = self.proxmox.import_disk(vmid, source_for_import, disk_target_storage, target_format)
                slot = f"scsi{index}"
                attach_command = self.proxmox.attach_disk(vmid, volume_id, slot=slot)
                migration_commands.append(
                    f"qm importdisk {vmid} {source_for_import} {disk_target_storage} --format {target_format.value}"
                )
                migration_commands.append(attach_command)
                import_records.append(
                    DiskImportRecord(
                        source=str(disk_path),
                        local_path=str(disk_path),
                        converted_path=str(converted_path),
                        volume_id=volume_id,
                        slot=slot,
                        target_storage=disk_target_storage,
                        source_datastore=getattr(disk_spec, "datastore", "") if disk_spec is not None else "",
                    )
                )

            self.proxmox.set_boot_order(vmid, "scsi0")

            if self.config.migration.guest_remediation:
                self.remediator.write_script(
                    remediation_path,
                    vm,
                    rewrite_fstab=self.config.migration.guest_rewrite_fstab,
                    install_qemu_agent=self.config.migration.guest_install_qemu_agent,
                )

            if start_after_import:
                self.proxmox.start_vm(vmid)

            if self.config.migration.guest_remediation and self.config.ssh.enabled and self.config.ssh.host:
                self.remediator.run_over_ssh(
                    host=self.config.ssh.host,
                    username=self.config.ssh.username,
                    script_path=remediation_path,
                    port=self.config.ssh.port,
                    password=self.config.ssh.password,
                    private_key=self.config.ssh.private_key,
                )

            return MigrationResult(
                name=vm.name,
                vmid=vmid,
                target_storage=target_storage,
                disk_format=target_format,
                firmware=firmware,
                warnings=warnings,
                details={
                    "source_mode": "vmware",
                    "source_vm_name": vm.name,
                    "proxmox_vm_name": proxmox_name,
                    "manifest": str(manifest_path),
                    "staging_dir": str(target_dir),
                    "remediation_script": str(remediation_path),
                    "migration_commands": migration_commands,
                    "disks": [asdict(item) for item in import_records],
                },
            )
        except Exception:
            if self.config.migration.rollback_on_failure:
                try:
                    self.proxmox.destroy_vm(vmid)
                except Exception as rollback_error:
                    self.logger.error("Rollback failed for VMID %s: %s", vmid, rollback_error)
            raise

    # ------------------------------------------------------------------
    # Archive-aware entry point for the local-disk migration path
    # ------------------------------------------------------------------

    def migrate_local_disks_or_archive(
        self,
        vm_name: str,
        manifest_path: Optional[Path],
        disk_paths: Iterable[str | Path],
        storage: Optional[str] = None,
        bridge: Optional[str] = None,
        disk_format: Optional[DiskFormat] = None,
        dry_run: Optional[bool] = None,
        start_after_import: bool = True,
        vmx_specs: Optional[dict] = None,
        vmid: Optional[int] = None,
        disk_storage_map: Optional[dict[str, str]] = None,
        nic_bridge_map: Optional[dict[str, str]] = None,
    ) -> MigrationResult:
        """High-level entry point for local-disk migration with archive support.

        For each path in *disk_paths*:
        - If it is a recognised archive (.zip / .tar.gz / .7z / …) the archive
          is extracted on the Proxmox HOST via SSH into a temporary directory
          (avoiding pulling gigabytes of data into the LXC).  The VMDKs found
          inside are then used as the effective source paths.
        - Otherwise the path is forwarded unchanged.

        Temp directories created for archive extraction are deleted after the
        migration completes (or fails).
        """
        resolved_paths: list[Path] = []
        host_temp_dirs: list[str] = []

        def _collect_remote_vmdks(remote_dir: str) -> list[Path]:
            collected: list[Path] = []
            try:
                listing = self.proxmox.list_remote_dir(remote_dir)
            except Exception:
                return collected
            for f in listing.get("files", []):
                if f["name"].lower().endswith(".vmdk"):
                    collected.append(Path(f["path"]))
            for folder in listing.get("folders", []):
                collected.extend(_collect_remote_vmdks(folder))
            return collected

        for raw_path in disk_paths:
            raw_path_str = str(raw_path)
            archive_type = detect_archive_type(raw_path_str)
            if archive_type is not None:
                stem = Path(raw_path_str).stem.replace(" ", "_")
                dest = f"/tmp/pve-extract-{stem}"
                self.logger.info(
                    "Archive detected (%s): extracting %s → %s on host",
                    archive_type, raw_path_str, dest,
                )
                self.proxmox.extract_archive(raw_path_str, dest)
                host_temp_dirs.append(dest)
                resolved_paths.extend(_collect_remote_vmdks(dest))
            else:
                resolved_paths.append(Path(raw_path_str))

        try:
            return self.migrate_local_disks(
                vm_name=vm_name,
                manifest_path=manifest_path,
                disk_paths=resolved_paths,
                storage=storage,
                bridge=bridge,
                disk_format=disk_format,
                dry_run=dry_run,
                start_after_import=start_after_import,
                vmx_specs=vmx_specs,
                vmid=vmid,
                disk_storage_map=disk_storage_map,
                nic_bridge_map=nic_bridge_map,
            )
        finally:
            for tmp in host_temp_dirs:
                try:
                    self.proxmox.remove_remote_dir(tmp)
                except Exception as cleanup_err:  # noqa: BLE001
                    self.logger.warning("Failed to clean up host temp dir %s: %s", tmp, cleanup_err)
