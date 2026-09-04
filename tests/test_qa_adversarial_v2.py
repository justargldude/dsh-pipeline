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
from validation.baseline import BaselineManager, BaselineState
from validation.behavioral import (
    BaseBehavioralValidator,
    BehavioralCheckResult,
    MockBehavioralValidator,
)
from validation.pipeline import ValidationPipeline, ValidationTier


@pytest.fixture
def temp_git_repo(tmp_path: Path):
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "QA Tester"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "qa@tester.local"], cwd=repo, check=True)

    test_file = repo / "Player.cs"
    test_file.write_text(
        "public class Player {\n"
        "    public void Update() {}\n"
        "}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)
    return repo


class TestQA_Adversarial_R1_ASTOverload:
    """R1a & R1b: Kiểm tra AST overload, target_symbols, và regression guard."""

    def test_r1a_overload_multi_target_allowed(self):
        """R1a: target_symbols có nhiều mục tiêu ['Player.Update', 'Player.Other'].
        Đổi chữ ký Update(int) -> Update(float) khi có Update(string) giữ nguyên -> ALLOWED.
        """
        guard = ASTGuard()
        old_code = """
        public class Player {
            public void Update(int count) { DoWork(); }
            public void Update(string message) { Log(message); }
            public void Other() { DoOther(); }
        }
        """
        new_code = """
        public class Player {
            public void Update(float deltaTime) { DoWork(); }
            public void Update(string message) { Log(message); }
            public void Other() { DoOther(); }
        }
        """
        # Multi-target list
        targets = ["Player.Update", "Player.Other"]
        try:
            guard.validate_csharp_transition(old_code, new_code, "Player.cs", target_symbols=targets)
        except ASTViolationError as e:
            pytest.fail(f"Overload signature swap with multi-target should be allowed, got error: {e}")

    def test_r1a_signature_change_with_unrelated_target_blocked(self):
        """R1a: target_symbols=['Player.Attack'] (Update không thuộc target).
        Đổi chữ ký Update(int) -> Update(float) khi Update(string) giữ nguyên -> phải bị CHẶN.
        """
        guard = ASTGuard()
        old_code = """
        public class Player {
            public void Update(int count) { DoWork(); }
            public void Update(string message) { Log(message); }
            public void Attack() { Swing(); }
        }
        """
        new_code = """
        public class Player {
            public void Update(float deltaTime) { DoWork(); }
            public void Update(string message) { Log(message); }
            public void Attack() { Swing(); }
        }
        """
        targets = ["Player.Attack"]
        with pytest.raises(ASTViolationError) as exc_info:
            guard.validate_csharp_transition(old_code, new_code, "Player.cs", target_symbols=targets)
        assert "Update" in str(exc_info.value)

    def test_r1b_legacy_ast_tests_pass(self):
        """R1b: Chạy lại 3 test AST cũ để đảm bảo không bị regression.
        - test_ast_blocks_method_deletion
        - test_target_symbol_correct_change_accepted
        - test_overloaded_methods_distinguished
        """
        repo_root = Path(__file__).resolve().parent.parent
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/safety/test_ast_guard.py::test_ast_blocks_method_deletion",
            "tests/safety/test_scope_and_symbol_safety.py::test_target_symbol_correct_change_accepted",
            "tests/safety/test_scope_and_symbol_safety.py::test_overloaded_methods_distinguished",
        ]
        res = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
        assert res.returncode == 0, f"Legacy AST tests failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        assert "3 passed" in res.stdout

    def test_adversarial_a_constructor_signature_change_in_target_allowed(self):
        """Adversarial (a): Đổi chữ ký constructor trong target (ctor identity_key có param_types).
        Constructor Player() -> Player(int health) với target_symbols=['Player.Player'] hoặc ['Player'] -> ALLOWED.
        """
        guard = ASTGuard()
        old_code = """
        public class Player {
            public Player() { Init(); }
        }
        """
        new_code = """
        public class Player {
            public Player(int health) { Init(health); }
        }
        """
        # Testing with qualified constructor target 'Player.Player'
        try:
            guard.validate_csharp_transition(old_code, new_code, "Player.cs", target_symbols=["Player.Player"])
        except ASTViolationError as e:
            pytest.fail(f"Constructor signature change under target 'Player.Player' should be allowed, got: {e}")

        # Testing with class/ctor target 'Player'
        try:
            guard.validate_csharp_transition(old_code, new_code, "Player.cs", target_symbols=["Player"])
        except ASTViolationError as e:
            pytest.fail(f"Constructor signature change under target 'Player' should be allowed, got: {e}")

    def test_adversarial_a_constructor_signature_change_outside_target_blocked(self):
        """Adversarial (a): Đổi chữ ký constructor khi constructor KHÔNG thuộc target_symbols (target=['Player.Attack']) -> BLOCKED."""
        guard = ASTGuard()
        old_code = """
        public class Player {
            public Player() { Init(); }
            public void Attack() { Swing(); }
        }
        """
        new_code = """
        public class Player {
            public Player(int health) { Init(health); }
            public void Attack() { Swing(); }
        }
        """
        with pytest.raises(ASTViolationError):
            guard.validate_csharp_transition(old_code, new_code, "Player.cs", target_symbols=["Player.Attack"])


