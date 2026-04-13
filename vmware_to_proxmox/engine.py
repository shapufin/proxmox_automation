from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
import json
import logging
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .config import AppConfig
from .disk import DiskConversionError, convert_disk, detect_archive_type, detect_disk_format, qemu_info, resize_disk, get_disk_size_gb, sha256_file
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
    attached: bool = False
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
    nics: list[dict[str, Any]] = field(default_factory=list)
    disks: list[dict[str, Any]] = field(default_factory=list)
    compatibility: dict[str, Any] = field(default_factory=dict)


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

    @staticmethod
    def _map_guest_os_to_ostype(guest_os: str, guest_os_full_name: str = "") -> str:
        """Map VMware guest OS identifier to Proxmox ostype."""
        guest_lower = (guest_os_full_name or guest_os).lower()
        
        # Windows mapping
        if "windows11" in guest_lower or "win11" in guest_lower:
            return "win11"
        if "windows10" in guest_lower or "win10" in guest_lower:
            return "win10"
        if "windows2019" in guest_lower or "win2019" in guest_lower or "server2019" in guest_lower:
            return "win2019"
        if "windows2016" in guest_lower or "win2016" in guest_lower or "server2016" in guest_lower:
            return "win2016"
        if "windows2012" in guest_lower or "win2012" in guest_lower or "server2012" in guest_lower:
            return "win2012"
        if "windows8" in guest_lower or "win8" in guest_lower:
            return "win8"
        if "windows7" in guest_lower or "win7" in guest_lower:
            return "win7"
        
        # Linux and others default to l26 (Linux kernel 2.6+)
        return "l26"

    @staticmethod
    def _map_scsi_to_proxmox(vmware_scsi: str) -> str:
        """Map VMware SCSI controller type to Proxmox scsihw."""
        scsi_lower = vmware_scsi.lower()
        
        # Direct mappings
        if scsi_lower == "pvscsi":
            return "pvscsi"
        if scsi_lower == "lsilogic":
            return "lsi"
        if scsi_lower == "lsisas1068":
            return "mptsas1068"
        if scsi_lower == "buslogic":
            return "buslogic"
        
        # Default to modern virtio-scsi-single for unknown types
        return "virtio-scsi-single"

    @staticmethod
    def _map_nic_model(vmware_model: str) -> str:
        """Map VMware NIC virtualDev to Proxmox model."""
        model_lower = vmware_model.lower()
        
        # Direct mappings
        if model_lower == "vmxnet3":
            return "vmxnet3"
        if model_lower in ("e1000e", "e1000"):
            return "e1000"
        if model_lower == "vlance":
            return "rtl8139"
        
        # Default to virtio for best performance on Linux
        return "virtio"

    @staticmethod
    def _default_migration_ledger() -> dict[str, Any]:
        return {
            "schema_version": 1,
            "stages": {
                "vm_created": {
                    "status": "pending",
                    "started_at": None,
                    "completed_at": None,
                    "error": "",
                    "artifacts": {
                        "vmid": None,
                        "proxmox_name": "",
                    },
                },
                "disks_exported": {
                    "status": "pending",
                    "started_at": None,
                    "completed_at": None,
                    "error": "",
                    "artifacts": {
                        "source_paths": [],
                        "export_paths": [],
                    },
                },
                "disks_imported": {
                    "status": "pending",
                    "started_at": None,
                    "completed_at": None,
                    "error": "",
                    "artifacts": {
                        "volume_ids": [],
                        "imported_disks": [],
                    },
                },
                "nics_configured": {
                    "status": "pending",
                    "started_at": None,
                    "completed_at": None,
                    "error": "",
                    "artifacts": {
                        "networks": [],
                    },
                },
                "remediation_applied": {
                    "status": "pending",
                    "started_at": None,
                    "completed_at": None,
                    "error": "",
                    "artifacts": {
                        "script_path": "",
                        "applied": False,
                    },
                },
            },
            "cleanup": {
                "status": "pending",
                "started_at": None,
                "completed_at": None,
                "deleted_volume_ids": [],
                "deleted_vmid": None,
                "errors": [],
            },
        }

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(dt_timezone.utc).isoformat()

    def _stage(self, ledger: dict[str, Any], stage_name: str) -> dict[str, Any]:
        stages = ledger.setdefault("stages", {})
        stage = stages.setdefault(stage_name, {})
        stage.setdefault("status", "pending")
        stage.setdefault("started_at", None)
        stage.setdefault("completed_at", None)
        stage.setdefault("error", "")
        stage.setdefault("artifacts", {})
        return stage

    def reconcile(self, migration_ledger: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        ledger = self._default_migration_ledger()
        if not isinstance(migration_ledger, dict):
            return ledger

        ledger["schema_version"] = int(migration_ledger.get("schema_version", ledger["schema_version"]) or ledger["schema_version"])
        source_stages = migration_ledger.get("stages", {}) if isinstance(migration_ledger.get("stages", {}), dict) else {}
        for stage_name in ledger["stages"]:
            source_stage = source_stages.get(stage_name, {}) if isinstance(source_stages, dict) else {}
            if not isinstance(source_stage, dict):
                continue
            stage = ledger["stages"][stage_name]
            stage["status"] = str(source_stage.get("status", stage["status"]))
            stage["started_at"] = source_stage.get("started_at", stage["started_at"])
            stage["completed_at"] = source_stage.get("completed_at", stage["completed_at"])
            stage["error"] = str(source_stage.get("error", stage["error"]))
            artifacts = source_stage.get("artifacts", {})
            if isinstance(artifacts, dict):
                stage["artifacts"].update(artifacts)

        source_cleanup = migration_ledger.get("cleanup", {}) if isinstance(migration_ledger.get("cleanup", {}), dict) else {}
        if isinstance(source_cleanup, dict):
            cleanup = ledger["cleanup"]
            cleanup["status"] = str(source_cleanup.get("status", cleanup["status"]))
            cleanup["started_at"] = source_cleanup.get("started_at", cleanup["started_at"])
            cleanup["completed_at"] = source_cleanup.get("completed_at", cleanup["completed_at"])
            cleanup["deleted_volume_ids"] = list(source_cleanup.get("deleted_volume_ids", cleanup["deleted_volume_ids"]))
            cleanup["deleted_vmid"] = source_cleanup.get("deleted_vmid", cleanup["deleted_vmid"])
            cleanup["errors"] = list(source_cleanup.get("errors", cleanup["errors"]))

        return ledger

    def _persist_ledger(self, ledger: dict[str, Any], persist_ledger: Optional[Callable[[dict[str, Any]], None]] = None) -> None:
        if persist_ledger is not None:
            persist_ledger(ledger)

    def _stage_started(self, ledger: dict[str, Any], stage_name: str, persist_ledger: Optional[Callable[[dict[str, Any]], None]] = None) -> dict[str, Any]:
        stage = self._stage(ledger, stage_name)
        if not stage.get("started_at"):
            stage["started_at"] = self._timestamp()
        stage["status"] = "running"
        self._persist_ledger(ledger, persist_ledger)
        return stage

    def _stage_touch(
        self,
        ledger: dict[str, Any],
        stage_name: str,
        persist_ledger: Optional[Callable[[dict[str, Any]], None]] = None,
        **artifacts: Any,
    ) -> dict[str, Any]:
        stage = self._stage(ledger, stage_name)
        stage["artifacts"].update({key: value for key, value in artifacts.items() if value is not None})
        stage["status"] = stage.get("status") or "running"
        if not stage.get("started_at"):
            stage["started_at"] = self._timestamp()
        self._persist_ledger(ledger, persist_ledger)
        return stage

    def _stage_succeeded(
        self,
        ledger: dict[str, Any],
        stage_name: str,
        persist_ledger: Optional[Callable[[dict[str, Any]], None]] = None,
        **artifacts: Any,
    ) -> dict[str, Any]:
        stage = self._stage(ledger, stage_name)
        stage["artifacts"].update({key: value for key, value in artifacts.items() if value is not None})
        if not stage.get("started_at"):
            stage["started_at"] = self._timestamp()
        stage["status"] = "succeeded"
        stage["completed_at"] = self._timestamp()
        stage["error"] = ""
        self._persist_ledger(ledger, persist_ledger)
        return stage

    def _stage_failed(
        self,
        ledger: dict[str, Any],
        stage_name: str,
        error: str,
        persist_ledger: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> dict[str, Any]:
        stage = self._stage(ledger, stage_name)
        if not stage.get("started_at"):
            stage["started_at"] = self._timestamp()
        stage["status"] = "failed"
        stage["completed_at"] = self._timestamp()
        stage["error"] = error
        self._persist_ledger(ledger, persist_ledger)
        return stage

    @staticmethod
    def _stage_succeeded_flag(ledger: dict[str, Any], stage_name: str) -> bool:
        stage = ledger.get("stages", {}).get(stage_name, {})
        return str(stage.get("status", "")).lower() == "succeeded"

    @staticmethod
    def _stage_artifacts(ledger: dict[str, Any], stage_name: str) -> dict[str, Any]:
        stage = ledger.get("stages", {}).get(stage_name, {})
        artifacts = stage.get("artifacts", {}) if isinstance(stage, dict) else {}
        return artifacts if isinstance(artifacts, dict) else {}

    def _cleanup_state(self, ledger: dict[str, Any]) -> dict[str, Any]:
        cleanup = ledger.setdefault("cleanup", {})
        cleanup.setdefault("status", "pending")
        cleanup.setdefault("started_at", None)
        cleanup.setdefault("completed_at", None)
        cleanup.setdefault("deleted_volume_ids", [])
        cleanup.setdefault("deleted_vmid", None)
        cleanup.setdefault("errors", [])
        return cleanup

    def _resolve_workdir(
        self,
        ledger: dict[str, Any],
        vm_name: str,
        staging_dir: Optional[Path] = None,
        persist_ledger: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> Path:
        stored = self._stage_artifacts(ledger, "disks_exported").get("working_dir")
        candidate = Path(stored) if stored else staging_dir
        if candidate is None:
            candidate = Path(tempfile.mkdtemp(prefix=f"pve-migrate-{vm_name}-"))
            self._stage_touch(ledger, "disks_exported", persist_ledger, working_dir=str(candidate))
            return candidate
        candidate = Path(candidate)
        candidate.mkdir(parents=True, exist_ok=True)
        if not stored:
            self._stage_touch(ledger, "disks_exported", persist_ledger, working_dir=str(candidate))
        return candidate

    @staticmethod
    def _ledger_import_records(ledger: dict[str, Any]) -> list[dict[str, Any]]:
        imported_stage = ledger.get("stages", {}).get("disks_imported", {})
        if not isinstance(imported_stage, dict):
            return []
        artifacts = imported_stage.get("artifacts", {})
        if not isinstance(artifacts, dict):
            return []
        records = artifacts.get("imported_disks", [])
        return [record for record in records if isinstance(record, dict)]

    @staticmethod
    def _path_key(path: str | Path) -> str:
        return str(Path(path).expanduser().resolve())

    def _find_import_record(
        self,
        ledger: dict[str, Any],
        source_path: str | Path,
        index: int,
    ) -> Optional[dict[str, Any]]:
        source_key = self._path_key(source_path)
        slot = f"scsi{index}"
        for record in self._ledger_import_records(ledger):
            record_source = record.get("source") or record.get("local_path") or record.get("converted_path")
            if record_source and self._path_key(record_source) == source_key:
                return record
            if str(record.get("slot", "")).strip() == slot:
                return record
        return None

    def _record_import(
        self,
        ledger: dict[str, Any],
        record: dict[str, Any],
        persist_ledger: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> dict[str, Any]:
        stage = self._stage(ledger, "disks_imported")
        records = stage["artifacts"].setdefault("imported_disks", [])
        volume_ids = stage["artifacts"].setdefault("volume_ids", [])
        normalized_record = {
            "source": str(record.get("source", "")),
            "local_path": str(record.get("local_path", "")),
            "converted_path": str(record.get("converted_path", "")),
            "volume_id": str(record.get("volume_id", "")),
            "slot": str(record.get("slot", "")),
            "attached": bool(record.get("attached", False)),
            "target_storage": str(record.get("target_storage", "")),
            "source_datastore": str(record.get("source_datastore", "")),
        }
        source_key = self._path_key(normalized_record["source"]) if normalized_record["source"] else ""
        replaced = False
        for index, existing in enumerate(records):
            if not isinstance(existing, dict):
                continue
            existing_source = str(existing.get("source", "")).strip()
            existing_slot = str(existing.get("slot", "")).strip()
            if source_key and existing_source and self._path_key(existing_source) == source_key:
                records[index] = normalized_record
                replaced = True
                break
            if normalized_record["slot"] and existing_slot == normalized_record["slot"]:
                records[index] = normalized_record
                replaced = True
                break
        if not replaced:
            records.append(normalized_record)
        if normalized_record["volume_id"] and normalized_record["volume_id"] not in volume_ids:
            volume_ids.append(normalized_record["volume_id"])
        self._persist_ledger(ledger, persist_ledger)
        return normalized_record

    @staticmethod
    def _ledger_network_records(ledger: dict[str, Any]) -> list[dict[str, Any]]:
        nic_stage = ledger.get("stages", {}).get("nics_configured", {})
        if not isinstance(nic_stage, dict):
            return []
        artifacts = nic_stage.get("artifacts", {})
        if not isinstance(artifacts, dict):
            return []
        records = artifacts.get("networks", [])
        return [record for record in records if isinstance(record, dict)]

    def _find_network_record(
        self,
        ledger: dict[str, Any],
        index: int,
    ) -> Optional[dict[str, Any]]:
        for record in self._ledger_network_records(ledger):
            if int(record.get("index", -1) or -1) == index:
                return record
        return None

    def _record_network(
        self,
        ledger: dict[str, Any],
        record: dict[str, Any],
        persist_ledger: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> dict[str, Any]:
        stage = self._stage(ledger, "nics_configured")
        records = stage["artifacts"].setdefault("networks", [])
        normalized_record = {
            "index": int(record.get("index", 0) or 0),
            "label": str(record.get("label", "")),
            "bridge": str(record.get("bridge", "")),
            "macaddr": str(record.get("macaddr", "")),
            "model": str(record.get("model", "virtio")),
            "vlan": record.get("vlan"),
        }
        replaced = False
        for pos, existing in enumerate(records):
            if not isinstance(existing, dict):
                continue
            if int(existing.get("index", -1) or -1) == normalized_record["index"]:
                records[pos] = normalized_record
                replaced = True
                break
        if not replaced:
            records.append(normalized_record)
        self._persist_ledger(ledger, persist_ledger)
        return normalized_record

    def _cleanup_failed_resources(
        self,
        ledger: dict[str, Any],
        persist_ledger: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        cleanup = self._cleanup_state(ledger)
        if not cleanup.get("started_at"):
            cleanup["started_at"] = self._timestamp()
        cleanup["status"] = "running"
        self._persist_ledger(ledger, persist_ledger)

        deleted_volume_ids: list[str] = [str(volume_id) for volume_id in cleanup.get("deleted_volume_ids", []) if str(volume_id).strip()]
        deleted_seen = set(deleted_volume_ids)
        errors: list[str] = [str(item) for item in cleanup.get("errors", []) if str(item).strip()]

        vm_stage = ledger.get("stages", {}).get("vm_created", {})
        vm_artifacts = vm_stage.get("artifacts", {}) if isinstance(vm_stage, dict) else {}
        created_vmid = vm_artifacts.get("vmid") if isinstance(vm_artifacts, dict) else None
        if self.config.migration.rollback_on_failure and self._stage_succeeded_flag(ledger, "vm_created") and created_vmid is not None:
            try:
                self.proxmox.destroy_vm(int(created_vmid))
                cleanup["deleted_vmid"] = int(created_vmid)
                vm_stage = self._stage(ledger, "vm_created")
                vm_stage["status"] = "failed"
                vm_stage["completed_at"] = self._timestamp()
                vm_stage["error"] = "rolled back during cleanup"
            except Exception as exc:  # noqa: BLE001
                errors.append(f"vm {created_vmid}: {exc}")

        imported_stage = ledger.get("stages", {}).get("disks_imported", {})
        imported_artifacts = imported_stage.get("artifacts", {}) if isinstance(imported_stage, dict) else {}
        candidates: list[str] = []
        if isinstance(imported_artifacts, dict):
            for volume_id in imported_artifacts.get("volume_ids", []) or []:
                text = str(volume_id).strip()
                if text:
                    candidates.append(text)
            for record in imported_artifacts.get("imported_disks", []) or []:
                if isinstance(record, dict):
                    volume_id = str(record.get("volume_id", "")).strip()
                    if volume_id:
                        candidates.append(volume_id)

        for volume_id in dict.fromkeys(candidates):
            if volume_id in deleted_seen:
                continue
            try:
                self.proxmox.remove_volume(volume_id)
                deleted_volume_ids.append(volume_id)
                deleted_seen.add(volume_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"volume {volume_id}: {exc}")

        cleanup["deleted_volume_ids"] = deleted_volume_ids
        cleanup["errors"] = errors
        cleanup["status"] = "failed" if errors else "succeeded"
        cleanup["completed_at"] = self._timestamp()
        if not errors:
            self._reset_ledger_for_retry(ledger)
        self._persist_ledger(ledger, persist_ledger)

    def _reset_ledger_for_retry(self, ledger: dict[str, Any]) -> None:
        template = self._default_migration_ledger()
        for stage_name, stage_template in template["stages"].items():
            stage = self._stage(ledger, stage_name)
            stage["status"] = "pending"
            stage["started_at"] = None
            stage["completed_at"] = None
            stage["error"] = ""
            stage["artifacts"] = dict(stage_template.get("artifacts", {}))

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

    @staticmethod
    def _disk_identity_keys(disk: Optional[VmwareDiskSpec], index: int, fallback_key: Optional[str] = None) -> list[str]:
        keys: list[str] = []
        if fallback_key:
            keys.append(fallback_key)
        if disk is None:
            keys.extend([f"disk-{index}", f"scsi{index}", str(index)])
            return keys
        controller_key = getattr(disk, "controller_key", None)
        unit_number = getattr(disk, "unit_number", None)
        label = getattr(disk, "label", "")
        file_name = getattr(disk, "file_name", "")
        datastore = getattr(disk, "datastore", "")
        # Add datastore as a high-priority key for mapping
        if datastore:
            keys.append(f"datastore:{datastore}")
        keys.extend(
            [
                getattr(disk, "path", "") if hasattr(disk, "path") else "",
                file_name,
                label,
                f"controller-{controller_key}-{unit_number}" if controller_key is not None and unit_number is not None else "",
                f"controller:{controller_key}:{unit_number}" if controller_key is not None and unit_number is not None else "",
                f"disk-{index}",
                f"scsi{index}",
                str(index),
            ]
        )
        # Keep ordering stable but drop empties and duplicates.
        seen: set[str] = set()
        ordered: list[str] = []
        for item in keys:
            text = str(item).strip()
            if text and text not in seen:
                seen.add(text)
                ordered.append(text)
        return ordered

    @staticmethod
    def _nic_identity_keys(nic: Optional[VmwareNicSpec], index: int) -> list[str]:
        keys: list[str] = []
        if nic is None:
            return [f"nic-{index}", str(index)]
        keys.extend(
            [
                getattr(nic, "label", ""),
                getattr(nic, "network_name", ""),
                getattr(nic, "mac_address", ""),
                getattr(nic, "adapter_type", ""),
                f"nic-{index}",
                str(index),
            ]
        )
        seen: set[str] = set()
        ordered: list[str] = []
        for item in keys:
            text = str(item).strip()
            if text and text not in seen:
                seen.add(text)
                ordered.append(text)
        return ordered

    def _build_compatibility_report(
        self,
        vm: VmwareVmSpec,
        *,
        source_mode: str,
        target_storage: Optional[str],
        target_bridge: Optional[str],
        disk_format: Optional[DiskFormat],
        allow_disk_shrink: bool = False,
        fallback_nic_bridge: Optional[str] = None,
    ) -> dict[str, Any]:
        warnings: list[str] = []
        blocking_issues: list[str] = []
        recommendations: list[str] = []
        findings: list[dict[str, Any]] = []

        def add_finding(severity: str, category: str, message: str, recommendation: str = "") -> None:
            findings.append({
                "severity": severity,
                "category": category,
                "message": message,
                "recommendation": recommendation,
            })

        add_finding("info", "source", f"Source mode: {source_mode}")
        add_finding("info", "target", f"Target storage: {target_storage or 'auto'}")
        add_finding("info", "target", f"Target bridge: {target_bridge or 'auto'}")
        add_finding("info", "target", f"Target disk format: {disk_format.value if disk_format else 'auto'}")

        if not vm.is_linux:
            msg = f"VM '{vm.name}' is not identified as Linux (guestId={vm.guest_id!r})"
            blocking_issues.append(msg)
            recommendations.append("Use a Linux guest or migrate through a guest-aware Windows workflow")
            add_finding("blocker", "guest_os", msg, "Use a Linux guest or migrate through a guest-aware Windows workflow")

        if vm.power_state.lower() not in {"poweredoff", "powered_off", "off"}:
            msg = f"VM '{vm.name}' must be powered off before migration"
            blocking_issues.append(msg)
            recommendations.append("Power off the VM cleanly before migration")
            add_finding("blocker", "power_state", msg, "Power off the VM cleanly before migration")

        if vm.has_snapshots:
            snapshot_note = f"VM '{vm.name}' has {vm.snapshot_count or 'one or more'} snapshot(s)"
            warnings.append(snapshot_note)
            recommendations.append("Consolidate or remove snapshots before migration")
            msg = f"VM '{vm.name}' still has snapshots; consolidate or remove them first"
            blocking_issues.append(msg)
            add_finding("blocker", "snapshot", msg, "Consolidate or remove snapshots before migration")
            if vm.snapshot_count:
                add_finding(
                    "warning",
                    "snapshot",
                    f"Snapshot count detected: {vm.snapshot_count}",
                    "Review snapshot chain depth and consolidation time",
                )

        if vm.has_vtpm:
            msg = "vTPM detected; Proxmox cannot migrate vTPM state from VMware"
            warnings.append(msg)
            recommendations.append("Plan to recreate or re-enroll trusted platform features in Proxmox")
            add_finding("warning", "security", msg, "Plan to recreate or re-enroll trusted platform features in Proxmox")

        if vm.has_pci_passthrough:
            msg = "PCI passthrough detected; manual reconfiguration may be required"
            warnings.append(msg)
            recommendations.append("Document passthrough devices and recreate them manually on the Proxmox side")
            add_finding("warning", "passthrough", msg, "Document passthrough devices and recreate them manually on the Proxmox side")

        if not vm.disks:
            msg = f"VM '{vm.name}' has no virtual disks"
            blocking_issues.append(msg)
            recommendations.append("Validate the source inventory or manifest before migrating")
            add_finding("blocker", "disk_inventory", msg, "Validate the source inventory or manifest before migrating")

        if not vm.nics:
            msg = "No NICs were discovered from VMware; a fallback bridge will be used if configured"
            warnings.append(msg)
            if fallback_nic_bridge:
                recommendations.append(f"Fallback bridge '{fallback_nic_bridge}' will be used for the first virtual NIC")
            else:
                recommendations.append("Configure a fallback NIC bridge to avoid target bridge ambiguity")
            add_finding("warning", "network", msg, "Configure a fallback NIC bridge to avoid target bridge ambiguity")

        if vm.hardware_version:
            add_finding("info", "hardware_version", f"VMware hardware version: {vm.hardware_version}")

        # Controller/layout fidelity checks.
        controller_keys = [disk.controller_key for disk in vm.disks if getattr(disk, "controller_key", None) is not None]
        if vm.disks and not controller_keys:
            msg = "Disk controller metadata is missing; migration will rely on disk order rather than controller topology"
            warnings.append(msg)
            recommendations.append("Export full VMware hardware metadata to preserve controller relationships")
            add_finding("warning", "controller_layout", msg, "Export full VMware hardware metadata to preserve controller relationships")
        else:
            unit_numbers = [disk.unit_number for disk in vm.disks if getattr(disk, "unit_number", None) is not None]
            if unit_numbers and len(set(controller_keys)) > 1:
                add_finding(
                    "info",
                    "controller_layout",
                    f"Multiple SCSI controllers detected: {sorted(set(str(item) for item in controller_keys))}",
                    "Ensure the target Proxmox SCSI controller configuration matches the source layout",
                )
            if any(getattr(disk, "unit_number", None) is None for disk in vm.disks):
                msg = "One or more disks are missing unit number metadata"
                warnings.append(msg)
                recommendations.append("Populate disk unit numbers to improve deterministic attachment ordering")
                add_finding("warning", "controller_layout", msg, "Populate disk unit numbers to improve deterministic attachment ordering")

        adapter_types = {nic.adapter_type for nic in vm.nics if getattr(nic, "adapter_type", "")}
        if len(adapter_types) > 1:
            add_finding(
                "warning",
                "network_adapter",
                f"Mixed NIC adapter types detected: {sorted(adapter_types)}",
                "Review whether virtio-only mapping is acceptable for this VM",
            )

        rdm_disks = [disk for disk in vm.disks if getattr(disk, "is_rdm", False)]
        if rdm_disks:
            labels: list[str] = []
            for idx, disk in enumerate(rdm_disks):
                bits = [getattr(disk, "label", "") or getattr(disk, "file_name", "") or f"disk-{idx}"]
                if getattr(disk, "lun_id", None) not in (None, ""):
                    bits.append(f"LUN {getattr(disk, 'lun_id')}")
                labels.append(" / ".join(bit for bit in bits if bit))
            label_text = ", ".join(labels)
            msg = "Raw LUN / RDM disks detected; automatic import is not supported in the VMDK migration path"
            warnings.append(msg)
            blocking_issues.append(msg)
            recommendations.append("Handle RDM/LUN devices separately using a dedicated Proxmox passthrough strategy or migrate them manually")
            add_finding(
                "blocker",
                "rdm_lun",
                msg,
                "Use a dedicated Proxmox passthrough strategy or migrate the LUNs manually",
            )
            add_finding(
                "warning",
                "rdm_lun",
                f"Detected RDM/LUN disks: {label_text}",
                "Review each LUN and decide whether it should be recreated, passed through, or excluded",
            )

        # Derived summary.
        can_proceed = not blocking_issues
        summary = "Compatible with warnings" if warnings and can_proceed else ("Blocked" if blocking_issues else "Compatible")
        return {
            "vm_name": vm.name,
            "vmware_hardware_version": vm.hardware_version,
            "source_mode": source_mode,
            "target_storage": target_storage,
            "target_bridge": target_bridge,
            "target_disk_format": disk_format.value if disk_format else None,
            "can_proceed": can_proceed,
            "summary": summary,
            "blocking_issues": blocking_issues,
            "warnings": warnings,
            "recommendations": recommendations,
            "findings": findings,
            "hardware": {
                "guest_id": vm.guest_id,
                "power_state": vm.power_state,
                "memory_mb": vm.memory_mb,
                "cpu_count": vm.cpu_count,
                "snapshot_count": vm.snapshot_count,
                "has_snapshots": vm.has_snapshots,
                "has_vtpm": vm.has_vtpm,
                "has_pci_passthrough": vm.has_pci_passthrough,
                "disk_count": len(vm.disks),
                "nic_count": len(vm.nics),
                "adapter_types": sorted(adapter_types),
            },
        }

    def _resolve_disk_storage(
        self,
        disk: Optional[VmwareDiskSpec],
        index: int,
        storage_override: Optional[str] = None,
        disk_storage_map: Optional[dict[str, str]] = None,
        datastore_map: Optional[dict[str, str]] = None,
    ) -> str:
        datastore = getattr(disk, "datastore", "") if disk is not None else ""
        # Validate datastore name format to prevent injection
        if datastore and not re.match(r'^[a-zA-Z0-9_\-\.]+$', datastore):
            self.logger.warning("Invalid datastore name format: %s", datastore)
            datastore = ""
        # First check per-disk storage map
        mapped = self._map_lookup(disk_storage_map, *self._disk_identity_keys(disk, index, datastore))
        # If no per-disk mapping, check datastore map (datastore: proxmox_storage)
        if not mapped and datastore and datastore_map:
            mapped = datastore_map.get(f"datastore:{datastore}") or datastore_map.get(datastore)
        # Fall back to storage_override or config-based datastore mapping
        preferred = mapped or storage_override or (self.config.storage_for_datastore(datastore) if datastore else None)
        if not preferred:
            disk_label = disk.label if disk else f"disk-{index}"
            raise ProxmoxClientError(f"No storage resolved for {disk_label}. Ensure a default storage is configured or provide explicit mapping.")
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
        mapped = self._map_lookup(nic_bridge_map, *self._nic_identity_keys(nic, index))
        return self._resolve_bridge(mapped or bridge_override or self.config.bridge_for_network(network_name))

    def _resolve_fallback_nic_bridge(self, bridge_override: Optional[str] = None) -> str:
        preferred = bridge_override or self.config.proxmox.default_bridge
        return self._resolve_bridge(preferred)

    @staticmethod
    def _resolve_disk_resize(
        disk_key: str,
        disk_resize_map: Optional[dict[str, int]] = None,
        disk: Optional[VmwareDiskSpec] = None,
        index: Optional[int] = None,
    ) -> Optional[int]:
        """Look up a target resize size (in GB) for a disk from the resize map.

        Tries exact key match first, then falls back to disk index patterns.
        Returns None if no resize is configured for this disk.
        """
        if not disk_resize_map:
            return None
        candidates: list[str] = [disk_key]
        if index is not None:
            candidates.extend([f"disk-{index}", f"scsi{index}", str(index)])
        if disk is not None:
            controller_key = getattr(disk, "controller_key", None)
            unit_number = getattr(disk, "unit_number", None)
            candidates.extend(
                [
                    getattr(disk, "file_name", ""),
                    getattr(disk, "label", ""),
                    f"controller-{controller_key}-{unit_number}" if controller_key is not None and unit_number is not None else "",
                    f"controller:{controller_key}:{unit_number}" if controller_key is not None and unit_number is not None else "",
                ]
            )
        # Try exact and basename matches
        candidates.append(Path(disk_key).name)
        seen: set[str] = set()
        for candidate in candidates:
            text = str(candidate).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            if text in disk_resize_map:
                val = disk_resize_map[text]
                if val and int(val) > 0:
                    return int(val)
        # Basename fallback for path-like keys
        basename = Path(disk_key).name
        if basename in disk_resize_map:
            val = disk_resize_map[basename]
            if val and int(val) > 0:
                return int(val)
        return None

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
        compatibility = self._build_compatibility_report(
            vm,
            source_mode="vmware",
            target_storage=storage,
            target_bridge=bridge,
            disk_format=disk_format,
        )
        warnings = list(dict.fromkeys((compatibility.get("warnings") or []) + (compatibility.get("recommendations") or [])))
        return self._plan_from_vm(vm, warnings, storage, bridge, disk_format, compatibility=compatibility)

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
                    datastore=str(disk.get("datastore", "")),
                    backing_mode=str(disk.get("backing_mode", "")),
                    device_type=str(disk.get("device_type", "")),
                    lun_id=disk.get("lun_id"),
                    is_rdm=bool(disk.get("is_rdm", False)),
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
            memory_mb=int(specs.get("memory_mb", 1024) or 1024),
            cpu_count=int(specs.get("cpu_count", 1) or 1),
            annotation="",
            datastore="",
            disks=disks,
            nics=nics,
            has_snapshots=False,
            has_vtpm=False,
            has_pci_passthrough=False,
        )

    @staticmethod
    def _synthetic_disk_specs_from_paths(disk_paths: Iterable[Path]) -> list[VmwareDiskSpec]:
        disks: list[VmwareDiskSpec] = []
        for index, disk_path in enumerate(disk_paths):
            disks.append(
                VmwareDiskSpec(
                    label=f"Hard disk {index + 1}",
                    file_name=str(disk_path),
                    capacity_bytes=0,
                    backing_type="file",
                )
            )
        return disks

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
                    virtual_dev=str(net.get("virtual_dev", net.get("adapter", "vmxnet3"))),
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
                    datastore=str(disk.get("datastore", "")),
                    backing_mode=str(disk.get("backing_mode", "")),
                    device_type=str(disk.get("device_type", "")),
                    lun_id=disk.get("lun_id"),
                    is_rdm=bool(disk.get("is_rdm", False)),
                )
                for idx, disk in enumerate(specs.get("disks", []))
                if isinstance(disk, dict)
            ]
        
        # Apply vmx_overrides (user-specified advanced specs from wizard)
        overrides = specs.get("vmx_overrides", {})
        if not isinstance(overrides, dict) or not overrides:
            overrides = {
                key: specs.get(key)
                for key in (
                    "memory_mb",
                    "cpu_count",
                    "firmware",
                    "guest_os",
                    "cpu_hotplug_enabled",
                    "memory_hotplug_enabled",
                    "scsi_controller_type",
                    "guest_os_full_name",
                    "description",
                    "nic_model_default",
                )
                if specs.get(key) is not None and specs.get(key) != ""
            }
        if isinstance(overrides, dict):
            # Hardware overrides
            if overrides.get("memory_mb"):
                vm.memory_mb = int(overrides["memory_mb"])
            if overrides.get("cpu_count"):
                vm.cpu_count = int(overrides["cpu_count"])
            if overrides.get("firmware"):
                vm.firmware = overrides["firmware"]
            if overrides.get("guest_os"):
                vm.guest_id = overrides["guest_os"]
            
            # Advanced spec overrides
            if overrides.get("cpu_hotplug_enabled") is not None:
                vm.cpu_hotplug_enabled = bool(overrides["cpu_hotplug_enabled"])
            if overrides.get("memory_hotplug_enabled") is not None:
                vm.memory_hotplug_enabled = bool(overrides["memory_hotplug_enabled"])
            if overrides.get("scsi_controller_type"):
                vm.scsi_controller_type = overrides["scsi_controller_type"]
            if overrides.get("guest_os_full_name"):
                vm.guest_os_full_name = overrides["guest_os_full_name"]
            if overrides.get("description"):
                vm.annotation = overrides["description"]
            
            # Apply NIC model override if specified
            if overrides.get("nic_model_default"):
                for nic in vm.nics:
                    if not nic.virtual_dev:
                        nic.virtual_dev = overrides["nic_model_default"]
        
        return vm

    def load_local_manifest(self, manifest_path: Optional[Path]) -> VmwareVmSpec:
        return self._vm_from_manifest(manifest_path)

    def _plan_from_vm(
        self,
        vm: VmwareVmSpec,
        warnings: list[str],
        storage: Optional[str],
        bridge: Optional[str],
        disk_format: Optional[DiskFormat],
        compatibility: Optional[dict[str, Any]] = None,
    ) -> MigrationPlan:
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
                    "adapter_type": nic.adapter_type,
                    "nic_keys": self._nic_identity_keys(nic, vm.nics.index(nic)),
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
                    "controller_key": disk.controller_key,
                    "unit_number": disk.unit_number,
                    "disk_keys": self._disk_identity_keys(disk, vm.disks.index(disk), datastore),
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
            compatibility=compatibility or {},
        )

    @staticmethod
    def _compatibility_summary_message(report: dict[str, Any]) -> str:
        blocking = report.get("blocking_issues") or []
        warnings = report.get("warnings") or []
        if blocking:
            return "; ".join(str(item) for item in blocking)
        if warnings:
            return "; ".join(str(item) for item in warnings)
        return ""

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
        datastore_map: Optional[dict[str, str]] = None,
        nic_bridge_map: Optional[dict[str, str]] = None,
        disk_resize_map: Optional[dict[str, int]] = None,
        allow_disk_shrink: bool = False,
        fallback_nic_bridge: Optional[str] = None,
        compatibility: Optional[dict[str, Any]] = None,
        migration_ledger: Optional[dict[str, Any]] = None,
        persist_ledger: Optional[Callable[[dict[str, Any]], None]] = None,
        staging_dir: Optional[Path] = None,
    ) -> MigrationResult:
        self.proxmox.ensure_prerequisites()
        dry_run = self.config.migration.dry_run if dry_run is None else dry_run
        disk_paths = list(disk_paths)
        vm = self._vm_from_manifest(manifest_path, vmx_specs=vmx_specs, fallback_name=vm_name)
        if vm_name and vm.name != vm_name:
            vm.name = vm_name
        synthesized_disk_warning = ""
        if not vm.disks and disk_paths:
            vm.disks = self._synthetic_disk_specs_from_paths(disk_paths)
            synthesized_disk_warning = (
                "No manifest or VMX disk metadata was available; "
                "synthesized minimal disk specs from the provided local source paths"
            )
        compatibility = self._build_compatibility_report(
            vm,
            source_mode="local",
            target_storage=storage,
            target_bridge=bridge,
            disk_format=disk_format,
            allow_disk_shrink=allow_disk_shrink,
            fallback_nic_bridge=fallback_nic_bridge,
        )
        warnings = list(dict.fromkeys((compatibility.get("warnings") or []) + (compatibility.get("recommendations") or [])))
        if synthesized_disk_warning:
            warnings = [synthesized_disk_warning, *warnings]

        ledger = self.reconcile(migration_ledger)
        if persist_ledger is not None:
            persist_ledger(ledger)

        ledger_vmid = self._stage_artifacts(ledger, "vm_created").get("vmid")
        if ledger_vmid not in (None, ""):
            vmid = int(ledger_vmid)
        elif not dry_run:
            vmid = vmid or self.proxmox.next_vmid()
        target_storage = self._resolve_storage(storage)
        target_format = disk_format or self.config.target_format()
        firmware = self._resolve_firmware(vm)
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

        if not vm.name and source_paths:
            vm.name = source_paths[0].stem or vm_name or "vm"

        if not vm.disks and source_paths:
            vm.disks = self._synthetic_disk_specs_from_paths(source_paths)
            self.logger.warning(
                "No disk metadata was available for %s; synthesized %s disk spec(s) from local source paths",
                vm.name,
                len(vm.disks),
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
                    "compatibility": compatibility,
                },
            )

        if compatibility.get("blocking_issues"):
            raise VmwareClientError(self._compatibility_summary_message(compatibility))

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
        target_dir = self._resolve_workdir(ledger, vm.name, staging_dir=staging_dir, persist_ledger=persist_ledger)
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

            if not self._stage_succeeded_flag(ledger, "disks_exported"):
                self._stage_started(ledger, "disks_exported", persist_ledger)
                self._stage_succeeded(
                    ledger,
                    "disks_exported",
                    persist_ledger,
                    source_paths=[str(path) for path in source_paths],
                    export_paths=[str(path) for path in source_paths],
                    working_dir=str(target_dir),
                )

            vm_stage = self._stage(ledger, "vm_created")
            vm_artifacts = vm_stage.get("artifacts", {}) if isinstance(vm_stage, dict) else {}
            proxmox_name = str(vm_artifacts.get("proxmox_name") or "")
            if self._stage_succeeded_flag(ledger, "vm_created") and vmid is not None:
                self.logger.info("Reusing previously created VM %s (VMID %s)", proxmox_name or vm.name, vmid)
            else:
                self._stage_started(ledger, "vm_created", persist_ledger)
                
                # Map VMware values to Proxmox equivalents
                ostype = self._map_guest_os_to_ostype(getattr(vm, "guest_id", ""), getattr(vm, "guest_os_full_name", ""))
                scsihw = self._map_scsi_to_proxmox(getattr(vm, "scsi_controller_type", "") or self.config.proxmox.scsi_controller)
                hotplug_cpu = getattr(vm, "cpu_hotplug_enabled", False)
                hotplug_memory = getattr(vm, "memory_hotplug_enabled", False)
                
                proxmox_name = self.proxmox.create_vm(
                    vmid=vmid,
                    name=vm.name,
                    memory_mb=vm.memory_mb,
                    cores=vm.cpu_count,
                    sockets=1,
                    bios="ovmf" if firmware == FirmwareMode.UEFI else "seabios",
                    scsihw=scsihw,
                    agent=True,
                    hotplug_cpu=hotplug_cpu,
                    hotplug_memory=hotplug_memory,
                    ostype=ostype,
                )
                efi_disk_added = False
                if firmware == FirmwareMode.UEFI and self.config.proxmox.create_efi_disk:
                    self.proxmox.add_efi_disk(vmid, target_storage, target_format)
                    efi_disk_added = True
                
                # Set VM description/annotation if available
                annotation = getattr(vm, "annotation", "")
                if annotation:
                    self.proxmox.set_vm_options(vmid, {"description": annotation})
                
                self._stage_succeeded(
                    ledger,
                    "vm_created",
                    persist_ledger,
                    vmid=vmid,
                    proxmox_name=proxmox_name,
                    efi_disk_added=efi_disk_added,
                )
            if proxmox_name != vm.name:
                self.logger.warning(
                    "Proxmox adjusted VM name for %s to %s to satisfy naming rules",
                    vm.name,
                    proxmox_name,
                )

            effective_nics = list(vm.nics)
            if not effective_nics:
                fallback_bridge = fallback_nic_bridge or bridge or self.config.proxmox.default_bridge
                if fallback_bridge:
                    warning = (
                        f"No NICs were discovered for {vm.name}; attaching one default virtio NIC on {fallback_bridge}"
                    )
                    self.logger.warning(warning)
                    warnings = list(warnings) + [warning]
                    effective_nics = [
                        VmwareNicSpec(
                            label="Network adapter 0",
                            network_name=fallback_bridge,
                            mac_address="",
                            adapter_type="virtio",
                        )
                    ]

            for index, nic in enumerate(effective_nics):
                bridge_override = fallback_nic_bridge if (not vm.nics and index == 0 and fallback_nic_bridge) else bridge
                ledger_network = self._find_network_record(ledger, index)
                if ledger_network is not None:
                    continue
                bridge_name = self._resolve_nic_bridge(nic, index, bridge_override, nic_bridge_map)
                mac = nic.mac_address if self.config.proxmox.preserve_mac else ""
                
                # Map VMware virtualDev to Proxmox model
                virtual_dev = getattr(nic, "virtual_dev", "")
                nic_model = self._map_nic_model(virtual_dev) if virtual_dev else "virtio"
                
                self._stage_started(ledger, "nics_configured", persist_ledger)
                self.proxmox.add_network(vmid, index, bridge_name, macaddr=mac, model=nic_model)
                self._record_network(
                    ledger,
                    {
                        "index": index,
                        "label": nic.label,
                        "bridge": bridge_name,
                        "macaddr": mac,
                        "model": nic_model,
                        "vlan": getattr(nic, "vlan_id", None),
                    },
                    persist_ledger,
                )
            self._stage_succeeded(ledger, "nics_configured", persist_ledger, networks=self._ledger_network_records(ledger))

            vmx_disk_specs = list(vm.disks)
            for index, source_path in enumerate(source_paths):
                disk_spec = vmx_disk_specs[index] if index < len(vmx_disk_specs) else None
                existing_record = self._find_import_record(ledger, source_path, index)
                if existing_record is not None and str(existing_record.get("volume_id", "")).strip():
                    slot = str(existing_record.get("slot", f"scsi{index}"))
                    if not bool(existing_record.get("attached", False)):
                        self._stage_started(ledger, "disks_imported", persist_ledger)
                        attach_command = self.proxmox.attach_disk(vmid, str(existing_record["volume_id"]), slot=slot)
                        migration_commands.append(attach_command)
                        existing_record = self._record_import(
                            ledger,
                            {**existing_record, "attached": True},
                            persist_ledger,
                        )
                    import_records.append(DiskImportRecord(**existing_record))
                    continue
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

                # Apply resize if specified (after conversion, before import)
                resize_key = str(source_path)
                resize_gb = self._resolve_disk_resize(resize_key, disk_resize_map)
                if resize_gb:
                    try:
                        current_gb = get_disk_size_gb(converted_path)
                        if resize_gb > current_gb:
                            self.logger.info(
                                "Resizing local disk %s from %.1f GB to %d GB",
                                source_path.name,
                                current_gb,
                                resize_gb,
                            )
                            resize_disk(converted_path, resize_gb)
                        elif resize_gb < current_gb:
                            if allow_disk_shrink:
                                self.logger.info(
                                    "Shrinking local disk %s from %.1f GB to %d GB",
                                    source_path.name,
                                    current_gb,
                                    resize_gb,
                                )
                                resize_disk(converted_path, resize_gb, shrink_ok=True)
                            else:
                                self.logger.warning(
                                    "Skipping local shrink for %s: target %d GB is smaller than current %.1f GB and shrink is not allowed",
                                    source_path.name,
                                    resize_gb,
                                    current_gb,
                                )
                        else:
                            self.logger.info(
                                "Skipping local resize: target %d GB matches current %.1f GB",
                                resize_gb,
                                current_gb,
                            )
                    except Exception as resize_err:
                        self.logger.warning("Failed to resize local disk %s: %s", source_path.name, resize_err)

                disk_target_storage = self._resolve_disk_storage(disk_spec, index, target_storage, disk_storage_map, datastore_map)
                volume_id = self.proxmox.import_disk(vmid, converted_path, disk_target_storage, target_format)
                slot = f"scsi{index}"
                attach_command = self.proxmox.attach_disk(vmid, volume_id, slot=slot)
                self._stage_started(ledger, "disks_imported", persist_ledger)
                migration_commands.append(
                    f"qm importdisk {vmid} {converted_path} {disk_target_storage} --format {target_format.value}"
                )
                migration_commands.append(attach_command)
                import_record = self._record_import(
                    ledger,
                    {
                        "source": str(source_path),
                        "local_path": str(source_path),
                        "converted_path": str(converted_path),
                        "volume_id": volume_id,
                        "slot": slot,
                        "attached": True,
                        "target_storage": disk_target_storage,
                        "source_datastore": getattr(disk_spec, "datastore", "") if disk_spec is not None else "",
                    },
                    persist_ledger,
                )
                import_records.append(DiskImportRecord(**import_record))

            if import_records:
                self._stage_succeeded(ledger, "disks_imported", persist_ledger, imported_disks=self._ledger_import_records(ledger), volume_ids=self._stage_artifacts(ledger, "disks_imported").get("volume_ids", []))

            self.proxmox.set_boot_order(vmid, "scsi0")

            if self.config.migration.guest_remediation and not self._stage_succeeded_flag(ledger, "remediation_applied"):
                self._stage_started(ledger, "remediation_applied", persist_ledger)
                self.remediator.write_script(
                    remediation_path,
                    vm,
                    rewrite_fstab=self.config.migration.guest_rewrite_fstab,
                    install_qemu_agent=self.config.migration.guest_install_qemu_agent,
                )
                self._stage_succeeded(ledger, "remediation_applied", persist_ledger, script_path=str(remediation_path), applied=True)

            if start_after_import:
                status_payload = self.proxmox.status(vmid)
                if str(status_payload.get("status") or status_payload.get("state") or "").lower() != "running":
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
                    "disk_count": len(source_paths),
                    "path_resolution": path_diagnostics,
                    "migration_commands": migration_commands,
                    "disks": [asdict(item) for item in import_records],
                    "compatibility": compatibility,
                },
            )
        except Exception:
            if self.config.migration.rollback_on_failure:
                try:
                    self._cleanup_failed_resources(ledger, persist_ledger)
                except Exception as rollback_error:  # noqa: BLE001
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
        datastore_map: Optional[dict[str, str]] = None,
        nic_bridge_map: Optional[dict[str, str]] = None,
        vmx_specs: Optional[dict] = None,
        disk_resize_map: Optional[dict[str, int]] = None,
        allow_disk_shrink: bool = False,
        fallback_nic_bridge: Optional[str] = None,
        migration_ledger: Optional[dict[str, Any]] = None,
        persist_ledger: Optional[Callable[[dict[str, Any]], None]] = None,
        staging_dir: Optional[Path] = None,
    ) -> MigrationResult:
        self.proxmox.ensure_prerequisites()
        dry_run = self.config.migration.dry_run if dry_run is None else dry_run
        with self.vmware:
            vm = self.vmware.get_vm_by_name(vm_name)
            vm = self._apply_vmx_specs(vm, vmx_specs)
        compatibility = self._build_compatibility_report(
            vm,
            source_mode="vmware",
            target_storage=storage,
            target_bridge=bridge,
            disk_format=disk_format,
            allow_disk_shrink=allow_disk_shrink,
            fallback_nic_bridge=fallback_nic_bridge,
        )
        if compatibility.get("blocking_issues"):
            raise VmwareClientError(self._compatibility_summary_message(compatibility))
        warnings = list(dict.fromkeys((compatibility.get("warnings") or []) + (compatibility.get("recommendations") or [])))
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
            datastore_map=datastore_map,
            nic_bridge_map=nic_bridge_map,
            disk_resize_map=disk_resize_map,
            allow_disk_shrink=allow_disk_shrink,
            fallback_nic_bridge=fallback_nic_bridge,
            compatibility=compatibility,
            migration_ledger=migration_ledger,
            persist_ledger=persist_ledger,
            staging_dir=staging_dir,
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
        datastore_map: Optional[dict[str, str]] = None,
        nic_bridge_map: Optional[dict[str, str]] = None,
        disk_resize_map: Optional[dict[str, int]] = None,
        allow_disk_shrink: bool = False,
        fallback_nic_bridge: Optional[str] = None,
        compatibility: Optional[dict[str, Any]] = None,
        migration_ledger: Optional[dict[str, Any]] = None,
        persist_ledger: Optional[Callable[[dict[str, Any]], None]] = None,
        staging_dir: Optional[Path] = None,
    ) -> MigrationResult:
        target_storage = self._resolve_storage(storage)
        target_format = disk_format or self.config.target_format()
        firmware = self._resolve_firmware(vm)
        network_bridge_override = bridge

        if dry_run:
            return MigrationResult(
                name=vm.name,
                vmid=vmid,
                target_storage=target_storage,
                disk_format=target_format,
                firmware=firmware,
                warnings=warnings,
                details={"dry_run": True, "source_mode": "vmware", "compatibility": compatibility or {}},
            )

        ledger = self.reconcile(migration_ledger)
        if persist_ledger is not None:
            persist_ledger(ledger)

        ledger_vmid = self._stage_artifacts(ledger, "vm_created").get("vmid")
        if ledger_vmid not in (None, ""):
            vmid = int(ledger_vmid)
        else:
            vmid = vmid or self.proxmox.next_vmid()

        target_dir = self._resolve_workdir(ledger, vm.name, staging_dir=staging_dir, persist_ledger=persist_ledger)
        manifest_path = target_dir / f"{vm.name}.manifest.json"
        import_records: list[DiskImportRecord] = []
        migration_commands: list[str] = []
        remediation_path = target_dir / f"{vm.name}.remediation.sh"

        try:
            self.logger.info("Downloading disks for %s into %s", vm.name, target_dir)
            with self.vmware:
                if not self._stage_succeeded_flag(ledger, "disks_exported"):
                    self._stage_started(ledger, "disks_exported", persist_ledger)
                    self.vmware.export_manifest(vm, manifest_path)
                    downloaded = self.vmware.download_vm_disks(vm, target_dir)
                    self._stage_succeeded(
                        ledger,
                        "disks_exported",
                        persist_ledger,
                        source_paths=[str(path) for path in downloaded],
                        export_paths=[str(path) for path in downloaded],
                        working_dir=str(target_dir),
                    )
                else:
                    downloaded = [Path(path) for path in self._stage_artifacts(ledger, "disks_exported").get("export_paths", []) if str(path).strip()]
                    if not downloaded or not all(path.exists() for path in downloaded):
                        downloaded = self.vmware.download_vm_disks(vm, target_dir)
                        self._stage_succeeded(
                            ledger,
                            "disks_exported",
                            persist_ledger,
                            source_paths=[str(path) for path in downloaded],
                            export_paths=[str(path) for path in downloaded],
                            working_dir=str(target_dir),
                        )

            if write_manifest:
                manifest_path.write_text(json.dumps({"vmware": asdict(vm), "target": {"storage": target_storage, "format": target_format.value, "firmware": firmware.value, "vmid": vmid}}, indent=2, sort_keys=True), encoding="utf-8")

            vm_stage = self._stage(ledger, "vm_created")
            vm_artifacts = vm_stage.get("artifacts", {}) if isinstance(vm_stage, dict) else {}
            proxmox_name = str(vm_artifacts.get("proxmox_name") or "")
            if self._stage_succeeded_flag(ledger, "vm_created") and vmid is not None:
                self.logger.info("Reusing previously created VM %s (VMID %s)", proxmox_name or vm.name, vmid)
            else:
                self._stage_started(ledger, "vm_created", persist_ledger)
                
                # Map VMware values to Proxmox equivalents
                ostype = self._map_guest_os_to_ostype(getattr(vm, "guest_id", ""), getattr(vm, "guest_os_full_name", ""))
                scsihw = self._map_scsi_to_proxmox(getattr(vm, "scsi_controller_type", "") or self.config.proxmox.scsi_controller)
                hotplug_cpu = getattr(vm, "cpu_hotplug_enabled", False)
                hotplug_memory = getattr(vm, "memory_hotplug_enabled", False)
                
                proxmox_name = self.proxmox.create_vm(
                    vmid=vmid,
                    name=vm.name,
                    memory_mb=vm.memory_mb,
                    cores=vm.cpu_count,
                    sockets=1,
                    bios="ovmf" if firmware == FirmwareMode.UEFI else "seabios",
                    scsihw=scsihw,
                    agent=True,
                    hotplug_cpu=hotplug_cpu,
                    hotplug_memory=hotplug_memory,
                    ostype=ostype,
                )
                efi_disk_added = False
                if firmware == FirmwareMode.UEFI and self.config.proxmox.create_efi_disk:
                    self.proxmox.add_efi_disk(vmid, target_storage, target_format)
                    efi_disk_added = True
                
                # Set VM description/annotation if available
                annotation = getattr(vm, "annotation", "")
                if annotation:
                    self.proxmox.set_vm_options(vmid, {"description": annotation})
                
                self._stage_succeeded(
                    ledger,
                    "vm_created",
                    persist_ledger,
                    vmid=vmid,
                    proxmox_name=proxmox_name,
                    efi_disk_added=efi_disk_added,
                )

            effective_nics = list(vm.nics)
            if not effective_nics:
                fallback_bridge = fallback_nic_bridge or network_bridge_override or self.config.proxmox.default_bridge
                if fallback_bridge:
                    warning = (
                        f"No NICs were discovered for {vm.name}; attaching one default virtio NIC on {fallback_bridge}"
                    )
                    self.logger.warning(warning)
                    warnings = list(warnings) + [warning]
                    effective_nics = [
                        VmwareNicSpec(
                            label="Network adapter 0",
                            network_name=fallback_bridge,
                            mac_address="",
                            adapter_type="virtio",
                        )
                    ]

            for index, nic in enumerate(effective_nics):
                bridge_override = fallback_nic_bridge if (not vm.nics and index == 0 and fallback_nic_bridge) else network_bridge_override
                ledger_network = self._find_network_record(ledger, index)
                if ledger_network is not None:
                    continue
                bridge_name = self._resolve_nic_bridge(nic, index, bridge_override, nic_bridge_map)
                mac = nic.mac_address if self.config.proxmox.preserve_mac else ""
                
                # Map VMware virtualDev to Proxmox model
                virtual_dev = getattr(nic, "virtual_dev", "")
                nic_model = self._map_nic_model(virtual_dev) if virtual_dev else "virtio"
                
                self._stage_started(ledger, "nics_configured", persist_ledger)
                self.proxmox.add_network(vmid, index, bridge_name, macaddr=mac, model=nic_model)
                self._record_network(
                    ledger,
                    {
                        "index": index,
                        "label": nic.label,
                        "bridge": bridge_name,
                        "macaddr": mac,
                        "model": nic_model,
                        "vlan": getattr(nic, "vlan_id", None),
                    },
                    persist_ledger,
                )
            self._stage_succeeded(ledger, "nics_configured", persist_ledger, networks=self._ledger_network_records(ledger))

            for index, disk_path in enumerate(downloaded):
                disk_spec = vm.disks[index] if index < len(vm.disks) else None
                existing_record = self._find_import_record(ledger, disk_path, index)
                if existing_record is not None and str(existing_record.get("volume_id", "")).strip():
                    slot = str(existing_record.get("slot", f"scsi{index}"))
                    if not bool(existing_record.get("attached", False)):
                        self._stage_started(ledger, "disks_imported", persist_ledger)
                        attach_command = self.proxmox.attach_disk(vmid, str(existing_record["volume_id"]), slot=slot)
                        migration_commands.append(attach_command)
                        existing_record = self._record_import(
                            ledger,
                            {**existing_record, "attached": True},
                            persist_ledger,
                        )
                    import_records.append(DiskImportRecord(**existing_record))
                    continue
                source_format = detect_disk_format(disk_path) or Path(disk_path).suffix.lower().lstrip(".") or "vmdk"
                converted_path = target_dir / f"{disk_path.stem}.{target_format.value}"
                try:
                    convert_disk(disk_path, converted_path, target_format, source_format=source_format)
                    qemu_info(converted_path)
                    source_for_import = converted_path
                except DiskConversionError:
                    source_for_import = disk_path
                    converted_path = disk_path

                # Apply resize if specified (after conversion, before import)
                resize_key = str(disk_path)
                resize_gb = self._resolve_disk_resize(resize_key, disk_resize_map)
                if resize_gb:
                    try:
                        current_gb = get_disk_size_gb(source_for_import)
                        if resize_gb > current_gb:
                            self.logger.info("Resizing disk %s from %.1f GB to %d GB", disk_path.name, current_gb, resize_gb)
                            resize_disk(source_for_import, resize_gb)
                        elif resize_gb < current_gb:
                            if allow_disk_shrink:
                                self.logger.info("Shrinking disk %s from %.1f GB to %d GB", disk_path.name, current_gb, resize_gb)
                                resize_disk(source_for_import, resize_gb, shrink_ok=True)
                            else:
                                self.logger.warning(
                                    "Skipping shrink for %s: target %d GB is smaller than current %.1f GB and shrink is not allowed",
                                    disk_path.name,
                                    resize_gb,
                                    current_gb,
                                )
                        else:
                            self.logger.info("Skipping resize: target %d GB matches current %.1f GB", resize_gb, current_gb)
                    except Exception as resize_err:
                        self.logger.warning("Failed to resize disk %s: %s", disk_path.name, resize_err)

                disk_target_storage = self._resolve_disk_storage(disk_spec, index, target_storage, disk_storage_map, datastore_map)
                volume_id = self.proxmox.import_disk(vmid, source_for_import, disk_target_storage, target_format)
                slot = f"scsi{index}"
                attach_command = self.proxmox.attach_disk(vmid, volume_id, slot=slot)
                self._stage_started(ledger, "disks_imported", persist_ledger)
                migration_commands.append(
                    f"qm importdisk {vmid} {source_for_import} {disk_target_storage} --format {target_format.value}"
                )
                migration_commands.append(attach_command)
                import_record = self._record_import(
                    ledger,
                    {
                        "source": str(disk_path),
                        "local_path": str(disk_path),
                        "converted_path": str(converted_path),
                        "volume_id": volume_id,
                        "slot": slot,
                        "attached": True,
                        "target_storage": disk_target_storage,
                        "source_datastore": getattr(disk_spec, "datastore", "") if disk_spec is not None else "",
                    },
                    persist_ledger,
                )
                import_records.append(DiskImportRecord(**import_record))

            if import_records:
                self._stage_succeeded(ledger, "disks_imported", persist_ledger, imported_disks=self._ledger_import_records(ledger), volume_ids=self._stage_artifacts(ledger, "disks_imported").get("volume_ids", []))

            self.proxmox.set_boot_order(vmid, "scsi0")

            if self.config.migration.guest_remediation and not self._stage_succeeded_flag(ledger, "remediation_applied"):
                self._stage_started(ledger, "remediation_applied", persist_ledger)
                self.remediator.write_script(
                    remediation_path,
                    vm,
                    rewrite_fstab=self.config.migration.guest_rewrite_fstab,
                    install_qemu_agent=self.config.migration.guest_install_qemu_agent,
                )
                self._stage_succeeded(ledger, "remediation_applied", persist_ledger, script_path=str(remediation_path), applied=True)

            if start_after_import:
                status_payload = self.proxmox.status(vmid)
                if str(status_payload.get("status") or status_payload.get("state") or "").lower() != "running":
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
                    "compatibility": compatibility or {},
                }
            )
        except Exception:
            if self.config.migration.rollback_on_failure:
                try:
                    self._cleanup_failed_resources(ledger, persist_ledger)
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
        datastore_map: Optional[dict[str, str]] = None,
        nic_bridge_map: Optional[dict[str, str]] = None,
        disk_resize_map: Optional[dict[str, int]] = None,
        allow_disk_shrink: bool = False,
        fallback_nic_bridge: Optional[str] = None,
        migration_ledger: Optional[dict[str, Any]] = None,
        persist_ledger: Optional[Callable[[dict[str, Any]], None]] = None,
        staging_dir: Optional[Path] = None,
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
        # Validate datastore_map parameter type
        if datastore_map is not None and not isinstance(datastore_map, dict):
            raise TypeError("datastore_map must be a dict or None")
        # Validate disk_storage_map parameter type
        if disk_storage_map is not None and not isinstance(disk_storage_map, dict):
            raise TypeError("disk_storage_map must be a dict or None")

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
                datastore_map=datastore_map,
                nic_bridge_map=nic_bridge_map,
                disk_resize_map=disk_resize_map,
                allow_disk_shrink=allow_disk_shrink,
                fallback_nic_bridge=fallback_nic_bridge,
                migration_ledger=migration_ledger,
                persist_ledger=persist_ledger,
                staging_dir=staging_dir,
            )
        finally:
            for tmp in host_temp_dirs:
                try:
                    self.proxmox.remove_remote_dir(tmp)
                except Exception as cleanup_err:  # noqa: BLE001
                    self.logger.warning("Failed to clean up host temp dir %s: %s", tmp, cleanup_err)
