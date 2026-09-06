"""
Comprehensive Regression & Characterization Test Suite for DSH Pipeline Investigation Gaps.

This file reproduces and characterizes the 14+ bugs and bizarre behaviors discovered
during the static and dynamic analysis of DSH Pipeline:
- Group 1: Coordinator & Workspace Pollution (Bug #4, Bug #7, Bug #7')
- Group 2: Baseline & Regression Paradoxes (Bug 10 / Doc 4 2.1, Bug 6 / Doc 4 2.2)
- Group 3: AST Guard & Scope Guard Logic (Bug 11, Bug 12, Bug B)
- Group 4: Concurrency & Recovery Loop (Bug A, Bug C, Bug D)
- Group 5: Security & Budget Vulnerabilities (Bug 1.1, Bug 1.2, Bug 1.3)
- Group 6: Disputed Behaviors / Tradeoffs (Item 13)

Tests asserting correct expected behavior on unfixed bugs are expected to FAIL (red)
on the current codebase, providing the TDD targets for subsequent refactoring.
"""

import difflib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from build.sandbox import run_hardened_command
from context.budget import ContextComplexity, TokenBudgetManager
from core.config import PipelineConfig
from core.journal import JournalRecord, TransactionJournal
from core.runtime import DSHRuntime
from core.state import TransactionState
from core.workspace import WorkspaceManager
from model.providers import MockModelProvider
from orchestrator.coordinator import AutonomousCoordinator, TaskExecutionRecord
from orchestrator.planner import PlannedTask
from orchestrator.subagents import AntigravityClient, DeepSeekClient
from recovery.classifier import FailureType
from recovery.history import RecoveryHistory
from safety.ast_guard import ASTGuard, ASTViolationError
from safety.patch_engine import PatchValidationError, _locate_unique_match
from safety.scope_guard import ScopeGuard, ScopeViolationError
from task.schema import FilePatch, PatchHunk, PatchProposal, TaskDefinition, TransactionResult
from taskqueue.cloud_worker import CloudTaskItem
from validation.baseline import BaselineManager, BaselineState
from validation.regression import RegressionCheckResult, SubprocessRegressionValidator