class TestQA_Adversarial_R2_AssertDetection:
    """R2a & R2b & Adversarial (b): Nhận diện assertion callee."""

    def test_r2a_r2b_nested_and_generic_assert_recognized(self):
        """R2a/R2b: Invocation 'Foo(DoSomethingElse())' lồng nhau không bị tính là assert;
        callee có generic 'Debug.Assert<T>(x)' nhận diện đúng là assert;
        'Logger.Warn(\"Debug.Assert removed\")' không bị đếm nhầm.
        """
        guard = ASTGuard()
        code = """
        public class Diagnostics {
            public void Test<T>(T x) {
                Foo(DoSomethingElse());
                Debug.Assert<T>(x != null);
                Logger.Warn("Debug.Assert removed");
            }
        }
        """
        tree = guard.parser.parse(code.encode("utf-8"))
        asserts = guard._extract_assert_predicates(tree.root_node, code.encode("utf-8"))

        # Verify only Debug.Assert<T> is captured
        assert len(asserts) == 1
        call_text, pred_text, is_tautology = asserts[0]
        assert "Debug.Assert<T>" in call_text
        assert pred_text == "x != null"
        assert not is_tautology

        # Verify transition: removing Logger.Warn is allowed
        clean_code = """
        public class Diagnostics {
            public void Test<T>(T x) {
                Foo(DoSomethingElse());
                Debug.Assert<T>(x != null);
            }
        }
        """
        guard.validate_csharp_transition(code, clean_code, "Diagnostics.cs")

        # Verify transition: removing generic Debug.Assert<T> is blocked
        no_assert_code = """
        public class Diagnostics {
            public void Test<T>(T x) {
                Foo(DoSomethingElse());
                Logger.Warn("Debug.Assert removed");
            }
        }
        """
        with pytest.raises(ASTViolationError):
            guard.validate_csharp_transition(code, no_assert_code, "Diagnostics.cs")

    def test_adversarial_b_fully_qualified_assert_recognized(self):
        """Adversarial (b): Assertion callee dài 'System.Diagnostics.Debug.Assert(x > 0)'.
        Nhận diện đúng là assert; xoá bị chặn; đổi thành tautology bị chặn.
        """
        guard = ASTGuard()
        old_code = """
        public class SystemChecker {
            public void Run(int x) {
                System.Diagnostics.Debug.Assert(x > 0);
                DoWork();
            }
        }
        """
        # Removing assert
        del_code = """
        public class SystemChecker {
            public void Run(int x) {
                DoWork();
            }
        }
        """
        with pytest.raises(ASTViolationError):
            guard.validate_csharp_transition(old_code, del_code, "SystemChecker.cs")

        # Tautological assert
        tautology_code = """
        public class SystemChecker {
            public void Run(int x) {
                System.Diagnostics.Debug.Assert(true);
                DoWork();
            }
        }
        """
        with pytest.raises(ASTViolationError):
            guard.validate_csharp_transition(old_code, tautology_code, "SystemChecker.cs")


