import pytest
from pathlib import Path
from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk
from safety.policy import SafetyPolicy
from safety.scope_guard import ScopeGuard, ScopeViolationError
from context.builder import ContextBuilder
from validation.pipeline import ValidationPipeline, ValidationTier
from build.sandbox import MockBuildRunner
from validation.regression import BaseRegressionValidator, RegressionCheckResult


def test_scope_guard_blocks_dev_modifying_test_file(tmp_path: Path):
    """SpecBench / EvilGenie Anti-Reward-Hacking:
    Dev agents must not be allowed to modify existing or new test files."""
    guard = ScopeGuard(policy=SafetyPolicy(forbid_dev_test_edits=True))
    
    # Target repo with test file
    test_file = tmp_path / "PlayerTest.cs"
    test_file.write_text("class PlayerTest {}", encoding="utf-8")
    
    task = TaskDefinition(
        task_id="T_TEST_LOCKDOWN",
        title="Malicious Test Tweak",
        allowed_files=["Player.cs"],
    )
    
    # Dev attempts to tamper with PlayerTest.cs
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="PlayerTest.cs",
                hunks=[PatchHunk(old_text="class PlayerTest {}", new_text="class PlayerTest { /* deleted asserts */ }")]
            )
        ]
    )
    
    with pytest.raises(ScopeViolationError) as exc_info:
        guard.validate(task, proposal, repo_root=tmp_path)
    
    assert "Test file modification forbidden for Dev agent" in str(exc_info.value)


def test_scope_guard_blocks_test_in_task_allowed_files(tmp_path: Path):
    """A task definition targeting a test file in allowed_files must be rejected immediately."""
    guard = ScopeGuard(policy=SafetyPolicy(forbid_dev_test_edits=True))
    
    task = TaskDefinition(
        task_id="T_TEST_LOCKDOWN_2",
        title="Allowed file with test",
        allowed_files=["tests/test_auth.py"],
    )
    proposal = PatchProposal(patches=[])
    
    with pytest.raises(ScopeViolationError) as exc_info:
        guard.validate(task, proposal, repo_root=tmp_path)
    
    assert "Test file in allowed_files forbidden for Dev agent" in str(exc_info.value)


def test_context_builder_filters_holdout_and_uses_fences():
    """ContextBuilder must strip holdout tests and render appropriate code fences."""
    builder = ContextBuilder()
    
    task = TaskDefinition(
        task_id="T_HOLDOUT_CTX",
        title="Calculator",
        allowed_files=["math.py"],
        visible_test_code="def test_add(): assert add(1, 2) == 3",
        holdout_test_code="def test_add_secret(): assert add(-1, -1) == -2",
        holdout_test_file="tests/test_secret.py",
    )
    
    snippets = {
        "math.py": "def add(a, b): return a + b",
        "tests/test_secret.py": "SECRET TEST LEAKED",
    }
    
    ctx = builder.build_context(task, snippets)
    
    # Holdout must be filtered out
    assert "SECRET TEST LEAKED" not in ctx
    assert "test_secret.py" not in ctx
    
    # Python code fence used
    assert "```python\ndef add(a, b): return a + b\n```" in ctx
    
    # Visible test spec present
    assert "TEST SPECIFICATION (BEHAVIORAL REQUIREMENTS)" in ctx
    assert "def test_add(): assert add(1, 2) == 3" in ctx


class DynamicRegressionValidator(BaseRegressionValidator):
    """A mock validator that checks for the existence and content of a holdout test file."""
    def validate_regression(self, repo_path: Path) -> RegressionCheckResult:
        holdout_path = repo_path / "tests/test_holdout.py"
        if holdout_path.exists() and "secret_assert" in holdout_path.read_text(encoding="utf-8"):
            # Holdout test was injected and ran!
            return RegressionCheckResult(success=False, broken_tests=["test_holdout_secret_failed"])
        return RegressionCheckResult(success=True)


def test_holdout_test_injected_pre_regression(tmp_path: Path):
    """Holdout tests must be injected right before T3 Regression validation."""
    pipeline = ValidationPipeline(regression_validator=DynamicRegressionValidator())
    
    task = TaskDefinition(
        task_id="T_HOLDOUT_INJECT",
        title="Holdout Injection Test",
        allowed_files=["calc.py"],
        holdout_test_code="def test_holdout(): assert secret_assert()",
        holdout_test_file="tests/test_holdout.py",
    )
    
    build_runner = MockBuildRunner(should_succeed=True)
    
    report = pipeline.validate_post_apply(
        task=task,
        repo_path=tmp_path,
        build_runner=build_runner,
    )
    
    # Regression validator must have caught the holdout test failure!
    assert not report.success
    assert report.failed_tier == ValidationTier.T3_REGRESSION
    assert "test_holdout_secret_failed" in report.error_message
    
    # Verify file was written to disk
    injected_file = tmp_path / "tests/test_holdout.py"
    assert injected_file.exists()
    assert "secret_assert" in injected_file.read_text(encoding="utf-8")
