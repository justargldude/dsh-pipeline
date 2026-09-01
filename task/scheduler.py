import logging
from typing import Any, Callable, Dict, List, Optional, Set
from pydantic import BaseModel, Field

from task.schema import TaskDefinition, PatchProposal, TransactionResult
from task.dag import TaskDAG
from task.locks import ResourceLockManager
from core.runtime import DSHRuntime

logger = logging.getLogger("dsh.scheduler")


class DAGExecutionSummary(BaseModel):
    total_tasks: int
    completed_tasks: List[str] = Field(default_factory=list)
    failed_tasks: List[str] = Field(default_factory=list)
    aborted_tasks: List[str] = Field(default_factory=list)
    success: bool = False


class DAGScheduler:
    def __init__(self, dag: TaskDAG, runtime: DSHRuntime):
        self.dag = dag
        self.runtime = runtime
        self.lock_mgr = ResourceLockManager()

    def run_sequential(
        self,
        patch_proposals: Dict[str, PatchProposal],
    ) -> DAGExecutionSummary:
        """Executes all tasks in topologically sorted order."""
        ordered_tasks = self.dag.topological_sort()
        summary = DAGExecutionSummary(total_tasks=len(ordered_tasks))

        for task in ordered_tasks:
            logger.info(f"==> [SCHEDULER] Dispatching task: [{task.task_id}] '{task.title}'")

            if not self.lock_mgr.acquire(task):
                logger.error(f"[LOCK_ERROR] Cannot acquire file locks for {task.task_id}")
                summary.failed_tasks.append(task.task_id)
                break

            proposal = patch_proposals.get(task.task_id)
            if not proposal:
                logger.error(f"[MISSING_PATCH] No patch proposal provided for task {task.task_id}")
                self.lock_mgr.release(task)
                summary.failed_tasks.append(task.task_id)
                break

            res: TransactionResult = self.runtime.execute_transaction(task, proposal)
            self.lock_mgr.release(task)

            if res.success:
                summary.completed_tasks.append(task.task_id)
            else:
                summary.failed_tasks.append(task.task_id)
                logger.error(f"[TASK_HALTED] Task {task.task_id} failed with {res.failure_type}. Aborting DAG.")
                break

        # Calculate aborted tasks
        executed = set(summary.completed_tasks + summary.failed_tasks)
        summary.aborted_tasks = [t.task_id for t in ordered_tasks if t.task_id not in executed]
        summary.success = len(summary.completed_tasks) == summary.total_tasks
        return summary
