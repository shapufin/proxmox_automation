"""
Driver purge automation for VMware-specific drivers
Blacklists vmxnet3, pvscsi, and other VMware-specific drivers in Linux guests
"""

import logging
import re
from typing import Dict, List, Optional, Set, Any
from dataclasses import dataclass
from enum import Enum


class DriverType(Enum):
    NETWORK = "network"
    STORAGE = "storage"
    BALLOON = "balloon"
    INPUT = "input"
    VIRTIO = "virtio"


@dataclass
class DriverInfo:
    name: str
    driver_type: DriverType
    vmware_specific: bool
    required_for_virtio: bool
    blacklist_priority: int  # Higher = should be blacklisted first
    description: str
    alternative_drivers: List[str] = None


class DriverPurgeManager:
    """Manages driver blacklisting for VMware to Proxmox migration"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.vmware_drivers = self._initialize_vmware_drivers()
        self.virtio_drivers = self._initialize_virtio_drivers()
    
    def _initialize_vmware_drivers(self) -> Dict[str, DriverInfo]:
        """Initialize list of VMware-specific drivers"""
        return {
            # Network drivers
            "vmxnet": DriverInfo(
                name="vmxnet",
                driver_type=DriverType.NETWORK,
                vmware_specific=True,
                required_for_virtio=False,
                blacklist_priority=1,
                description="VMware vmxnet legacy network driver",
                alternative_drivers=["virtio_net", "e1000"]
            ),
            "vmxnet3": DriverInfo(
                name="vmxnet3",
                driver_type=DriverType.NETWORK,
                vmware_specific=True,
                required_for_virtio=False,
                blacklist_priority=2,
                description="VMware vmxnet3 network driver",
                alternative_drivers=["virtio_net", "e1000e"]
            ),
            
            # Storage drivers
            "pvscsi": DriverInfo(
                name="pvscsi",
                driver_type=DriverType.STORAGE,
                vmware_specific=True,
                required_for_virtio=False,
                blacklist_priority=2,
                description="VMware paravirtual SCSI driver",
                alternative_drivers=["virtio_scsi", "lsilogic", "megasas"]
            ),
            "buslogic": DriverInfo(
                name="buslogic",
                driver_type=DriverType.STORAGE,
                vmware_specific=True,
                required_for_virtio=False,
                blacklist_priority=1,
                description="VMware BusLogic SCSI driver",
                alternative_drivers=["virtio_scsi", "lsilogic"]
            ),
            "lsilogic": DriverInfo(
                name="lsilogic",
                driver_type=DriverType.STORAGE,
                vmware_specific=False,  # Used by other virtualization platforms
                required_for_virtio=False,
                blacklist_priority=0,
                description="LSI Logic SCSI driver (not VMware-specific)",
                alternative_drivers=["virtio_scsi", "megasas"]
            ),
            
            # Memory balloon
            "vmballoon": DriverInfo(
                name="vmballoon",
                driver_type=DriverType.BALLOON,
                vmware_specific=True,
                required_for_virtio=False,
                blacklist_priority=2,
                description="VMware memory balloon driver",
                alternative_drivers=["virtio_balloon"]
            ),
            
            # Input drivers
            "vmw_vmci": DriverInfo(
                name="vmw_vmci",
                driver_type=DriverType.INPUT,
                vmware_specific=True,
                required_for_virtio=False,
                blacklist_priority=1,
                description="VMware VMCI communication interface",
                alternative_drivers=[]
            ),
            "vmw_balloon": DriverInfo(
                name="vmw_balloon",
                driver_type=DriverType.BALLOON,
                vmware_specific=True,
                required_for_virtio=False,
                blacklist_priority=1,
                description="VMware balloon driver (alternative)",
                alternative_drivers=["virtio_balloon"]
            ),
            "vmwgfx": DriverInfo(
                name="vmwgfx",
                driver_type=DriverType.INPUT,
                vmware_specific=True,
                required_for_virtio=False,
                blacklist_priority=1,
                description="VMware graphics driver",
                alternative_drivers=["virtio_gpu", "cirrus", "vga"]
            ),
            
            # VMware tools
            "vmtools": DriverInfo(
                name="vmtools",
                driver_type=DriverType.INPUT,
                vmware_specific=True,
                required_for_virtio=False,
                blacklist_priority=1,
                description="VMware tools driver",
                alternative_drivers=["qemu-guest-agent"]
            )
        }
    
    def _initialize_virtio_drivers(self) -> Dict[str, DriverInfo]:
        """Initialize list of VirtIO drivers (should NOT be blacklisted)"""
        return {
            "virtio_net": DriverInfo(
                name="virtio_net",
                driver_type=DriverType.NETWORK,
                vmware_specific=False,
                required_for_virtio=True,
                blacklist_priority=0,
                description="VirtIO network driver (required for Proxmox)",
                alternative_drivers=[]
            ),
            "virtio_scsi": DriverInfo(
                name="virtio_scsi",
                driver_type=DriverType.STORAGE,
                vmware_specific=False,
                required_for_virtio=True,
                blacklist_priority=0,
                description="VirtIO SCSI driver (required for Proxmox)",
                alternative_drivers=[]
            ),
            "virtio_balloon": DriverInfo(
                name="virtio_balloon",
                driver_type=DriverType.BALLOON,
                vmware_specific=False,
                required_for_virtio=True,
                blacklist_priority=0,
                description="VirtIO memory balloon driver",
                alternative_drivers=[]
            ),
            "virtio_blk": DriverInfo(
                name="virtio_blk",
                driver_type=DriverType.STORAGE,
                vmware_specific=False,
                required_for_virtio=True,
                blacklist_priority=0,
                description="VirtIO block driver",
                alternative_drivers=[]
            ),
            "virtio_console": DriverInfo(
                name="virtio_console",
                driver_type=DriverType.INPUT,
                vmware_specific=False,
                required_for_virtio=True,
                blacklist_priority=0,
                description="VirtIO console driver",
                alternative_drivers=[]
            ),
            "virtio_rng": DriverInfo(
                name="virtio_rng",
                driver_type=DriverType.INPUT,
                vmware_specific=False,
                required_for_virtio=True,
                blacklist_priority=0,
                description="VirtIO random number generator",
                alternative_drivers=[]
            )
        }
    
    def detect_loaded_drivers(self, lsmod_output: str) -> Set[str]:
        """Parse lsmod output to detect currently loaded drivers"""
        loaded_drivers = set()
        
        for line in lsmod_output.split('\n'):
            if line.strip() and not line.startswith('Module'):
                parts = line.split()
                if parts:
                    loaded_drivers.add(parts[0])
        
        return loaded_drivers
    
    def detect_available_drivers(self, modprobe_output: str) -> Set[str]:
        """Parse modprobe output to detect available drivers"""
        available_drivers = set()
        
        for line in modprobe_output.split('\n'):
            if line.strip():
                # modprobe -c shows configuration, but we want available modules
                # This is a simplified parser - in practice you'd check /lib/modules/
                driver_name = line.split()[0] if line.split() else ""
                if driver_name and not driver_name.startswith('#'):
                    available_drivers.add(driver_name)
        
        return available_drivers
    
    def analyze_driver_state(self, lsmod_output: str, lspci_output: str, 
                           lsscsi_output: str) -> Dict[str, Any]:
        """Analyze current driver state and identify VMware drivers"""
        loaded_drivers = self.detect_loaded_drivers(lsmod_output)
        
        # Identify VMware-specific drivers that are loaded
        loaded_vmware_drivers = []
        loaded_virtio_drivers = []
        
        for driver_name in loaded_drivers:
            if driver_name in self.vmware_drivers:
                loaded_vmware_drivers.append(self.vmware_drivers[driver_name])
            elif driver_name in self.virtio_drivers:
                loaded_virtio_drivers.append(self.virtio_drivers[driver_name])
        
        # Analyze hardware to determine required drivers
        hardware_analysis = self._analyze_hardware(lspci_output, lsscsi_output)
        
        # Determine which drivers should be blacklisted
        blacklist_candidates = []
        for driver in loaded_vmware_drivers:
            if driver.vmware_specific and not driver.required_for_virtio:
                blacklist_candidates.append(driver)
        
        # Sort by blacklist priority
        blacklist_candidates.sort(key=lambda d: d.blacklist_priority, reverse=True)
        
        return {
            "loaded_drivers": list(loaded_drivers),
            "loaded_vmware_drivers": [
                {
                    "name": d.name,
                    "type": d.driver_type.value,
                    "description": d.description,
                    "priority": d.blacklist_priority
                }
                for d in loaded_vmware_drivers
            ],
            "loaded_virtio_drivers": [
                {
                    "name": d.name,
                    "type": d.driver_type.value,
                    "description": d.description
                }
                for d in loaded_virtio_drivers
            ],
            "blacklist_candidates": [
                {
                    "name": d.name,
                    "type": d.driver_type.value,
                    "description": d.description,
                    "priority": d.blacklist_priority,
                    "alternatives": d.alternative_drivers
                }
                for d in blacklist_candidates
            ],
            "hardware_analysis": hardware_analysis
        }
    
    def _analyze_hardware(self, lspci_output: str, lsscsi_output: str) -> Dict[str, Any]:
        """Analyze hardware to determine what drivers are needed"""
        hardware_info = {
            "network_devices": [],
            "storage_controllers": [],
            "other_devices": []
        }
        
        # Parse PCI devices
        for line in lspci_output.split('\n'):
            if 'Network' in line or 'Ethernet' in line:
                hardware_info["network_devices"].append(line.strip())
            elif 'SCSI' in line or 'SATA' in line or 'RAID' in line:
                hardware_info["storage_controllers"].append(line.strip())
            else:
                hardware_info["other_devices"].append(line.strip())
        
        # Parse SCSI devices
        for line in lsscsi_output.split('\n'):
            if line.strip():
                hardware_info["storage_controllers"].append(line.strip())
        
        return hardware_info
    
    def generate_blacklist_rules(self, drivers_to_blacklist: List[str]) -> str:
        """Generate modprobe blacklist rules"""
        rules = [
            "# Driver blacklist for VMware to Proxmox migration",
            "# This file prevents VMware-specific drivers from loading",
            "# Generated by vmware-to-proxmox migration tool",
            ""
        ]
        
        for driver_name in drivers_to_blacklist:
            if driver_name in self.vmware_drivers:
                driver = self.vmware_drivers[driver_name]
                rules.append(f"# Blacklist {driver.description}")
                rules.append(f"blacklist {driver_name}")
                
                # Add alias rules to prevent loading by device ID
                rules.append(f"# Prevent loading by device alias")
                rules.append(f"alias {driver_name} off")
                rules.append("")
        
        return "\n".join(rules)
    
    def generate_whitelist_rules(self, drivers_to_whitelist: List[str]) -> str:
        """Generate modprobe whitelist rules for VirtIO drivers"""
        rules = [
            "# Driver whitelist for VirtIO drivers",
            "# Ensure VirtIO drivers are prioritized",
            "# Generated by vmware-to-proxmox migration tool",
            ""
        ]
        
        for driver_name in drivers_to_whitelist:
            if driver_name in self.virtio_drivers:
                driver = self.virtio_drivers[driver_name]
                rules.append(f"# Prioritize {driver.description}")
                rules.append(f"install {driver_name} /bin/true")
                rules.append("")
        
        return "\n".join(rules)
    
    def create_driver_purge_script(self, driver_analysis: Dict[str, Any]) -> str:
        """Create comprehensive driver purge script"""
        blacklist_candidates = [d["name"] for d in driver_analysis["blacklist_candidates"]]
        
        script_content = [
            "#!/bin/bash",
            "#",
            "# Driver Purge Script for VMware to Proxmox Migration",
            "# This script blacklists VMware-specific drivers and ensures VirtIO drivers are used",
            "#",
            "",
            "set -e",
            "",
            "LOG_FILE='/var/log/vmware-to-proxmox-driver-purge.log'",
            "BACKUP_DIR='/tmp/vmware-to-proxmox-driver-backup-$(date +%s)'",
            "",
            "log() {",
            "    echo \"[$(date '+%Y-%m-%d %H:%M:%S')] $1\" | tee -a \"$LOG_FILE\"",
            "}",
            "",
            "backup_file() {",
            "    local file=\"$1\"",
            "    if [ -f \"$file\" ]; then",
            "        mkdir -p \"$BACKUP_DIR$(dirname \"$file\")\"",
            "        cp \"$file\" \"$BACKUP_DIR$file\"",
            "        log \"Backed up $file\"",
            "    fi",
            "}",
            "",
            "log 'Starting driver purge for VMware to Proxmox migration'",
            "log \"Creating backup directory: $BACKUP_DIR\"",
            "mkdir -p \"$BACKUP_DIR\"",
            "",
            "# Backup existing modprobe configuration",
            "backup_file '/etc/modprobe.d/blacklist.conf'",
            "backup_file '/etc/modprobe.d/vmware.conf'",
            "backup_file '/etc/modules-load.d/vmware.conf'",
            "",
            "# Create VMware driver blacklist",
            "log 'Creating VMware driver blacklist'",
            "cat > /etc/modprobe.d/99-vmware-to-proxmox-blacklist.conf << 'EOF'",
            self.generate_blacklist_rules(blacklist_candidates),
            "EOF",
            "",
            "# Create VirtIO driver whitelist",
            "log 'Creating VirtIO driver whitelist'",
            "cat > /etc/modprobe.d/99-vmware-to-proxmox-whitelist.conf << 'EOF'",
            self.generate_whitelist_rules(["virtio_net", "virtio_scsi", "virtio_balloon", "virtio_blk"]),
            "EOF",
            "",
            "# Remove VMware drivers from modules-load configuration",
            "log 'Removing VMware drivers from auto-load configuration'",
            "for config_file in /etc/modules-load.d/*.conf; do",
            "    if [ -f \"$config_file\" ]; then",
            "        temp_file=\"$(mktemp)\"",
            "        while IFS= read -r line; do",
            "            # Skip VMware-specific drivers",
            "            case \"$line\" in",
            "                vmxnet*|pvscsi|vmballoon|vmw_*)",
            "                    log \"Skipping VMware driver: $line\"",
            "                    ;;",
            "                *)",
            "                    echo \"$line\" >> \"$temp_file\"",
            "                    ;;",
            "            esac",
            "        done < \"$config_file\"",
            "        mv \"$temp_file\" \"$config_file\"",
            "    fi",
            "done",
            "",
            "# Add VirtIO drivers to modules-load if not present",
            "log 'Ensuring VirtIO drivers are configured to load'",
            "cat >> /etc/modules-load.d/virtio.conf << 'EOF'",
            "# VirtIO drivers for Proxmox",
            "virtio_net",
            "virtio_scsi",
            "virtio_balloon",
            "virtio_blk",
            "EOF",
            "",
            "# Unload currently loaded VMware drivers",
            "log 'Unloading currently loaded VMware drivers'",
            "VMWARE_DRIVERS=\"vmxnet vmxnet3 pvscsi vmballoon vmw_vmci vmw_balloon\"",
            "",
            "for driver in $VMWARE_DRIVERS; do",
            "    if lsmod | grep -q \"^$driver \"; then",
            "        log \"Unloading driver: $driver\"",
            "        modprobe -r \"$driver\" || log \"Warning: Failed to unload $driver\"",
            "    else",
            "        log \"Driver $driver is not loaded\"",
            "    fi",
            "done",
            "",
            "# Update initramfs to include new driver configuration",
            "log 'Updating initramfs with new driver configuration'",
            "if command -v update-initramfs >/dev/null 2>&1; then",
            "    update-initramfs -u -k all",
            "elif command -v dracut >/dev/null 2>&1; then",
            "    dracut -f",
            "elif command -v mkinitrd >/dev/null 2>&1; then",
            "    mkinitrd -f",
            "else",
            "    log 'Warning: No initramfs update tool found'",
            "fi",
            "",
            "# Update GRUB configuration",
            "log 'Updating GRUB configuration'",
            "if command -v update-grub >/dev/null 2>&1; then",
            "    update-grub",
            "elif command -v grub2-mkconfig >/dev/null 2>&1; then",
            "    grub2-mkconfig -o /boot/grub2/grub.cfg",
            "else",
            "    log 'Warning: No GRUB update tool found'",
            "fi",
            "",
            "# Display summary",
            "log 'Driver purge completed'",
            "log 'Blacklisted VMware drivers:'"
        ]
        
        for driver_name in blacklist_candidates:
            script_content.append(f"log '  - {driver_name}'")
        
        script_content.extend([
            "log 'Backup files stored in: $BACKUP_DIR'",
            "log 'Log file: $LOG_FILE'",
            "",
            "echo",
            "echo '=== Driver Purge Summary ==='",
            "echo 'Blacklisted VMware drivers:'"
        ])
        
        for driver_name in blacklist_candidates:
            script_content.append(f"echo '  - {driver_name}'")
        
        script_content.extend([
            "echo",
            "echo 'Next steps:'",
            "echo '1. Reboot the system to apply all changes'",
            "echo '2. Verify that VirtIO drivers are loaded (lsmod | grep virtio)'",
            "echo '3. Test network and storage connectivity'",
            "echo '4. Remove VMware tools if installed'",
            "echo",
            "echo 'Backup location:'",
            "echo \"  $BACKUP_DIR\"",
            "echo",
            "echo 'Log file:'",
            "echo \"  $LOG_FILE\"",
            ""
        ])
        
        return "\n".join(script_content)
    
    def create_vmware_tools_removal_script(self) -> str:
        """Create script to remove VMware tools"""
        script_content = [
            "#!/bin/bash",
            "#",
            "# VMware Tools Removal Script",
            "# Removes VMware tools and replaces with qemu-guest-agent",
            "#",
            "",
            "set -e",
            "",
            "LOG_FILE='/var/log/vmware-tools-removal.log'",
            "",
            "log() {",
            "    echo \"[$(date '+%Y-%m-%d %H:%M:%S')] $1\" | tee -a \"$LOG_FILE\"",
            "}",
            "",
            "log 'Starting VMware tools removal'",
            "",
            "# Stop VMware tools services",
            "log 'Stopping VMware tools services'",
            "systemctl stop vmware-tools || true",
            "systemctl disable vmware-tools || true",
            "service vmware-tools stop || true",
            "",
            "# Remove VMware tools packages",
            "log 'Removing VMware tools packages'",
            "",
            "# Debian/Ubuntu",
            "if command -v apt-get >/dev/null 2>&1; then",
            "    apt-get remove --purge -y open-vm-tools || true",
            "    apt-get remove --purge -y vmware-tools || true",
            "    apt-get autoremove -y || true",
            "# RHEL/CentOS",
            "elif command -v yum >/dev/null 2>&1; then",
            "    yum remove -y open-vm-tools || true",
            "    yum remove -y vmware-tools || true",
            "    yum autoremove -y || true",
            "# SLES",
            "elif command -v zypper >/dev/null 2>&1; then",
            "    zypper remove -y open-vm-tools || true",
            "    zypper remove -y vmware-tools || true",
            "fi",
            "",
            "# Install qemu-guest-agent",
            "log 'Installing qemu-guest-agent'",
            "",
            "if command -v apt-get >/dev/null 2>&1; then",
            "    apt-get update",
            "    apt-get install -y qemu-guest-agent",
            "    systemctl enable qemu-guest-agent",
            "    systemctl start qemu-guest-agent",
            "elif command -v yum >/dev/null 2>&1; then",
            "    yum install -y qemu-guest-agent",
            "    systemctl enable qemu-guest-agent",
            "    systemctl start qemu-guest-agent",
            "elif command -v zypper >/dev/null 2>&1; then",
            "    zypper install -y qemu-guest-agent",
            "    systemctl enable qemu-guest-agent",
            "    systemctl start qemu-guest-agent",
            "fi",
            "",
            "# Remove VMware tools remnants",
            "log 'Removing VMware tools remnants'",
            "rm -rf /etc/vmware-tools || true",
            "rm -rf /usr/lib/vmware-tools || true",
            "rm -rf /var/lib/vmware-tools || true",
            "",
            "log 'VMware tools removal completed'",
            "log 'qemu-guest-agent installed and started'",
            "",
            "echo 'VMware tools has been removed and replaced with qemu-guest-agent'",
            "echo 'Please reboot the system to complete the transition'",
            ""
        ]
        
        return "\n".join(script_content)
    
    def validate_driver_configuration(self, driver_analysis: Dict[str, Any]) -> Dict[str, Any]:
        """Validate driver configuration and identify potential issues"""
        issues = []
        warnings = []
        recommendations = []
        
        loaded_vmware_drivers = driver_analysis["loaded_vmware_drivers"]
        loaded_virtio_drivers = driver_analysis["loaded_virtio_drivers"]
        blacklist_candidates = driver_analysis["blacklist_candidates"]
        
        # Check for critical VMware drivers
        critical_vmware_drivers = [d for d in loaded_vmware_drivers if d["priority"] >= 2]
        if critical_vmware_drivers:
            warnings.append(f"Critical VMware drivers detected: {[d['name'] for d in critical_vmware_drivers]}")
        
        # Check if VirtIO drivers are available
        if not loaded_virtio_drivers:
            issues.append("No VirtIO drivers detected - migration may fail")
        
        # Check for network driver conflicts
        network_drivers = [d for d in loaded_vmware_drivers if d["type"] == "network"]
        if network_drivers:
            warnings.append(f"VMware network drivers detected: {[d['name'] for d in network_drivers]}")
        
        # Check for storage driver conflicts
        storage_drivers = [d for d in loaded_vmware_drivers if d["type"] == "storage"]
        if storage_drivers:
            warnings.append(f"VMware storage drivers detected: {[d['name'] for d in storage_drivers]}")
        
        # Generate recommendations
        if blacklist_candidates:
            recommendations.append(f"Blacklist {len(blacklist_candidates)} VMware-specific drivers")
        
        if not loaded_virtio_drivers:
            recommendations.append("Install VirtIO drivers before migration")
        
        recommendations.append("Test driver purge script in a non-production environment")
        recommendations.append("Ensure you have console access to the VM after driver changes")
        
        return {
            "issues": issues,
            "warnings": warnings,
            "recommendations": recommendations,
            "vmware_drivers_count": len(loaded_vmware_drivers),
            "virtio_drivers_count": len(loaded_virtio_drivers),
            "blacklist_candidates_count": len(blacklist_candidates)
        }