class TestQA_Adversarial_R3_Classification:
    """R3a & R3b & Adversarial (c): Exception classification and hierarchy."""

    def test_r3a_circular_import_verification(self):
        """R3a: Kiểm tra cả 2 thứ tự import giữa recovery.classifier và core.workspace,
        và đảm bảo core.workspace không import recovery.
        """
        repo_root = Path(__file__).resolve().parent.parent
        # Order 1
        cmd1 = [sys.executable, "-c", "import recovery.classifier, core.workspace; print('ok1')"]
        res1 = subprocess.run(cmd1, cwd=repo_root, capture_output=True, text=True)
        assert res1.returncode == 0 and res1.stdout.strip() == "ok1"

        # Order 2
        cmd2 = [sys.executable, "-c", "import core.workspace, recovery.classifier; print('ok2')"]
        res2 = subprocess.run(cmd2, cwd=repo_root, capture_output=True, text=True)
        assert res2.returncode == 0 and res2.stdout.strip() == "ok2"

        # Verify core.workspace does not import recovery
        ws_file = repo_root / "core" / "workspace.py"
        content = ws_file.read_text(encoding="utf-8")
        assert "recovery" not in content

    def test_r3b_rollback_failure_test_pass(self):
        """R3b: Chạy lại test_rollback_failure_classified_as_rollback_failed trong suite phase 7."""
        repo_root = Path(__file__).resolve().parent.parent
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_phase7_robustness.py::test_rollback_failure_classified_as_rollback_failed",
        ]
        res = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
        assert res.returncode == 0, f"test_rollback_failure_classified_as_rollback_failed failed:\n{res.stdout}\n{res.stderr}"
        assert "1 passed" in res.stdout

    def test_adversarial_c_workspace_error_hierarchy_classification(self):
        """Adversarial (c): WorktreeStagingError và WorktreeCleanupError là subclass của WorkspaceError.
        Verify:
        - WorktreeStagingError -> TRANSACTION_INTEGRITY_FAILURE
        - WorktreeCleanupError -> ROLLBACK_FAILED
        - Base WorkspaceError -> UNKNOWN (không bị nuốt nhầm bởi subclass)
        """
        assert issubclass(WorktreeStagingError, WorkspaceError)
        assert issubclass(WorktreeCleanupError, WorkspaceError)

        staging_err = WorktreeStagingError("staging broke")
        cleanup_err = WorktreeCleanupError("cleanup broke")
        base_err = WorkspaceError("generic workspace err")

        assert FailureClassifier.classify_exception(staging_err) == FailureType.TRANSACTION_INTEGRITY_FAILURE
        assert FailureClassifier.classify_exception(cleanup_err) == FailureType.ROLLBACK_FAILED
        assert FailureClassifier.classify_exception(base_err) == FailureType.UNKNOWN


