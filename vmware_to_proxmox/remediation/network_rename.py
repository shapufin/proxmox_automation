"""
Network interface renaming and udev rule management for migrated VMs
Ensures consistent network interface naming across VMware to Proxmox migration
"""

import logging
import re
import json
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum
import tempfile
import os


class InterfaceType(Enum):
    ETHERNET = "ethernet"
    VLAN = "vlan"
    BRIDGE = "bridge"
    BOND = "bond"


@dataclass
class NetworkInterface:
    name: str
    mac_address: str
    driver: str
    interface_type: InterfaceType
    original_name: str
    target_name: str
    is_primary: bool = False
    vlan_id: Optional[int] = None
    bridge_name: Optional[str] = None


@dataclass
class UdevRule:
    rule_id: str
    mac_address: str
    interface_name: str
    driver: Optional[str] = None
    comment: str = ""


class NetworkRemediationManager:
    """Manages network interface renaming for migrated VMs"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
    
    def analyze_vmware_network_config(self, vmx_content: str) -> List[NetworkInterface]:
        """Analyze VMX file to extract network interface configuration"""
        interfaces = []
        
        # Parse VMX network entries
        network_pattern = r'ethernet(\d+)\.(.+?)\s*=\s*"?(.+?)"?\s*$'
        
        for match in re.finditer(network_pattern, vmx_content, re.MULTILINE):
            interface_num = int(match.group(1))
            property_name = match.group(2).strip()
            property_value = match.group(3).strip().strip('"')
            
            # Find or create interface
            interface = next((i for i in interfaces if i.original_name == f"ethernet{interface_num}"), None)
            if not interface:
                interface = NetworkInterface(
                    name=f"ethernet{interface_num}",
                    mac_address="",
                    driver="",
                    interface_type=InterfaceType.ETHERNET,
                    original_name=f"ethernet{interface_num}",
                    target_name=f"eth{interface_num}",  # Default target
                    is_primary=(interface_num == 0)
                )
                interfaces.append(interface)
            
            # Set properties
            if property_name == "present" and property_value == "TRUE":
                pass  # Interface is present
            elif property_name == "addressType" and property_value == "generated":
                pass  # MAC is generated
            elif property_name == "address":
                interface.mac_address = property_value.lower()
            elif property_name == "virtualDev":
                interface.driver = property_value.lower()  # e.g., vmxnet3, e1000
        
        return interfaces
    
    def analyze_current_network_config(self, ifconfig_output: str, ip_output: str) -> List[NetworkInterface]:
        """Analyze current network configuration in the guest"""
        interfaces = []
        
        # Parse ip link output for interface information
        ip_pattern = r'^(\d+):\s+(\w+):.*?link/ether\s+([0-9a-f:]+).*?state\s+(\w+)'
        
        for match in re.finditer(ip_pattern, ip_output, re.MULTILINE | re.DOTALL):
            interface_num = match.group(1)
            interface_name = match.group(2)
            mac_address = match.group(3).lower()
            state = match.group(4)
            
            # Determine interface type
            interface_type = InterfaceType.ETHERNET
            if interface_name.startswith("eth"):
                interface_type = InterfaceType.ETHERNET
            elif interface_name.startswith("ens"):
                interface_type = InterfaceType.ETHERNET
            elif interface_name.startswith("enp"):
                interface_type = InterfaceType.ETHERNET
            elif interface_name.startswith("vlan"):
                interface_type = InterfaceType.VLAN
            elif interface_name.startswith("br"):
                interface_type = InterfaceType.BRIDGE
            elif interface_name.startswith("bond"):
                interface_type = InterfaceType.BOND
            
            interface = NetworkInterface(
                name=interface_name,
                mac_address=mac_address,
                driver="",  # Will be determined separately
                interface_type=interface_type,
                original_name=interface_name,
                target_name=interface_name,  # Will be updated based on mapping
                is_primary=(interface_num == "1" and state == "UP")
            )
            
            interfaces.append(interface)
        
        # Try to determine drivers from ethtool or /sys
        # This would need to be run on the guest
        
        return interfaces
    
    def create_interface_mapping(self, vmware_interfaces: List[NetworkInterface], 
                               current_interfaces: List[NetworkInterface]) -> Dict[str, str]:
        """Create mapping between VMware interfaces and current interfaces based on MAC"""
        mapping = {}
        
        for vmware_iface in vmware_interfaces:
            if not vmware_iface.mac_address:
                continue
            
            # Find matching interface by MAC address
            matching_current = next(
                (iface for iface in current_interfaces if iface.mac_address == vmware_iface.mac_address),
                None
            )
            
            if matching_current:
                mapping[matching_current.name] = vmware_iface.target_name
                self.logger.info(f"Mapped {matching_current.name} -> {vmware_iface.target_name} (MAC: {vmware_iface.mac_address})")
            else:
                self.logger.warning(f"No matching interface found for MAC {vmware_iface.mac_address}")
        
        return mapping
    
    def generate_udev_rules(self, interface_mapping: Dict[str, str], 
                           interfaces: List[NetworkInterface]) -> List[UdevRule]:
        """Generate udev rules for persistent interface naming"""
        rules = []
        
        for current_name, target_name in interface_mapping.items():
            # Find the interface with this current name
            interface = next((i for i in interfaces if i.name == current_name), None)
            if not interface or not interface.mac_address:
                continue
            
            rule = UdevRule(
                rule_id=f"70-persistent-net-{target_name}",
                mac_address=interface.mac_address,
                interface_name=target_name,
                driver=interface.driver if interface.driver else None,
                comment=f"VMware to Proxmox migration: {current_name} -> {target_name}"
            )
            
            rules.append(rule)
        
        return rules
    
    def generate_udev_rules_file(self, rules: List[UdevRule]) -> str:
        """Generate udev rules file content"""
        content = [
            "# Network interface rules for VMware to Proxmox migration",
            "# Generated by vmware-to-proxmox migration tool",
            ""
        ]
        
        for rule in rules:
            content.append(f"# {rule.comment}")
            
            # Build udev rule
            rule_parts = [
                'ACTION=="add"',
                'SUBSYSTEM=="net"',
                f'ATTR{{address}}=="{rule.mac_address}"'
            ]
            
            if rule.driver:
                rule_parts.append(f'DRIVER=="{rule.driver}"')
            
            rule_parts.append(f'NAME="{rule.interface_name}"')
            
            content.append(' '.join(rule_parts))
            content.append("")
        
        return "\n".join(content)
    
    def generate_network_scripts(self, interface_mapping: Dict[str, str], 
                               network_config: Dict[str, Any]) -> Dict[str, str]:
        """Generate network configuration scripts for different distributions"""
        scripts = {}
        
        # Determine distribution type
        distro = network_config.get("distribution", "").lower()
        
        if distro in ["ubuntu", "debian"]:
            scripts.update(self._generate_debian_network_config(interface_mapping, network_config))
        elif distro in ["centos", "rhel", "fedora", "rocky", "almalinux"]:
            scripts.update(self._generate_rhel_network_config(interface_mapping, network_config))
        elif distro in ["sles", "opensuse"]:
            scripts.update(self._generate_suse_network_config(interface_mapping, network_config))
        else:
            # Generic fallback
            scripts.update(self._generate_generic_network_config(interface_mapping, network_config))
        
        return scripts
    
    def _generate_debian_network_config(self, interface_mapping: Dict[str, str], 
                                      network_config: Dict[str, Any]) -> Dict[str, str]:
        """Generate Debian/Ubuntu network configuration"""
        interfaces_content = [
            "# This file describes the network interfaces available on your system",
            "# and how to activate them. For more information, see interfaces(5).",
            "",
            "source /etc/network/interfaces.d/*",
            "",
            "auto lo",
            "iface lo inet loopback",
            ""
        ]
        
        # Map old interface names to new configurations
        for old_name, new_name in interface_mapping.items():
            old_config = network_config.get("interfaces", {}).get(old_name, {})
            
            if old_config.get("method") == "dhcp":
                interfaces_content.extend([
                    f"auto {new_name}",
                    f"iface {new_name} inet dhcp",
                    ""
                ])
            elif old_config.get("method") == "static":
                interfaces_content.extend([
                    f"auto {new_name}",
                    f"iface {new_name} inet static",
                    f"    address {old_config.get('address', '')}",
                    f"    netmask {old_config.get('netmask', '')}",
                ])
                
                if old_config.get("gateway"):
                    interfaces_content.append(f"    gateway {old_config.get('gateway')}")
                
                if old_config.get("dns_servers"):
                    dns_servers = " ".join(old_config.get("dns_servers", []))
                    interfaces_content.append(f"    dns-nameservers {dns_servers}")
                
                interfaces_content.append("")
        
        return {
            "/etc/network/interfaces": "\n".join(interfaces_content)
        }
    
    def _generate_rhel_network_config(self, interface_mapping: Dict[str, str], 
                                    network_config: Dict[str, Any]) -> Dict[str, str]:
        """Generate RHEL/CentOS network configuration"""
        scripts = {}
        
        for old_name, new_name in interface_mapping.items():
            old_config = network_config.get("interfaces", {}).get(old_name, {})
            
            # Generate ifcfg file
            ifcfg_content = [
                f"# Network configuration for {new_name}",
                f"DEVICE={new_name}",
                f"NAME={new_name}",
                "ONBOOT=yes"
            ]
            
            if old_config.get("method") == "dhcp":
                ifcfg_content.extend([
                    "BOOTPROTO=dhcp",
                    "DEFROUTE=yes"
                ])
            elif old_config.get("method") == "static":
                ifcfg_content.extend([
                    "BOOTPROTO=static",
                    f"IPADDR={old_config.get('address', '')}",
                    f"NETMASK={old_config.get('netmask', '')}"
                ])
                
                if old_config.get("gateway"):
                    ifcfg_content.append(f"GATEWAY={old_config.get('gateway')}")
            
            if old_config.get("mtu"):
                ifcfg_content.append(f"MTU={old_config.get('mtu')}")
            
            scripts[f"/etc/sysconfig/network-scripts/ifcfg-{new_name}"] = "\n".join(ifcfg_content)
        
        return scripts
    
    def _generate_suse_network_config(self, interface_mapping: Dict[str, str], 
                                    network_config: Dict[str, Any]) -> Dict[str, str]:
        """Generate SLES/openSUSE network configuration"""
        scripts = {}
        
        for old_name, new_name in interface_mapping.items():
            old_config = network_config.get("interfaces", {}).get(old_name, {})
            
            ifcfg_content = [
                f"# Network configuration for {new_name}",
                f"BOOTPROTO='{old_config.get('method', 'dhcp')}'",
                f"STARTMODE='auto'"
            ]
            
            if old_config.get("method") == "static":
                ifcfg_content.extend([
                    f"IPADDR='{old_config.get('address', '')}'",
                    f"NETMASK='{old_config.get('netmask', '')}'"
                ])
                
                if old_config.get("gateway"):
                    ifcfg_content.append(f"DEFAULTROUTE='{old_config.get('gateway')}'")
            
            scripts[f"/etc/sysconfig/network/ifcfg-{new_name}"] = "\n".join(ifcfg_content)
        
        return scripts
    
    def _generate_generic_network_config(self, interface_mapping: Dict[str, str], 
                                       network_config: Dict[str, Any]) -> Dict[str, str]:
        """Generate generic network configuration fallback"""
        # This would be a basic script that can be adapted
        script_content = [
            "#!/bin/bash",
            "# Generic network configuration script",
            "",
            "# Interface renaming would be handled by udev rules",
            "# This script ensures interfaces are brought up correctly",
            ""
        ]
        
        for old_name, new_name in interface_mapping.items():
            script_content.extend([
                f"# Bring up {new_name}",
                f"ip link set {new_name} up",
                ""
            ])
            
            old_config = network_config.get("interfaces", {}).get(old_name, {})
            if old_config.get("method") == "static":
                script_content.extend([
                    f"ip addr add {old_config.get('address', '')}/{self._cidr_from_netmask(old_config.get('netmask', '255.255.255.0'))} dev {new_name}",
                    ""
                ])
        
        return {
            "/usr/local/bin/configure-network.sh": "\n".join(script_content)
        }
    
    def _cidr_from_netmask(self, netmask: str) -> int:
        """Convert netmask to CIDR notation"""
        # Simple conversion - could be improved
        netmask_parts = netmask.split('.')
        binary_str = ''.join(f'{int(part):08b}' for part in netmask_parts)
        return binary_str.count('1')
    
    def create_remediation_script(self, vmware_interfaces: List[NetworkInterface],
                                current_interfaces: List[NetworkInterface],
                                network_config: Dict[str, Any]) -> str:
        """Create complete network remediation script"""
        # Create interface mapping
        interface_mapping = self.create_interface_mapping(vmware_interfaces, current_interfaces)
        
        # Generate udev rules
        udev_rules = self.generate_udev_rules(interface_mapping, current_interfaces)
        udev_rules_content = self.generate_udev_rules_file(udev_rules)
        
        # Generate network configuration
        network_scripts = self.generate_network_scripts(interface_mapping, network_config)
        
        # Build complete script
        script_content = [
            "#!/bin/bash",
            "#",
            "# Network Interface Remediation Script",
            "# VMware to Proxmox Migration Tool",
            "#",
            "# This script ensures consistent network interface naming",
            "# after migration from VMware to Proxmox.",
            "",
            "set -e",
            "",
            "LOG_FILE='/var/log/vmware-to-proxmox-network-remediation.log'",
            "BACKUP_DIR='/tmp/vmware-to-proxmox-backup-$(date +%s)'",
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
            "log 'Starting network interface remediation'",
            "log \"Creating backup directory: $BACKUP_DIR\"",
            "mkdir -p \"$BACKUP_DIR\"",
            "",
            "# Backup existing network configuration",
            "backup_file '/etc/udev/rules.d/70-persistent-net.rules'",
            "backup_file '/etc/network/interfaces'",
            "backup_file '/etc/sysconfig/network'",
            "",
            "# Create udev rules",
            "log 'Creating udev rules for interface naming'",
            "cat > /etc/udev/rules.d/70-persistent-net.rules << 'EOF'",
            udev_rules_content,
            "EOF",
            "",
            "# Generate network configuration files",
            ""
        ]
        
        # Add network configuration files
        for file_path, content in network_scripts.items():
            script_content.extend([
                f"log 'Creating {file_path}'",
                f"backup_file '{file_path}'",
                f"mkdir -p '$(dirname \"{file_path}\")'",
                f"cat > {file_path} << 'EOF'",
                content,
                "EOF",
                ""
            ])
        
        # Add finalization steps
        script_content.extend([
            "# Reload udev rules",
            "log 'Reloading udev rules'",
            "udevadm control --reload-rules",
            "udevadm trigger --subsystem-match=net",
            "",
            "# Wait for udev to process rules",
            "sleep 2",
            "",
            "# Restart networking (method depends on distribution)",
            "if command -v systemctl >/dev/null 2>&1; then",
            "    log 'Restarting network service via systemctl'",
            "    systemctl restart networking || systemctl restart network || true",
            "elif command -v service >/dev/null 2>&1; then",
            "    log 'Restarting network service via service command'",
            "    service networking restart || service network restart || true",
            "else",
            "    log 'No network service manager found, manual restart may be required'",
            "fi",
            "",
            "# Verify interface names",
            "log 'Verifying interface names'",
            "ip link show | grep -E '^[0-9]+:' | tee -a \"$LOG_FILE\"",
            "",
            "log 'Network interface remediation completed'",
            "log \"Backup files stored in: $BACKUP_DIR\"",
            "",
            "# Display summary",
            "echo",
            "echo '=== Network Interface Remediation Summary ==='",
            "echo 'Interface mappings applied:'"
        ])
        
        for old_name, new_name in interface_mapping.items():
            script_content.append(f"echo '  {old_name} -> {new_name}'")
        
        script_content.extend([
            "echo",
            "echo 'Backup location:'",
            "echo \"  $BACKUP_DIR\"",
            "echo",
            "echo 'Log file:'",
            "echo \"  $LOG_FILE\"",
            "echo",
            "echo 'Please reboot the system to ensure all changes take effect.'",
            "echo 'After reboot, verify network connectivity and interface names.'",
            ""
        ])
        
        return "\n".join(script_content)
    
    def validate_network_config(self, vmware_interfaces: List[NetworkInterface],
                              current_interfaces: List[NetworkInterface]) -> Dict[str, Any]:
        """Validate network configuration and identify potential issues"""
        issues = []
        warnings = []
        recommendations = []
        
        # Check for MAC address mismatches
        vmware_macs = {iface.mac_address for iface in vmware_interfaces if iface.mac_address}
        current_macs = {iface.mac_address for iface in current_interfaces if iface.mac_address}
        
        missing_macs = vmware_macs - current_macs
        extra_macs = current_macs - vmware_macs
        
        if missing_macs:
            issues.append(f"Missing network interfaces with MACs: {', '.join(missing_macs)}")
        
        if extra_macs:
            warnings.append(f"Extra network interfaces found with MACs: {', '.join(extra_macs)}")
        
        # Check for interface name conflicts
        target_names = {iface.target_name for iface in vmware_interfaces}
        if len(target_names) != len(vmware_interfaces):
            issues.append("Duplicate target interface names detected")
        
        # Check for problematic interface names
        for iface in vmware_interfaces:
            if iface.target_name.startswith("ens"):
                warnings.append(f"Target name '{iface.target_name}' may conflict with systemd predictable naming")
        
        # Check for primary interface
        primary_interfaces = [iface for iface in vmware_interfaces if iface.is_primary]
        if len(primary_interfaces) != 1:
            warnings.append(f"Expected 1 primary interface, found {len(primary_interfaces)}")
        
        # Generate recommendations
        if issues:
            recommendations.append("Resolve critical issues before proceeding with migration")
        
        if warnings:
            recommendations.append("Review warnings and adjust configuration if needed")
        
        recommendations.append("Test network remediation script in a non-production environment first")
        recommendations.append("Ensure you have console access to the VM after migration")
        
        return {
            "issues": issues,
            "warnings": warnings,
            "recommendations": recommendations,
            "vmware_interfaces": len(vmware_interfaces),
            "current_interfaces": len(current_interfaces),
            "mac_matches": len(vmware_macs & current_macs)
        }
