"""
Pre-flight checklist system for migration validation
Ensures all requirements are met before migration execution
"""

import logging
from typing import List, Dict, Any, Optional, Callable, Set
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
import asyncio
import json


class CheckStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"
    SKIPPED = "skipped"


class CheckCategory(Enum):
    CONNECTIVITY = "connectivity"
    HARDWARE = "hardware"
    STORAGE = "storage"
    NETWORK = "network"
    SECURITY = "security"
    COMPATIBILITY = "compatibility"
    PERFORMANCE = "performance"


class CheckSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class CheckResult:
    """Result of a pre-flight check"""
    check_id: str
    name: str
    category: CheckCategory
    status: CheckStatus
    severity: CheckSeverity
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)
    execution_time_seconds: float = 0.0
    is_blocking: bool = True
    timestamp: datetime = field(default_factory=datetime.utcnow)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "check_id": self.check_id,
            "name": self.name,
            "category": self.category.value,
            "status": self.status.value,
            "severity": self.severity.value,
            "message": self.message,
            "details": self.details,
            "recommendations": self.recommendations,
            "execution_time_seconds": self.execution_time_seconds,
            "is_blocking": self.is_blocking,
            "timestamp": self.timestamp.isoformat()
        }


class PreFlightCheck:
    """Base class for pre-flight checks"""
    
    def __init__(self, 
                 check_id: str,
                 name: str,
                 category: CheckCategory,
                 severity: CheckSeverity = CheckSeverity.ERROR,
                 is_blocking: bool = True,
                 timeout_seconds: int = 300):
        self.check_id = check_id
        self.name = name
        self.category = category
        self.severity = severity
        self.is_blocking = is_blocking
        self.timeout_seconds = timeout_seconds
        self.logger = logging.getLogger(f"{__name__}.{check_id}")
    
    async def execute(self, context: Dict[str, Any]) -> CheckResult:
        """Execute the pre-flight check"""
        start_time = datetime.utcnow()
        
        try:
            self.logger.info(f"Executing pre-flight check: {self.name}")
            
            # Execute with timeout
            result = await asyncio.wait_for(
                self._check_logic(context),
                timeout=self.timeout_seconds
            )
            
            execution_time = (datetime.utcnow() - start_time).total_seconds()
            result.execution_time_seconds = execution_time
            
            self.logger.info(f"Check completed: {self.name} - {result.status.value}")
            return result
            
        except asyncio.TimeoutError:
            execution_time = (datetime.utcnow() - start_time).total_seconds()
            self.logger.error(f"Check timed out: {self.name}")
            
            return CheckResult(
                check_id=self.check_id,
                name=self.name,
                category=self.category,
                status=CheckStatus.FAILED,
                severity=CheckSeverity.CRITICAL,
                message=f"Check timed out after {self.timeout_seconds} seconds",
                execution_time_seconds=execution_time,
                is_blocking=self.is_blocking
            )
        
        except Exception as e:
            execution_time = (datetime.utcnow() - start_time).total_seconds()
            self.logger.error(f"Check failed with exception: {self.name} - {e}")
            
            return CheckResult(
                check_id=self.check_id,
                name=self.name,
                category=self.category,
                status=CheckStatus.FAILED,
                severity=self.severity,
                message=f"Check failed with exception: {str(e)}",
                details={"exception": str(e)},
                execution_time_seconds=execution_time,
                is_blocking=self.is_blocking
            )
    
    async def _check_logic(self, context: Dict[str, Any]) -> CheckResult:
        """Actual check logic - to be implemented by subclasses"""
        raise NotImplementedError("Subclasses must implement _check_logic")


class ConnectivityCheck(PreFlightCheck):
    """Check connectivity to VMware and Proxmox hosts"""
    
    def __init__(self):
        super().__init__(
            check_id="connectivity_check",
            name="VMware and Proxmox Connectivity",
            category=CheckCategory.CONNECTIVITY,
            severity=CheckSeverity.CRITICAL
        )
    
    async def _check_logic(self, context: Dict[str, Any]) -> CheckResult:
        """Check connectivity to both hosts"""
        vmware_config = context.get("vmware_config")
        proxmox_config = context.get("proxmox_config")
        
        results = {}
        issues = []
        recommendations = []
        
        # Check VMware connectivity
        if vmware_config:
            try:
                # This would use the actual VMware client
                # For now, simulate the check
                await asyncio.sleep(0.1)  # Simulate network call
                results["vmware"] = "connected"
            except Exception as e:
                results["vmware"] = f"failed: {str(e)}"
                issues.append(f"VMware connectivity failed: {str(e)}")
                recommendations.append("Verify VMware host credentials and network access")
        else:
            results["vmware"] = "not_configured"
        
        # Check Proxmox connectivity
        if proxmox_config:
            try:
                # This would use the actual Proxmox client
                await asyncio.sleep(0.1)  # Simulate network call
                results["proxmox"] = "connected"
            except Exception as e:
                results["proxmox"] = f"failed: {str(e)}"
                issues.append(f"Proxmox connectivity failed: {str(e)}")
                recommendations.append("Verify Proxmox host credentials and network access")
        else:
            results["proxmox"] = "not_configured"
        
        # Determine overall status
        if all(status == "connected" for status in results.values() if status != "not_configured"):
            status = CheckStatus.PASSED
            message = "All configured hosts are reachable"
        elif any("failed" in str(status) for status in results.values()):
            status = CheckStatus.FAILED
            message = "One or more hosts are not reachable"
        else:
            status = CheckStatus.WARNING
            message = "Some hosts are not configured"
        
        return CheckResult(
            check_id=self.check_id,
            name=self.name,
            category=self.category,
            status=status,
            severity=self.severity,
            message=message,
            details={"connectivity_results": results},
            recommendations=recommendations
        )