def _init_test_git_repo(path: Path) -> Path:
    """Helper to initialize an isolated git repo with initial commit."""
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@pipeline.local"], cwd=path, check=True)
    (path / "README.md").write_text("# Initial Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "Initial commit"], cwd=path, check=True)
    return path


# ==============================================================================
# NHÓM 1: CRASH & MAIN WORKSPACE POLLUTION (COORDINATOR & WORKSPACE)
# ==============================================================================

class TestGroup1_CoordinatorWorkspacePollution:
    """Tests for coordinator crashes on failure and workspace self-pollution."""

    def test_bug_4_task_execution_record_rejects_empty_string_verdict(self):
        """Bug #4: review_verdict='' raises ValidationError when task fails.

        TaskExecutionRecord.review_verdict expects Optional[Dict[str, Any]],
        so assigning an empty string '' causes Pydantic to raise ValidationError.
        """
        # A valid dict or None is allowed
        record_none = TaskExecutionRecord(
            task_id="T1", title="Test", success=False, review_verdict=None
        )
        assert record_none.review_verdict is None

        # On the unfixed coordinator code, review_verdict was initialized to ""
        # Passing "" must raise ValidationError
        with pytest.raises(Exception):
            TaskExecutionRecord(
                task_id="T1", title="Test", success=False, review_verdict=""  # type: ignore
            )

    def test_bug_7_manifest_init_dirty_gitignore_blocks_integration(self, tmp_path: Path):
        """Bug #7: _manifest_init() mutates .gitignore on main without commit or allowance.

        When _manifest_init modifies .gitignore in target_repo, .gitignore becomes
        tracked-modified ('M .gitignore'). When integrate_transaction runs, is_clean()
        evaluates to False because .gitignore is not in allowed_untracked_paths,
        causing all subsequent integrations to fail with READY_TO_INTEGRATE.
        """
        repo = _init_test_git_repo(tmp_path / "repo")
        (repo / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "Add gitignore"], cwd=repo, check=True)

        coord = AutonomousCoordinator(
            target_repo=repo,
            qa_name="agy",
            dev_name="deepseek",
            dry_run=False,
            test_mode=True,
        )
        run_dir = repo / "run_test_001"
        coord._manifest_init(run_dir)

        # Create a valid transaction worktree from current HEAD
        ws = WorkspaceManager(repo)
        base_commit = ws.get_head_commit()
        tx_worktree = ws.create_transaction_worktree(
            tx_id="tx_bug7",
            base_commit=base_commit,
        )
        # Commit a simple change in worktree
        (tx_worktree.worktree_path / "feature.txt").write_text("done\n", encoding="utf-8")
        tx_worktree.stage_exact(["feature.txt"])
        tx_commit = tx_worktree.commit("feature.txt")

        # Attempt to integrate with manifest allowance
        allowance = coord._untracked_allowance_for_manifest()
        status = ws.integrate_transaction(
            tx_worktree,
            tx_commit,
            allowed_untracked_paths=allowance,
        )

        # Clean worktree
        ws.remove_transaction_worktree(tx_worktree)

        # Expected: A healthy coordinator should allow integration.
        # BUG: Fails because status is 'READY_TO_INTEGRATE' due to dirty .gitignore!
        assert status == "INTEGRATED", (
            f"Expected INTEGRATED, but got '{status}' because .gitignore was mutated on main!"
        )

    def test_bug_7_variant_untracked_red_test_on_main_blocks_task2(self, tmp_path: Path):
        """Bug #7 Variant: QA red test written to main repo blocks task 2 integration.

        _write_red_test_to_main writes the red test directly into target_repo.
        Because it is untracked and not in allowed_untracked_paths, when Task 2 completes
        in its worktree and tries to integrate, is_clean() sees the untracked file
        and rejects integration.
        """
        repo = _init_test_git_repo(tmp_path / "repo")
        coord = AutonomousCoordinator(
            target_repo=repo,
            dry_run=False,
            test_mode=True,
        )

        task1 = PlannedTask(
            task_id="TASK_001",
            title="Task 1",
            description="T1",
            allowed_files=["code.py"],
            test_file="tests/test_red_1.py",
            test_code="def test_fail(): assert False",
        )
        coord._write_red_test_to_main(task1)

        # Now simulate Task 2 executing in an isolated worktree
        ws = WorkspaceManager(repo)
        tx_worktree2 = ws.create_transaction_worktree(
            tx_id="tx_task2",
            base_commit=ws.get_head_commit(),
        )
        (tx_worktree2.worktree_path / "task2.py").write_text("x = 1\n", encoding="utf-8")
        tx_worktree2.stage_exact(["task2.py"])
        commit2 = tx_worktree2.commit("task2.py")

        status2 = ws.integrate_transaction(
            tx_worktree2,
            commit2,
            allowed_untracked_paths=coord._untracked_allowance_for_manifest(),
        )
        ws.remove_transaction_worktree(tx_worktree2)

        # Expected: Task 2 should integrate cleanly.
        # BUG: Fails because status2 is 'READY_TO_INTEGRATE' due to leftover test_red_1.py!
        assert status2 == "INTEGRATED", (
            f"Expected INTEGRATED for Task 2, but got '{status2}' because of leftover red test on main!"
        )


# ==============================================================================
# NHÓM 2: BASELINE & REGRESSION PARADOXES
# ==============================================================================

