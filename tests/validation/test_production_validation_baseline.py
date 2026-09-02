import subprocess
from pathlib import Path
import pytest

from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk
from core.config import PipelineConfig
from core.runtime import DSHRuntime
from core.state import PipelineEvent
from build.sandbox import MockBuildRunner, SubprocessBuildRunner, BuildResult, BuildErrorDetail
from validation.behavioral import BaseBehavioralValidator, MockBehavioralValidator, SubprocessBehavioralValidator
from validation.regression import BaseRegressionValidator, MockRegressionValidator, SubprocessRegressionValidator, RegressionCheckResult
from validation.baseline import BaselineManager, BaselineState
from recovery.classifier import FailureType


@pytest.fixture
def temp_git_repo(tmp_path: Path):
    """Creates an isolated clean Git repository for testing."""
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


# 1, 2, 3: Production runtime does NOT instantiate mock runners
def test_production_runtime_does_not_instantiate_mocks(temp_git_repo: Path):
    """Verify that normal production runtime does not silently instantiate mock runners."""
    runtime = DSHRuntime(temp_git_repo)  # default production mode (test_mode=False)
    assert runtime.build_runner is None
    assert runtime.behavioral_validator is None
    assert runtime.regression_validator is None
    assert runtime.validation_pipeline.behavioral_validator is None
    assert runtime.validation_pipeline.regression_validator is None


# 4: Missing production build configuration fails closed
def test_missing_production_build_configuration_fails_closed(temp_git_repo: Path):
    """Verify that attempting to execute a transaction without build configuration fails closed."""
    runtime = DSHRuntime(temp_git_repo)  # No build runner, no build_cmd
    task = TaskDefinition(
        task_id="T_NO_CFG",
        title="Task without build config",
        allowed_files=["Player.cs"],
    )
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() { /* test */ }")]
            )
        ]
    )

    result = runtime.execute_transaction(task, proposal)
    assert result.success is False
    assert result.failure_type == FailureType.CONFIG_MISSING.value
    assert "BUILD_CONFIGURATION_MISSING" in result.error_message
    assert runtime.ws.is_clean() is True


# 5: Baseline is captured BEFORE patch application
def test_baseline_captured_before_patch_application(temp_git_repo: Path):
    """Verify that baseline build/validation is captured before any patch is applied."""
    captured_events = []
    
    class RecordingBuildRunner(MockBuildRunner):
        def build(self, repo_path: Path) -> BuildResult:
            content = (repo_path / "Player.cs").read_text()
            captured_events.append("Patched" if "PATCHED" in content else "Baseline")
            return BuildResult(success=True, exit_code=0)

    runner = RecordingBuildRunner(should_succeed=True)
    runtime = DSHRuntime(temp_git_repo, build_runner=runner)

    task = TaskDefinition(task_id="T_BASE_01", title="Baseline sequence test", allowed_files=["Player.cs"])
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() {\n        // PATCHED\n    }")]
            )
        ]
    )

    result = runtime.execute_transaction(task, proposal)
    assert result.success is True
    assert PipelineEvent.BASELINE_CAPTURED.value in result.events
    assert captured_events == ["Baseline", "Patched"]
    assert result.base_commit is not None


# 6: Passing baseline + failing post-patch build = regression
def test_passing_baseline_failing_post_patch_is_regression(temp_git_repo: Path):
    """Case B: Baseline BUILD PASSED -> After patch BUILD FAILED => Regression!"""
    class StateDependentBuildRunner(MockBuildRunner):
        def build(self, repo_path: Path) -> BuildResult:
            content = (repo_path / "Player.cs").read_text()
            if "BrokenMethodCall" in content:
                return BuildResult(
                    success=False,
                    exit_code=1,
                    errors=[BuildErrorDetail(message="Player.cs(2,5): error CS0103: The name 'BrokenMethodCall' does not exist in the current context")],
                    raw_output="Build Failed",
                )
            return BuildResult(success=True, exit_code=0, raw_output="Build Succeeded")

    runner = StateDependentBuildRunner()
    runtime = DSHRuntime(temp_git_repo, build_runner=runner)

    task = TaskDefinition(task_id="T_REG_01", title="Build regression test", allowed_files=["Player.cs"])
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() {\n        BrokenMethodCall();\n    }")]
            )
        ]
    )

    result = runtime.execute_transaction(task, proposal)
    assert result.success is False
    assert result.failure_type == FailureType.TYPE_SEMANTIC.value
    assert "Build regression detected" in result.error_message
    assert runtime.ws.is_clean() is True


# 7: Failing baseline + same failure after patch != automatically a new regression
def test_failing_baseline_failing_post_patch_not_new_regression(temp_git_repo: Path):
    """Case A: Baseline BUILD FAILED -> After patch BUILD FAILED => Pre-existing failure, not regression."""
    failing_runner = MockBuildRunner(
        should_succeed=False,
        errors=[BuildErrorDetail(message="CS1002: Pre-existing syntax error")]
    )
    runtime = DSHRuntime(temp_git_repo, build_runner=failing_runner)

    task = TaskDefinition(task_id="T_BASE_FAIL", title="Pre-existing fail test", allowed_files=["Player.cs"])
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() {\n        // touch\n    }")]
            )
        ]
    )

    result = runtime.execute_transaction(task, proposal)
    assert result.success is False
    assert "Pre-existing build failure" in result.error_message
    assert "Build regression detected" not in result.error_message


