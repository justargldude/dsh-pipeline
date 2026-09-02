import subprocess
from pathlib import Path
import pytest

from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk
from core.runtime import DSHRuntime
from core.workspace import (
    WorkspaceManager,
    TransactionWorktree,
    WorktreeStagingError,
    WorktreeCleanupError,
    generate_transaction_id,
    parse_porcelain_v1_z,
    parse_diff_cached_name_only_z,
)
from core.state import TransactionState, FileStatus
from build.sandbox import MockBuildRunner, BuildResult, BuildErrorDetail
from recovery.classifier import FailureType


@pytest.fixture
def temp_git_repo(tmp_path: Path):
    """Creates an isolated clean Git repository for testing."""
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "agent@test.local"], cwd=repo, check=True)

    # Initial commit
    test_file = repo / "Player.cs"
    test_file.write_text("public class Player {\n    public void Update() {}\n}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)
    return repo


# 1. Isolated worktree creation
def test_isolated_worktree_creation(temp_git_repo: Path):
    ws = WorkspaceManager(temp_git_repo)
    head = ws.get_head_commit()
    tx_id = generate_transaction_id("T_WT_01")

    tx_wt = ws.create_transaction_worktree(tx_id, base_commit=head)
    try:
        assert tx_wt.worktree_path.exists()
        assert (tx_wt.worktree_path / ".git").exists()
        assert tx_wt.get_head_commit() == head
        assert tx_wt.base_commit == head
        assert tx_wt.metadata.state == TransactionState.WORKTREE_READY
        assert (tx_wt.worktree_path / "Player.cs").exists()
    finally:
        ws.remove_transaction_worktree(tx_wt)
        assert not tx_wt.worktree_path.exists()


# 2. Transaction operates inside its own worktree
def test_transaction_operates_inside_its_own_worktree(temp_git_repo: Path):
    paths_seen_by_builder = []

    class PathCheckingBuildRunner(MockBuildRunner):
        def build(self, repo_path: Path) -> BuildResult:
            paths_seen_by_builder.append(repo_path)
            return BuildResult(success=True, exit_code=0)

    runner = PathCheckingBuildRunner(should_succeed=True)
    runtime = DSHRuntime(temp_git_repo, build_runner=runner)

    task = TaskDefinition(
        task_id="T_ISO_01",
        title="Verify worktree build path",
        allowed_files=["Player.cs"],
    )
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() { /* isolated */ }")],
            )
        ]
    )

    result = runtime.execute_transaction(task, proposal)
    assert result.success is True
    assert len(paths_seen_by_builder) >= 2  # baseline + post-apply
    for seen_path in paths_seen_by_builder:
        # Build path must NOT be the main workspace path; it must be an isolated worktree path
        assert seen_path != temp_git_repo
        assert ".git" in str(seen_path) or "dsh_worktrees" in str(seen_path)


# 3. Two transactions can coexist without modifying each other
def test_two_transactions_can_coexist_without_mutual_interference(temp_git_repo: Path):
    ws = WorkspaceManager(temp_git_repo)
    head = ws.get_head_commit()

    tx1 = ws.create_transaction_worktree("tx_coexist_1", base_commit=head)
    tx2 = ws.create_transaction_worktree("tx_coexist_2", base_commit=head)

    try:
        # Modify tx1
        (tx1.worktree_path / "Player.cs").write_text("public class Player { /* tx1 */ }", encoding="utf-8")
        (tx1.worktree_path / "tx1_only.txt").write_text("tx1 file", encoding="utf-8")

        # Modify tx2 differently
        (tx2.worktree_path / "Player.cs").write_text("public class Player { /* tx2 */ }", encoding="utf-8")
        (tx2.worktree_path / "tx2_only.txt").write_text("tx2 file", encoding="utf-8")

        # Verify tx1
        assert "/* tx1 */" in (tx1.worktree_path / "Player.cs").read_text()
        assert (tx1.worktree_path / "tx1_only.txt").exists()
        assert not (tx1.worktree_path / "tx2_only.txt").exists()

        # Verify tx2
        assert "/* tx2 */" in (tx2.worktree_path / "Player.cs").read_text()
        assert (tx2.worktree_path / "tx2_only.txt").exists()
        assert not (tx2.worktree_path / "tx1_only.txt").exists()

        # Verify main repository is completely untouched
        assert "public void Update() {}" in (temp_git_repo / "Player.cs").read_text()
        assert not (temp_git_repo / "tx1_only.txt").exists()
        assert not (temp_git_repo / "tx2_only.txt").exists()
    finally:
        ws.remove_transaction_worktree(tx1)
        ws.remove_transaction_worktree(tx2)


