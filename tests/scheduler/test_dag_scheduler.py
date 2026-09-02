import subprocess
from pathlib import Path
import pytest

from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk
from task.dag import TaskDAG, DAGCycleError, DAGDependencyError
from task.locks import ResourceLockManager
from task.scheduler import DAGScheduler
from core.runtime import DSHRuntime


@pytest.fixture
def temp_git_repo(tmp_path: Path):
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "agent@test.local"], cwd=repo, check=True)

    # Initial files
    (repo / "Player.cs").write_text("public class Player { void Update() {} }\n", encoding="utf-8")
    (repo / "UI.cs").write_text("public class UI { void Draw() {} }\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)
    return repo


def test_dag_cycle_detection():
    dag = TaskDAG()
    dag.add_task(TaskDefinition(task_id="T1", title="Task 1", allowed_files=["A.cs"], dependencies=["T2"]))
    dag.add_task(TaskDefinition(task_id="T2", title="Task 2", allowed_files=["B.cs"], dependencies=["T1"]))

    with pytest.raises(DAGCycleError):
        dag.topological_sort()


def test_dag_missing_dependency():
    dag = TaskDAG()
    dag.add_task(TaskDefinition(task_id="T1", title="Task 1", allowed_files=["A.cs"], dependencies=["T_NON_EXISTENT"]))

    with pytest.raises(DAGDependencyError):
        dag.topological_sort()


def test_resource_locks():
    lock_mgr = ResourceLockManager()
    t1 = TaskDefinition(task_id="T1", title="Task 1", allowed_files=["Player.cs"])
    t2 = TaskDefinition(task_id="T2", title="Task 2", allowed_files=["Player.cs"])
    t3 = TaskDefinition(task_id="T3", title="Task 3", allowed_files=["UI.cs"])

    # T1 acquires Player.cs
    assert lock_mgr.acquire(t1) is True

    # T2 cannot acquire Player.cs concurrently
    assert lock_mgr.can_acquire(t2) is False
    assert lock_mgr.acquire(t2) is False

    # T3 can acquire UI.cs (no conflict)
    assert lock_mgr.can_acquire(t3) is True
    assert lock_mgr.acquire(t3) is True

    # After T1 releases, T2 can acquire
    lock_mgr.release(t1)
    assert lock_mgr.can_acquire(t2) is True
    assert lock_mgr.acquire(t2) is True


def test_dag_sequential_execution_success(temp_git_repo: Path):
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)

    dag = TaskDAG()
    t1 = TaskDefinition(task_id="T1", title="Update Player", allowed_files=["Player.cs"])
    t2 = TaskDefinition(task_id="T2", title="Update UI", allowed_files=["UI.cs"], dependencies=["T1"])
    dag.add_task(t1)
    dag.add_task(t2)

    patches = {
        "T1": PatchProposal(
            patches=[FilePatch(file="Player.cs", hunks=[PatchHunk(old_text="void Update() {}", new_text="void Update() { /* T1 */ }")])]
        ),
        "T2": PatchProposal(
            patches=[FilePatch(file="UI.cs", hunks=[PatchHunk(old_text="void Draw() {}", new_text="void Draw() { /* T2 */ }")])]
        ),
    }

    scheduler = DAGScheduler(dag, runtime)
    summary = scheduler.run_sequential(patches)

    assert summary.success is True
    assert summary.completed_tasks == ["T1", "T2"]
    assert len(summary.failed_tasks) == 0


def test_dag_aborts_on_failure(temp_git_repo: Path):
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)

    dag = TaskDAG()
    t1 = TaskDefinition(task_id="T1", title="Failing Task", allowed_files=["Player.cs"])
    t2 = TaskDefinition(task_id="T2", title="Dependent Task", allowed_files=["UI.cs"], dependencies=["T1"])
    dag.add_task(t1)
    dag.add_task(t2)

    # T1 provides invalid patch (old_text mismatch)
    patches = {
        "T1": PatchProposal(
            patches=[FilePatch(file="Player.cs", hunks=[PatchHunk(old_text="invalid_old_text()", new_text="")])]
        ),
        "T2": PatchProposal(
            patches=[FilePatch(file="UI.cs", hunks=[PatchHunk(old_text="void Draw() {}", new_text="void Draw() { /* redraw */ }")])]
        ),
    }

    scheduler = DAGScheduler(dag, runtime)
    summary = scheduler.run_sequential(patches)

    assert summary.success is False
    assert summary.failed_tasks == ["T1"]
    assert summary.aborted_tasks == ["T2"]
    assert runtime.ws.is_clean() is True
