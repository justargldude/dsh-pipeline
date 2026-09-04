import os
import signal
import subprocess
import sys
import time
from pathlib import Path
import pytest

from build.sandbox import BuildResult, MockBuildRunner, terminate_process_tree
from core.workspace import WorkspaceError, WorktreeCleanupError, WorktreeStagingError
from recovery.classifier import FailureClassifier, FailureType
from safety.ast_guard import ASTGuard, ASTViolationError
from task.schema import TaskDefinition
from validation.baseline import BaselineState
from validation.behavioral import (
    BaseBehavioralValidator,
    BehavioralCheckResult,
    MockBehavioralValidator,
)
from validation.pipeline import ValidationPipeline, ValidationTier


class TestBug31_SignatureChangeInTarget:
    """Bug 3.1: Cho phép đổi chữ ký method thuộc target_symbols."""

    def test_signature_change_in_target_allowed(self):
        """Theo inventory_2 mục 1: Đổi chữ ký method trong target_symbols phải được ALLOWED (hiện REJECTED)."""
        guard = ASTGuard()
        old_code = "public class Player { public void Update() { DoWork(); } }"
        new_code = "public class Player { public void Update(float deltaTime) { DoWork(); } }"
        try:
            guard.validate_csharp_transition(
                old_code, new_code, "Player.cs", target_symbols=["Player.Update"]
            )
        except ASTViolationError as e:
            pytest.fail(f"Signature change in target symbol should be allowed, but raised: {e}")

    def test_signature_change_outside_target_blocked(self):
        """Đổi chữ ký method ngoài target_symbols (Attack) phải bị chặn (regression guard)."""
        guard = ASTGuard()
        old_code = "public class Player { public void Update() { DoWork(); } public void Attack() { Swing(); } }"
        new_code = "public class Player { public void Update() { DoWork(); } public void Attack(int power) { Swing(); } }"
        with pytest.raises(ASTViolationError):
            guard.validate_csharp_transition(
                old_code, new_code, "Player.cs", target_symbols=["Player.Update"]
            )

    def test_real_deletion_in_target_still_blocked(self):
        """Xoá hẳn method trong target_symbols mà không có method mới cùng tên phải bị chặn (regression guard)."""
        guard = ASTGuard()
        old_code = "public class Player { public void Update() { DoWork(); } }"
        new_code = "public class Player { }"
        with pytest.raises(ASTViolationError):
            guard.validate_csharp_transition(
                old_code, new_code, "Player.cs", target_symbols=["Player.Update"]
            )

    def test_overload_signature_swap_in_target(self):
        """R1a: Player có Update(int) + Update(string), đổi Update(int)->Update(float), target=['Player.Update'] -> ALLOWED (hiện REJECTED)."""
        guard = ASTGuard()
        old_code = """
        public class Player {
            public void Update(int count) { DoWork(); }
            public void Update(string message) { Log(message); }
        }
        """
        new_code = """
        public class Player {
            public void Update(float deltaTime) { DoWork(); }
            public void Update(string message) { Log(message); }
        }
        """
        try:
            guard.validate_csharp_transition(
                old_code, new_code, "Player.cs", target_symbols=["Player.Update"]
            )
        except ASTViolationError as e:
            pytest.fail(f"Overload signature swap in target should be allowed, but raised: {e}")

    def test_no_target_still_blocks_deletion(self):
        """Không truyền target_symbols, xoá method phải bị chặn (regression guard)."""
        guard = ASTGuard()
        old_code = "public class Player { public void Helper() { DoWork(); } }"
        new_code = "public class Player { }"
        with pytest.raises(ASTViolationError):
            guard.validate_csharp_transition(old_code, new_code, "Player.cs", target_symbols=None)


