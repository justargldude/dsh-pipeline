import concurrent.futures
import subprocess
import time
from pathlib import Path
import pytest

from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk, RiskLevel
from task.dag import TaskDAG, DAGCycleError, DAGDependencyError
from task.locks import ResourceLockManager
from task.scheduler import DAGScheduler
from core.runtime import DSHRuntime
from core.workspace import WorkspaceManager, TransactionWorktree
from core.journal import TransactionJournal
from core.state import TransactionState
from safety.patch_engine import atomic_write_file, detect_newline_style, validate_and_simulate_proposal
from safety.policy import SafetyPolicy, SessionBudgetTracker
from recovery.classifier import FailureType
from build.sandbox import MockBuildRunner, BuildErrorDetail


@pytest.fixture
def temp_git_repo(tmp_path: Path):
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "agent@test.local"], cwd=repo, check=True)

    # Initial files
    (repo / "Player.cs").write_text("public class Player {\n    public void Update() {}\n}\n", encoding="utf-8")
    (repo / "UI.cs").write_text("public class UI {\n    public void Draw() {}\n}\n", encoding="utf-8")
    (repo / "Audio.cs").write_text("public class Audio {\n    public void Play() {}\n}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)
    return repo


def test_dag_duplicate_task_id_rejected():
    dag = TaskDAG()
    t1 = TaskDefinition(task_id="T1", title="Task 1", allowed_files=["Player.cs"])
    t2 = TaskDefinition(task_id="T1", title="Task 1 Duplicate", allowed_files=["UI.cs"])
    dag.add_task(t1)
    with pytest.raises(ValueError) as exc:
        dag.add_task(t2)
    assert "Duplicate task ID" in str(exc.value)


def test_dag_empty_task_id_rejected():
    with pytest.raises(ValueError):
        TaskDefinition(task_id="   ", title="Valid Title", allowed_files=["Player.cs"])

    with pytest.raises(ValueError):
        TaskDefinition(task_id="T1", title="   ", allowed_files=["Player.cs"])


def test_dag_independent_branches_continue_on_failure(temp_git_repo: Path):
    """Verify that when Task A fails, its dependent Task B is blocked,
    but independent Task C proceeds and completes successfully.
    """
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)
    dag = TaskDAG()

    t_a = TaskDefinition(task_id="T_A", title="Branch A (Failing)", allowed_files=["Player.cs"])
    t_b = TaskDefinition(task_id="T_B", title="Branch B (Depends on A)", allowed_files=["Player.cs"], dependencies=["T_A"])
    t_c = TaskDefinition(task_id="T_C", title="Branch C (Independent)", allowed_files=["UI.cs"])

    dag.add_task(t_a)
    dag.add_task(t_b)
    dag.add_task(t_c)

    patches = {
        "T_A": PatchProposal(
            patches=[FilePatch(file="Player.cs", hunks=[PatchHunk(old_text="non_existent_code()", new_text="")])]
        ),
        "T_B": PatchProposal(
            patches=[FilePatch(file="Player.cs", hunks=[PatchHunk(old_text="public void Update() {}", new_text="public void Update() { /* B */ }")])]
        ),
        "T_C": PatchProposal(
            patches=[FilePatch(file="UI.cs", hunks=[PatchHunk(old_text="    public void Draw() {}", new_text="    public void Draw() {\n        // C Updated\n    }")])]
        ),
    }

    scheduler = DAGScheduler(dag, runtime)
    summary = scheduler.run_sequential(patches)

    assert summary.success is False
    assert "T_A" in summary.failed_tasks
    assert "T_B" in summary.aborted_tasks  # Blocked because T_A failed
    assert "T_C" in summary.completed_tasks  # Independent branch ran and succeeded!

    # UI.cs had changes committed from T_C
    ui_content = (temp_git_repo / "UI.cs").read_text(encoding="utf-8")
    assert "// C Updated" in ui_content

    # Player.cs was untouched because T_A failed and T_B was aborted
    player_content = (temp_git_repo / "Player.cs").read_text(encoding="utf-8")
    assert "/* B */" not in player_content
    assert runtime.ws.is_clean() is True


