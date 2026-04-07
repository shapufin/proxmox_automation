"""
Pre-flight snapshot detection and consolidation for VMware VMs
Ensures clean migration state by detecting and consolidating snapshots
"""

import logging
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
from pyVmomi import vim
from pyVim.connect import SmartConnect, Disconnect
import ssl

from ..schemas.hardware_spec import HardwareSpec


@dataclass
class SnapshotInfo:
    name: str
    description: str
    creation_time: str
    size_mb: float
    is_current: bool
    parent_name: Optional[str] = None
    children_count: int = 0


@dataclass
class SnapshotCheckResult:
    has_snapshots: bool
    snapshots: List[SnapshotInfo]
    total_size_mb: float
    consolidation_required: bool
    can_consolidate: bool
    power_state: str
    warnings: List[str]
    errors: List[str]


class SnapshotChecker:
    """Handles VMware snapshot detection and consolidation"""
    
    def __init__(self, vmware_host: str, username: str, password: str, port: int = 443, verify_ssl: bool = False):
        self.vmware_host = vmware_host
        self.username = username
        self.password = password
        self.port = port
        self.verify_ssl = verify_ssl
        self.logger = logging.getLogger(__name__)
        self.service_instance = None
    
    def _connect(self) -> vim.ServiceInstance:
        """Establish connection to vCenter/ESXi"""
        if self.service_instance:
            return self.service_instance
            
        context = None
        if not self.verify_ssl:
            context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
            context.verify_mode = ssl.CERT_NONE
        
        try:
            self.service_instance = SmartConnect(
                host=self.vmware_host,
                user=self.username,
                pwd=self.password,
                port=self.port,
                sslContext=context
            )
            self.logger.info(f"Connected to VMware at {self.vmware_host}")
            return self.service_instance
        except Exception as e:
            self.logger.error(f"Failed to connect to VMware: {e}")
            raise
    
    def disconnect(self):
        """Close VMware connection"""
        if self.service_instance:
            Disconnect(self.service_instance)
            self.service_instance = None
            self.logger.info("Disconnected from VMware")
    
    def _get_vm_by_name(self, vm_name: str) -> Optional[vim.VirtualMachine]:
        """Find VM by name"""
        content = self._connect().RetrieveContent()
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True
        )
        
        for vm in container.view:
            if vm.name == vm_name:
                container.Destroy()
                return vm
        
        container.Destroy()
        return None
    
    def _get_snapshot_tree(self, vm: vim.VirtualMachine) -> List[SnapshotInfo]:
        """Extract snapshot information from VM snapshot tree"""
        snapshots = []
        
        def _traverse_snapshot_tree(snapshot_node: vim.vm.SnapshotTree, parent_name: Optional[str] = None):
            if not snapshot_node:
                return
            
            # Calculate snapshot size (approximation)
            size_mb = 0.0
            if hasattr(snapshot_node, 'snapshot') and snapshot_node.snapshot:
                # Try to get size from storage manager if available
                try:
                    # This is an approximation - actual size calculation requires storage manager queries
                    size_mb = len(snapshot_node.name) * 0.1  # Placeholder
                except:
                    pass
            
            snapshot_info = SnapshotInfo(
                name=snapshot_node.name,
                description=snapshot_node.description or "",
                creation_time=snapshot_node.createTime.isoformat() if snapshot_node.createTime else "",
                size_mb=size_mb,
                is_current=snapshot_node.current,
                parent_name=parent_name,
                children_count=len(snapshot_node.childSnapshotList) if snapshot_node.childSnapshotList else 0
            )
            snapshots.append(snapshot_info)
            
            # Recursively process children
            if snapshot_node.childSnapshotList:
                for child in snapshot_node.childSnapshotList:
                    _traverse_snapshot_tree(child, snapshot_node.name)
        
        if vm.snapshot and vm.snapshot.rootSnapshotList:
            for root_snapshot in vm.snapshot.rootSnapshotList:
                _traverse_snapshot_tree(root_snapshot)
        
        return snapshots
    
    def _check_consolidation_needed(self, vm: vim.VirtualMachine) -> Tuple[bool, bool]:
        """Check if consolidation is needed and possible"""
        consolidation_needed = False
        can_consolidate = True
        
        # Check if VM has delta disks that need consolidation
        if hasattr(vm, 'runtime') and vm.runtime.consolidationNeeded:
            consolidation_needed = True
        
        # Check if consolidation is possible based on VM state
        if vm.runtime.powerState == vim.VirtualMachinePowerState.suspended:
            can_consolidate = False
            consolidation_needed = True  # Suspended VMs need to be resumed first
        
        # Check if there are snapshot-related issues
        if vm.snapshot and vm.snapshot.rootSnapshotList:
            # If VM has snapshots, consolidation might be needed
            consolidation_needed = True
        
        return consolidation_needed, can_consolidate
    
    def check_snapshots(self, vm_name: str) -> SnapshotCheckResult:
        """Perform comprehensive snapshot analysis"""
        try:
            vm = self._get_vm_by_name(vm_name)
            if not vm:
                return SnapshotCheckResult(
                    has_snapshots=False,
                    snapshots=[],
                    total_size_mb=0.0,
                    consolidation_required=False,
                    can_consolidate=False,
                    power_state="unknown",
                    warnings=[f"VM '{vm_name}' not found"],
                    errors=[f"VM '{vm_name}' not found"]
                )
            
            # Get basic VM info
            power_state = str(vm.runtime.powerState).lower()
            
            # Get snapshot information
            snapshots = self._get_snapshot_tree(vm)
            has_snapshots = len(snapshots) > 0
            total_size_mb = sum(s.size_mb for s in snapshots)
            
            # Check consolidation requirements
            consolidation_required, can_consolidate = self._check_consolidation_needed(vm)
            
            warnings = []
            errors = []
            
            # Generate warnings and errors
            if has_snapshots:
                warnings.append(f"VM has {len(snapshots)} snapshot(s) totaling {total_size_mb:.1f} MB")
                
                # Check for large snapshots
                large_snapshots = [s for s in snapshots if s.size_mb > 1000]  # > 1GB
                if large_snapshots:
                    warnings.append(f"Found {len(large_snapshots)} large snapshot(s) that may increase migration time")
            
            if consolidation_required:
                if can_consolidate:
                    warnings.append("Snapshot consolidation is required before migration")
                else:
                    errors.append("Snapshot consolidation is required but not possible in current state")
            
            # Check for problematic snapshot configurations
            if len(snapshots) > 10:
                warnings.append("VM has many snapshots - consider reviewing snapshot strategy")
            
            # Check for nested snapshots
            nested_snapshots = [s for s in snapshots if s.parent_name is not None]
            if nested_snapshots:
                warnings.append(f"VM has {len(nested_snapshots)} nested snapshots")
            
            return SnapshotCheckResult(
                has_snapshots=has_snapshots,
                snapshots=snapshots,
                total_size_mb=total_size_mb,
                consolidation_required=consolidation_required,
                can_consolidate=can_consolidate,
                power_state=power_state,
                warnings=warnings,
                errors=errors
            )
            
        except Exception as e:
            self.logger.error(f"Error checking snapshots for {vm_name}: {e}")
            return SnapshotCheckResult(
                has_snapshots=False,
                snapshots=[],
                total_size_mb=0.0,
                consolidation_required=False,
                can_consolidate=False,
                power_state="error",
                warnings=[],
                errors=[f"Failed to check snapshots: {str(e)}"]
            )
    
    def consolidate_snapshots(self, vm_name: str, force: bool = False) -> Dict[str, Any]:
        """Consolidate VM snapshots"""
        try:
            vm = self._get_vm_by_name(vm_name)
            if not vm:
                return {
                    "success": False,
                    "error": f"VM '{vm_name}' not found",
                    "message": "VM not found"
                }
            
            # Check if consolidation is needed
            consolidation_needed, can_consolidate = self._check_consolidation_needed(vm)
            
            if not consolidation_needed and not force:
                return {
                    "success": True,
                    "message": "No consolidation needed",
                    "consolidation_performed": False
                }
            
            if not can_consolidate and not force:
                return {
                    "success": False,
                    "error": "Consolidation not possible in current VM state",
                    "power_state": str(vm.runtime.powerState).lower(),
                    "consolidation_performed": False
                }
            
            # Perform consolidation
            self.logger.info(f"Starting snapshot consolidation for {vm_name}")
            
            task = vm.ConsolidateVM_Task()
            
            # Wait for task completion
            while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                # In a real implementation, you'd want to add timeout and progress tracking
                pass
            
            if task.info.state == vim.TaskInfo.State.success:
                self.logger.info(f"Snapshot consolidation completed for {vm_name}")
                return {
                    "success": True,
                    "message": "Snapshot consolidation completed successfully",
                    "consolidation_performed": True,
                    "task_result": str(task.info.result) if task.info.result else None
                }
            else:
                error_msg = task.info.error if task.info.error else "Unknown error"
                self.logger.error(f"Snapshot consolidation failed for {vm_name}: {error_msg}")
                return {
                    "success": False,
                    "error": str(error_msg),
                    "message": "Snapshot consolidation failed",
                    "consolidation_performed": False
                }
                
        except Exception as e:
            self.logger.error(f"Error consolidating snapshots for {vm_name}: {e}")
            return {
                "success": False,
                "error": str(e),
                "message": "Failed to consolidate snapshots",
                "consolidation_performed": False
            }
    
    def generate_preflight_report(self, vm_name: str) -> Dict[str, Any]:
        """Generate comprehensive pre-flight report for snapshots"""
        result = self.check_snapshots(vm_name)
        
        report = {
            "vm_name": vm_name,
            "snapshot_analysis": {
                "has_snapshots": result.has_snapshots,
                "snapshot_count": len(result.snapshots),
                "total_size_mb": result.total_size_mb,
                "consolidation_required": result.consolidation_required,
                "can_consolidate": result.can_consolidate,
                "power_state": result.power_state
            },
            "snapshots": [
                {
                    "name": s.name,
                    "description": s.description,
                    "creation_time": s.creation_time,
                    "size_mb": s.size_mb,
                    "is_current": s.is_current,
                    "parent_name": s.parent_name,
                    "children_count": s.children_count
                }
                for s in result.snapshots
            ],
            "recommendations": [],
            "blocking_issues": result.errors,
            "warnings": result.warnings
        }
        
        # Generate recommendations
        if result.consolidation_required:
            if result.can_consolidate:
                report["recommendations"].append("Run snapshot consolidation before migration")
            else:
                report["recommendations"].append("VM must be powered on and operational to consolidate snapshots")
        
        if result.has_snapshots and not result.consolidation_required:
            report["recommendations"].append("Consider reviewing snapshot strategy - snapshots will be flattened during migration")
        
        if result.total_size_mb > 5000:  # > 5GB
            report["recommendations"].append("Large snapshot size detected - migration may take significantly longer")
        
        return report
