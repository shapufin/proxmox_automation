from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .models import DiskFormat, FirmwareMode


def _coerce_int(value: Any, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


@dataclass(slots=True)
class VmwareConfig:
    host: str
    username: str
    password: str
    port: int = 443
    datacenter: str = ""
    allow_insecure_ssl: bool = True
    ssh_enabled: bool = True
    ssh_port: int = 22


@dataclass(slots=True)
class ProxmoxConfig:
    node: str
    default_storage: str
    default_bridge: str
    bridge_map: dict[str, str] = field(default_factory=dict)
    datastore_map: dict[str, str] = field(default_factory=dict)
    format: DiskFormat = DiskFormat.QCOW2
    boot_firmware: FirmwareMode = FirmwareMode.AUTO
    scsi_controller: str = "virtio-scsi-single"
    create_efi_disk: bool = True
    preserve_mac: bool = True
    api_user: str = "root@pam"
    api_token_name: str = ""
    api_token_value: str = ""
    api_host: str = ""
    api_verify_ssl: bool = False
    ssh_enabled: bool = False
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_username: str = "root"
    ssh_private_key: str = ""
    ssh_password: str = ""


@dataclass(slots=True)
class MigrationConfig:
    dry_run: bool = True
    batch: bool = False
    allow_snapshots: bool = False
    parallel_jobs: int = 1
    retry_attempts: int = 2
    cleanup_source: bool = False
    guest_remediation: bool = True
    guest_rewrite_fstab: bool = True
    guest_install_qemu_agent: bool = True
    rollback_on_failure: bool = True
    backup_source_manifest: bool = True
    target_format_override: Optional[DiskFormat] = None


@dataclass(slots=True)
class SshConfig:
    enabled: bool = False
    host: str = ""
    port: int = 22
    username: str = "root"
    private_key: str = ""
    password: str = ""


@dataclass(slots=True)
class AppConfig:
    vmware: VmwareConfig
    proxmox: ProxmoxConfig
    migration: MigrationConfig = field(default_factory=MigrationConfig)
    ssh: SshConfig = field(default_factory=SshConfig)

    @classmethod
    def load(cls, path: str | Path) -> "AppConfig":
        raw_path = Path(path)
        data = yaml.safe_load(raw_path.read_text(encoding="utf-8")) or {}
        vmware = data.get("vmware", {})
        proxmox = data.get("proxmox", {})
        migration = data.get("migration", {})
        ssh = data.get("ssh", {})

        format_value = proxmox.get("format", DiskFormat.QCOW2.value)
        boot_value = proxmox.get("boot_firmware", FirmwareMode.AUTO.value)
        override_value = migration.get("target_format_override")

        return cls(
            vmware=VmwareConfig(
                host=str(vmware.get("host", "")),
                username=str(vmware.get("username", "")),
                password=str(vmware.get("password", "")),
                port=_coerce_int(vmware.get("port", 443), 443),
                datacenter=str(vmware.get("datacenter", "")),
                allow_insecure_ssl=_coerce_bool(vmware.get("allow_insecure_ssl", True), True),
                ssh_enabled=_coerce_bool(vmware.get("ssh_enabled", True), True),
                ssh_port=_coerce_int(vmware.get("ssh_port", 22), 22),
            ),
            proxmox=ProxmoxConfig(
                node=str(proxmox.get("node", "")),
                default_storage=str(proxmox.get("default_storage", "")),
                default_bridge=str(proxmox.get("default_bridge", "")),
                bridge_map=dict(proxmox.get("bridge_map", {})),
                datastore_map=dict(proxmox.get("datastore_map", {})),
                format=DiskFormat(format_value),
                boot_firmware=FirmwareMode(boot_value),
                scsi_controller=str(proxmox.get("scsi_controller", "virtio-scsi-single")),
                create_efi_disk=_coerce_bool(proxmox.get("create_efi_disk", True), True),
                preserve_mac=_coerce_bool(proxmox.get("preserve_mac", True), True),
                api_user=str(proxmox.get("api_user", "root@pam")),
                api_token_name=str(proxmox.get("api_token_name", "")),
                api_token_value=str(proxmox.get("api_token_value", "")),
                api_host=str(proxmox.get("api_host", "")),
                api_verify_ssl=_coerce_bool(proxmox.get("api_verify_ssl", False), False),
                ssh_enabled=_coerce_bool(proxmox.get("ssh_enabled", False), False),
                ssh_host=str(proxmox.get("ssh_host", "")),
                ssh_port=_coerce_int(proxmox.get("ssh_port", 22), 22),
                ssh_username=str(proxmox.get("ssh_username", "root")),
                ssh_private_key=str(proxmox.get("ssh_private_key", "")),
                ssh_password=str(proxmox.get("ssh_password", "")),
            ),
            migration=MigrationConfig(
                dry_run=_coerce_bool(migration.get("dry_run", True), True),
                batch=_coerce_bool(migration.get("batch", False), False),
                allow_snapshots=_coerce_bool(migration.get("allow_snapshots", False), False),
                parallel_jobs=max(1, _coerce_int(migration.get("parallel_jobs", 1), 1)),
                retry_attempts=max(1, _coerce_int(migration.get("retry_attempts", 2), 2)),
                cleanup_source=_coerce_bool(migration.get("cleanup_source", False), False),
                guest_remediation=_coerce_bool(migration.get("guest_remediation", True), True),
                guest_rewrite_fstab=_coerce_bool(migration.get("guest_rewrite_fstab", True), True),
                guest_install_qemu_agent=_coerce_bool(migration.get("guest_install_qemu_agent", True), True),
                rollback_on_failure=_coerce_bool(migration.get("rollback_on_failure", True), True),
                backup_source_manifest=_coerce_bool(migration.get("backup_source_manifest", True), True),
                target_format_override=DiskFormat(override_value) if override_value else None,
            ),
            ssh=SshConfig(
                enabled=_coerce_bool(ssh.get("enabled", False), False),
                host=str(ssh.get("host", "")),
                port=_coerce_int(ssh.get("port", 22), 22),
                username=str(ssh.get("username", "root")),
                private_key=str(ssh.get("private_key", "")),
                password=str(ssh.get("password", "")),
            ),
        )

    def target_format(self, override: Optional[DiskFormat] = None) -> DiskFormat:
        return override or self.migration.target_format_override or self.proxmox.format

    def bridge_for_network(self, vmware_network_name: str) -> str:
        """Return the Proxmox bridge for a VMware network name.

        Lookup order:
        1. Exact match in proxmox.bridge_map
        2. proxmox.default_bridge
        """
        return self.proxmox.bridge_map.get(vmware_network_name, self.proxmox.default_bridge)

    def storage_for_datastore(self, vmware_datastore_name: str) -> str:
        """Return the Proxmox storage for a VMware datastore name.

        Lookup order:
        1. Exact match in proxmox.datastore_map
        2. proxmox.default_storage
        """
        return self.proxmox.datastore_map.get(vmware_datastore_name, self.proxmox.default_storage)

