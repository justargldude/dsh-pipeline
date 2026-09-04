import subprocess
from pathlib import Path
import pytest

from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk, RiskLevel
from recovery.classifier import FailureType
from validation.regression import BaseRegressionValidator, MockRegressionValidator
from core.runtime import DSHRuntime


@pytest.fixture
def temp_git_repo(tmp_path: Path):
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "agent@test.local"], cwd=repo, check=True)

    test_file = repo / "Player.cs"
    test_file.write_text("public class Player {\n    public void Update() {}\n}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)
    return repo


def test_validation_all_tiers_pass(temp_git_repo: Path):
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)

    task = TaskDefinition(
        task_id="T_VAL_01",
        title="Full 5-tier validation pass",
        allowed_files=["Player.cs"],
    )

    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() {\n        // Tier pass\n    }")]
            )
        ]
    )

    res = runtime.execute_transaction(task, proposal)
    assert res.success is True
    assert res.commit_hash is not None


def test_validation_t2_behavioral_failure(temp_git_repo: Path):
    # Behavioral validator that passes on baseline but fails post-patch
    # (a behavioral regression actually caused by the patch). Updated per
    # contract change: T2 now compares post-patch failures against the
    # baseline, so a fixture failing identically in both captures is a
    # pre-existing failure, not a regression.
    from validation.behavioral import BaseBehavioralValidator, BehavioralCheckResult

    class PostPatchBehavioralValidator(BaseBehavioralValidator):
        def __init__(self):
            self.calls = 0

        def validate_behavior(self, repo_path: Path, task_id: str) -> BehavioralCheckResult:
            self.calls += 1
            if self.calls == 1:
                return BehavioralCheckResult(success=True, output="Baseline behavioral clean")
            return BehavioralCheckResult(
                success=False,
                failures=["Hook failed to trigger in smoke test."],
                output="Behavioral test failure.",
            )

    failing_behavioral = PostPatchBehavioralValidator()
    runtime = DSHRuntime(temp_git_repo, behavioral_validator=failing_behavioral, dry_run=False, test_mode=True)

    task = TaskDefinition(
        task_id="T_VAL_02",
        title="Behavioral regression test",
        allowed_files=["Player.cs"],
    )

    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() {\n        // Bad hook\n    }")]
            )
        ]
    )

    res = runtime.execute_transaction(task, proposal)
    assert res.success is False
    assert res.failure_type == FailureType.BEHAVIORAL.value
    assert "Hook failed to trigger" in res.error_message

    # Ensure rollback happened
    assert "// Bad hook" not in (temp_git_repo / "Player.cs").read_text()
    assert runtime.ws.is_clean() is True


def test_validation_t3_regression_failure(temp_git_repo: Path):
    # Regression validator that passes on baseline but fails post-patch (introducing new regression)
    from validation.regression import RegressionCheckResult
    
    class PostPatchRegressionValidator(BaseRegressionValidator):
        def __init__(self):
            self.calls = 0

        def validate_regression(self, repo_path: Path) -> RegressionCheckResult:
            self.calls += 1
            if self.calls == 1:
                return RegressionCheckResult(success=True, output="Baseline clean")
            return RegressionCheckResult(
                success=False,
                broken_tests=["PlayerInventoryTest.TestDropItem: Crash"],
                output="Regression failure",
            )

    failing_regression = PostPatchRegressionValidator()
    runtime = DSHRuntime(temp_git_repo, regression_validator=failing_regression, dry_run=False, test_mode=True)

    task = TaskDefinition(
        task_id="T_VAL_03",
        title="Regression failure test",
        allowed_files=["Player.cs"],
    )

    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() {\n        // Broken test\n    }")]
            )
        ]
    )

    res = runtime.execute_transaction(task, proposal)
    assert res.success is False
    assert res.failure_type == FailureType.REGRESSION.value
    assert "T3 Regression Failure" in res.error_message

    # Workspace clean
    assert runtime.ws.is_clean() is True


def test_validation_t4_risk_failure(temp_git_repo: Path):
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)

    task = TaskDefinition(
        task_id="T_VAL_04",
        title="Risk detection test",
        allowed_files=["Player.cs"],
        risk=RiskLevel.MEDIUM,  # Not HIGH risk -> unsafe operations forbidden
    )

    # Patch with stackalloc and raw P/Invoke
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() {\n        DangerousCall();\n    }\n    [DllImport(\"kernel32\")] static extern void DangerousCall();\n")]
            )
        ]
    )

    res = runtime.execute_transaction(task, proposal)
    assert res.success is False
    assert res.failure_type == FailureType.SCOPE_VIOLATION.value
    assert "T4 Risk Violation" in res.error_message
    assert "DllImport" in res.error_message