class TestBug32_CalleeOnlyAssertDetection:
    """Bug 3.2: Nhận diện assertion chỉ theo callee."""

    def test_logger_with_assert_string_not_counted(self):
        """Theo inventory_2 mục 2: Xoá Logger.Warn('removed assert check') không bị tính là xoá assertion (hiện REJECTED)."""
        guard = ASTGuard()
        old_code = 'public class Player { public void Update() { Logger.Warn("removed assert check"); DoWork(); } }'
        new_code = 'public class Player { public void Update() { DoWork(); } }'
        try:
            guard.validate_csharp_transition(old_code, new_code, "Player.cs")
        except ASTViolationError as e:
            pytest.fail(f"Removing log message containing 'assert' should be allowed, but raised: {e}")

    def test_real_assert_removal_still_blocked(self):
        """Xoá Debug.Assert thật sự phải bị chặn (regression guard)."""
        guard = ASTGuard()
        old_code = 'public class Player { public void Update() { Debug.Assert(this.Health > 0); DoWork(); } }'
        new_code = 'public class Player { public void Update() { DoWork(); } }'
        with pytest.raises(ASTViolationError):
            guard.validate_csharp_transition(old_code, new_code, "Player.cs")

    def test_bypass_via_fake_literal_closed(self):
        """Xoá Debug.Assert thật và thêm Logger.Warn('assert check stays') để giữ count phải bị chặn."""
        guard = ASTGuard()
        old_code = 'public class Player { public void Update() { Debug.Assert(x > 0); DoWork(); } }'
        new_code = 'public class Player { public void Update() { Logger.Warn("assert check stays"); DoWork(); } }'
        with pytest.raises(ASTViolationError):
            guard.validate_csharp_transition(old_code, new_code, "Player.cs")

    def test_callee_identifier_assert_recognized(self):
        """R2a: Invocation 'Assert(x > 0)' (identifier thuần) xoá đi phải bị chặn (regression guard)."""
        guard = ASTGuard()
        old_code = 'public class Player { public void Update() { Assert(x > 0); DoWork(); } }'
        new_code = 'public class Player { public void Update() { DoWork(); } }'
        with pytest.raises(ASTViolationError):
            guard.validate_csharp_transition(old_code, new_code, "Player.cs")


class TestBug4_HardStopClassification:
    """Bug 4: classify_exception gán đúng hard-stop types."""

    def test_staging_error_classified_integrity_failure(self):
        """WorktreeStagingError phải được phân loại thành TRANSACTION_INTEGRITY_FAILURE (hiện UNKNOWN)."""
        exc = WorktreeStagingError("Post-commit git hook unexpectedly mutated worktree state.")
        assert FailureClassifier.classify_exception(exc) == FailureType.TRANSACTION_INTEGRITY_FAILURE

    def test_cleanup_error_classified_rollback_failed(self):
        """WorktreeCleanupError phải được phân loại thành ROLLBACK_FAILED (hiện UNKNOWN)."""
        exc = WorktreeCleanupError("Failed to cleanly remove worktree.")
        assert FailureClassifier.classify_exception(exc) == FailureType.ROLLBACK_FAILED

    def test_base_workspace_error_still_unknown(self):
        """WorkspaceError cơ sở không phải staging/cleanup vẫn là UNKNOWN (regression guard)."""
        exc = WorkspaceError("Generic workspace error.")
        assert FailureClassifier.classify_exception(exc) == FailureType.UNKNOWN

    def test_hard_stop_flags(self):
        """TRANSACTION_INTEGRITY_FAILURE và ROLLBACK_FAILED phải là hard-stop (regression guard)."""
        assert FailureClassifier.is_hard_stop(FailureType.TRANSACTION_INTEGRITY_FAILURE) is True
        assert FailureClassifier.is_hard_stop(FailureType.ROLLBACK_FAILED) is True

    def test_circular_import_check(self):
        """R3a: Kiểm tra không bị circular import giữa recovery.classifier và core.workspace."""
        repo_root = Path(__file__).resolve().parent.parent
        # Order 1: recovery.classifier then core.workspace
        cmd1 = [sys.executable, "-c", "import recovery.classifier, core.workspace; print('ok')"]
        res1 = subprocess.run(cmd1, cwd=repo_root, capture_output=True, text=True)
        assert res1.returncode == 0, f"Import order 1 failed: {res1.stderr}"
        assert res1.stdout.strip() == "ok"

        # Order 2: core.workspace then recovery.classifier
        cmd2 = [sys.executable, "-c", "import core.workspace, recovery.classifier; print('ok')"]
        res2 = subprocess.run(cmd2, cwd=repo_root, capture_output=True, text=True)
        assert res2.returncode == 0, f"Import order 2 failed: {res2.stderr}"
        assert res2.stdout.strip() == "ok"


