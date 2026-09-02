import os
import signal
import subprocess
import time
from pathlib import Path
from typing import List, Optional
import httpx
import pytest

from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk, RiskLevel
from model.schemas import ModelRequest, ModelResponse, ModelType
from model.providers import (
    BaseModelProvider,
    MockModelProvider,
    OpenAICompatibleProvider,
)
from recovery.classifier import FailureClassifier, FailureType
from recovery.history import RecoveryHistory
from recovery.manager import RecoveryManager
from build.sandbox import (
    BuildResult,
    BuildErrorDetail,
    MockBuildRunner,
    SubprocessBuildRunner,
    run_hardened_command,
    terminate_process_tree,
)
from core.workspace import (
    WorkspaceManager,
    TransactionWorktree,
    WorkspaceError,
    WorktreeCleanupError,
    _run_git_subprocess,
)
from core.runtime import DSHRuntime
from safety.policy import SafetyPolicy, SessionBudgetTracker
from safety.scope_guard import ScopeGuard
from context.builder import ContextBuilder


@pytest.fixture
def temp_git_repo(tmp_path: Path):
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Phase 7 Tester"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tester@phase7.local"], cwd=repo, check=True)

    test_file = repo / "Player.cs"
    test_file.write_text("public class Player {\n    public void Update() {}\n}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)
    return repo


# 1. malformed model output -> MODEL_FORMAT_ERROR
def test_malformed_model_output_classified_as_format_error(temp_git_repo: Path):
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)
    context_builder = ContextBuilder()

    task = TaskDefinition(task_id="T_P7_01", title="Malformed format", allowed_files=["Player.cs"])
    provider = MockModelProvider(raw_response="This is plain text without any JSON or codeblocks.")

    res = runtime.execute_with_model(task, provider=provider, context_builder=context_builder)
    assert res.success is False
    assert res.failure_type == FailureType.MODEL_FORMAT_ERROR.value
    assert "not valid JSON" in res.error_message


# 2. invalid patch content -> MODEL_CONTENT_INVALID
def test_invalid_patch_content_classified_as_content_invalid(temp_git_repo: Path):
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)
    context_builder = ContextBuilder()

    task = TaskDefinition(task_id="T_P7_02", title="Invalid schema content", allowed_files=["Player.cs"])
    # JSON is valid, but missing 'patches' field or empty hunk semantics
    provider = MockModelProvider(raw_response='{"unexpected_key": "some_value"}')

    res = runtime.execute_with_model(task, provider=provider, context_builder=context_builder)
    assert res.success is False
    assert res.failure_type == FailureType.MODEL_CONTENT_INVALID.value


# 3. 429 -> RATE_LIMIT
def test_http_429_classified_as_rate_limit(temp_git_repo: Path):
    def mock_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Too Many Requests", headers={"Retry-After": "0.01"})

    client = httpx.Client(transport=httpx.MockTransport(mock_transport))
    provider = OpenAICompatibleProvider(
        api_key="test-key",
        client=client,
        max_retries=0,
    )
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)
    context_builder = ContextBuilder()

    task = TaskDefinition(task_id="T_P7_03", title="Rate limit test", allowed_files=["Player.cs"])
    res = runtime.execute_with_model(task, provider=provider, context_builder=context_builder)

    assert res.success is False
    assert res.failure_type == FailureType.RATE_LIMIT.value


# 4. network failure -> NETWORK_ERROR
def test_network_failure_classified_as_network_error(temp_git_repo: Path):
    def mock_transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused by peer")

    client = httpx.Client(transport=httpx.MockTransport(mock_transport))
    provider = OpenAICompatibleProvider(
        api_key="test-key",
        client=client,
        max_retries=0,
    )
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)
    context_builder = ContextBuilder()

    task = TaskDefinition(task_id="T_P7_04", title="Network failure test", allowed_files=["Player.cs"])
    res = runtime.execute_with_model(task, provider=provider, context_builder=context_builder)

    assert res.success is False
    assert res.failure_type == FailureType.NETWORK_ERROR.value


# 5. 5xx -> SERVER_ERROR
def test_http_5xx_classified_as_server_error(temp_git_repo: Path):
    def mock_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    client = httpx.Client(transport=httpx.MockTransport(mock_transport))
    provider = OpenAICompatibleProvider(
        api_key="test-key",
        client=client,
        max_retries=0,
    )
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)
    context_builder = ContextBuilder()

    task = TaskDefinition(task_id="T_P7_05", title="Server 5xx test", allowed_files=["Player.cs"])
    res = runtime.execute_with_model(task, provider=provider, context_builder=context_builder)

    assert res.success is False
    assert res.failure_type == FailureType.SERVER_ERROR.value


# 6. timeout -> TIMEOUT
def test_model_timeout_classified_as_timeout(temp_git_repo: Path):
    def mock_transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Read operation timed out")

    client = httpx.Client(transport=httpx.MockTransport(mock_transport))
    provider = OpenAICompatibleProvider(
        api_key="test-key",
        client=client,
        max_retries=0,
    )
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)
    context_builder = ContextBuilder()

    task = TaskDefinition(task_id="T_P7_06", title="Model timeout test", allowed_files=["Player.cs"])
    res = runtime.execute_with_model(task, provider=provider, context_builder=context_builder)

    assert res.success is False
    assert res.failure_type == FailureType.TIMEOUT.value


