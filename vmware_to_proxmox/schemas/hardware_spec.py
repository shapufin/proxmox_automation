"""
HardwareSpec JSON Schema for VMware-to-Proxmox migration
Ensures hardware fidelity and preserves critical hardware metadata
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any
import json
import jsonschema


class CpuTopology(str, Enum):
    SOCKETS = "sockets"
    CORES = "cores"
    THREADS = "threads"


class PassthroughType(str, Enum):
    PCI = "pci"
    USB = "usb"
    ISO = "iso"


@dataclass(slots=True)
class CpuSpec:
    sockets: int
    cores_per_socket: int
    threads_per_core: int
    topology: CpuTopology
    features: List[str] = field(default_factory=list)
    numa_nodes: Optional[List[Dict[str, Any]]] = None
    
    @property
    def total_vcpus(self) -> int:
        return self.sockets * self.cores_per_socket * self.threads_per_core


@dataclass(slots=True)
class MacAddressSpec:
    original: str
    preserved: bool = True
    generated: Optional[str] = None  # Fallback if preservation fails


@dataclass(slots=True)
class NetworkInterfaceSpec:
    label: str
    mac_address: MacAddressSpec
    network_name: str
    adapter_type: str  # e.g., vmxnet3, e1000
    vlan_id: Optional[int] = None
    connected: bool = True


@dataclass(slots=True)
class PassthroughDevice:
    device_type: PassthroughType
    vendor_id: str
    device_id: str
    subsystem_vendor_id: Optional[str] = None
    subsystem_device_id: Optional[str] = None
    address: str  # PCI address or USB bus:device
    description: str = ""
    required: bool = False  # Critical for VM operation


@dataclass(slots=True)
class DiskControllerSpec:
    controller_type: str  # lsilogic, buslogic, pvscsi, etc.
    bus_number: int
    scsi_controller: Optional[int] = None
    unit_number: Optional[int] = None


@dataclass(slots=True)
class DiskSpec:
    label: str
    capacity_bytes: int
    backing_type: str
    thin_provisioned: bool
    controller: DiskControllerSpec
    file_name: str
    hardware_version: str = ""


@dataclass(slots=True)
class FirmwareSpec:
    firmware_type: str  # bios, uefi
    secure_boot: bool = False
    tpm_present: bool = False
    efi_vars: Optional[Dict[str, Any]] = None


@dataclass(slots=True)
class HardwareSpec:
    """Comprehensive hardware specification for migration fidelity"""
    vm_name: str
    vmware_hardware_version: str
    cpu: CpuSpec
    memory_mb: int
    firmware: FirmwareSpec
    network_interfaces: List[NetworkInterfaceSpec]
    disks: List[DiskSpec]
    passthrough_devices: List[PassthroughDevice] = field(default_factory=list)
    snapshot_count: int = 0
    has_encryption: bool = False
    
    def to_json(self) -> str:
        return json.dumps(self.__dict__, default=str, indent=2)
    
    @classmethod
    def from_json(cls, json_str: str) -> 'HardwareSpec':
        data = json.loads(json_str)
        # Reconstruct nested objects
        data['cpu'] = CpuSpec(**data['cpu'])
        data['firmware'] = FirmwareSpec(**data['firmware'])
        data['network_interfaces'] = [
            NetworkInterfaceSpec(**ni) for ni in data['network_interfaces']
        ]
        data['disks'] = [
            DiskSpec(**disk) for disk in data['disks']
        ]
        data['passthrough_devices'] = [
            PassthroughDevice(**dev) for dev in data['passthrough_devices']
        ]
        for ni in data['network_interfaces']:
            ni['mac_address'] = MacAddressSpec(**ni['mac_address'])
        for disk in data['disks']:
            disk['controller'] = DiskControllerSpec(**disk['controller'])
        return cls(**data)
    
    def validate(self) -> List[str]:
        """Validate hardware spec against migration constraints"""
        errors = []
        
        # Check for unsupported features
        if self.has_encryption:
            errors.append("Encrypted VMs are not supported for migration")
        
        if self.snapshot_count > 0:
            errors.append(f"VM has {self.snapshot_count} snapshots - consolidation required")
        
        # Check for critical passthrough devices
        critical_devices = [d for d in self.passthrough_devices if d.required]
        if critical_devices:
            errors.append(f"Critical passthrough devices found: {[d.description for d in critical_devices]}")
        
        # Validate MAC addresses
        for ni in self.network_interfaces:
            if not ni.mac_address.original:
                errors.append(f"Network interface {ni.label} missing MAC address")
        
        return errors


# JSON Schema for validation
HARDWARE_SPEC_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "properties": {
        "vm_name": {"type": "string"},
        "vmware_hardware_version": {"type": "string"},
        "cpu": {
            "type": "object",
            "properties": {
                "sockets": {"type": "integer", "minimum": 1},
                "cores_per_socket": {"type": "integer", "minimum": 1},
                "threads_per_core": {"type": "integer", "minimum": 1},
                "topology": {"enum": ["sockets", "cores", "threads"]},
                "features": {"type": "array", "items": {"type": "string"}},
                "numa_nodes": {"type": "array"}
            },
            "required": ["sockets", "cores_per_socket", "threads_per_core", "topology"]
        },
        "memory_mb": {"type": "integer", "minimum": 1},
        "firmware": {
            "type": "object",
            "properties": {
                "firmware_type": {"enum": ["bios", "uefi"]},
                "secure_boot": {"type": "boolean"},
                "tpm_present": {"type": "boolean"}
            },
            "required": ["firmware_type"]
        },
        "network_interfaces": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "mac_address": {
                        "type": "object",
                        "properties": {
                            "original": {"type": "string", "pattern": "^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$"},
                            "preserved": {"type": "boolean"}
                        },
                        "required": ["original", "preserved"]
                    },
                    "network_name": {"type": "string"},
                    "adapter_type": {"type": "string"},
                    "vlan_id": {"type": "integer"},
                    "connected": {"type": "boolean"}
                },
                "required": ["label", "mac_address", "network_name", "adapter_type"]
            }
        },
        "disks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "capacity_bytes": {"type": "integer", "minimum": 1},
                    "backing_type": {"type": "string"},
                    "thin_provisioned": {"type": "boolean"},
                    "controller": {
                        "type": "object",
                        "properties": {
                            "controller_type": {"type": "string"},
                            "bus_number": {"type": "integer"},
                            "scsi_controller": {"type": "integer"},
                            "unit_number": {"type": "integer"}
                        },
                        "required": ["controller_type", "bus_number"]
                    },
                    "file_name": {"type": "string"}
                },
                "required": ["label", "capacity_bytes", "backing_type", "thin_provisioned", "controller", "file_name"]
            }
        }
    },
    "required": ["vm_name", "vmware_hardware_version", "cpu", "memory_mb", "firmware", "network_interfaces", "disks"]
}


def validate_hardware_spec_json(json_str: str) -> List[str]:
    """Validate JSON against HardwareSpec schema"""
    try:
        data = json.loads(json_str)
        jsonschema.validate(data, HARDWARE_SPEC_SCHEMA)
        return []
    except json.JSONDecodeError as e:
        return [f"Invalid JSON: {e}"]
    except jsonschema.ValidationError as e:
        return [f"Schema validation failed: {e.message}"]
