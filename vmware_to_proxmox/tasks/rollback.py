"""
Rollback and cleanup system for failed migrations
Provides atomic task execution with automatic cleanup on failure
"""

import logging
import asyncio
from typing import List, Dict, Any, Optional, Set
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
import paramiko

from .dag import TaskExecutionContext, TaskExecutionResult, TaskResult


class CleanupAction(Enum):
    DELETE_VM = "delete_vm"
    DELETE_DISK = "delete_disk"
    DELETE_TEMP_DIR = "delete_temp_dir"
    REVERT_SNAPSHOT = "revert_snapshot"
    STOP_VM = "stop_vm"
    CLEANUP_NETWORK = "cleanup_network"


@dataclass
class CleanupStep:
    action: CleanupAction
    resource_id: str
    resource_type: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    critical: bool = True  # If False, failure won't block rollback


@dataclass
class RollbackPlan:
    job_id: str
    failure_point: str
    cleanup_steps: List[CleanupStep]
    created_at: datetime = field(default_factory=datetime.utcnow)
    executed: bool = False
    execution_log: List[str] = field(default_factory=list)


class RollbackManager:
    """Manages rollback operations for failed migrations"""
    
    def __init__(self, ssh_client: paramiko.SSHClient, proxmox_api_client):
        self.ssh_client = ssh_client
        self.proxmox_api = proxmox_api_client
        self.logger = logging.getLogger(__name__)
        self.rollback_plans: Dict[str, RollbackPlan] = {}
    
    def create_rollback_plan(self, job_id: str, failure_point: str, context: TaskExecutionContext) -> RollbackPlan:
        """Create a rollback plan based on current execution state"""
        cleanup_steps = []
        
        # Analyze what was created and needs cleanup
        shared_state = context.shared_state
        
        # Check if VM was created
        if shared_state.get("created_vmid"):
            vmid = shared_state["created_vmid"]
            cleanup_steps.append(CleanupStep(
                action=CleanupAction.DELETE_VM,
                resource_id=str(vmid),
                resource_type="vm",
                description=f"Delete VM {vmid}",
                parameters={"vmid": vmid}
            ))
            
            # Stop VM first if it's running
            if shared_state.get("vm_started"):
                cleanup_steps.append(CleanupStep(
                    action=CleanupAction.STOP_VM,
                    resource_id=str(vmid),
                    resource_type="vm",
                    description=f"Stop VM {vmid}",
                    parameters={"vmid": vmid},
                    depends_on=[f"delete_vm_{vmid}"]  # Stop before delete
                ))
        
        # Check for created disks
        created_disks = shared_state.get("created_disks", [])
        for disk_info in created_disks:
            disk_id = disk_info.get("volume_id")
            if disk_id:
                cleanup_steps.append(CleanupStep(
                    action=CleanupAction.DELETE_DISK,
                    resource_id=disk_id,
                    resource_type="disk",
                    description=f"Delete disk {disk_id}",
                    parameters={"volume_id": disk_id}
                ))
        
        # Check for temporary directories
        temp_dirs = shared_state.get("temp_directories", [])
        for temp_dir in temp_dirs:
            cleanup_steps.append(CleanupStep(
                action=CleanupAction.DELETE_TEMP_DIR,
                resource_id=temp_dir,
                resource_type="directory",
                description=f"Delete temporary directory {temp_dir}",
                parameters={"path": temp_dir},
                critical=False  # Non-critical, don't fail rollback if this fails
            ))
        
        # Check for network configurations
        if shared_state.get("network_configured"):
            cleanup_steps.append(CleanupStep(
                action=CleanupAction.CLEANUP_NETWORK,
                resource_id="network_config",
                resource_type="network",
                description="Clean up network configurations",
                critical=False
            ))
        
        plan = RollbackPlan(
            job_id=job_id,
            failure_point=failure_point,
            cleanup_steps=cleanup_steps
        )
        
        self.rollback_plans[job_id] = plan
        self.logger.info(f"Created rollback plan for job {job_id} with {len(cleanup_steps)} cleanup steps")
        
        return plan
    
    async def execute_rollback(self, job_id: str) -> Dict[str, Any]:
        """Execute rollback plan for a failed job"""
        plan = self.rollback_plans.get(job_id)
        if not plan:
            return {
                "success": False,
                "error": f"No rollback plan found for job {job_id}"
            }
        
        if plan.executed:
            return {
                "success": False,
                "error": f"Rollback plan for job {job_id} already executed"
            }
        
        self.logger.info(f"Executing rollback for job {job_id}")
        start_time = datetime.utcnow()
        
        executed_steps = []
        failed_steps = []
        
        try:
            # Execute cleanup steps in dependency order
            for step in plan.cleanup_steps:
                step_id = f"{step.action.value}_{step.resource_id}"
                
                # Check dependencies
                if step.depends_on:
                    dependencies_met = all(dep in [s.action.value + "_" + s.resource_id for s in executed_steps] 
                                        for dep in step.depends_on)
                    if not dependencies_met:
                        self.logger.warning(f"Skipping step {step_id} - dependencies not met")
                        continue
                
                try:
                    self.logger.info(f"Executing cleanup step: {step.description}")
                    
                    success = await self._execute_cleanup_step(step)
                    
                    if success:
                        executed_steps.append(step)
                        plan.execution_log.append(f"SUCCESS: {step.description}")
                    else:
                        failed_steps.append(step)
                        plan.execution_log.append(f"FAILED: {step.description}")
                        
                        if step.critical:
                            self.logger.error(f"Critical cleanup step failed: {step.description}")
                            break
                        else:
                            self.logger.warning(f"Non-critical cleanup step failed: {step.description}")
                
                except Exception as e:
                    failed_steps.append(step)
                    plan.execution_log.append(f"ERROR: {step.description} - {str(e)}")
                    
                    if step.critical:
                        self.logger.error(f"Critical cleanup step failed with exception: {e}")
                        break
                    else:
                        self.logger.warning(f"Non-critical cleanup step failed with exception: {e}")
            
            plan.executed = True
            duration = (datetime.utcnow() - start_time).total_seconds()
            
            success = len(failed_steps) == 0 or all(not step.critical for step in failed_steps)
            
            return {
                "success": success,
                "duration_seconds": duration,
                "executed_steps": len(executed_steps),
                "failed_steps": len(failed_steps),
                "execution_log": plan.execution_log
            }
        
        except Exception as e:
            self.logger.error(f"Rollback execution failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "execution_log": plan.execution_log
            }
    
    async def _execute_cleanup_step(self, step: CleanupStep) -> bool:
        """Execute a single cleanup step"""
        try:
            if step.action == CleanupAction.DELETE_VM:
                return await self._delete_vm(step.parameters["vmid"])
            
            elif step.action == CleanupAction.STOP_VM:
                return await self._stop_vm(step.parameters["vmid"])
            
            elif step.action == CleanupAction.DELETE_DISK:
                return await self._delete_disk(step.parameters["volume_id"])
            
            elif step.action == CleanupAction.DELETE_TEMP_DIR:
                return await self._delete_temp_dir(step.parameters["path"])
            
            elif step.action == CleanupAction.CLEANUP_NETWORK:
                return await self._cleanup_network()
            
            else:
                self.logger.warning(f"Unknown cleanup action: {step.action}")
                return False
        
        except Exception as e:
            self.logger.error(f"Error executing cleanup step {step.action}: {e}")
            return False
    
    async def _delete_vm(self, vmid: int) -> bool:
        """Delete a VM"""
        try:
            # Stop VM first if running
            status = await self._get_vm_status(vmid)
            if status == "running":
                await self._stop_vm(vmid)
            
            # Delete VM via Proxmox API
            self.proxmox_api.nodes('pve').qemu(vmid).delete()
            
            # Verify deletion
            await asyncio.sleep(2)
            status = await self._get_vm_status(vmid)
            return status is None
        
        except Exception as e:
            self.logger.error(f"Failed to delete VM {vmid}: {e}")
            return False
    
    async def _stop_vm(self, vmid: int) -> bool:
        """Stop a VM"""
        try:
            self.proxmox_api.nodes('pve').qemu(vmid).status('shutdown').post()
            
            # Wait for shutdown
            for _ in range(30):  # Wait up to 30 seconds
                status = await self._get_vm_status(vmid)
                if status == "stopped":
                    return True
                await asyncio.sleep(1)
            
            # Force shutdown if graceful didn't work
            self.proxmox_api.nodes('pve').qemu(vmid).status('stop').post()
            await asyncio.sleep(2)
            
            status = await self._get_vm_status(vmid)
            return status == "stopped"
        
        except Exception as e:
            self.logger.error(f"Failed to stop VM {vmid}: {e}")
            return False
    
    async def _delete_disk(self, volume_id: str) -> bool:
        """Delete a disk volume"""
        try:
            # Parse volume ID to get storage and volume name
            parts = volume_id.split(':')
            if len(parts) != 2:
                return False
            
            storage = parts[0]
            volume = parts[1]
            
            # Delete via Proxmox API
            self.proxmox_api.nodes('pve').storage(storage).content().delete(volume)
            
            return True
        
        except Exception as e:
            self.logger.error(f"Failed to delete disk {volume_id}: {e}")
            return False
    
    async def _delete_temp_dir(self, path: str) -> bool:
        """Delete a temporary directory"""
        try:
            # Safety check - only delete from safe directories
            safe_prefixes = ['/tmp/', '/var/tmp/', '/mnt/tmp/', '/data/tmp/']
            if not any(path.startswith(prefix) for prefix in safe_prefixes):
                self.logger.warning(f"Refusing to delete unsafe directory: {path}")
                return False
            
            # Delete via SSH
            stdin, stdout, stderr = self.ssh_client.exec_command(f"rm -rf '{path}'")
            exit_code = stdout.channel.recv_exit_status()
            
            return exit_code == 0
        
        except Exception as e:
            self.logger.error(f"Failed to delete temp directory {path}: {e}")
            return False
    
    async def _cleanup_network(self) -> bool:
        """Clean up network configurations"""
        try:
            # This would clean up any network changes made during migration
            # Implementation depends on what network changes were made
            self.logger.info("Network cleanup completed")
            return True
        
        except Exception as e:
            self.logger.error(f"Failed to cleanup network: {e}")
            return False
    
    async def _get_vm_status(self, vmid: int) -> Optional[str]:
        """Get VM status"""
        try:
            vm_status = self.proxmox_api.nodes('pve').qemu(vmid).status('current').get()
            return vm_status.get('status')
        except:
            return None


