"""
Directed Acyclic Graph (DAG) system for migration task orchestration
Provides atomic task execution, dependency management, and checkpointing
"""

import logging
from typing import List, Dict, Set, Optional, Any, Callable
from dataclasses import dataclass, field
from enum import Enum
import uuid
import asyncio
from datetime import datetime, timedelta

from ..schemas.postgresql_schema import task_type, task_status


class TaskResult(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    PAUSED = "paused"


@dataclass
class TaskDependency:
    task_id: str
    dependency_type: str = "success"  # "success", "completion", "failure"


@dataclass
class TaskExecutionContext:
    job_id: str
    hardware_spec: Dict[str, Any]
    config: Dict[str, Any]
    checkpoint_data: Dict[str, Any] = field(default_factory=dict)
    shared_state: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskExecutionResult:
    task_id: str
    status: TaskResult
    result_data: Dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None
    checkpoint_data: Dict[str, Any] = field(default_factory=dict)
    duration_seconds: Optional[float] = None
    can_resume: bool = True


class MigrationTask:
    """Base class for migration tasks"""
    
    def __init__(self, task_id: str, task_type: task_type, name: str, description: str = ""):
        self.task_id = task_id
        self.task_type = task_type
        self.name = name
        self.description = description
        self.dependencies: List[TaskDependency] = []
        self.priority = 0
        self.max_retries = 3
        self.retry_delay_seconds = 30
        self.estimated_duration_seconds = 0
        self.is_resumable = True
        self.logger = logging.getLogger(f"{__name__}.{task_id}")
    
    def add_dependency(self, task_id: str, dependency_type: str = "success"):
        """Add a dependency on another task"""
        self.dependencies.append(TaskDependency(task_id, dependency_type))
    
    def can_execute(self, completed_tasks: Set[str], failed_tasks: Set[str]) -> bool:
        """Check if this task can execute based on dependencies"""
        for dep in self.dependencies:
            if dep.dependency_type == "success" and dep.task_id not in completed_tasks:
                return False
            elif dep.dependency_type == "completion" and dep.task_id not in completed_tasks and dep.task_id not in failed_tasks:
                return False
            elif dep.dependency_type == "failure" and dep.task_id not in failed_tasks:
                return False
        return True
    
    async def execute(self, context: TaskExecutionContext) -> TaskExecutionResult:
        """Execute the task - to be implemented by subclasses"""
        raise NotImplementedError("Subclasses must implement execute method")
    
    async def resume(self, context: TaskExecutionContext) -> TaskExecutionResult:
        """Resume from checkpoint - default implementation calls execute"""
        if not self.is_resumable:
            return TaskExecutionResult(
                task_id=self.task_id,
                status=TaskResult.FAILED,
                error_message="Task is not resumable"
            )
        return await self.execute(context)
    
    def create_checkpoint(self, context: TaskExecutionContext, progress_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create checkpoint data for resuming"""
        return {
            "task_id": self.task_id,
            "timestamp": datetime.utcnow().isoformat(),
            "progress_data": progress_data,
            "context_snapshot": {
                "job_id": context.job_id,
                "shared_state": context.shared_state.copy()
            }
        }


class TaskGraph:
    """Manages the execution graph of migration tasks"""
    
    def __init__(self):
        self.tasks: Dict[str, MigrationTask] = {}
        self.logger = logging.getLogger(__name__)
    
    def add_task(self, task: MigrationTask):
        """Add a task to the graph"""
        self.tasks[task.task_id] = task
    
    def add_dependency(self, task_id: str, depends_on: str, dependency_type: str = "success"):
        """Add a dependency between tasks"""
        if task_id not in self.tasks or depends_on not in self.tasks:
            raise ValueError("Both tasks must exist in the graph")
        self.tasks[task_id].add_dependency(depends_on, dependency_type)
    
    def validate(self) -> List[str]:
        """Validate the DAG for cycles and missing dependencies"""
        errors = []
        
        # Check for missing dependencies
        for task_id, task in self.tasks.items():
            for dep in task.dependencies:
                if dep.task_id not in self.tasks:
                    errors.append(f"Task {task_id} depends on non-existent task {dep.task_id}")
        
        # Check for cycles using DFS
        visited = set()
        rec_stack = set()
        
        def has_cycle(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)
            
            for dep in self.tasks[node].dependencies:
                if dep.task_id not in visited:
                    if has_cycle(dep.task_id):
                        return True
                elif dep.task_id in rec_stack:
                    return True
            
            rec_stack.remove(node)
            return False
        
        for task_id in self.tasks:
            if task_id not in visited:
                if has_cycle(task_id):
                    errors.append(f"Cyclic dependency detected involving task {task_id}")
        
        return errors
    
    def get_ready_tasks(self, completed_tasks: Set[str], failed_tasks: Set[str]) -> List[MigrationTask]:
        """Get tasks that are ready to execute"""
        ready_tasks = []
        for task_id, task in self.tasks.items():
            if task_id not in completed_tasks and task_id not in failed_tasks:
                if task.can_execute(completed_tasks, failed_tasks):
                    ready_tasks.append(task)
        
        # Sort by priority (higher first)
        ready_tasks.sort(key=lambda t: t.priority, reverse=True)
        return ready_tasks
    
    def get_execution_order(self) -> List[str]:
        """Get a valid execution order (topological sort)"""
        in_degree = {task_id: 0 for task_id in self.tasks}
        
        # Calculate in-degrees
        for task_id, task in self.tasks.items():
            for dep in task.dependencies:
                in_degree[task_id] += 1
        
        # Queue of tasks with no dependencies
        queue = [task_id for task_id, degree in in_degree.items() if degree == 0]
        result = []
        
        while queue:
            current = queue.pop(0)
            result.append(current)
            
            # Reduce in-degree for dependent tasks
            for task_id, task in self.tasks.items():
                for dep in task.dependencies:
                    if dep.task_id == current:
                        in_degree[task_id] -= 1
                        if in_degree[task_id] == 0:
                            queue.append(task_id)
        
        if len(result) != len(self.tasks):
            raise ValueError("Graph has cycles - cannot determine execution order")
        
        return result
    
    def estimate_total_duration(self) -> int:
        """Estimate total execution time in seconds"""
        # Simple sum - could be improved with parallel execution analysis
        return sum(task.estimated_duration_seconds for task in self.tasks.values())


class TaskExecutor:
    """Executes tasks in the DAG with checkpointing and retry logic"""
    
    def __init__(self, task_graph: TaskGraph):
        self.task_graph = task_graph
        self.logger = logging.getLogger(__name__)
        self.completed_tasks: Set[str] = set()
        self.failed_tasks: Set[str] = set()
        self.running_tasks: Set[str] = set()
        self.task_results: Dict[str, TaskExecutionResult] = {}
    
    async def execute_graph(self, context: TaskExecutionContext) -> Dict[str, Any]:
        """Execute the entire task graph"""
        self.logger.info(f"Starting task graph execution for job {context.job_id}")
        
        start_time = datetime.utcnow()
        
        # Validate the graph first
        validation_errors = self.task_graph.validate()
        if validation_errors:
            return {
                "success": False,
                "error": "Graph validation failed",
                "validation_errors": validation_errors
            }
        
        # Execute until all tasks are completed or failed
        while len(self.completed_tasks) + len(self.failed_tasks) < len(self.task_graph.tasks):
            # Get ready tasks
            ready_tasks = self.task_graph.get_ready_tasks(self.completed_tasks, self.failed_tasks)
            
            if not ready_tasks:
                # Check if we're stuck (no ready tasks but not all completed)
                if self.running_tasks:
                    # Wait for running tasks to complete
                    await asyncio.sleep(1)
                    continue
                else:
                    # Deadlock - no tasks can proceed
                    remaining_tasks = set(self.task_graph.tasks.keys()) - self.completed_tasks - self.failed_tasks
                    return {
                        "success": False,
                        "error": "Execution deadlock",
                        "remaining_tasks": list(remaining_tasks)
                    }
            
            # Execute ready tasks (can be parallelized)
            execution_tasks = []
            for task in ready_tasks:
                self.running_tasks.add(task.task_id)
                execution_tasks.append(self._execute_task_with_retry(task, context))
            
            # Wait for current batch to complete
            if execution_tasks:
                await asyncio.gather(*execution_tasks, return_exceptions=True)
        
        end_time = datetime.utcnow()
        duration = (end_time - start_time).total_seconds()
        
        # Prepare final result
        success = len(self.failed_tasks) == 0
        
        return {
            "success": success,
            "duration_seconds": duration,
            "completed_tasks": list(self.completed_tasks),
            "failed_tasks": list(self.failed_tasks),
            "task_results": {
                task_id: {
                    "status": result.status.value,
                    "duration_seconds": result.duration_seconds,
                    "error_message": result.error_message
                }
                for task_id, result in self.task_results.items()
            }
        }
    
    async def _execute_task_with_retry(self, task: MigrationTask, context: TaskExecutionContext):
        """Execute a task with retry logic"""
        retry_count = 0
        
        while retry_count <= task.max_retries:
            try:
                # Check if we can resume from checkpoint
                if retry_count > 0 and task.is_resumable and context.checkpoint_data.get(task.task_id):
                    self.logger.info(f"Resuming task {task.task_id} (attempt {retry_count + 1})")
                    result = await task.resume(context)
                else:
                    self.logger.info(f"Executing task {task.task_id} (attempt {retry_count + 1})")
                    result = await task.execute(context)
                
                # Handle result
                if result.status == TaskResult.COMPLETED:
                    self.completed_tasks.add(task.task_id)
                    self.task_results[task.task_id] = result
                    self.logger.info(f"Task {task.task_id} completed successfully")
                    
                    # Update shared state
                    if result.result_data:
                        context.shared_state.update(result.result_data)
                    
                    # Save checkpoint
                    if result.checkpoint_data:
                        context.checkpoint_data[task.task_id] = result.checkpoint_data
                    
                    return
                
                elif result.status == TaskResult.FAILED:
                    if retry_count < task.max_retries:
                        retry_count += 1
                        self.logger.warning(f"Task {task.task_id} failed, retrying in {task.retry_delay_seconds}s (attempt {retry_count + 1})")
                        await asyncio.sleep(task.retry_delay_seconds)
                        continue
                    else:
                        self.failed_tasks.add(task.task_id)
                        self.task_results[task.task_id] = result
                        self.logger.error(f"Task {task.task_id} failed after {task.max_retries} retries")
                        return
                
                elif result.status == TaskResult.SKIPPED:
                    self.completed_tasks.add(task.task_id)
                    self.task_results[task.task_id] = result
                    self.logger.info(f"Task {task.task_id} skipped")
                    return
                
                else:
                    # For PAUSED or other states, just mark as running
                    self.logger.info(f"Task {task.task_id} in state {result.status.value}")
                    return
            
            except Exception as e:
                self.logger.error(f"Exception in task {task.task_id}: {e}")
                if retry_count < task.max_retries:
                    retry_count += 1
                    await asyncio.sleep(task.retry_delay_seconds)
                    continue
                else:
                    self.failed_tasks.add(task.task_id)
                    self.task_results[task.task_id] = TaskExecutionResult(
                        task_id=task.task_id,
                        status=TaskResult.FAILED,
                        error_message=str(e)
                    )
                    return
            
            finally:
                self.running_tasks.discard(task.task_id)
    
    def get_progress(self) -> Dict[str, Any]:
        """Get current execution progress"""
        total_tasks = len(self.task_graph.tasks)
        completed = len(self.completed_tasks)
        failed = len(self.failed_tasks)
        running = len(self.running_tasks)
        
        return {
            "total_tasks": total_tasks,
            "completed_tasks": completed,
            "failed_tasks": failed,
            "running_tasks": running,
            "progress_percentage": (completed / total_tasks * 100) if total_tasks > 0 else 0,
            "status": "completed" if completed + failed == total_tasks else "running"
        }