# 4. Failure cleanup: worktree safely removed
def test_failure_cleanup_discards_worktree_safely(temp_git_repo: Path):
    failing_runner = MockBuildRunner(
        should_succeed=False,
        errors=[BuildErrorDetail(message="Compilation failed")]
    )
    runtime = DSHRuntime(temp_git_repo, build_runner=failing_runner)

    task = TaskDefinition(task_id="T_FAIL_01", title="Failing build task", allowed_files=["Player.cs"])
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() { invalid }")],
            )
        ]
    )

    result = runtime.execute_transaction(task, proposal)
    assert result.success is False
    assert result.worktree_path is not None
    # Worktree directory is removed from disk
    assert not Path(result.worktree_path).exists()
    assert result.integration_status == "ROLLED_BACK"
    assert runtime.ws.is_clean() is True


# 5. Unrelated main workspace modification survives
def test_unrelated_main_workspace_modification_survives(temp_git_repo: Path):
    # Main workspace has an uncommitted modification
    unrelated_file = temp_git_repo / "Unrelated.cs"
    unrelated_file.write_text("public class Unrelated { /* user WIP */ }", encoding="utf-8")
    subprocess.run(["git", "add", "Unrelated.cs"], cwd=temp_git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "Add Unrelated"], cwd=temp_git_repo, check=True)

    # User modifies Unrelated.cs without committing
    unrelated_file.write_text("public class Unrelated { /* user DIRTY edits */ }", encoding="utf-8")

    failing_runner = MockBuildRunner(should_succeed=False, errors=[BuildErrorDetail(message="Build fail")])
    runtime = DSHRuntime(temp_git_repo, build_runner=failing_runner)

    task = TaskDefinition(task_id="T_SURVIVE_01", title="Task with dirty main", allowed_files=["Player.cs"])
    proposal = PatchProposal(
        patches=[FilePatch(file="Player.cs", hunks=[PatchHunk(old_text="public void Update() {}", new_text="public void Update() { /* fail */ }")])]
    )

    result = runtime.execute_transaction(task, proposal)
    assert result.success is False

    # Main workspace dirty modification is 100% untouched and intact
    assert unrelated_file.read_text(encoding="utf-8") == "public class Unrelated { /* user DIRTY edits */ }"