# 8 & 9: Baseline caching and invalidation
def test_baseline_cache_key_and_invalidation(temp_git_repo: Path):
    """Verify that baseline key contains HEAD, config, environment and invalidates on changes."""
    mgr = BaselineManager(enable_cache=True)
    runner_a = MockBuildRunner(should_succeed=True)
    runner_b = SubprocessBuildRunner(build_cmd=["dotnet", "build"])

    from core.workspace import WorkspaceManager
    ws = WorkspaceManager(temp_git_repo)

    # 1. Capture baseline with runner_a
    base1 = mgr.get_or_capture_baseline(temp_git_repo, ws, runner_a)
    assert base1.cache_key in mgr._cache

    # 2. Re-capturing with runner_a returns cached instance
    base1_cached = mgr.get_or_capture_baseline(temp_git_repo, ws, runner_a)
    assert base1_cached is base1

    # 3. Changing runner config produces different cache key
    base2 = mgr.get_or_capture_baseline(temp_git_repo, ws, runner_b)
    assert base2.cache_key != base1.cache_key

    # 4. Creating a commit in the repo changes HEAD -> changes cache key
    (temp_git_repo / "new_file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=temp_git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "Second commit"], cwd=temp_git_repo, check=True)

    base3 = mgr.get_or_capture_baseline(temp_git_repo, ws, runner_a)
    assert base3.cache_key != base1.cache_key
    assert base3.base_commit != base1.base_commit


# Structured Test / Regression comparison - Case D (Pre-existing failure)
def test_regression_comparison_case_d_pre_existing_failures_pass(temp_git_repo: Path):
    """Case D: Baseline has test A failing. Post-patch has test A failing -> Pass (no new regression)."""
    class PreExistingFailureValidator(BaseRegressionValidator):
        def validate_regression(self, repo_path: Path) -> RegressionCheckResult:
            return RegressionCheckResult(
                success=False,
                broken_tests=["TestPreExistingFailed"],
                output="1 failed",
            )

    runner = MockBuildRunner(should_succeed=True)
    runtime = DSHRuntime(
        temp_git_repo,
        build_runner=runner,
        regression_validator=PreExistingFailureValidator(),
    )

    task = TaskDefinition(task_id="T_NO_NEW_REG", title="No new regression", allowed_files=["Player.cs"])
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() {\n        // touch\n    }")]
            )
        ]
    )

    result = runtime.execute_transaction(task, proposal)
    assert result.success is True


# Structured Test / Regression comparison - Case C (New broken test)
def test_regression_comparison_case_c_new_failures_fail(temp_git_repo: Path):
    """Case C: Baseline has test A failing. Post-patch has test A + test B failing -> Fail (new regression)."""
    class NewFailureValidator(BaseRegressionValidator):
        def __init__(self):
            self.calls = 0

        def validate_regression(self, repo_path: Path) -> RegressionCheckResult:
            self.calls += 1
            if self.calls == 1:
                return RegressionCheckResult(success=False, broken_tests=["TestPreExistingFailed"])
            else:
                return RegressionCheckResult(success=False, broken_tests=["TestPreExistingFailed", "TestNewRegression"])

    runner = MockBuildRunner(should_succeed=True)
    runtime = DSHRuntime(
        temp_git_repo,
        build_runner=runner,
        regression_validator=NewFailureValidator(),
    )

    task = TaskDefinition(task_id="T_NEW_REG", title="New regression introduced", allowed_files=["Player.cs"])
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() {\n        // touch\n    }")]
            )
        ]
    )

    result = runtime.execute_transaction(task, proposal)
    assert result.success is False
    assert result.failure_type == FailureType.REGRESSION.value
    assert "TestNewRegression" in result.error_message
    assert "TestPreExistingFailed" not in result.error_message


# 10: Explicit mock injection still works in unit tests
def test_explicit_mock_injection_works(temp_git_repo: Path):
    """Verify that unit tests can explicitly inject mock runners and validators."""
    mock_build = MockBuildRunner(should_succeed=True)
    mock_beh = MockBehavioralValidator(should_succeed=True)
    mock_reg = MockRegressionValidator(should_succeed=True)

    runtime = DSHRuntime(
        temp_git_repo,
        build_runner=mock_build,
        behavioral_validator=mock_beh,
        regression_validator=mock_reg,
    )

    task = TaskDefinition(task_id="T_MOCK_INJECT", title="Mock injection", allowed_files=["Player.cs"])
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() {\n        int z = 1;\n    }")]
            )
        ]
    )

    result = runtime.execute_transaction(task, proposal)
    assert result.success is True
    assert result.commit_hash is not None


# Timeout handling in SubprocessBuildRunner and SubprocessRegressionValidator
def test_subprocess_runners_timeout_and_error_handling(temp_git_repo: Path):
    """Verify timeout and file not found handling in subprocess runners."""
    # Timeout in build runner
    runner_timeout = SubprocessBuildRunner(build_cmd=["sleep", "5"], timeout_seconds=1)
    res_timeout = runner_timeout.build(temp_git_repo)
    assert res_timeout.success is False
    assert "timed out" in res_timeout.raw_output

    # Executable not found in build runner
    runner_not_found = SubprocessBuildRunner(build_cmd=["non_existent_compiler_xyz_123"])
    res_not_found = runner_not_found.build(temp_git_repo)
    assert res_not_found.success is False
    assert res_not_found.exit_code == 127
    assert "not found" in res_not_found.raw_output

    # Timeout in regression validator
    reg_timeout = SubprocessRegressionValidator(test_cmd=["sleep", "5"], timeout_seconds=1)
    res_reg_timeout = reg_timeout.validate_regression(temp_git_repo)
    assert res_reg_timeout.success is False
    assert "timed out" in res_reg_timeout.output
