from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .models import DiskFormat, FirmwareMode


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
    format: DiskFormat = DiskFormat.QCOW2
    boot_firmware: FirmwareMode = FirmwareMode.AUTO
    scsi_controller: str = "virtio-scsi-single"
    create_efi_disk: bool = True
    preserve_mac: bool = True
    api_user: str = "root@pam"
    api_token_name: str = ""
    api_token_value: str = ""
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
                host=vmware["host"],
                username=vmware["username"],
                password=vmware["password"],
                port=int(vmware.get("port", 443)),
                datacenter=str(vmware.get("datacenter", "")),
                allow_insecure_ssl=bool(vmware.get("allow_insecure_ssl", True)),
                ssh_enabled=bool(vmware.get("ssh_enabled", True)),
                ssh_port=int(vmware.get("ssh_port", 22)),
            ),
            proxmox=ProxmoxConfig(
                node=proxmox["node"],
                default_storage=proxmox["default_storage"],
                default_bridge=proxmox["default_bridge"],
                bridge_map=dict(proxmox.get("bridge_map", {})),
                format=DiskFormat(format_value),
                boot_firmware=FirmwareMode(boot_value),
                scsi_controller=str(proxmox.get("scsi_controller", "virtio-scsi-single")),
                create_efi_disk=bool(proxmox.get("create_efi_disk", True)),
                preserve_mac=bool(proxmox.get("preserve_mac", True)),
                api_user=str(proxmox.get("api_user", "root@pam")),
                api_token_name=str(proxmox.get("api_token_name", "")),
                api_token_value=str(proxmox.get("api_token_value", "")),
                ssh_enabled=bool(proxmox.get("ssh_enabled", False)),
                ssh_host=str(proxmox.get("ssh_host", "")),
                ssh_port=int(proxmox.get("ssh_port", 22)),
                ssh_username=str(proxmox.get("ssh_username", "root")),
                ssh_private_key=str(proxmox.get("ssh_private_key", "")),
                ssh_password=str(proxmox.get("ssh_password", "")),
            ),
            migration=MigrationConfig(
                dry_run=bool(migration.get("dry_run", True)),
                batch=bool(migration.get("batch", False)),
                allow_snapshots=bool(migration.get("allow_snapshots", False)),
                parallel_jobs=max(1, int(migration.get("parallel_jobs", 1))),
                retry_attempts=max(1, int(migration.get("retry_attempts", 2))),
                cleanup_source=bool(migration.get("cleanup_source", False)),
                guest_remediation=bool(migration.get("guest_remediation", True)),
                guest_rewrite_fstab=bool(migration.get("guest_rewrite_fstab", True)),
                guest_install_qemu_agent=bool(migration.get("guest_install_qemu_agent", True)),
                rollback_on_failure=bool(migration.get("rollback_on_failure", True)),
                backup_source_manifest=bool(migration.get("backup_source_manifest", True)),
                target_format_override=DiskFormat(override_value) if override_value else None,
            ),
            ssh=SshConfig(
                enabled=bool(ssh.get("enabled", False)),
                host=str(ssh.get("host", "")),
                port=int(ssh.get("port", 22)),
                username=str(ssh.get("username", "root")),
                private_key=str(ssh.get("private_key", "")),
                password=str(ssh.get("password", "")),
            ),
        )

    def target_format(self, override: Optional[DiskFormat] = None) -> DiskFormat:
        return override or self.migration.target_format_override or self.proxmox.format

    def bridge_for_network(self, network_name: str) -> str:
        return self.proxmox.bridge_map.get(network_name, self.proxmox.default_bridge)
