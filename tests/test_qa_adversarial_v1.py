import ast
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import List

import pytest

from build.sandbox import MockBuildRunner, SubprocessBuildRunner
from core.journal import TransactionJournal
from core.runtime import DSHRuntime
from core.workspace import WorkspaceManager
from task.schema import FilePatch, PatchHunk, PatchProposal, TaskDefinition
from validation.baseline import BaselineManager


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
        "    public void Update() {\n"
        "        int x = 1;\n"
        "        int y = 1;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)
    return repo


# ==============================================================================
# NHIỆM VỤ 1: ADVERSARIAL TESTS CHO TỪNG MỤC RISK LIST TRONG SPEC (R1.1 -> R4.2)
# ==============================================================================

class TestRiskItemR1Queue:
    def test_r1_1_queue_pycache_and_stdlib_import(self):
        """R1.1: Verify no leftover queue/ or __pycache__ shadows stdlib queue."""
        repo_root = Path(__file__).resolve().parent.parent

        # 1. Physical directory check
        assert not (repo_root / "queue").exists(), "Found leftover 'queue' directory in repo root!"

        # 2. Subprocess check from repo root CWD
        cmd = [
            sys.executable,
            "-c",
            "import queue, sys; "
            "assert 'dsh-pipeline' not in getattr(queue, '__file__', ''), f'Shadowed: {queue.__file__}'; "
            "assert hasattr(queue, 'SimpleQueue'), 'SimpleQueue missing'"
        ]
        res = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
        assert res.returncode == 0, f"Subprocess import queue failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}"

        # 3. In-process check
        import queue
        assert "dsh-pipeline" not in str(getattr(queue, "__file__", "")), (
            f"queue should be from Python stdlib, got: {queue.__file__}"
        )
        assert hasattr(queue, "SimpleQueue"), "queue module missing SimpleQueue"

    def test_r1_2_grep_no_stale_queue_references(self):
        """R1.2: Verify 0 stale references to queue package outside .venv/.git/.scratch."""
        repo_root = Path(__file__).resolve().parent.parent

        # Check using ripgrep
        patterns = [
            r"\bfrom\s+queue\s+import\b",
            r"\bimport\s+queue\.cloud_worker\b",
            r"\bqueue\.cloud_worker\b",
        ]

        found_matches: List[str] = []
        for pat in patterns:
            cmd = [
                "rg",
                "-g", "*.py",
                "-g", "!*.venv*",
                "-g", "!*.git*",
                "-g", "!*.scratch*",
                pat,
                str(repo_root),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0 and res.stdout.strip():
                found_matches.append(f"Pattern '{pat}' matched:\n{res.stdout.strip()}")

        # Python-level fallback check across *.py files
        re_pats = [re.compile(p) for p in patterns]
        for py_file in repo_root.rglob("*.py"):
            parts = py_file.parts
            if any(p.startswith((".venv", ".git", ".scratch")) for p in parts):
                continue
            text = py_file.read_text(encoding="utf-8", errors="replace")
            for line_idx, line in enumerate(text.splitlines(), start=1):
                for p in re_pats:
                    if p.search(line):
                        match_str = f"{py_file}:{line_idx}: {line}"
                        if match_str not in found_matches:
                            found_matches.append(match_str)

        assert not found_matches, f"Found stale queue references:\n" + "\n".join(found_matches)


class TestRiskItemR2Baseline:
    def test_r2_1_baseline_cache_different_config_fingerprints(self, temp_git_repo: Path):
        """R2.1: Verify same base_commit with different config fingerprints yields different cache keys."""
        mgr = BaselineManager(enable_cache=True)
        runner_a = MockBuildRunner(should_succeed=True)
        runner_b = SubprocessBuildRunner(build_cmd=["dotnet", "build"])
        ws = WorkspaceManager(temp_git_repo)

        fixed_base = "AAA000111222333444555666777888999aaabbb"

        base1 = mgr.get_or_capture_baseline(temp_git_repo, ws, runner_a, base_commit=fixed_base)
        base2 = mgr.get_or_capture_baseline(temp_git_repo, ws, runner_b, base_commit=fixed_base)

        assert base1.cache_key != base2.cache_key
        assert len(mgr._cache) == 2

    def test_r2_2_tx_worktree_base_commit_is_valid_sha(self, temp_git_repo: Path):
        """R2.2: Verify TransactionWorktree.base_commit is always a valid non-empty 40-hex Git commit SHA."""
        ws = WorkspaceManager(temp_git_repo)

        # 1. Default base_commit (falls back to main HEAD)
        tx1 = ws.create_transaction_worktree("tx_r22_test1")
        try:
            assert tx1.base_commit is not None, "base_commit must not be None"
            assert isinstance(tx1.base_commit, str)
            assert len(tx1.base_commit) == 40, f"base_commit must be 40 chars, got: {tx1.base_commit}"
            assert re.fullmatch(r"[0-9a-fA-F]{40}", tx1.base_commit) is not None
        finally:
            ws.remove_transaction_worktree(tx1)

        # 2. Explicit base_commit
        head = ws.get_head_commit()
        tx2 = ws.create_transaction_worktree("tx_r22_test2", base_commit=head)
        try:
            assert tx2.base_commit == head
            assert re.fullmatch(r"[0-9a-fA-F]{40}", tx2.base_commit) is not None
        finally:
            ws.remove_transaction_worktree(tx2)


class TestRiskItemR3PendingIntegrationAndIsolation:
    def test_r3_1_pending_worktree_present_in_orphaned_list(self, temp_git_repo: Path, monkeypatch):
        """R3.1: Verify current behavior: PENDING worktree remains in journal and appears in get_orphaned_worktrees."""
        runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)

        task = TaskDefinition(task_id="T_ORPHAN_1", title="Pending task", allowed_files=["Player.cs"])
        proposal = PatchProposal(
            patches=[
                FilePatch(
                    file="Player.cs",
                    hunks=[PatchHunk(old_text="        int x = 1;", new_text="        int x = 42;")],
                )
            ]
        )

        orig_create_wt = runtime.ws.create_transaction_worktree

        def wrapped_create_wt(*args, **kwargs):
            wt = orig_create_wt(*args, **kwargs)
            # Diverge main HEAD to make transaction STALE_BASE
            (temp_git_repo / "divergent_head.txt").write_text("diverging main HEAD")
            subprocess.run(["git", "add", "."], cwd=temp_git_repo, check=True)
            subprocess.run(["git", "commit", "-m", "Advance main HEAD to make base stale"], cwd=temp_git_repo, check=True)
            return wt

        monkeypatch.setattr(runtime.ws, "create_transaction_worktree", wrapped_create_wt)

        result = runtime.execute_transaction(task, proposal)
        assert result.success is False
        assert result.integration_status == "STALE_BASE"
        assert Path(result.worktree_path).exists()

        try:
            orphaned = runtime.ws.get_orphaned_worktrees()
            orphaned_paths = [rec.worktree_path for rec in orphaned]
            # Document and verify current behavior: pending worktree is tracked in orphaned list
            assert str(Path(result.worktree_path).resolve()) in [str(Path(p).resolve()) for p in orphaned_paths]
        finally:
            # Cleanup for test isolation
            if Path(result.worktree_path).exists():
                shutil.rmtree(result.worktree_path, ignore_errors=True)
            runtime.ws.journal.record_end(Path(result.worktree_path).name)

    def test_r3_2_non_integrated_task_releases_session_budget(self, temp_git_repo: Path, monkeypatch):
        """R3.2: Verify non-integrated task releases reserved budget in SessionBudgetTracker (no budget leak)."""
        runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)

        # Probe 1: Initial state before transaction
        assert len(runtime.session_tracker._active_reservations) == 0
        assert runtime.session_tracker.cumulative_lines_added == 0
        assert runtime.session_tracker.cumulative_lines_deleted == 0

        task = TaskDefinition(task_id="T_BUDGET_1", title="Pending budget task", allowed_files=["Player.cs"])
        proposal = PatchProposal(
            patches=[
                FilePatch(
                    file="Player.cs",
                    hunks=[PatchHunk(old_text="        int x = 1;", new_text="        int x = 999;")],
                )
            ]
        )

        orig_create_wt = runtime.ws.create_transaction_worktree

        def wrapped_create_wt(*args, **kwargs):
            wt = orig_create_wt(*args, **kwargs)
            (temp_git_repo / "divergent_head.txt").write_text("diverging main HEAD")
            subprocess.run(["git", "add", "."], cwd=temp_git_repo, check=True)
            subprocess.run(["git", "commit", "-m", "Advance main HEAD to make base stale"], cwd=temp_git_repo, check=True)
            return wt

        monkeypatch.setattr(runtime.ws, "create_transaction_worktree", wrapped_create_wt)

        result = runtime.execute_transaction(task, proposal)
        assert result.success is False
        assert result.integration_status == "STALE_BASE"

        try:
            # Probe 2: Post-execution state — budget must be completely released
            active_reservations = runtime.session_tracker._active_reservations
            assert len(active_reservations) == 0, f"Leaked active reservations: {active_reservations}"
            assert runtime.session_tracker.cumulative_lines_added == 0, (
                f"Budget committed despite STALE_BASE: {runtime.session_tracker.cumulative_lines_added}"
            )
            assert runtime.session_tracker.cumulative_lines_deleted == 0
        finally:
            if Path(result.worktree_path).exists():
                shutil.rmtree(result.worktree_path, ignore_errors=True)
            runtime.ws.journal.record_end(Path(result.worktree_path).name)

    def test_r3_3_main_head_movement_detected_stale_base_regression_guard(self, temp_git_repo: Path):
        """R3.3: Verify test_main_head_movement_detected_stale_base contract remains intact."""
        from tests.test_worktree_isolation import test_main_head_movement_detected_stale_base
        # Must execute and pass cleanly without assertion conflict
        test_main_head_movement_detected_stale_base(temp_git_repo)