# 6. Unrelated untracked main file survives
def test_unrelated_untracked_main_file_survives(temp_git_repo: Path):
    untracked = temp_git_repo / "user_notes.txt"
    untracked.write_text("Important user notes", encoding="utf-8")

    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)
    task = TaskDefinition(task_id="T_UNTRACKED_01", title="Task with untracked file in main", allowed_files=["Player.cs"])
    proposal = PatchProposal(
        patches=[FilePatch(file="Player.cs", hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() { /* ok */ }")])]
    )

    result = runtime.execute_transaction(task, proposal)
    # Transaction executes and leaves untracked file untouched
    assert untracked.exists()
    assert untracked.read_text(encoding="utf-8") == "Important user notes"


# 7. Exact staging
def test_exact_staging(temp_git_repo: Path):
    ws = WorkspaceManager(temp_git_repo)
    head = ws.get_head_commit()
    tx_wt = ws.create_transaction_worktree("tx_exact_staging", base_commit=head)

    try:
        # Modify expected file and create unexpected stray file
        (tx_wt.worktree_path / "Player.cs").write_text("public class Player { /* exact */ }", encoding="utf-8")
        (tx_wt.worktree_path / "stray_file.txt").write_text("stray content", encoding="utf-8")

        # Stage ONLY Player.cs
        tx_wt.stage_exact(["Player.cs"])

        # Check staged files
        diff_bytes = tx_wt._run_git_bytes("diff", "--cached", "--name-only", "-z")
        staged = parse_diff_cached_name_only_z(diff_bytes)
        assert staged == ["Player.cs"]
        assert "stray_file.txt" not in staged

        # Staging invalid unexpected path raises WorktreeStagingError
        with pytest.raises(WorktreeStagingError):
            tx_wt.stage_exact(["NonExistent.cs"])
    finally:
        ws.remove_transaction_worktree(tx_wt)


# 8. Unexpected generated file causes failure (fail closed)
def test_unexpected_generated_file_causes_failure(temp_git_repo: Path):
    class PollutingBuildRunner(MockBuildRunner):
        def build(self, repo_path: Path) -> BuildResult:
            # Tool generates an untracked file not allowed by config
            (repo_path / "unexpected_output.bin").write_text("generated binary junk")
            return BuildResult(success=True, exit_code=0)

    runner = PollutingBuildRunner(should_succeed=True)
    runtime = DSHRuntime(temp_git_repo, build_runner=runner)

    task = TaskDefinition(task_id="T_POLLUTE_01", title="Task with unexpected tooling output", allowed_files=["Player.cs"])
    proposal = PatchProposal(
        patches=[FilePatch(file="Player.cs", hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() { /* clean */ }")])]
    )

    result = runtime.execute_transaction(task, proposal)
    assert result.success is False
    assert result.failure_type == FailureType.SCOPE_VIOLATION.value
    assert "UNEXPECTED_CHANGES" in result.error_message
    assert "unexpected_output.bin" in result.error_message
    assert not (temp_git_repo / "unexpected_output.bin").exists()


# 9. Robust Git status parsing
def test_robust_git_status_parsing():
    # Test standard records
    raw_status = (
        b" M src/Player.cs\x00"
        b"A  src/New File With Spaces.cs\x00"
        b"?? untracked_\xc3\xa9.txt\x00"
        b" D removed/file.cs\x00"
        b"R  new/renamed file.cs\x00old/renamed file.cs\x00"
    )
    statuses = parse_porcelain_v1_z(raw_status)
    assert len(statuses) == 5

    assert statuses[0].status_code == " M"
    assert statuses[0].path == "src/Player.cs"
    assert statuses[0].old_path is None

    assert statuses[1].status_code == "A "
    assert statuses[1].path == "src/New File With Spaces.cs"

    assert statuses[2].status_code == "??"
    assert statuses[2].path == "untracked_é.txt"

    assert statuses[3].status_code == " D"
    assert statuses[3].path == "removed/file.cs"

    assert statuses[4].status_code == "R "
    assert statuses[4].path == "new/renamed file.cs"
    assert statuses[4].old_path == "old/renamed file.cs"


# 10. Base HEAD is recorded
def test_base_head_is_recorded(temp_git_repo: Path):
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)
    initial_head = runtime.ws.get_head_commit()

    task = TaskDefinition(task_id="T_BASE_HEAD", title="Verify Base Head Recorded", allowed_files=["Player.cs"])
    proposal = PatchProposal(
        patches=[FilePatch(file="Player.cs", hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() { /* base */ }")])]
    )

    result = runtime.execute_transaction(task, proposal)
    assert result.success is True
    assert result.base_commit == initial_head
    assert result.commit_hash != initial_head


# 11. Main HEAD movement is detected
def test_main_head_movement_detected_stale_base(temp_git_repo: Path):
    ws = WorkspaceManager(temp_git_repo)
    head0 = ws.get_head_commit()

    # Create transaction worktree on head0
    tx_wt = ws.create_transaction_worktree("tx_stale_test", base_commit=head0)

    try:
        # Advance main branch with another commit
        (temp_git_repo / "Other.cs").write_text("public class Other {}", encoding="utf-8")
        subprocess.run(["git", "add", "Other.cs"], cwd=temp_git_repo, check=True)
        subprocess.run(["git", "commit", "-m", "Advance main HEAD"], cwd=temp_git_repo, check=True)
        head1 = ws.get_head_commit()
        assert head0 != head1

        # Complete commit in worktree
        (tx_wt.worktree_path / "Player.cs").write_text("public class Player { /* from stale base */ }", encoding="utf-8")
        tx_wt.stage_exact(["Player.cs"])
        tx_commit = tx_wt.commit("T_STALE", "Commit on stale base")

        # Integration detects STALE_BASE
        status = ws.integrate_transaction(tx_wt, tx_commit)
        assert status == "STALE_BASE"

        # Main HEAD remains head1 without silent overwrite
        assert ws.get_head_commit() == head1
    finally:
        ws.remove_transaction_worktree(tx_wt)


# 12. Transaction ID collision is prevented
def test_transaction_id_collision_prevention():
    ids = [generate_transaction_id("T_COLLISION") for _ in range(500)]
    assert len(ids) == len(set(ids))
    for tid in ids:
        assert tid.startswith("tx_T_COLLISION_")
        assert len(tid) > len("tx_T_COLLISION_")


# 13. Cleanup failure reported explicitly
def test_cleanup_failure_reported_explicitly(temp_git_repo: Path, monkeypatch):
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)

    def failing_remove(*args, **kwargs):
        raise WorktreeCleanupError("Simulated disk error during worktree removal")

    monkeypatch.setattr(runtime.ws, "remove_transaction_worktree", failing_remove)

    task = TaskDefinition(task_id="T_CLEANUP_ERR", title="Cleanup failure test", allowed_files=["Player.cs"])
    proposal = PatchProposal(
        patches=[FilePatch(file="Player.cs", hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() { /* cleanup fail */ }")])]
    )

    result = runtime.execute_transaction(task, proposal)
    assert result.success is False
    assert result.cleanup_error is not None
    assert "Simulated disk error" in result.cleanup_error
    assert "CLEANUP_FAILED" in result.error_message
