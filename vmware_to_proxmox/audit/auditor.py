"""
Migration auditor for comprehensive audit trail and logging
Tracks all migration operations for compliance and troubleshooting
"""

import logging
import json
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import uuid


class AuditLevel(Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AuditCategory(Enum):
    SYSTEM = "system"
    NETWORK = "network"
    STORAGE = "storage"
    SECURITY = "security"
    COMPLIANCE = "compliance"
    PERFORMANCE = "performance"


@dataclass
class AuditEvent:
    """Single audit event"""
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=datetime.utcnow)
    job_id: Optional[str] = None
    task_id: Optional[str] = None
    category: AuditCategory = AuditCategory.SYSTEM
    level: AuditLevel = AuditLevel.INFO
    event_type: str = ""
    description: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    source: str = ""
    user: Optional[str] = None
    ip_address: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat(),
            "job_id": self.job_id,
            "task_id": self.task_id,
            "category": self.category.value,
            "level": self.level.value,
            "event_type": self.event_type,
            "description": self.description,
            "details": self.details,
            "source": self.source,
            "user": self.user,
            "ip_address": self.ip_address
        }


class MigrationAuditor:
    """Comprehensive audit system for migration operations"""
    
    def __init__(self, job_id: Optional[str] = None):
        self.job_id = job_id
        self.logger = logging.getLogger(f"{__name__}.{job_id}" if job_id else __name__)
        self.events: List[AuditEvent] = []
        self.session_id = str(uuid.uuid4())
        
    def log_event(self, 
                  event_type: str,
                  description: str,
                  category: AuditCategory = AuditCategory.SYSTEM,
                  level: AuditLevel = AuditLevel.INFO,
                  task_id: Optional[str] = None,
                  details: Optional[Dict[str, Any]] = None,
                  source: str = "",
                  user: Optional[str] = None,
                  ip_address: Optional[str] = None) -> AuditEvent:
        """Log an audit event"""
        
        event = AuditEvent(
            job_id=self.job_id,
            task_id=task_id,
            category=category,
            level=level,
            event_type=event_type,
            description=description,
            details=details or {},
            source=source or self.__class__.__name__,
            user=user,
            ip_address=ip_address
        )
        
        self.events.append(event)
        
        # Also log to standard logger
        log_message = f"[{category.value.upper()}] {description}"
        if details:
            log_message += f" | Details: {json.dumps(details, default=str)}"
        
        if level == AuditLevel.CRITICAL:
            self.logger.critical(log_message)
        elif level == AuditLevel.ERROR:
            self.logger.error(log_message)
        elif level == AuditLevel.WARNING:
            self.logger.warning(log_message)
        else:
            self.logger.info(log_message)
        
        return event
    
    def log_migration_start(self, vm_name: str, mode: str, user: Optional[str] = None) -> AuditEvent:
        """Log migration start"""
        return self.log_event(
            event_type="migration_started",
            description=f"Migration started for VM: {vm_name}",
            category=AuditCategory.SYSTEM,
            level=AuditLevel.INFO,
            user=user,
            details={
                "vm_name": vm_name,
                "mode": mode,
                "session_id": self.session_id
            }
        )
    
    def log_migration_complete(self, vm_name: str, vmid: int, duration_seconds: float) -> AuditEvent:
        """Log migration completion"""
        return self.log_event(
            event_type="migration_completed",
            description=f"Migration completed for VM: {vm_name} (VMID: {vmid})",
            category=AuditCategory.SYSTEM,
            level=AuditLevel.INFO,
            details={
                "vm_name": vm_name,
                "vmid": vmid,
                "duration_seconds": duration_seconds,
                "session_id": self.session_id
            }
        )
    
    def log_migration_failed(self, vm_name: str, error: str, stage: str) -> AuditEvent:
        """Log migration failure"""
        return self.log_event(
            event_type="migration_failed",
            description=f"Migration failed for VM: {vm_name} at stage: {stage}",
            category=AuditCategory.SYSTEM,
            level=AuditLevel.ERROR,
            details={
                "vm_name": vm_name,
                "error": error,
                "stage": stage,
                "session_id": self.session_id
            }
        )
    
    def log_task_start(self, task_id: str, task_name: str) -> AuditEvent:
        """Log task start"""
        return self.log_event(
            event_type="task_started",
            description=f"Task started: {task_name}",
            category=AuditCategory.SYSTEM,
            level=AuditLevel.INFO,
            task_id=task_id,
            details={
                "task_name": task_name,
                "session_id": self.session_id
            }
        )
    
    def log_task_complete(self, task_id: str, task_name: str, duration_seconds: float) -> AuditEvent:
        """Log task completion"""
        return self.log_event(
            event_type="task_completed",
            description=f"Task completed: {task_name}",
            category=AuditCategory.SYSTEM,
            level=AuditLevel.INFO,
            task_id=task_id,
            details={
                "task_name": task_name,
                "duration_seconds": duration_seconds,
                "session_id": self.session_id
            }
        )
    
    def log_task_failed(self, task_id: str, task_name: str, error: str) -> AuditEvent:
        """Log task failure"""
        return self.log_event(
            event_type="task_failed",
            description=f"Task failed: {task_name}",
            category=AuditCategory.SYSTEM,
            level=AuditLevel.ERROR,
            task_id=task_id,
            details={
                "task_name": task_name,
                "error": error,
                "session_id": self.session_id
            }
        )
    
    def log_security_event(self, event_type: str, description: str, details: Optional[Dict[str, Any]] = None) -> AuditEvent:
        """Log security-related event"""
        return self.log_event(
            event_type=event_type,
            description=description,
            category=AuditCategory.SECURITY,
            level=AuditLevel.WARNING,
            details=details
        )
    
    def log_performance_metric(self, metric_name: str, value: float, unit: str = "") -> AuditEvent:
        """Log performance metric"""
        return self.log_event(
            event_type="performance_metric",
            description=f"Performance metric: {metric_name} = {value} {unit}",
            category=AuditCategory.PERFORMANCE,
            level=AuditLevel.INFO,
            details={
                "metric_name": metric_name,
                "value": value,
                "unit": unit,
                "session_id": self.session_id
            }
        )
    
    def log_compliance_check(self, check_name: str, passed: bool, details: Optional[Dict[str, Any]] = None) -> AuditEvent:
        """Log compliance check result"""
        level = AuditLevel.INFO if passed else AuditLevel.WARNING
        status = "PASSED" if passed else "FAILED"
        
        return self.log_event(
            event_type="compliance_check",
            description=f"Compliance check {check_name}: {status}",
            category=AuditCategory.COMPLIANCE,
            level=level,
            details=details or {"check_name": check_name, "passed": passed}
        )
    
    def get_events_by_category(self, category: AuditCategory) -> List[AuditEvent]:
        """Get all events for a specific category"""
        return [event for event in self.events if event.category == category]
    
    def get_events_by_level(self, level: AuditLevel) -> List[AuditEvent]:
        """Get all events for a specific level"""
        return [event for event in self.events if event.level == level]
    
    def get_events_by_task(self, task_id: str) -> List[AuditEvent]:
        """Get all events for a specific task"""
        return [event for event in self.events if event.task_id == task_id]
    
    def get_error_events(self) -> List[AuditEvent]:
        """Get all error and critical events"""
        return [event for event in self.events if event.level in [AuditLevel.ERROR, AuditLevel.CRITICAL]]
    
    def get_summary(self) -> Dict[str, Any]:
        """Get audit summary"""
        total_events = len(self.events)
        error_count = len(self.get_error_events())
        
        category_counts = {}
        for category in AuditCategory:
            category_counts[category.value] = len(self.get_events_by_category(category))
        
        level_counts = {}
        for level in AuditLevel:
            level_counts[level.value] = len(self.get_events_by_level(level))
        
        return {
            "job_id": self.job_id,
            "session_id": self.session_id,
            "total_events": total_events,
            "error_count": error_count,
            "category_counts": category_counts,
            "level_counts": level_counts,
            "start_time": self.events[0].timestamp.isoformat() if self.events else None,
            "end_time": self.events[-1].timestamp.isoformat() if self.events else None
        }
    
    def export_events(self, format: str = "json") -> str:
        """Export events in specified format"""
        if format.lower() == "json":
            return json.dumps([event.to_dict() for event in self.events], indent=2, default=str)
        else:
            raise ValueError(f"Unsupported export format: {format}")
    
    def clear_events(self):
        """Clear all events"""
        self.events.clear()
        self.logger.info("Audit events cleared")