class TestQA_Adversarial_R4_BehavioralBaseline:
    """R4a & R4b & Adversarial (d): T2 Behavioral baseline comparison."""

    def test_r4a_pipeline_t2_pass_then_fail_behavioral(self, temp_git_repo: Path):
        """R4a: Audit pattern T2: validator pass ở baseline (call 1) và fail ở post-apply (call 2)
        được nhận diện chính xác là regression và làm transaction fail.
        """
        class TwoPhaseValidator(BaseBehavioralValidator):
            def __init__(self):
                self.calls = 0

            def validate_behavior(self, repo_path: Path, task_id: str) -> BehavioralCheckResult:
                self.calls += 1
                if self.calls == 1:
                    return BehavioralCheckResult(success=True, output="Clean baseline")
                return BehavioralCheckResult(
                    success=False,
                    failures=["Regression detected on post-patch"],
                    output="Post-apply failure",
                )

        from core.runtime import DSHRuntime
        from task.schema import PatchProposal, FilePatch, PatchHunk

        runtime = DSHRuntime(temp_git_repo, behavioral_validator=TwoPhaseValidator(), dry_run=False, test_mode=True)
        task = TaskDefinition(task_id="T_ADV_01", title="Adv T2", allowed_files=["Player.cs"])
        proposal = PatchProposal(
            patches=[
                FilePatch(
                    file="Player.cs",
                    hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() {\n        // New\n    }")],
                )
            ]
        )
        res = runtime.execute_transaction(task, proposal)
        assert res.success is False
        assert res.failure_type == FailureType.BEHAVIORAL

    def test_r4b_failures_deduplication_and_ordering(self):
        """R4b: baseline failures=['A', 'B'], post failures=['A', 'B', 'A', 'C'].
        new_failures phải loại bỏ 'A' và 'B' đã có trong baseline, chỉ còn ['C'].
        """
        baseline = BehavioralCheckResult(success=False, failures=["A", "B"])
        post = BehavioralCheckResult(success=False, failures=["A", "B", "A", "C"])

        is_reg, new_fails = BaselineManager.compare_behavioral(baseline, post)
        assert is_reg is True
        assert new_fails == ["C"]

    def test_r4b_post_success_with_failures_no_regression(self):
        """R4b: Khi baseline is None nhưng post.success is True (có thông báo failure/warning trong list),
        compare_behavioral trả is_regression=False (vì success=True -> not success=False).
        """
        post = BehavioralCheckResult(success=True, failures=["Non-fatal warning"])
        is_reg, new_fails = BaselineManager.compare_behavioral(None, post)
        assert is_reg is False
        assert new_fails == ["Non-fatal warning"]

    def test_adversarial_d_t2_skipped_when_validator_is_none(self):
        """Adversarial (d): baseline.behavioral_result None + behavioral_validator None -> T2 skip hoàn toàn."""
        pipeline = ValidationPipeline(behavioral_validator=None)
        task = TaskDefinition(task_id="T_ADV_SKIP", title="Skip T2", allowed_files=["Player.cs"])
        runner = MockBuildRunner(should_succeed=True)

        report = pipeline.validate_post_apply(task, Path("."), runner, baseline=None)
        assert report.success is True
        assert report.failed_tier is None


class TestQA_Adversarial_R5_ProcessTree:
    """R5a, R5b, R5c & Adversarial (e): terminate_process_tree robustness."""

    def test_r5a_already_dead_process_early_returns(self):
        """R5a: Tiến trình đã kết thúc trước khi terminate_process_tree được gọi (proc.poll() is not None).
        Hàm phải return ngay, không raise bất kỳ lỗi nào.
        """
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=2.0)
        assert proc.poll() is not None

        # Call terminate_process_tree on already dead process
        try:
            terminate_process_tree(proc)
        except Exception as e:
            pytest.fail(f"terminate_process_tree raised on dead process: {e}")

    @pytest.mark.skip(
        reason="R5b: PermissionError on killpg(pgid, 0) cannot be deterministically simulated without root/multi-user environment."
    )
    def test_r5b_permission_error_simulation(self):
        """R5b: Giả lập PermissionError khi killpg sang group của user khác.
        (Được skip và note trong báo cáo theo SPEC).
        """
        pass

    @pytest.mark.skip(
        reason="R5c: Process group race condition between killpg(0) check and subsequent SIGKILL is non-deterministic in userland."
    )
    def test_r5c_race_group_death_simulation(self):
        """R5c: Giả lập race condition khi process group chết ngay giữa check killpg(0) và SIGKILL.
        (Được skip và note trong báo cáo theo SPEC).
        """
        pass

    def test_adversarial_e_multilevel_descendants_sigterm_ignoring_killed(self, tmp_path: Path):
        """Adversarial (e): Cây 3 cấp con/cháu/chắt (Parent -> Child -> Grandchild -> Great-Grandchild).
        Cả Child, Grandchild và Great-Grandchild đều SIG_IGN SIGTERM.
        Cha thoát ngay trên SIGTERM.
        terminate_process_tree phải dọn sạch toàn bộ các cấp con cháu chắt trong process group.
        """
        gc_pid_file = tmp_path / "gc.pid"
        ggc_pid_file = tmp_path / "ggc.pid"
        script_ggc_path = tmp_path / "test_ggc.py"
        script_gc_path = tmp_path / "test_gc.py"
        script_parent_path = tmp_path / "test_parent.py"

        script_ggc_path.write_text(
            f"""import time, signal, os
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(r"{ggc_pid_file}", "w") as f:
    f.write(str(os.getpid()))
time.sleep(30)
""",
            encoding="utf-8",
        )

        script_gc_path.write_text(
            f"""import subprocess, sys, time, signal, os
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(r"{gc_pid_file}", "w") as f:
    f.write(str(os.getpid()))
subprocess.Popen([sys.executable, r"{script_ggc_path}"])
time.sleep(30)
""",
            encoding="utf-8",
        )

        script_parent_path.write_text(
            f"""import subprocess, sys, time, signal, os
subprocess.Popen([sys.executable, r"{script_gc_path}"])
def handle_term(signum, frame):
    sys.exit(0)
signal.signal(signal.SIGTERM, handle_term)
while not (os.path.exists(r"{gc_pid_file}") and os.path.exists(r"{ggc_pid_file}")):
    time.sleep(0.05)
print("READY", flush=True)
while True:
    time.sleep(1)
""",
            encoding="utf-8",
        )

        proc = subprocess.Popen(
            [sys.executable, str(script_parent_path)],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        pgid = os.getpgid(proc.pid)
        try:
            line = proc.stdout.readline()
            assert "READY" in line

            with open(gc_pid_file) as f:
                gc_pid = int(f.read().strip())
            with open(ggc_pid_file) as f:
                ggc_pid = int(f.read().strip())

            terminate_process_tree(proc, timeout_grace=0.2)

            # Poll up to 1.0s to confirm all descendants are dead
            gc_alive = True
            ggc_alive = True
            deadline = time.time() + 1.0
            while time.time() < deadline:
                if gc_alive:
                    try:
                        os.kill(gc_pid, 0)
                    except OSError:
                        gc_alive = False
                if ggc_alive:
                    try:
                        os.kill(ggc_pid, 0)
                    except OSError:
                        ggc_alive = False
                if not gc_alive and not ggc_alive:
                    break
                time.sleep(0.05)

            assert not gc_alive, f"Child {gc_pid} in pgid {pgid} remained alive after terminate_process_tree"
            assert not ggc_alive, f"Grandchild {ggc_pid} in pgid {pgid} remained alive after terminate_process_tree"
        finally:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