class TestBug5_T2BaselineComparison:
    """Bug 5: T2 Behavioral so sánh baseline như T1/T3."""

    def test_preexisting_behavioral_failure_passes_t2(self):
        """Lỗi behavioral đã tồn tại từ trước trong baseline không được coi là regression ở T2 (hiện FAIL -> kỳ vọng PASS)."""
        pipeline = ValidationPipeline(
            behavioral_validator=MockBehavioralValidator(
                should_succeed=False, failures=["Pre-existing failure in legacy hook"]
            )
        )
        task = TaskDefinition(task_id="T_01", title="Test task", allowed_files=["Player.cs"])
        runner = MockBuildRunner(should_succeed=True)

        baseline = BaselineState(
            base_commit="abc",
            build_result=BuildResult(success=True),
            behavioral_result=BehavioralCheckResult(
                success=False, failures=["Pre-existing failure in legacy hook"]
            ),
            env_fingerprint="env",
            config_fingerprint="cfg",
            cache_key="key",
        )

        report = pipeline.validate_post_apply(task, Path("."), runner, baseline=baseline)
        assert report.success is True, f"Pre-existing behavioral failure should pass T2, got: {report.error_message}"

    def test_new_behavioral_failure_fails_t2(self):
        """Lỗi behavioral mới xuất hiện sau patch phải làm T2 thất bại (regression guard)."""
        pipeline = ValidationPipeline(
            behavioral_validator=MockBehavioralValidator(
                should_succeed=False,
                failures=["Pre-existing failure in legacy hook", "New regression Y"],
            )
        )
        task = TaskDefinition(task_id="T_02", title="Test task", allowed_files=["Player.cs"])
        runner = MockBuildRunner(should_succeed=True)

        baseline = BaselineState(
            base_commit="abc",
            build_result=BuildResult(success=True),
            behavioral_result=BehavioralCheckResult(
                success=False, failures=["Pre-existing failure in legacy hook"]
            ),
            env_fingerprint="env",
            config_fingerprint="cfg",
            cache_key="key",
        )

        report = pipeline.validate_post_apply(task, Path("."), runner, baseline=baseline)
        assert report.success is False
        assert report.failed_tier == ValidationTier.T2_BEHAVIORAL
        assert "New regression Y" in report.error_message

    def test_no_baseline_behavioral_fails_as_before(self):
        """Khi baseline.behavioral_result is None, lỗi post-apply vẫn làm T2 fail như cũ (regression guard)."""
        pipeline = ValidationPipeline(
            behavioral_validator=MockBehavioralValidator(
                should_succeed=False, failures=["Standalone behavioral error"]
            )
        )
        task = TaskDefinition(task_id="T_03", title="Test task", allowed_files=["Player.cs"])
        runner = MockBuildRunner(should_succeed=True)

        report = pipeline.validate_post_apply(task, Path("."), runner, baseline=None)
        assert report.success is False
        assert report.failed_tier == ValidationTier.T2_BEHAVIORAL


class TestBug6_ProcessGroupConfirmedDead:
    """Bug 6: terminate_process_tree xác nhận process group chết."""

    def test_sigterm_ignoring_grandchild_killed(self, tmp_path: Path):
        """Tiến trình cha bắt SIGTERM thoát ngay, tiến trình cháu SIG_IGN SIGTERM -> sau terminate_process_tree cháu phải bị kill (hiện còn sống)."""
        pid_file = tmp_path / "grandchild.pid"
        parent_script = f"""import subprocess, time, sys, os, signal
gc = subprocess.Popen([sys.executable, "-c", '''
import time, signal, os
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open({repr(str(pid_file))}, "w") as f:
    f.write(str(os.getpid()))
time.sleep(30)
'''])
def handle_term(signum, frame):
    sys.exit(0)
signal.signal(signal.SIGTERM, handle_term)
while not os.path.exists({repr(str(pid_file))}):
    time.sleep(0.05)
print("READY", flush=True)
while True:
    time.sleep(1)
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", parent_script],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        pgid = os.getpgid(proc.pid)
        try:
            # Wait for handshake
            line = proc.stdout.readline()
            assert "READY" in line
            with open(pid_file) as f:
                gc_pid = int(f.read().strip())

            terminate_process_tree(proc, timeout_grace=0.2)

            # Poll up to 1.0s to verify grandchild is dead
            gc_alive = True
            deadline = time.time() + 1.0
            while time.time() < deadline:
                try:
                    os.kill(gc_pid, 0)
                    time.sleep(0.05)
                except OSError:
                    gc_alive = False
                    break

            assert not gc_alive, (
                f"Grandchild process {gc_pid} in pgid {pgid} should have been terminated, "
                f"but is still alive after terminate_process_tree"
            )
        finally:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            if pid_file.exists():
                pid_file.unlink(missing_ok=True)

    def test_normal_sleep_tree_cleanup(self, tmp_path: Path):
        """Tiến trình sleep bình thường bị terminate_process_tree dọn sạch toàn bộ group (regression guard)."""
        pid_file = tmp_path / "child.pid"
        script = f"""import subprocess, sys, time, os
proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
with open({repr(str(pid_file))}, "w") as f:
    f.write(str(proc.pid))
print("READY", flush=True)
time.sleep(30)
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        pgid = os.getpgid(proc.pid)
        try:
            line = proc.stdout.readline()
            assert "READY" in line
            with open(pid_file) as f:
                child_pid = int(f.read().strip())

            terminate_process_tree(proc, timeout_grace=0.2)

            child_alive = True
            deadline = time.time() + 1.0
            while time.time() < deadline:
                try:
                    os.kill(child_pid, 0)
                    time.sleep(0.05)
                except OSError:
                    child_alive = False
                    break

            assert not child_alive, f"Child process {child_pid} was not cleaned up!"
        finally:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            if pid_file.exists():
                pid_file.unlink(missing_ok=True)
