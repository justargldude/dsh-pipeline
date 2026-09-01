import subprocess
from pathlib import Path
import pytest

from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk
from core.runtime import DSHRuntime
from core.workspace import WorkspaceManager
from build.sandbox import MockBuildRunner, BuildErrorDetail
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


def test_valid_patch_commit(temp_git_repo: Path):
    runtime = DSHRuntime(temp_git_repo, dry_run=False)

    task = TaskDefinition(
        task_id="T001",
        title="Hook Player.Update logic",
        allowed_files=["Player.cs"],
        max_lines_added=10,
        max_lines_deleted=5
    )

    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[
                    PatchHunk(
                        old_text="    public void Update() {}\n",
                        new_text="    public void Update() {\n        // Hooked\n    }\n"
                    )
                ]
            )
        ],
        reason="Add hook",
        confidence=0.95
    )

    result = runtime.execute_transaction(task, proposal)

    assert result.success is True
    assert result.commit_hash is not None
    assert result.failure_type is None

    # Check file content after commit
    content = (temp_git_repo / "Player.cs").read_text(encoding="utf-8")
    assert "// Hooked" in content

    # Verify Git commit message
    res = subprocess.run(["git", "log", "-1", "--pretty=%B"], cwd=temp_git_repo, capture_output=True, text=True)
    assert "[T001] Hook Player.Update logic" in res.stdout


def test_scope_violation_unallowed_file(temp_git_repo: Path):
    runtime = DSHRuntime(temp_git_repo, dry_run=False)

    task = TaskDefinition(
        task_id="T002",
        title="Modify Secret file",
        allowed_files=["Player.cs"],
        max_lines_added=10,
        max_lines_deleted=5
    )

    # Proposal touches secret.cs which is forbidden
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="secret.cs",
                hunks=[PatchHunk(old_text="", new_text="// malicious injection")]
            )
        ],
        reason="Bypass attempt",
        confidence=0.1
    )

    result = runtime.execute_transaction(task, proposal)

    assert result.success is False
    assert result.failure_type == FailureType.SCOPE_VIOLATION.value
    assert "not in allowed_files" in result.error_message

    # Ensure secret.cs was NOT created or committed
    assert not (temp_git_repo / "secret.cs").exists()
    ws = WorkspaceManager(temp_git_repo)
    assert ws.is_clean() is True


def test_scope_violation_lines_budget(temp_git_repo: Path):
    runtime = DSHRuntime(temp_git_repo, dry_run=False)

    task = TaskDefinition(
        task_id="T003",
        title="Modify Player with line limit overflow",
        allowed_files=["Player.cs"],
        max_lines_added=2,
        max_lines_deleted=1
    )

    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[
                    PatchHunk(
                        old_text="",
                        new_text="// line 1\n// line 2\n// line 3\n// line 4\n"
                    )
                ]
            )
        ]
    )

    result = runtime.execute_transaction(task, proposal)

    assert result.success is False
    assert result.failure_type == FailureType.SCOPE_VIOLATION.value
    assert "exceeded task limit" in result.error_message


def test_patch_invalid_hunk_mismatch(temp_git_repo: Path):
    runtime = DSHRuntime(temp_git_repo, dry_run=False)

    task = TaskDefinition(
        task_id="T004",
        title="Invalid old_text match",
        allowed_files=["Player.cs"]
    )

    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[
                    PatchHunk(
                        old_text="non_existent_function_call();",
                        new_text="// replace"
                    )
                ]
            )
        ]
    )

    result = runtime.execute_transaction(task, proposal)

    assert result.success is False
    assert result.failure_type == FailureType.PATCH_INVALID.value
    assert "Hunk old_text not found" in result.error_message


def test_build_failure_and_rollback(temp_git_repo: Path):
    failing_runner = MockBuildRunner(
        should_succeed=False,
        errors=[BuildErrorDetail(message="Player.cs(2,15): error CS1002: ; expected")]
    )
    runtime = DSHRuntime(temp_git_repo, build_runner=failing_runner, dry_run=False)

    task = TaskDefinition(
        task_id="T005",
        title="Syntax error fix attempt",
        allowed_files=["Player.cs"]
    )

    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="", new_text="// broken syntax\n")]
            )
        ]
    )

    result = runtime.execute_transaction(task, proposal)

    assert result.success is False
    assert result.failure_type == FailureType.SYNTAX.value

    content = (temp_git_repo / "Player.cs").read_text(encoding="utf-8")
    assert "// broken syntax" not in content

    ws = WorkspaceManager(temp_git_repo)
    assert ws.is_clean() is True


def test_dry_run_mode(temp_git_repo: Path):
    runtime = DSHRuntime(temp_git_repo, dry_run=True)

    task = TaskDefinition(
        task_id="T006",
        title="Dry-run verification",
        allowed_files=["Player.cs"]
    )

    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="", new_text="// dry run comment\n")]
            )
        ]
    )

    initial_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=temp_git_repo, capture_output=True, text=True).stdout.strip()
    result = runtime.execute_transaction(task, proposal)

    assert result.success is True
    assert result.dry_run is True

    current_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=temp_git_repo, capture_output=True, text=True).stdout.strip()
    assert initial_head == current_head

    content = (temp_git_repo / "Player.cs").read_text(encoding="utf-8")
    assert "// dry run comment" not in content


def test_anti_bypass_detection(temp_git_repo: Path):
    runtime = DSHRuntime(temp_git_repo, dry_run=False)

    task = TaskDefinition(
        task_id="T007",
        title="Test anti-bypass",
        allowed_files=["Player.cs"]
    )

    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="", new_text="#if FALSE\nvoid Dummy() {}\n#endif\n")]
            )
        ]
    )

    result = runtime.execute_transaction(task, proposal)
    assert result.success is False
    assert result.failure_type == FailureType.SCOPE_VIOLATION.value
    assert "Anti-bypass violation" in result.error_message