# 7. transient failure retries
def test_transient_failure_retries_and_succeeds(temp_git_repo: Path):
    call_count = 0

    def mock_transport(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return httpx.Response(503, text="Temporary glitch")
        # 3rd attempt succeeds
        valid_json = {
            "choices": [
                {
                    "message": {
                        "content": '{"patches": [{"file": "Player.cs", "hunks": [{"old_text": "    public void Update() {}", "new_text": "    public void Update() { /* Fixed */ }"}]}], "reason": "Fixed", "confidence": 0.95}'
                    }
                }
            ],
            "usage": {"total_tokens": 120},
        }
        return httpx.Response(200, json=valid_json)

    client = httpx.Client(transport=httpx.MockTransport(mock_transport))
    provider = OpenAICompatibleProvider(
        api_key="test-key",
        client=client,
        max_retries=3,
        backoff_factor=0.01,  # Fast for test
    )
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)
    context_builder = ContextBuilder()

    task = TaskDefinition(task_id="T_P7_07", title="Transient retry test", allowed_files=["Player.cs"])
    res = runtime.execute_with_model(task, provider=provider, context_builder=context_builder)

    assert res.success is True
    assert call_count == 3


# 8. policy violation does not retry (hard stop)
def test_policy_violation_does_not_retry(temp_git_repo: Path):
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)
    context_builder = ContextBuilder()

    task = TaskDefinition(
        task_id="T_P7_08",
        title="Hard stop policy violation",
        allowed_files=["Player.cs"],
    )

    class CountingMockProvider(BaseModelProvider):
        def __init__(self):
            self.calls = 0

        def generate(self, req: ModelRequest) -> ModelResponse:
            self.calls += 1
            # Proposal attempts unauthorized file -> SCOPE_VIOLATION
            proposal = PatchProposal(
                patches=[FilePatch(file="Secret.cs", hunks=[PatchHunk(old_text="", new_text="// secret")])]
            )
            return ModelResponse(raw_content=proposal.model_dump_json(), patch_proposal=proposal)

    provider = CountingMockProvider()
    res = runtime.execute_with_recovery(task, provider=provider, context_builder=context_builder)

    assert res.success is False
    assert res.failure_type == FailureType.SCOPE_VIOLATION.value
    # Crucial: Must NOT retry after a hard-stop policy violation
    assert provider.calls == 1


# 9. build timeout
def test_build_timeout_classified_as_timeout(temp_git_repo: Path):
    runner = SubprocessBuildRunner(
        build_cmd=["python3", "-c", "import time; time.sleep(10)"],
        timeout_seconds=1,
    )
    result = runner.build(temp_git_repo)

    assert result.success is False
    assert result.timed_out is True
    assert result.exit_code == -1
    assert "timed out after 1s" in result.raw_output


# 10. process-tree cleanup
def test_process_tree_cleanup_on_timeout(tmp_path: Path):
    # A script that spawns a background child process and sleeps
    script_path = tmp_path / "spawn_tree.py"
    pid_file = tmp_path / "child.pid"
    script_path.write_text(
        f"""
import subprocess, time, sys, os
proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
with open({repr(str(pid_file))}, "w") as f:
    f.write(str(proc.pid))
time.sleep(60)
""",
        encoding="utf-8",
    )

    returncode, raw_output, timed_out, sig_num, truncated = run_hardened_command(
        cmd=["python3", str(script_path)],
        cwd=tmp_path,
        timeout_seconds=1,
    )

    assert timed_out is True

    # Check child process PID
    if pid_file.exists():
        child_pid = int(pid_file.read_text().strip())
        # Verify child process was terminated by process group kill
        try:
            os.kill(child_pid, 0)
            child_alive = True
        except OSError:
            child_alive = False
        assert not child_alive, f"Child process {child_pid} was not cleaned up!"


# 11. output limit
def test_output_limit_truncation(tmp_path: Path):
    # Generates 100 KB output while limit is 4 KB
    returncode, raw_output, timed_out, sig_num, truncated = run_hardened_command(
        cmd=["python3", "-c", "for i in range(2000): print(f'Line {i}: test log message output data')"],
        cwd=tmp_path,
        timeout_seconds=5,
        max_output_bytes=4 * 1024,
    )

    assert returncode == 0
    assert truncated is True
    assert "TRUNCATED: Output exceeded 4096 bytes" in raw_output
    assert "Line 0:" in raw_output
    assert "Line 1999:" in raw_output


# 12. exit-code based build classification
def test_exit_code_based_build_classification():
    # Non-zero exit code without 'error' keyword in text is still classified as BUILD_FAILED
    res = BuildResult(
        success=False,
        exit_code=42,
        errors=[BuildErrorDetail(message="Process terminated abnormally")],
        raw_output="Process terminated abnormally",
    )
    f_type = FailureClassifier.classify_build_failure(res)
    assert f_type == FailureType.BUILD_FAILED