class TestRiskItemR4Concurrency:
    def test_r4_1_concurrent_interleaved_journal_start_end(self, temp_git_repo: Path):
        """R4.1: 8 threads concurrently executing interleaved record_start and record_end with Barrier."""
        journal = TransactionJournal(temp_git_repo)
        num_threads = 8

        # Pre-seed 4 active records
        for i in range(4):
            journal.record_start(
                tx_id=f"tx_pre_{i}",
                task_id=f"T_PRE_{i}",
                base_commit="dummy_base",
                worktree_path=str(temp_git_repo / f".worktrees/wt_pre_{i}"),
            )
        assert len(journal.list_active()) == 4

        barrier_start = threading.Barrier(num_threads)
        barrier_phase2 = threading.Barrier(num_threads)
        errors = []

        def worker(idx: int):
            try:
                # Barrier synchronizes all 8 threads to run concurrently
                barrier_start.wait(timeout=5)
                if idx < 4:
                    # Threads 0-3 end pre-seeded records
                    journal.record_end(f"tx_pre_{idx}")
                else:
                    # Threads 4-7 create new records
                    journal.record_start(
                        tx_id=f"tx_new_{idx}",
                        task_id=f"T_NEW_{idx}",
                        base_commit="dummy_base",
                        worktree_path=str(temp_git_repo / f".worktrees/wt_new_{idx}"),
                    )

                barrier_phase2.wait(timeout=5)
                if idx >= 4:
                    # Clean up the newly created records
                    journal.record_end(f"tx_new_{idx}")
            except Exception as e:
                errors.append((idx, e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors in concurrent journal workers: {errors}"
        active_records = journal.list_active()
        assert len(active_records) == 0, f"Expected empty journal, but found: {active_records}"

    def test_r4_2_audit_deterministic_race_testing_uses_barriers(self):
        """R4.2: Audit check verifying concurrency tests use deterministic threading.Barrier, not sleep calls."""
        repo_root = Path(__file__).resolve().parent.parent
        test_files = [
            repo_root / "tests" / "test_red_fix_batch.py",
            repo_root / "tests" / "test_qa_adversarial_v1.py",
        ]

        for tf in test_files:
            content = tf.read_text(encoding="utf-8")
            # Verify threading.Barrier is present
            assert "threading.Barrier" in content or "Barrier(" in content, (
                f"File {tf.name} does not use threading.Barrier for thread synchronization"
            )
            # Verify no sleep calls in AST outside the audit function itself
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and "audit" in node.name:
                    continue
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name) and node.func.id == "sleep":
                        pytest.fail(f"File {tf.name} contains direct sleep() call at line {node.lineno}")
                    if isinstance(node.func, ast.Attribute) and node.func.attr == "sleep":
                        pytest.fail(f"File {tf.name} contains .sleep() call at line {node.lineno}")


# ==============================================================================
# NHIỆM VỤ 2: TEST CHO PHÁT HIỆN MỚI (BUG 2.2-TAIL, CHECKPOINT.MD)
# ==============================================================================

class TestBug22TailAllowedUntrackedPaths:
    def test_integrate_respects_allowed_untracked_paths(self, temp_git_repo: Path):
        """Bug 2.2-tail: integrate_transaction must honor allowed_untracked_paths instead of failing cleanliness check."""
        # 1. Create an unrelated untracked file allowed by runtime config
        unrelated_file = temp_git_repo / "unrelated_secret.txt"
        unrelated_file.write_text("unrelated secret data", encoding="utf-8")

        # 2. Runtime configured with allowed_untracked_paths
        runtime = DSHRuntime(
            temp_git_repo,
            dry_run=False,
            allowed_untracked_paths=["unrelated_secret.txt"],
            test_mode=True,
        )

        task = TaskDefinition(
            task_id="T_GIT_ALLOW_01",
            title="Commit with allowed untracked file in workspace",
            allowed_files=["Player.cs"],
        )
        proposal = PatchProposal(
            patches=[
                FilePatch(
                    file="Player.cs",
                    hunks=[PatchHunk(old_text="        int x = 1;", new_text="        int x = 99;")],
                )
            ]
        )

        res = runtime.execute_transaction(task, proposal)

        # CONTRACT ASSERTIONS:
        # Currently FAILS (RED) because core/workspace.py:515 integrate_transaction calls is_clean()
        # without passing allowed_untracked_paths -> deems workspace dirty -> returns READY_TO_INTEGRATE -> success=False.
        # After Dev fixes integrate_transaction plumbing, these will pass (GREEN).
        assert res.success is True, f"Expected success=True but got False. Error: {res.error_message}"
        assert res.integration_status == "INTEGRATED", (
            f"Expected integration_status='INTEGRATED', got: {res.integration_status}"
        )

        # Check git log in main repo - unrelated_secret.txt must NOT be committed
        res_diff = subprocess.run(
            ["git", "show", "--name-only", "--pretty=", "HEAD"],
            cwd=temp_git_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        committed_files = res_diff.stdout.strip().splitlines()
        assert "unrelated_secret.txt" not in committed_files, "unrelated_secret.txt was improperly committed!"
        assert "Player.cs" in committed_files

        # Unrelated file survives in main workspace untouched
        assert (temp_git_repo / "unrelated_secret.txt").read_text(encoding="utf-8") == "unrelated secret data"
