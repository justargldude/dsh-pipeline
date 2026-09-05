import time
from unittest.mock import MagicMock
import pytest

from task.dag import TaskDAG
from task.scheduler import DAGScheduler
from task.schema import PatchProposal, TaskDefinition, TransactionResult
from core.runtime import DSHRuntime


def test_parallel_failed_leaf_task_not_redispatched():
    dag = TaskDAG()
    dag.add_task(TaskDefinition(task_id="A", title="Failing Task A", allowed_files=["A.cs"]))
    dag.add_task(TaskDefinition(task_id="B", title="Slow Task B", allowed_files=["B.cs"]))
    dag.add_task(TaskDefinition(task_id="C", title="Slow Task C", allowed_files=["C.cs"]))
    dag.add_task(TaskDefinition(task_id="D", title="Slow Task D", allowed_files=["D.cs"]))

    execution_counts = {"A": 0, "B": 0, "C": 0, "D": 0}

    def mock_execute(task_def: TaskDefinition, proposal: PatchProposal) -> TransactionResult:
        execution_counts[task_def.task_id] += 1
        if task_def.task_id == "A":
            return TransactionResult(
                task_id="A",
                success=False,
                failure_type="TEST_FAILURE",
                error_message="Simulated failure in Task A",
            )
        time.sleep(0.1)
        return TransactionResult(
            task_id=task_def.task_id,
            success=True,
        )

    runtime = MagicMock(spec=DSHRuntime)
    runtime.execute_transaction.side_effect = mock_execute

    scheduler = DAGScheduler(dag, runtime)
    proposals = {
        "A": MagicMock(spec=PatchProposal),
        "B": MagicMock(spec=PatchProposal),
        "C": MagicMock(spec=PatchProposal),
        "D": MagicMock(spec=PatchProposal),
    }

    summary = scheduler.run_parallel(proposals, max_workers=4)

    # Assert each task executes exactly once
    assert execution_counts == {"A": 1, "B": 1, "C": 1, "D": 1}

    # Assert no task appears in both completed and failed lists
    assert set(summary.completed_tasks).isdisjoint(set(summary.failed_tasks))

    # Assert failed_tasks has no duplicate entries and contains A
    assert summary.failed_tasks == ["A"]
    assert set(summary.completed_tasks) == {"B", "C", "D"}
    assert summary.success is False
