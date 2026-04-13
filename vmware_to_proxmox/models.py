from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class DiskFormat(str, Enum):
    QCOW2 = "qcow2"
    RAW = "raw"


class FirmwareMode(str, Enum):
    AUTO = "auto"
    BIOS = "bios"
    UEFI = "uefi"


class SourceMode(str, Enum):
    VMWARE = "vmware"
    LOCAL = "local"


@dataclass(slots=True)
class VmwareDiskSpec:
    label: str
    file_name: str
    capacity_bytes: int
    backing_type: str = ""
    controller_key: Optional[int] = None
    unit_number: Optional[int] = None
    thin_provisioned: bool = False
    datastore: str = ""  # Per-disk datastore name from VMware
    backing_mode: str = ""  # "persistent", "independent_persistent", "independent_nonpersistent", "rdm"
    device_type: str = ""  # "scsi-hardDisk", "ide-hardDisk", "sata-hardDisk", "nvme"
    lun_id: Optional[int] = None  # LUN identifier for RDM devices
    is_rdm: bool = False  # True if this is a Raw Device Mapping


@dataclass(slots=True)
class VmwareNicSpec:
    label: str
    network_name: str
    mac_address: str
    adapter_type: str
    vlan_id: Optional[int] = None
    virtual_dev: str = ""


@dataclass(slots=True)
class VmwareVmSpec:
    name: str
    moid: str
    guest_id: str
    power_state: str
    firmware: str
    memory_mb: int
    cpu_count: int
    hardware_version: str = ""
    annotation: str = ""
    datastore: str = ""
    disks: list[VmwareDiskSpec] = field(default_factory=list)
    nics: list[VmwareNicSpec] = field(default_factory=list)
    has_snapshots: bool = False
    snapshot_count: int = 0
    has_vtpm: bool = False
    has_pci_passthrough: bool = False
    cpu_hotplug_enabled: bool = False
    memory_hotplug_enabled: bool = False
    scsi_controller_type: str = ""
    guest_os_full_name: str = ""

    @property
    def is_linux(self) -> bool:
        guest = (self.guest_id or "").lower()
        return not guest.startswith("windows") and not guest.startswith("microsoft")


@dataclass(slots=True)
class ProxmoxStorageSpec:
    storage: str
    content: str = ""
    storage_type: str = ""
    total: int = 0
    used: int = 0
    available: int = 0
    shared: bool = False
    active: bool = True

    @property
    def free(self) -> int:
        return max(self.available, 0)


@dataclass(slots=True)
class ProxmoxBridgeSpec:
    name: str
    active: bool = True
    vlan_aware: bool = False
    bridge_ports: str = ""
    comments: str = ""


@dataclass(slots=True)
class MigrationTarget:
    node: str
    storage: str
    bridge: str
    disk_format: DiskFormat
    firmware: FirmwareMode
    vmid: Optional[int] = None


@dataclass(slots=True)
class MigrationResult:
    name: str
    vmid: int
    target_storage: str
    disk_format: DiskFormat
    firmware: FirmwareMode
    warnings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