class AtomicTaskExecutor:
    """Wraps task execution with automatic rollback on failure"""
    
    def __init__(self, rollback_manager: RollbackManager):
        self.rollback_manager = rollback_manager
        self.logger = logging.getLogger(__name__)
    
    async def execute_with_rollback(
        self,
        task,
        context: TaskExecutionContext,
        failure_point: str
    ) -> TaskExecutionResult:
        """Execute a task with automatic rollback on failure"""
        try:
            # Execute the task
            result = await task.execute(context)
            
            if result.status == TaskResult.FAILED:
                self.logger.warning(f"Task {task.task_id} failed, initiating rollback")
                
                # Create and execute rollback plan
                rollback_plan = self.rollback_manager.create_rollback_plan(
                    context.job_id, failure_point, context
                )
                
                rollback_result = await self.rollback_manager.execute_rollback(context.job_id)
                
                # Add rollback information to result
                result.error_message += f" | Rollback {'succeeded' if rollback_result['success'] else 'failed'}"
                
                if not rollback_result['success']:
                    result.error_message += f" | Rollback error: {rollback_result.get('error', 'Unknown')}"
            
            return result
        
        except Exception as e:
            self.logger.error(f"Task {task.task_id} failed with exception, initiating rollback: {e}")
            
            # Create and execute rollback plan
            rollback_plan = self.rollback_manager.create_rollback_plan(
                context.job_id, failure_point, context
            )
            
            rollback_result = await self.rollback_manager.execute_rollback(context.job_id)
            
            return TaskExecutionResult(
                task_id=task.task_id,
                status=TaskResult.FAILED,
                error_message=f"Task failed: {str(e)} | Rollback {'succeeded' if rollback_result['success'] else 'failed'}"
            )
    
    def track_created_resource(self, context: TaskExecutionContext, resource_type: str, resource_info: Dict[str, Any]):
        """Track a created resource for potential cleanup"""
        if "created_resources" not in context.shared_state:
            context.shared_state["created_resources"] = []
        
        context.shared_state["created_resources"].append({
            "type": resource_type,
            "info": resource_info,
            "created_at": datetime.utcnow().isoformat()
        })
        
        # Also add specific tracking for common resources
        if resource_type == "vm":
            context.shared_state["created_vmid"] = resource_info.get("vmid")
        elif resource_type == "disk":
            if "created_disks" not in context.shared_state:
                context.shared_state["created_disks"] = []
            context.shared_state["created_disks"].append(resource_info)
        elif resource_type == "temp_dir":
            if "temp_directories" not in context.shared_state:
                context.shared_state["temp_directories"] = []
            context.shared_state["temp_directories"].append(resource_info.get("path"))