class TestGroup2_BaselineAndRegressionParadoxes:
    """Tests for baseline capture order and regression comparison jitter."""

    def test_bug_10_doc4_21_baseline_paradox_masks_unfixed_red_test(self, tmp_path: Path):
        """Bug 10 / Doc 4 (2.1): 'Baseline Paradox'.

        If on_worktree_created copies the red test into the worktree BEFORE baseline
        is captured, baseline runs the test suite including the red test and records
        it as a pre-existing failure.
        If the Dev model produces a patch that does NOT fix the red test, post-apply
        validation sees the exact same failure. compare_regression computes
        (post - baseline) = 0 new failures, and T3 passes (False Green)!
        """
        # Baseline captured with broken test 'test_red_bug'
        baseline_reg = RegressionCheckResult(
            success=False,
            broken_tests=["test_red_bug: assertion failed"],
            output="FAILED test_red_bug",
        )
        # Post-apply runs; bug is STILL not fixed
        post_reg = RegressionCheckResult(
            success=False,
            broken_tests=["test_red_bug: assertion failed"],
            output="FAILED test_red_bug",
        )

        is_regression, new_broken = BaselineManager.compare_regression(baseline_reg, post_reg)

        # In current logic, is_regression is FALSE because set difference is empty!
        # This demonstrates the baseline paradox:
        assert is_regression is False
        assert len(new_broken) == 0
        # If the red test was intended to enforce a fix, T3 falsely concludes success.

    def test_bug_10_ordering_baseline_must_precede_red_test_injection(self, tmp_path: Path):
        """Bug 10 Contract: Baseline must be captured BEFORE on_worktree_created injects red tests.

        In DSHRuntime.execute_transaction:
        - Baseline represents the clean, unadulterated state of the base commit.
        - Red tests must be injected AFTER baseline capture so that they are evaluated
          as new requirements, not pre-existing failures.
        """
        repo = _init_test_git_repo(tmp_path / "repo")
        runtime = DSHRuntime(repo, test_mode=True)

        call_order = []

        orig_capture = runtime.baseline_manager.get_or_capture_baseline
        def spy_capture(*args, **kwargs):
            call_order.append("BASELINE_CAPTURED")
            return orig_capture(*args, **kwargs)
        runtime.baseline_manager.get_or_capture_baseline = spy_capture

        def spy_on_worktree_created(wt_path):
            call_order.append("RED_TEST_INJECTED")
        runtime.on_worktree_created = spy_on_worktree_created

        task = TaskDefinition(
            task_id="TASK_ORDER",
            title="Order Test",
            allowed_files=["README.md"],
        )
        proposal = PatchProposal(
            patches=[
                FilePatch(
                    file="README.md",
                    hunks=[PatchHunk(old_text="# Initial Repo", new_text="# Updated Repo")]
                )
            ]
        )

        runtime.execute_transaction(task, proposal)

        # Expected: Clean baseline captured first, then red test injected.
        # BUG: Currently on_worktree_created is called at line 360, while baseline is at line 412!
        # So call_order is ['RED_TEST_INJECTED', 'BASELINE_CAPTURED']
        assert call_order == ["BASELINE_CAPTURED", "RED_TEST_INJECTED"], (
            f"Ordering violation (Baseline Paradox): Expected baseline before injection, but got: {call_order}"
        )

    def test_bug_6_doc4_22_regression_timing_jitter_causes_false_regression(self):
        """Bug 6 / Doc 4 (2.2): Test runner timing [XX ms] produces false positive regression.

        When runner output includes execution duration like 'Failed TestMethod [12 ms]'
        vs 'Failed TestMethod [18 ms]', compare_regression treats it as a new broken test.
        """
        validator = SubprocessRegressionValidator(test_cmd=["dummy"])

        baseline_raw = "Failed TestNamespace.TestClass.TestMethod [12 ms]\n"
        post_raw = "Failed TestNamespace.TestClass.TestMethod [18 ms]\n"

        baseline_broken = validator._parse_broken_tests(baseline_raw)
        post_broken = validator._parse_broken_tests(post_raw)

        baseline_reg = RegressionCheckResult(success=False, broken_tests=baseline_broken)
        post_reg = RegressionCheckResult(success=False, broken_tests=post_broken)

        is_regression, new_broken = BaselineManager.compare_regression(baseline_reg, post_reg)

        # Expected: Same test failed in both, so no new regression should be flagged.
        # BUG: Fails because raw line comparison treats [18 ms] as a brand new broken test!
        assert is_regression is False, (
            f"Expected is_regression=False for same test with different timing, but got new_broken={new_broken}"
        )


# ==============================================================================
# NHÓM 3: AST GUARD & SCOPE GUARD LOGIC
# ==============================================================================