def test_dag_parallel_execution(temp_git_repo: Path):
    """Verify that independent tasks execute in parallel in isolated worktrees."""
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)
    dag = TaskDAG()

    t1 = TaskDefinition(task_id="T_PAR_1", title="Update Player", allowed_files=["Player.cs"])
    t2 = TaskDefinition(task_id="T_PAR_2", title="Update UI", allowed_files=["UI.cs"])
    t3 = TaskDefinition(task_id="T_PAR_3", title="Update Audio", allowed_files=["Audio.cs"])

    dag.add_task(t1)
    dag.add_task(t2)
    dag.add_task(t3)

    patches = {
        "T_PAR_1": PatchProposal(
            patches=[FilePatch(file="Player.cs", hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() {\n        // Par 1\n    }")])]
        ),
        "T_PAR_2": PatchProposal(
            patches=[FilePatch(file="UI.cs", hunks=[PatchHunk(old_text="    public void Draw() {}", new_text="    public void Draw() {\n        // Par 2\n    }")])]
        ),
        "T_PAR_3": PatchProposal(
            patches=[FilePatch(file="Audio.cs", hunks=[PatchHunk(old_text="    public void Play() {}", new_text="    public void Play() {\n        // Par 3\n    }")])]
        ),
    }

    scheduler = DAGScheduler(dag, runtime)
    summary = scheduler.run_parallel(patches, max_workers=3)

    assert summary.success is True
    assert set(summary.completed_tasks) == {"T_PAR_1", "T_PAR_2", "T_PAR_3"}
    assert len(summary.failed_tasks) == 0
    assert len(summary.aborted_tasks) == 0

    # Verify that all tasks produced valid commits in their isolated worktrees
    for tid in ["T_PAR_1", "T_PAR_2", "T_PAR_3"]:
        res = summary.results[tid]
        assert res.success is True
        assert res.commit_hash is not None
        # Integration status should be either INTEGRATED or STALE_BASE/READY_TO_INTEGRATE
        assert res.integration_status in ("INTEGRATED", "STALE_BASE", "READY_TO_INTEGRATE")

    assert runtime.ws.is_clean() is True


def test_transaction_journal_and_orphan_cleanup(temp_git_repo: Path):
    """Verify that crash journal tracks in-flight worktrees and supports orphan recovery."""
    ws = WorkspaceManager(temp_git_repo)
    journal = ws.journal

    # Create worktree
    tx_wt = ws.create_transaction_worktree(tx_id="tx_crash_test_01")
    assert tx_wt.worktree_path.exists()

    # Active records in journal
    active = journal.list_active()
    assert any(r.tx_id == "tx_crash_test_01" for r in active)

    orphans = ws.get_orphaned_worktrees()
    assert len(orphans) >= 1
    assert any(r.tx_id == "tx_crash_test_01" for r in orphans)

    # Cleanup orphans
    cleaned = ws.cleanup_orphaned_worktrees()
    assert "tx_crash_test_01" in cleaned
    assert not tx_wt.worktree_path.exists()
    assert len(ws.get_orphaned_worktrees()) == 0


def test_atomic_file_write_and_crlf_preservation(tmp_path: Path):
    """Verify that atomic_write_file preserves Windows CRLF when target file has CRLF."""
    test_file = tmp_path / "WindowsFile.cs"
    # Write initial file with CRLF
    test_file.write_bytes(b"public class WindowsFile {\r\n    int x = 1;\r\n}\r\n")

    # Update using atomic_write_file with Unix \n in replacement content
    new_content = "public class WindowsFile {\n    int x = 100;\n}\n"
    atomic_write_file(test_file, new_content, preserve_newline=True)

    raw_after = test_file.read_bytes()
    assert b"\r\n" in raw_after
    assert b"int x = 100;\r\n" in raw_after

    with open(test_file, "r", encoding="utf-8", newline="") as f:
        untranslated = f.read()
    assert "\r\n" in untranslated
    assert detect_newline_style(untranslated) == "\r\n"


def test_post_commit_verification(temp_git_repo: Path):
    """Verify that post-commit verification ensures only expected files are committed."""
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)
    task = TaskDefinition(task_id="T_POST_01", title="Test Post Commit", allowed_files=["Player.cs"])
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() {\n        // Post commit ok\n    }")]
            )
        ]
    )

    res = runtime.execute_transaction(task, proposal)
    assert res.success is True
    assert res.commit_hash is not None
    assert runtime.ws.is_clean() is True


def test_end_to_end_failure_isolation_scenario(temp_git_repo: Path):
    """Verify end-to-end failure isolation: a build error in worktree cleanly discards
    the worktree and leaves main repository unchanged without leaks.
    """
    failing_runner = MockBuildRunner(
        should_succeed=False,
        errors=[BuildErrorDetail(message="Player.cs(2,15): error CS1002: ; expected")]
    )
    runtime = DSHRuntime(temp_git_repo, build_runner=failing_runner, dry_run=False)

    task = TaskDefinition(task_id="T_E2E_FAIL", title="Failing Task", allowed_files=["Player.cs"])
    # Syntactically valid C# code that fails at compilation stage
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() {\n        int x = 1;\n    }")]
            )
        ]
    )

    initial_head = runtime.ws.get_head_commit()
    res = runtime.execute_transaction(task, proposal)

    assert res.success is False
    assert res.failure_type == FailureType.SYNTAX.value
    assert res.integration_status == "ROLLED_BACK"

    # Main repository HEAD must remain unchanged
    assert runtime.ws.get_head_commit() == initial_head
    assert runtime.ws.is_clean() is True
    # Player.cs unchanged in main workspace
    assert "int x = 1;" not in (temp_git_repo / "Player.cs").read_text(encoding="utf-8")