class StorageCheck(PreFlightCheck):
    """Check storage availability and capacity"""
    
    def __init__(self):
        super().__init__(
            check_id="storage_check",
            name="Storage Capacity and Availability",
            category=CheckCategory.STORAGE,
            severity=CheckSeverity.ERROR
        )
    
    async def _check_logic(self, context: Dict[str, Any]) -> CheckResult:
        """Check storage capacity"""
        hardware_spec = context.get("hardware_spec", {})
        target_storage = context.get("target_storage")
        
        if not hardware_spec or not target_storage:
            return CheckResult(
                check_id=self.check_id,
                name=self.name,
                category=self.category,
                status=CheckStatus.FAILED,
                severity=self.severity,
                message="Hardware specification or target storage not provided",
                recommendations=["Ensure VM hardware analysis is complete"]
            )
        
        # Calculate total disk capacity
        total_disk_capacity = sum(
            disk.get("capacity_bytes", 0) 
            for disk in hardware_spec.get("disks", [])
        )
        
        # Check target storage capacity (simulated)
        available_capacity = target_storage.get("available_bytes", 0)
        
        issues = []
        recommendations = []
        
        if available_capacity < total_disk_capacity:
            issues.append(f"Insufficient storage: need {total_disk_capacity:,} bytes, have {available_capacity:,} bytes")
            recommendations.append("Free up space on target storage or choose a different storage target")
            status = CheckStatus.FAILED
            message = "Insufficient storage capacity"
        elif available_capacity < total_disk_capacity * 1.2:  # 20% buffer
            recommendations.append("Consider freeing up additional space for safety margin")
            status = CheckStatus.WARNING
            message = "Limited storage capacity"
        else:
            status = CheckStatus.PASSED
            message = "Sufficient storage capacity available"
        
        return CheckResult(
            check_id=self.check_id,
            name=self.name,
            category=self.category,
            status=status,
            severity=self.severity,
            message=message,
            details={
                "total_disk_capacity_bytes": total_disk_capacity,
                "available_capacity_bytes": available_capacity,
                "utilization_percent": (total_disk_capacity / available_capacity * 100) if available_capacity > 0 else 0
            },
            recommendations=recommendations
        )


class CompatibilityCheck(PreFlightCheck):
    """Check VM compatibility with Proxmox"""
    
    def __init__(self):
        super().__init__(
            check_id="compatibility_check",
            name="VM Compatibility",
            category=CheckCategory.COMPATIBILITY,
            severity=CheckSeverity.ERROR
        )
    
    async def _check_logic(self, context: Dict[str, Any]) -> CheckResult:
        """Check VM compatibility"""
        hardware_spec = context.get("hardware_spec", {})
        
        if not hardware_spec:
            return CheckResult(
                check_id=self.check_id,
                name=self.name,
                category=self.category,
                status=CheckStatus.FAILED,
                severity=self.severity,
                message="Hardware specification not provided"
            )
        
        issues = []
        warnings = []
        recommendations = []
        
        # Check for unsupported features
        if hardware_spec.get("has_encryption", False):
            issues.append("Encrypted VMs are not supported")
            recommendations.append("Remove VM encryption before migration")
        
        if hardware_spec.get("snapshot_count", 0) > 0:
            warnings.append(f"VM has {hardware_spec['snapshot_count']} snapshots")
            recommendations.append("Consolidate snapshots before migration")
        
        # Check guest OS
        guest_id = hardware_spec.get("guest_id", "").lower()
        if any(os_name in guest_id for os_name in ["windows", "microsoft"]):
            issues.append("Windows guests are not supported")
            recommendations.append("Use alternative migration method for Windows VMs")
        
        # Check hardware version
        hw_version = hardware_spec.get("vmware_hardware_version", "")
        if hw_version and int(hw_version.split('-')[-1]) < 10:  # Very old versions
            warnings.append(f"Old VMware hardware version: {hw_version}")
            recommendations.append("Consider upgrading VM hardware version in VMware first")
        
        # Determine status
        if issues:
            status = CheckStatus.FAILED
            message = "VM compatibility issues found"
        elif warnings:
            status = CheckStatus.WARNING
            message = "VM compatibility warnings"
        else:
            status = CheckStatus.PASSED
            message = "VM is compatible with Proxmox"
        
        return CheckResult(
            check_id=self.check_id,
            name=self.name,
            category=self.category,
            status=status,
            severity=self.severity,
            message=message,
            details={
                "guest_id": hardware_spec.get("guest_id"),
                "hardware_version": hardware_spec.get("vmware_hardware_version"),
                "has_encryption": hardware_spec.get("has_encryption"),
                "snapshot_count": hardware_spec.get("snapshot_count")
            },
            recommendations=issues + warnings + recommendations
        )