# 13. Git timeout
def test_git_subprocess_timeout(temp_git_repo: Path):
    with pytest.raises(WorkspaceError) as exc:
        _run_git_subprocess(temp_git_repo, "status", timeout=0.00000000001)
    assert "timed out after" in str(exc.value)


# 14. Git failure is surfaced
def test_git_failure_surfaced(temp_git_repo: Path):
    ws = WorkspaceManager(temp_git_repo)
    with pytest.raises(WorkspaceError) as exc:
        ws._run_git("checkout", "non_existent_branch_12345")
    assert "Git command failed" in str(exc.value)
    assert "non_existent_branch_12345" in str(exc.value)


# 15. rollback failure -> UNKNOWN_STATE / ROLLBACK_FAILED
def test_rollback_failure_classified_as_rollback_failed(temp_git_repo: Path, monkeypatch):
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)

    def failing_remove(*args, **kwargs):
        raise WorktreeCleanupError("Disk hardware failure during removal")

    monkeypatch.setattr(runtime.ws, "remove_transaction_worktree", failing_remove)

    task = TaskDefinition(task_id="T_P7_15", title="Rollback failure test", allowed_files=["Player.cs"])
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() { // fail }")],
            )
        ]
    )

    res = runtime.execute_transaction(task, proposal)
    assert res.success is False
    assert res.failure_type == FailureType.ROLLBACK_FAILED.value
    assert res.cleanup_error is not None
    assert "Disk hardware failure" in res.cleanup_error


# 16. budget release after failed attempt
def test_budget_release_after_failed_attempt(temp_git_repo: Path):
    policy = SafetyPolicy(max_cumulative_lines_added=20, max_cumulative_lines_deleted=10)
    runtime = DSHRuntime(temp_git_repo, policy=policy, dry_run=False, test_mode=True)

    task = TaskDefinition(task_id="T_P7_16", title="Failed attempt budget release", allowed_files=["Player.cs"])
    # Proposal that will fail on hunk mismatch (adding 15 lines)
    failing_proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="non_existent_line()", new_text="\n".join([f"// line {i}" for i in range(15)]))],
            )
        ]
    )

    res = runtime.execute_transaction(task, failing_proposal)
    assert res.success is False

    # Budget was released; cumulative lines added remains 0
    assert runtime.session_tracker.cumulative_lines_added == 0
    assert len(runtime.session_tracker._active_reservations) == 0

    # Another transaction adding 15 lines can now proceed without overflowing the 20 line limit
    valid_proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() {\n" + "\n".join([f"        // line {i}" for i in range(10)]) + "\n    }")],
            )
        ]
    )
    res2 = runtime.execute_transaction(task, valid_proposal)
    assert res2.success is True
    assert runtime.session_tracker.cumulative_lines_added > 0


# 17. budget release after dry-run
def test_budget_release_after_dry_run(temp_git_repo: Path):
    policy = SafetyPolicy(max_cumulative_lines_added=50, max_cumulative_lines_deleted=20)
    runtime = DSHRuntime(temp_git_repo, policy=policy, dry_run=True, test_mode=True)

    task = TaskDefinition(task_id="T_P7_17", title="Dry run budget release", allowed_files=["Player.cs"])
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() {\n        // dry run\n    }")],
            )
        ]
    )

    res = runtime.execute_transaction(task, proposal)
    assert res.success is True
    assert res.dry_run is True

    # Dry-run must NOT permanently consume session cumulative lines!
    assert runtime.session_tracker.cumulative_lines_added == 0
    assert len(runtime.session_tracker._active_reservations) == 0


# 18. budget commit after successful transaction
def test_budget_commit_after_successful_transaction(temp_git_repo: Path):
    policy = SafetyPolicy(max_cumulative_lines_added=50, max_cumulative_lines_deleted=20)
    runtime = DSHRuntime(temp_git_repo, policy=policy, dry_run=False, test_mode=True)

    task = TaskDefinition(task_id="T_P7_18", title="Successful commit budget", allowed_files=["Player.cs"])
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() {\n        int committed = 1;\n    }")],
            )
        ]
    )

    res = runtime.execute_transaction(task, proposal)
    assert res.success is True
    assert runtime.session_tracker.cumulative_lines_added > 0
    assert runtime.session_tracker.tasks_executed == 1
    assert len(runtime.session_tracker._active_reservations) == 0


# 19. recovery history remains bounded
def test_recovery_history_remains_bounded():
    history = RecoveryHistory(task_id="T_P7_19")
    for i in range(10):
        proposal = PatchProposal(
            patches=[
                FilePatch(
                    file=f"Module{i}.cs",
                    hunks=[PatchHunk(old_text=f"old_{i}()", new_text=f"new_{i}()")],
                )
            ]
        )
        history.record_attempt(
            attempt_index=i,
            model_type_used="FAST",
            failure_type=FailureType.SYNTAX,
            error_message="A" * 500,  # long error
            patch_attempted=proposal,
        )

    formatted = history.format_history_for_prompt(max_history_chars=1200)
    assert len(formatted) <= 1300
    assert "truncated to preserve token budget" in formatted