class TestGroup3_ASTGuardAndScopeGuard:
    """Tests for constructor signature changes, domain assert methods, and diff budget."""

    def test_bug_11_ast_guard_constructor_signature_change_when_no_targets(self):
        """Bug 11: Changing constructor signature when target_symbols=None is rejected as deletion.

        ASTGuard.validate_csharp_transition only exempts signature changes when
        target_symbols is non-empty. When target_symbols=None (allowing full file edit),
        changing a constructor signature is falsely flagged as method deletion.
        """
        guard = ASTGuard()
        old_code = """
        public class ConnectionPool {
            public ConnectionPool() {
                Init();
            }
        }
        """
        new_code = """
        public class ConnectionPool {
            public ConnectionPool(int maxConnections) {
                Init(maxConnections);
            }
        }
        """
        # With target_symbols=None, this valid refactor should be ALLOWED.
        # BUG: Raises ASTViolationError("Disallowed method deletion: ['ConnectionPool()']")
        try:
            guard.validate_csharp_transition(
                old_code=old_code,
                new_code=new_code,
                file_path="ConnectionPool.cs",
                target_symbols=None,
            )
        except ASTViolationError as e:
            pytest.fail(f"Valid constructor signature change with target_symbols=None was rejected: {e}")

    def test_bug_12_ast_guard_misclassifies_domain_assert_method(self):
        """Bug 12: Business method token.AssertOwnership() misclassified as test assertion.

        ast_guard._extract_assert_predicates checks 'assert' in callee_text.lower().
        If domain code has a method like token.AssertOwnership(), refactoring or removing
        it is falsely treated as removing a test assertion.
        """
        guard = ASTGuard()
        old_code = """
        public class TokenService {
            public void Transfer(Token token, User recipient) {
                token.AssertOwnership();
                token.Owner = recipient;
            }
        }
        """
        # Refactor token.AssertOwnership() to token.ValidateOwnership()
        new_code = """
        public class TokenService {
            public void Transfer(Token token, User recipient) {
                token.ValidateOwnership();
                token.Owner = recipient;
            }
        }
        """
        # Expected: Domain method refactor should pass ASTGuard.
        # BUG: Raises ASTViolationError because it thinks an assertion was removed!
        try:
            guard.validate_csharp_transition(
                old_code=old_code,
                new_code=new_code,
                file_path="TokenService.cs",
                target_symbols=None,
            )
        except ASTViolationError as e:
            pytest.fail(f"Domain method AssertOwnership() was falsely treated as a test assertion: {e}")

    def test_bug_b_scope_guard_anchor_lines_counted_as_deleted(self, tmp_path: Path):
        """Bug B: ScopeGuard counts hunk anchor lines as deleted lines.

        When inserting code after an anchor with max_lines_deleted=0,
        ScopeGuard adds len(old_lines) to total_deleted and rejects the patch.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        target_file = repo / "Config.cs"
        target_file.write_text("public class Config {\n    int x = 1;\n}\n", encoding="utf-8")

        guard = ScopeGuard()
        task = TaskDefinition(
            task_id="TASK_ADD",
            title="Add field",
            allowed_files=["Config.cs"],
            max_lines_added=10,
            max_lines_deleted=0,  # Pure addition
        )
        # Hunk matches 'int x = 1;' anchor and appends 'int y = 2;'
        proposal = PatchProposal(
            patches=[
                FilePatch(
                    file="Config.cs",
                    hunks=[
                        PatchHunk(
                            old_text="    int x = 1;",
                            new_text="    int x = 1;\n    int y = 2;",
                        )
                    ],
                )
            ]
        )

        # Expected: 0 lines deleted in reality, should pass.
        # BUG: ScopeGuard calculates total_deleted = 1 and raises ScopeViolationError!
        try:
            guard.validate_pre_apply(task, proposal, repo_root=repo)
        except ScopeViolationError as e:
            pytest.fail(f"ScopeGuard rejected pure insertion with anchor context: {e}")


# ==============================================================================
# NHÓM 4: CONCURRENCY & RECOVERY LOOP
# ==============================================================================

class TestGroup4_ConcurrencyAndRecovery:
    """Tests for orphaned worktree cleanup, environment leakage, and history truncation."""

    def test_bug_a_cleanup_orphaned_worktrees_wipes_pending_integration(self, tmp_path: Path):
        """Bug A: cleanup_orphaned_worktrees() deletes STALE_BASE / READY_TO_INTEGRATE worktrees.

        When a parallel task finishes validation and commits, but integration is deferred
        (STALE_BASE), its worktree must be preserved.
        However, cleanup_orphaned_worktrees() indiscriminately deletes any worktree
        present in the journal, destroying validated commits.
        """
        repo = _init_test_git_repo(tmp_path / "repo")
        ws = WorkspaceManager(repo)

        tx_wt = ws.create_transaction_worktree("tx_stale_1", ws.get_head_commit())
        (tx_wt.worktree_path / "stale_feature.txt").write_text("commit\n", encoding="utf-8")
        tx_wt.stage_exact(["stale_feature.txt"])
        commit_hash = tx_wt.commit("stale_feature.txt")

        # Mark in journal as STALE_BASE
        ws.journal.update_state("tx_stale_1", TransactionState.STALE_BASE)

        # Run orphan cleanup
        cleaned = ws.cleanup_orphaned_worktrees()

        # Expected: STALE_BASE worktrees should NOT be wiped as orphans.
        # BUG: tx_stale_1 is cleaned and deleted!
        assert "tx_stale_1" not in cleaned, (
            "cleanup_orphaned_worktrees() wiped a STALE_BASE worktree awaiting rebase/integration!"
        )
        assert tx_wt.worktree_path.exists(), "Worktree directory was destroyed!"

    def test_bug_c_pipeline_env_breaks_test_mode_runner(self, tmp_path: Path, monkeypatch):
        """Bug C: PIPELINE_* env vars override test_mode=True default mock runner.

        PipelineConfig reads PIPELINE_BUILD_COMMAND from env. DSHRuntime checks
        config.build_command before test_mode, instantiating SubprocessBuildRunner
        even when test_mode=True.
        """
        repo = _init_test_git_repo(tmp_path / "repo")
        monkeypatch.setenv("PIPELINE_BUILD_COMMAND", '["nonexistent_build_tool"]')

        runtime = DSHRuntime(repo, test_mode=True)

        from build.sandbox import MockBuildRunner
        # Expected: test_mode=True guarantees MockBuildRunner for isolated tests.
        # BUG: build_runner is SubprocessBuildRunner!
        assert isinstance(runtime.build_runner, MockBuildRunner), (
            f"Expected MockBuildRunner under test_mode=True, but got {type(runtime.build_runner)}"
        )

    def test_bug_d_tier1_history_truncation_drops_latest_attempt(self):
        """Bug D (Tầng 1): RecoveryHistory.format_history_for_prompt() truncates from the end,
        losing the latest attempt.

        result[:max_history_chars] keeps Attempt 1 and cuts off Attempt 3,
        preventing the Dev model from seeing the most recent error.
        """
        history = RecoveryHistory(task_id="T_LOOP")
        for i in range(1, 4):
            history.record_attempt(
                attempt_index=i,
                model_type_used="deepseek",
                failure_type=FailureType.SYNTAX,
                error_message=f"Detailed syntax error message for attempt {i} " * 10,
            )

        # Format with strict limit
        formatted = history.format_history_for_prompt(max_history_chars=400)

        # Expected: The prompt MUST include the most recent attempt (Attempt 3).
        # BUG: Formatted history retains Attempt 1 and cuts off Attempt 3!
        assert "Attempt 3" in formatted, (
            "format_history_for_prompt() sliced the tail, dropping Attempt 3!"
        )

    def test_bug_d_tier2_error_preslice_drops_root_cause_header(self):
        """Bug D (Tầng 2): err_summary[-300:] pre-slice drops root-cause header.

        When an error traceback has a critical root cause at the beginning
        (e.g., 'CS0246: The type or namespace name Widget could not be found' or 'SyntaxError: ...')
        followed by a long 500-char traceback or verbose warnings,
        history.py:72-73 takes ONLY the last 300 chars ('...' + err_summary[-300:]),
        completely discarding the root-cause error header!
        """
        history = RecoveryHistory(task_id="T_ROOT_CAUSE")
        root_cause_header = "CS0246: The type or namespace name 'PlayerController' could not be found"
        long_trailing_garbage = "\n  at Namespace.Compiler.InternalParser.ParseTree() [verbose diagnostic]\n" * 15
        full_error = f"{root_cause_header}\n{long_trailing_garbage}"
        assert len(full_error) > 600

        history.record_attempt(
            attempt_index=1,
            model_type_used="deepseek",
            failure_type=FailureType.TYPE_SEMANTIC,
            error_message=full_error,
        )

        formatted = history.format_history_for_prompt(max_history_chars=2000)

        # Expected: The prompt MUST preserve the root cause header from the top of the error message!
        # BUG: Fails because err_summary = '...' + err_summary[-300:] chops off the top 300+ characters!
        assert root_cause_header in formatted, (
            "format_history_for_prompt() pre-sliced [-300:], discarding the root-cause header at the beginning!"
        )


# ==============================================================================
# NHÓM 5: BẢO MẬT & QUẢN LÝ TOKEN (DOC 4)
# ==============================================================================

class TestGroup5_SecurityAndTokenBudget:
    """Tests for UNLIMITED_MODE hardcode, symlink escaping, and cloud worker defaults."""

    def test_bug_1_1_token_budget_unlimited_mode_hardcoded(self, monkeypatch):
        """Bug 1.1: TokenBudgetManager.UNLIMITED_MODE = True is hardcoded.

        Bypasses token budgets and returns 2,000,000 tokens by default.
        """
        monkeypatch.delenv("DSH_UNLIMITED_BUDGET", raising=False)
        budget = TokenBudgetManager.compute_available_budget(ContextComplexity.NORMAL)

        # Expected: NORMAL complexity budget is ~6,000 tokens (minus reserved).
        # BUG: Returns 2,000,000 because UNLIMITED_MODE=True is hardcoded!
        assert budget <= 10000, (
            f"Expected budget <= 10000, but got {budget} due to hardcoded UNLIMITED_MODE!"
        )

    def test_bug_1_2_symlink_traversal_escapes_target_repo(self, tmp_path: Path):
        """Bug 1.2: Reading files into prompt does not check relative_to after resolve().

        If a symlink inside the target repo points to a sensitive file outside
        (e.g. /tmp/secret.txt), resolve() escapes the repository root.
        """
        repo = _init_test_git_repo(tmp_path / "repo")
        secret_file = tmp_path / "secret.txt"
        secret_file.write_text("SUPER_SECRET_TOKEN=xyz123\n", encoding="utf-8")

        symlink_path = repo / "symlink_secret.txt"
        try:
            symlink_path.symlink_to(secret_file)
        except OSError:
            pytest.skip("Symlinks not supported in this filesystem")

        # Simulate cli.py / planner.py file resolution:
        resolved = (repo / "symlink_secret.txt").resolve()

        # Expected: An authoritative security check must ensure resolved is inside repo.
        # Current code simply checks resolved.exists() and calls read_text()!
        assert resolved.is_relative_to(repo), (
            f"Security flaw: Symlink resolved to outside repo: {resolved}"
        )

    def test_bug_1_3_cloud_worker_hardcoded_personal_path(self):
        """Bug 1.3: CloudTaskItem defaults to hardcoded personal path.

        cloud_worker.py has default target_repo pointing to a developer's home folder.
        """
        task_item = CloudTaskItem(id="CT_1", title="Test")
        # Expected: target_repo should not have a hardcoded personal Linux path default.
        assert not task_item.target_repo.startswith("/home/"), (
            f"Security/Config flaw: CloudTaskItem has personal hardcoded path: {task_item.target_repo}"
        )


# ==============================================================================
# NHÓM 6: ĐÓNG BĂNG HÀNH VI ĐANG TRANH CÃI (TRADEOFFS)
# ==============================================================================

class TestGroup6_DisputedBehaviorsAndTradeoffs:
    """Characterization tests for disputed design tradeoffs."""

    def test_tradeoff_13_locate_unique_match_ignores_overlapping_patterns(self):
        """Tradeoff #13: _locate_unique_match allows 'aa' inside 'aaa'.

        Characterizes the current behavior: find(old, pos + len(old)) finds only
        non-overlapping occurrences, so 'aa' in 'aaa' returns 0 instead of raising
        an ambiguous match exception.
        """
        content = "aaa"
        old_text = "aa"
        # Current implementation succeeds because non-overlapping count is 1:
        pos = _locate_unique_match(content, old_text, "test.txt", 0)
        assert pos == 0

        # Note: If strict non-ambiguity is enforced, this should raise PatchValidationError.