class PreFlightChecklist:
    """Manages and executes pre-flight checklist"""
    
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.logger = logging.getLogger(f"{__name__}.{job_id}")
        self.checks: Dict[str, PreFlightCheck] = {}
        self.results: Dict[str, CheckResult] = {}
        self._register_default_checks()
    
    def _register_default_checks(self):
        """Register default pre-flight checks"""
        self.register_check(ConnectivityCheck())
        self.register_check(StorageCheck())
        self.register_check(CompatibilityCheck())
    
    def register_check(self, check: PreFlightCheck):
        """Register a pre-flight check"""
        self.checks[check.check_id] = check
        self.logger.info(f"Registered pre-flight check: {check.name}")
    
    def unregister_check(self, check_id: str):
        """Unregister a pre-flight check"""
        if check_id in self.checks:
            del self.checks[check_id]
            self.logger.info(f"Unregistered pre-flight check: {check_id}")
    
    async def execute_all(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute all pre-flight checks"""
        self.logger.info(f"Executing {len(self.checks)} pre-flight checks")
        
        start_time = datetime.utcnow()
        
        # Execute all checks concurrently where possible
        tasks = []
        for check in self.checks.values():
            task = asyncio.create_task(check.execute(context))
            tasks.append((check.check_id, task))
        
        # Wait for all checks to complete
        for check_id, task in tasks:
            try:
                result = await task
                self.results[check_id] = result
            except Exception as e:
                self.logger.error(f"Check {check_id} failed with exception: {e}")
                self.results[check_id] = CheckResult(
                    check_id=check_id,
                    name=self.checks[check_id].name,
                    category=self.checks[check_id].category,
                    status=CheckStatus.FAILED,
                    severity=CheckSeverity.CRITICAL,
                    message=f"Check execution failed: {str(e)}"
                )
        
        execution_time = (datetime.utcnow() - start_time).total_seconds()
        
        # Generate summary
        summary = self._generate_summary()
        summary["execution_time_seconds"] = execution_time
        
        self.logger.info(f"Pre-flight checks completed in {execution_time:.2f}s")
        
        return summary
    
    async def execute_check(self, check_id: str, context: Dict[str, Any]) -> CheckResult:
        """Execute a specific check"""
        if check_id not in self.checks:
            raise ValueError(f"Check not found: {check_id}")
        
        check = self.checks[check_id]
        result = await check.execute(context)
        self.results[check_id] = result
        
        return result
    
    def _generate_summary(self) -> Dict[str, Any]:
        """Generate checklist summary"""
        total_checks = len(self.results)
        passed_checks = len([r for r in self.results.values() if r.status == CheckStatus.PASSED])
        failed_checks = len([r for r in self.results.values() if r.status == CheckStatus.FAILED])
        warning_checks = len([r for r in self.results.values() if r.status == CheckStatus.WARNING])
        skipped_checks = len([r for r in self.results.values() if r.status == CheckStatus.SKIPPED])
        
        # Check for blocking failures
        blocking_failures = [
            r for r in self.results.values() 
            if r.status == CheckStatus.FAILED and r.is_blocking
        ]
        
        can_proceed = len(blocking_failures) == 0
        
        return {
            "job_id": self.job_id,
            "total_checks": total_checks,
            "passed_checks": passed_checks,
            "failed_checks": failed_checks,
            "warning_checks": warning_checks,
            "skipped_checks": skipped_checks,
            "can_proceed": can_proceed,
            "blocking_failures": len(blocking_failures),
            "check_results": {check_id: result.to_dict() for check_id, result in self.results.items()}
        }
    
    def get_check_result(self, check_id: str) -> Optional[CheckResult]:
        """Get result of a specific check"""
        return self.results.get(check_id)
    
    def get_failed_checks(self) -> List[CheckResult]:
        """Get all failed checks"""
        return [result for result in self.results.values() if result.status == CheckStatus.FAILED]
    
    def get_blocking_failures(self) -> List[CheckResult]:
        """Get all blocking failures"""
        return [
            result for result in self.results.values() 
            if result.status == CheckStatus.FAILED and result.is_blocking
        ]
    
    def get_all_recommendations(self) -> List[str]:
        """Get all recommendations from all checks"""
        recommendations = []
        for result in self.results.values():
            recommendations.extend(result.recommendations)
        return list(set(recommendations))  # Remove duplicates
    
    def clear_results(self):
        """Clear all check results"""
        self.results.clear()
        self.logger.info("Pre-flight check results cleared")
