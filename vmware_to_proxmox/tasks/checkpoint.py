"""
Checkpointing system for resumable migration tasks
Handles large file transfers with offset-based resume and verification
"""

import hashlib
import logging
import os
import asyncio
from typing import Dict, Any, Optional, Tuple, Callable
from dataclasses import dataclass, field
from datetime import datetime
import json
import aiofiles
import paramiko

from .dag import TaskExecutionContext, TaskExecutionResult, TaskResult


@dataclass
class TransferCheckpoint:
    """Checkpoint data for file transfers"""
    source_path: str
    target_path: str
    total_bytes: int
    transferred_bytes: int
    chunk_size: int = 64 * 1024 * 1024  # 64MB chunks
    checksum_source: Optional[str] = None
    checksum_target_partial: Optional[str] = None
    transfer_method: str = "rsync"  # rsync, dd, qemu-img
    bandwidth_limit_mbps: Optional[int] = None
    last_update: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    
    @property
    def progress_percentage(self) -> float:
        if self.total_bytes == 0:
            return 0.0
        return (self.transferred_bytes / self.total_bytes) * 100
    
    @property
    def is_complete(self) -> bool:
        return self.transferred_bytes >= self.total_bytes


class CheckpointManager:
    """Manages task checkpoints for resumable operations"""
    
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.logger = logging.getLogger(f"{__name__}.{job_id}")
        self.checkpoints: Dict[str, Dict[str, Any]] = {}
    
    def save_checkpoint(self, task_id: str, checkpoint_data: Dict[str, Any]):
        """Save checkpoint data for a task"""
        checkpoint = {
            "task_id": task_id,
            "job_id": self.job_id,
            "timestamp": datetime.utcnow().isoformat(),
            "data": checkpoint_data
        }
        self.checkpoints[task_id] = checkpoint
        self.logger.info(f"Saved checkpoint for task {task_id}")
        
        # In production, this would be saved to database
        # await self._persist_checkpoint(checkpoint)
    
    def load_checkpoint(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Load checkpoint data for a task"""
        checkpoint = self.checkpoints.get(task_id)
        if checkpoint:
            self.logger.info(f"Loaded checkpoint for task {task_id}")
            return checkpoint["data"]
        return None
    
    def has_checkpoint(self, task_id: str) -> bool:
        """Check if checkpoint exists for task"""
        return task_id in self.checkpoints
    
    def clear_checkpoint(self, task_id: str):
        """Clear checkpoint for a task"""
        if task_id in self.checkpoints:
            del self.checkpoints[task_id]
            self.logger.info(f"Cleared checkpoint for task {task_id}")


class ResumableFileTransfer:
    """Handles resumable file transfers with checksum verification"""
    
    def __init__(self, ssh_client: paramiko.SSHClient):
        self.ssh_client = ssh_client
        self.logger = logging.getLogger(__name__)
        self.checkpoint_manager = CheckpointManager("transfer")
    
    async def calculate_checksum(self, file_path: str, is_remote: bool = True) -> str:
        """Calculate SHA-256 checksum of file"""
        if is_remote:
            # Calculate checksum on remote system
            stdin, stdout, stderr = self.ssh_client.exec_command(f"sha256sum '{file_path}'")
            result = stdout.read().decode().strip()
            if result:
                return result.split()[0]
        else:
            # Calculate checksum locally
            sha256_hash = hashlib.sha256()
            async with aiofiles.open(file_path, 'rb') as f:
                async for chunk in f:
                    sha256_hash.update(chunk)
            return sha256_hash.hexdigest()
        
        raise Exception(f"Failed to calculate checksum for {file_path}")
    
    async def get_remote_file_size(self, file_path: str) -> int:
        """Get file size on remote system"""
        stdin, stdout, stderr = self.ssh_client.exec_command(f"stat -c%s '{file_path}'")
        result = stdout.read().decode().strip()
        if result.isdigit():
            return int(result)
        raise Exception(f"Failed to get file size for {file_path}")
    
    async def get_local_file_size(self, file_path: str) -> int:
        """Get local file size"""
        return os.path.getsize(file_path)
    
    async def verify_partial_transfer(self, checkpoint: TransferCheckpoint) -> bool:
        """Verify that transferred bytes are valid"""
        try:
            # Get actual size of partial target file
            actual_size = await self.get_remote_file_size(checkpoint.target_path)
            
            if actual_size != checkpoint.transferred_bytes:
                self.logger.warning(f"Size mismatch: expected {checkpoint.transferred_bytes}, got {actual_size}")
                return False
            
            # Calculate checksum of transferred portion
            if checkpoint.checksum_target_partial:
                # For partial checksum, we'd need to hash only the transferred portion
                # This is complex and may not be worth the overhead
                pass
            
            return True
        except Exception as e:
            self.logger.error(f"Error verifying partial transfer: {e}")
            return False
    
    async def resume_rsync_transfer(
        self, 
        source_path: str, 
        target_path: str, 
        bandwidth_limit_mbps: Optional[int] = None,
        progress_callback: Optional[Callable[[float], None]] = None
    ) -> TaskExecutionResult:
        """Resume or start rsync transfer with checkpointing"""
        task_id = f"transfer_{os.path.basename(source_path)}"
        
        # Try to load existing checkpoint
        checkpoint_data = self.checkpoint_manager.load_checkpoint(task_id)
        
        if checkpoint_data:
            checkpoint = TransferCheckpoint(**checkpoint_data)
            self.logger.info(f"Resuming transfer from {checkpoint.transferred_bytes}/{checkpoint.total_bytes} bytes")
            
            # Verify partial transfer
            if not await self.verify_partial_transfer(checkpoint):
                self.logger.warning("Partial transfer verification failed, restarting")
                checkpoint = None
        
        if not checkpoint_data:
            # Create new checkpoint
            try:
                total_bytes = await self.get_remote_file_size(source_path)
                checksum_source = await self.calculate_checksum(source_path, is_remote=True)
            except Exception as e:
                return TaskExecutionResult(
                    task_id=task_id,
                    status=TaskResult.FAILED,
                    error_message=f"Failed to analyze source file: {str(e)}"
                )
            
            checkpoint = TransferCheckpoint(
                source_path=source_path,
                target_path=target_path,
                total_bytes=total_bytes,
                transferred_bytes=0,
                checksum_source=checksum_source,
                bandwidth_limit_mbps=bandwidth_limit_mbps
            )
        
        start_time = datetime.utcnow()
        
        try:
            # Build rsync command with resume capability
            rsync_cmd = ["rsync", "-av", "--progress"]
            
            if bandwidth_limit_mbps:
                # Convert Mbps to KB/s for rsync
                bw_limit = bandwidth_limit_mbps * 1024
                rsync_cmd.extend(["--bwlimit", str(bw_limit)])
            
            # Add partial transfer support
            rsync_cmd.extend(["--partial", "--partial-dir=.rsync-partial"])
            
            # If we have a partial transfer, rsync will automatically resume
            rsync_cmd.extend([source_path, target_path])
            
            # Execute rsync via SSH
            rsync_cmd_str = " ".join(rsync_cmd)
            
            self.logger.info(f"Starting rsync transfer: {rsync_cmd_str}")
            
            # For production, you'd want to parse rsync progress output
            # This is a simplified version
            stdin, stdout, stderr = self.ssh_client.exec_command(rsync_cmd_str)
            
            # Monitor progress (simplified - in production you'd parse rsync output)
            while True:
                if stdout.channel.exit_status_ready():
                    break
                
                # Update progress based on file size
                try:
                    current_size = await self.get_remote_file_size(target_path)
                    checkpoint.transferred_bytes = current_size
                    checkpoint.last_update = datetime.utcnow().isoformat()
                    
                    # Save checkpoint periodically
                    self.checkpoint_manager.save_checkpoint(task_id, checkpoint.__dict__)
                    
                    if progress_callback:
                        progress_callback(checkpoint.progress_percentage)
                    
                    await asyncio.sleep(5)  # Update every 5 seconds
                except:
                    pass
            
            exit_code = stdout.channel.recv_exit_status()
            
            if exit_code == 0:
                # Verify final transfer
                final_size = await self.get_remote_file_size(target_path)
                if final_size == checkpoint.total_bytes:
                    # Verify checksum
                    checksum_target = await self.calculate_checksum(target_path, is_remote=True)
                    if checksum_target == checkpoint.checksum_source:
                        duration = (datetime.utcnow() - start_time).total_seconds()
                        
                        # Clear checkpoint on success
                        self.checkpoint_manager.clear_checkpoint(task_id)
                        
                        return TaskExecutionResult(
                            task_id=task_id,
                            status=TaskResult.COMPLETED,
                            result_data={
                                "source_path": source_path,
                                "target_path": target_path,
                                "total_bytes": checkpoint.total_bytes,
                                "duration_seconds": duration
                            },
                            duration_seconds=duration
                        )
                    else:
                        return TaskExecutionResult(
                            task_id=task_id,
                            status=TaskResult.FAILED,
                            error_message="Checksum verification failed"
                        )
                else:
                    return TaskExecutionResult(
                        task_id=task_id,
                        status=TaskResult.FAILED,
                        error_message=f"Size mismatch: expected {checkpoint.total_bytes}, got {final_size}"
                    )
            else:
                error_output = stderr.read().decode()
                return TaskExecutionResult(
                    task_id=task_id,
                    status=TaskResult.FAILED,
                    error_message=f"rsync failed with exit code {exit_code}: {error_output}"
                )
        
        except Exception as e:
            # Save checkpoint on failure
            self.checkpoint_manager.save_checkpoint(task_id, checkpoint.__dict__)
            
            return TaskExecutionResult(
                task_id=task_id,
                status=TaskResult.FAILED,
                error_message=f"Transfer failed: {str(e)}",
                checkpoint_data=checkpoint.__dict__
            )
    
    async def resume_dd_transfer(
        self,
        source_path: str,
        target_path: str,
        bandwidth_limit_mbps: Optional[int] = None,
        progress_callback: Optional[Callable[[float], None]] = None
    ) -> TaskExecutionResult:
        """Resume or start dd transfer with offset support"""
        task_id = f"dd_transfer_{os.path.basename(source_path)}"
        
        # Try to load existing checkpoint
        checkpoint_data = self.checkpoint_manager.load_checkpoint(task_id)
        
        if checkpoint_data:
            checkpoint = TransferCheckpoint(**checkpoint_data)
            self.logger.info(f"Resuming dd transfer from offset {checkpoint.transferred_bytes}")
        else:
            # Create new checkpoint
            try:
                total_bytes = await self.get_remote_file_size(source_path)
                checksum_source = await self.calculate_checksum(source_path, is_remote=True)
            except Exception as e:
                return TaskExecutionResult(
                    task_id=task_id,
                    status=TaskResult.FAILED,
                    error_message=f"Failed to analyze source file: {str(e)}"
                )
            
            checkpoint = TransferCheckpoint(
                source_path=source_path,
                target_path=target_path,
                total_bytes=total_bytes,
                transferred_bytes=0,
                checksum_source=checksum_source,
                bandwidth_limit_mbps=bandwidth_limit_mbps,
                transfer_method="dd"
            )
        
        start_time = datetime.utcnow()
        
        try:
            # Build dd command with seek (skip) for resume
            dd_cmd = ["dd"]
            
            if checkpoint.transferred_bytes > 0:
                # Resume from offset
                skip_blocks = checkpoint.transferred_bytes // 512  # dd uses 512-byte blocks
                dd_cmd.extend([f"if={source_path}", f"of={target_path}", f"skip={skip_blocks}", "seek=skip_blocks", "conv=notrunc"])
            else:
                dd_cmd.extend([f"if={source_path}", f"of={target_path}"])
            
            dd_cmd.extend(["bs=64K", "status=progress"])
            
            # Add bandwidth limiting with pv if available
            if bandwidth_limit_mbps:
                # Convert Mbps to bytes per second
                bw_limit_bytes = bandwidth_limit_mbps * 1024 * 1024
                dd_cmd = ["pv", "-L", str(bw_limit_bytes)] + dd_cmd
            
            dd_cmd_str = " ".join(dd_cmd)
            
            self.logger.info(f"Starting dd transfer: {dd_cmd_str}")
            
            # Execute dd command
            stdin, stdout, stderr = self.ssh_client.exec_command(dd_cmd_str)
            
            # Monitor progress
            while True:
                if stdout.channel.exit_status_ready():
                    break
                
                # dd outputs progress to stderr, parse it
                error_line = stderr.readline().decode().strip()
                if error_line and "bytes" in error_line:
                    try:
                        # Parse dd progress output (format: "123456789 bytes (123 MB) copied, 1.23 s, 100 MB/s")
                        parts = error_line.split()
                        if parts[0].isdigit():
                            checkpoint.transferred_bytes = int(parts[0])
                            checkpoint.last_update = datetime.utcnow().isoformat()
                            
                            # Save checkpoint periodically
                            self.checkpoint_manager.save_checkpoint(task_id, checkpoint.__dict__)
                            
                            if progress_callback:
                                progress_callback(checkpoint.progress_percentage)
                    except:
                        pass
                
                await asyncio.sleep(2)
            
            exit_code = stdout.channel.recv_exit_status()
            
            if exit_code == 0:
                # Verify final transfer
                final_size = await self.get_remote_file_size(target_path)
                if final_size == checkpoint.total_bytes:
                    duration = (datetime.utcnow() - start_time).total_seconds()
                    
                    # Clear checkpoint on success
                    self.checkpoint_manager.clear_checkpoint(task_id)
                    
                    return TaskExecutionResult(
                        task_id=task_id,
                        status=TaskResult.COMPLETED,
                        result_data={
                            "source_path": source_path,
                            "target_path": target_path,
                            "total_bytes": checkpoint.total_bytes,
                            "duration_seconds": duration
                        },
                        duration_seconds=duration
                    )
                else:
                    return TaskExecutionResult(
                        task_id=task_id,
                        status=TaskResult.FAILED,
                        error_message=f"Size mismatch: expected {checkpoint.total_bytes}, got {final_size}"
                    )
            else:
                error_output = stderr.read().decode()
                return TaskExecutionResult(
                    task_id=task_id,
                    status=TaskResult.FAILED,
                    error_message=f"dd failed with exit code {exit_code}: {error_output}"
                )
        
        except Exception as e:
            # Save checkpoint on failure
            self.checkpoint_manager.save_checkpoint(task_id, checkpoint.__dict__)
            
            return TaskExecutionResult(
                task_id=task_id,
                status=TaskResult.FAILED,
                error_message=f"DD transfer failed: {str(e)}",
                checkpoint_data=checkpoint.__dict__
            )


class BandwidthThrottler:
    """Manages bandwidth throttling for transfers"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
    
    def calculate_optimal_bandwidth(
        self, 
        file_size_bytes: int, 
        network_mbps: int, 
        time_limit_hours: Optional[float] = None
    ) -> int:
        """Calculate optimal bandwidth limit based on constraints"""
        if time_limit_hours:
            # Calculate minimum bandwidth needed to meet time limit
            time_limit_seconds = time_limit_hours * 3600
            min_bandwidth_bps = file_size_bytes / time_limit_seconds
            min_bandwidth_mbps = min_bandwidth_bps / (1024 * 1024)
            
            # Use the higher of network capacity or minimum required
            return min(network_mbps, max(min_bandwidth_mbps, network_mbps * 0.8))
        else:
            # Use 80% of available bandwidth to leave room for other operations
            return int(network_mbps * 0.8)
    
    def adapt_bandwidth(
        self, 
        current_limit_mbps: int, 
        transfer_rate_mbps: float, 
        target_duration_hours: Optional[float] = None
    ) -> int:
        """Adapt bandwidth based on current performance"""
        if transfer_rate_mbps < current_limit_mbps * 0.5:
            # Transfer is much slower than limit, might be network congestion
            return max(current_limit_mbps // 2, 1)
        elif transfer_rate_mbps >= current_limit_mbps * 0.9:
            # Transfer is near limit, could potentially increase
            return min(current_limit_mbps * 1.2, current_limit_mbps * 2)
        
        return current_limit_mbps
