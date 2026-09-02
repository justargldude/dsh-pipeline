import concurrent.futures
import logging
import threading
import time
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
    results: Dict[str, TransactionResult] = Field(default_factory=dict)
    success: bool = False


class DAGScheduler:
    def __init__(self, dag: TaskDAG, runtime: DSHRuntime):
        self.dag = dag
        self.runtime = runtime
        self.lock_mgr = ResourceLockManager()
        self._integration_lock = threading.Lock()

    def run_sequential(
        self,
        patch_proposals: Dict[str, PatchProposal],
    ) -> DAGExecutionSummary:
        """Executes tasks in topologically sorted order.
        
        If a task fails, only its descendants are blocked/aborted;
        independent branches proceed to completion.
        """
        ordered_tasks = self.dag.topological_sort()
        summary = DAGExecutionSummary(total_tasks=len(ordered_tasks))
        blocked_task_ids: Set[str] = set()

        for task in ordered_tasks:
            # If task was blocked by a failed dependency
            if task.task_id in blocked_task_ids or any(dep not in summary.completed_tasks for dep in task.dependencies):
                logger.warning(f"[TASK_BLOCKED] Skipping task '{task.task_id}' because an upstream dependency failed.")
                if task.task_id not in summary.aborted_tasks:
                    summary.aborted_tasks.append(task.task_id)
                blocked_task_ids.update(self.dag.get_descendants(task.task_id))
                continue

            logger.info(f"==> [SCHEDULER] Dispatching task: [{task.task_id}] '{task.title}'")

            if not self.lock_mgr.acquire(task):
                logger.error(f"[LOCK_ERROR] Cannot acquire file locks for {task.task_id}")
                summary.failed_tasks.append(task.task_id)
                blocked_task_ids.update(self.dag.get_descendants(task.task_id))
                continue

            proposal = patch_proposals.get(task.task_id)
            if not proposal:
                logger.error(f"[MISSING_PATCH] No patch proposal provided for task {task.task_id}")
                self.lock_mgr.release(task)
                summary.failed_tasks.append(task.task_id)
                blocked_task_ids.update(self.dag.get_descendants(task.task_id))
                continue

            res: TransactionResult = self.runtime.execute_transaction(task, proposal)
            self.lock_mgr.release(task)
            summary.results[task.task_id] = res

            if res.success:
                summary.completed_tasks.append(task.task_id)
            else:
                summary.failed_tasks.append(task.task_id)
                descendants = self.dag.get_descendants(task.task_id)
                blocked_task_ids.update(descendants)
                for desc in descendants:
                    if desc not in summary.aborted_tasks and desc not in summary.failed_tasks and desc not in summary.completed_tasks:
                        summary.aborted_tasks.append(desc)
                logger.error(
                    f"[TASK_FAILED] Task {task.task_id} failed ({res.failure_type}). "
                    f"Blocked downstream tasks: {descendants or 'None'}."
                )

        # Ensure all un-executed tasks are accounted for
        executed = set(summary.completed_tasks + summary.failed_tasks + summary.aborted_tasks)
        for t in ordered_tasks:
            if t.task_id not in executed:
                summary.aborted_tasks.append(t.task_id)

        summary.success = len(summary.completed_tasks) == summary.total_tasks
        return summary

    def run_parallel(
        self,
        patch_proposals: Dict[str, PatchProposal],
        max_workers: int = 4,
    ) -> DAGExecutionSummary:
        """Executes tasks concurrently in isolated worktrees where transaction isolation is guaranteed.
        
        Concurrency rules:
        1. Each concurrent task runs in its own isolated worktree with atomic budget reservations.
        2. File locks prevent two concurrent tasks from modifying overlapping files.
        3. Failures block only dependent tasks; independent branches run concurrently.
        4. Main repository integrations are serialized cleanly.
        """
        ordered_tasks = self.dag.topological_sort()
        summary = DAGExecutionSummary(total_tasks=len(ordered_tasks))

        completed_set: Set[str] = set()
        failed_set: Set[str] = set()
        aborted_set: Set[str] = set()
        blocked_set: Set[str] = set()
        active_set: Set[str] = set()

        state_lock = threading.Lock()
        task_map = {t.task_id: t for t in ordered_tasks}

        def execute_worker(task_def: TaskDefinition, prop: PatchProposal) -> TransactionResult:
            logger.info(f"[PARALLEL_WORKER] Starting execution for task [{task_def.task_id}]")
            try:
                # Runtime execute_transaction runs in isolated worktree
                result = self.runtime.execute_transaction(task_def, prop)
                return result
            finally:
                with state_lock:
                    self.lock_mgr.release(task_def)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_task: Dict[concurrent.futures.Future, str] = {}

            while True:
                with state_lock:
                    # Check if all tasks finished
                    total_done = len(completed_set) + len(failed_set) + len(aborted_set)
                    if total_done == len(ordered_tasks):
                        break

                    # Find ready tasks
                    ready_tasks = self.dag.get_ready_tasks(
                        completed_task_ids=completed_set,
                        active_task_ids=active_set,
                        blocked_task_ids=blocked_set,
                    )

                    dispatched_any = False
                    for task in ready_tasks:
                        if task.task_id in active_set:
                            continue

                        # Check file lock
                        if not self.lock_mgr.acquire(task):
                            continue

                        prop = patch_proposals.get(task.task_id)
                        if not prop:
                            self.lock_mgr.release(task)
                            failed_set.add(task.task_id)
                            summary.failed_tasks.append(task.task_id)
                            desc = self.dag.get_descendants(task.task_id)
                            blocked_set.update(desc)
                            for d in desc:
                                if d not in aborted_set and d not in failed_set and d not in completed_set:
                                    aborted_set.add(d)
                                    summary.aborted_tasks.append(d)
                            continue

                        active_set.add(task.task_id)
                        fut = executor.submit(execute_worker, task, prop)
                        future_to_task[fut] = task.task_id
                        dispatched_any = True

                # If no tasks are currently active and none could be dispatched, handle deadlock/blocked
                if not future_to_task:
                    with state_lock:
                        for t in ordered_tasks:
                            if t.task_id not in completed_set and t.task_id not in failed_set and t.task_id not in aborted_set:
                                aborted_set.add(t.task_id)
                                summary.aborted_tasks.append(t.task_id)
                    break

                # Wait for at least one future to complete
                done, _ = concurrent.futures.wait(
                    list(future_to_task.keys()),
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )

                for fut in done:
                    task_id = future_to_task.pop(fut)
                    try:
                        res: TransactionResult = fut.result()
                    except Exception as e:
                        res = TransactionResult(
                            task_id=task_id,
                            success=False,
                            failure_type="PROCESS_FAILURE",
                            error_message=str(e),
                        )

                    with state_lock:
                        active_set.discard(task_id)
                        summary.results[task_id] = res

                        if res.success:
                            completed_set.add(task_id)
                            summary.completed_tasks.append(task_id)
                        else:
                            failed_set.add(task_id)
                            summary.failed_tasks.append(task_id)
                            desc = self.dag.get_descendants(task_id)
                            blocked_set.update(desc)
                            for d in desc:
                                if d not in aborted_set and d not in failed_set and d not in completed_set:
                                    aborted_set.add(d)
                                    summary.aborted_tasks.append(d)

        summary.success = len(summary.completed_tasks) == summary.total_tasks
        return summary
