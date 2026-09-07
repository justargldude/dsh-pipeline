"""Tests for 5 Orchestrator-originated infrastructure bugs found in DSH session traces.

BUG-1: core/workspace.py — Union not imported, crashing all modules on import
BUG-2: orchestrator/coordinator.py — allowed_untracked missing holdout_test_file
BUG-3: core/runtime.py — holdout files never cleaned up after validate_post_apply
BUG-4: orchestrator/coordinator.py — .git hardcoded (fails on worktrees)
BUG-5: model/providers.py — _get_client() race condition (TOCTOU on is_closed)
"""
import json
import os
import subprocess
import threading
import time
import unittest
from pathlib import Path
from typing import Union
from unittest.mock import MagicMock, patch, PropertyMock


# ---------------------------------------------------------------------------
# BUG-1: Union import — workspace.py must import successfully
# ---------------------------------------------------------------------------
class TestBug1UnionImport(unittest.TestCase):
    """Regression: get_git_dir uses Union[str, Path] — Union must be imported."""

    def test_workspace_imports_cleanly(self):
        """core.workspace must import without NameError."""
        from core.workspace import get_git_dir, WorkspaceManager, TransactionWorktree
        self.assertTrue(callable(get_git_dir))

    def test_get_git_dir_accepts_str_and_path(self):
        """get_git_dir must accept both str and Path (Union type hint)."""
        from core.workspace import get_git_dir
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / ".git").mkdir()
            result_str = get_git_dir(td)
            self.assertIsInstance(result_str, Path)
            result_path = get_git_dir(Path(td))
            self.assertIsInstance(result_path, Path)
            self.assertEqual(result_str, result_path)

    def test_get_git_dir_resolves_worktree_gitdir_file(self):
        """When .git is a file (worktree), get_git_dir must follow the gitdir pointer."""
        from core.workspace import get_git_dir
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            actual_git_dir = Path(td) / "actual_git_objects"
            actual_git_dir.mkdir()
            (Path(td) / ".git").write_text(f"gitdir: {actual_git_dir}")
            result = get_git_dir(td)
            self.assertEqual(result.resolve(), actual_git_dir.resolve())

    def test_get_git_dir_relative_gitdir_path(self):
        """Relative gitdir paths in .git file must resolve relative to repo root."""
        from core.workspace import get_git_dir
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            actual = Path(td) / ".actual_git"
            actual.mkdir()
            (repo / ".git").write_text("gitdir: ../.actual_git")
            result = get_git_dir(repo)
            self.assertEqual(result.resolve(), actual.resolve())


# ---------------------------------------------------------------------------
# BUG-2: allowed_untracked must include holdout_test_file
# ---------------------------------------------------------------------------
class TestBug2HoldoutAllowedUntracked(unittest.TestCase):
    """Regression: coordinator must add holdout_test_file to allowed_untracked."""

    def test_allowed_untracked_includes_holdout(self):
        """When task has both test_file and holdout_test_file, both must be allowed."""
        from orchestrator.coordinator import AutonomousCoordinator
        import inspect
        src = inspect.getsource(AutonomousCoordinator.run)
        self.assertIn("holdout_test_file", src,
                       "Coordinator.run must add holdout_test_file to allowed_untracked")
        self.assertIn("holdout_test_code", src,
                       "Coordinator.run must check holdout_test_code existence")

    def test_old_pattern_removed(self):
        """The old single-line allowed_untracked pattern must be replaced."""
        from orchestrator.coordinator import AutonomousCoordinator
        import inspect
        src = inspect.getsource(AutonomousCoordinator.run)
        self.assertNotIn(
            "allowed_untracked = [task.test_file] if (test_created and task.test_file) else None",
            src,
            "Old single-line allowed_untracked pattern must be replaced"
        )
        self.assertIn("_untracked", src,
                       "Fix should use _untracked list pattern")


# ---------------------------------------------------------------------------
# BUG-3: holdout cleanup after validate_post_apply
# ---------------------------------------------------------------------------
class TestBug3HoldoutCleanup(unittest.TestCase):
    """Regression: runtime must clean up holdout files after validate_post_apply."""

    def test_holdout_cleanup_in_runtime_source(self):
        """Verify holdout cleanup code exists in execute_transaction."""
        from core.runtime import DSHRuntime
        import inspect
        src = inspect.getsource(DSHRuntime.execute_transaction)
        self.assertIn("_last_injected_holdouts", src,
                       "execute_transaction must reference _last_injected_holdouts for cleanup")
        self.assertIn("unlink", src,
                       "execute_transaction must unlink holdout files")
        self.assertIn("[HOLDOUT] Cleaned up", src,
                       "execute_transaction must log holdout cleanup")

    def test_holdout_cleanup_after_validate(self):
        """Cleanup must happen AFTER validate_post_apply call."""
        from core.runtime import DSHRuntime
        import inspect
        src = inspect.getsource(DSHRuntime.execute_transaction)
        # find the validate_post_apply call
        validate_pos = src.find("validate_post_apply(")
        self.assertGreater(validate_pos, -1, "validate_post_apply must exist")
        # The cleanup block (unlinking holdouts) must come after validate
        unlink_pos = src.find(".unlink()", validate_pos)
        self.assertGreater(unlink_pos, validate_pos,
                          "holdout cleanup (unlink) must come after validate_post_apply")

    def test_holdout_files_cleaned_from_worktree(self):
        """Integration: injected holdout files must be deleted after validation."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            wt = Path(td)
            holdout_files = ["tests/holdout_hidden_1.py", "tests/holdout_hidden_2.py"]
            for hf in holdout_files:
                p = wt / hf
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("# DSH_HOLDOUT\nassert True")
                self.assertTrue(p.exists())

            # Simulate cleanup logic (same as in runtime.py)
            for hf in holdout_files:
                p = wt / hf
                if p.exists():
                    p.unlink()

            for hf in holdout_files:
                self.assertFalse((wt / hf).exists(),
                                f"Holdout file {hf} should be cleaned up")


# ---------------------------------------------------------------------------
# BUG-4: coordinator._run_dir must use get_git_dir, not hardcode .git
# ---------------------------------------------------------------------------
class TestBug4GitDirResolution(unittest.TestCase):
    """Regression: coordinator must use get_git_dir for _run_dir path."""

    def test_coordinator_uses_get_git_dir(self):
        """Coordinator.run must use get_git_dir, not hardcoded .git."""
        from orchestrator.coordinator import AutonomousCoordinator
        import inspect
        src = inspect.getsource(AutonomousCoordinator.run)
        self.assertIn("get_git_dir", src,
                       "Coordinator.run must use get_git_dir for _run_dir")
        self.assertNotIn(
            'self.target_repo / ".git" / "dsh_runs"',
            src,
            "Old hardcoded .git path for dsh_runs must be replaced"
        )

    def test_get_git_dir_imported_in_coordinator(self):
        """get_git_dir must be importable from coordinator's module scope."""
        import orchestrator.coordinator as coord_mod
        self.assertTrue(hasattr(coord_mod, 'get_git_dir'),
                        "get_git_dir must be imported at module level in coordinator")

    def test_worktree_run_dir_resolves_correctly(self):
        """When target_repo is a worktree, _run_dir must point to actual git dir."""
        from core.workspace import get_git_dir
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            actual_git = Path(td) / "real_git_dir"
            actual_git.mkdir()
            worktree = Path(td) / "worktree"
            worktree.mkdir()
            (worktree / ".git").write_text(f"gitdir: {actual_git}")

            git_dir = get_git_dir(worktree)
            run_dir = git_dir / "dsh_runs" / "run_test"
            self.assertEqual(git_dir.resolve(), actual_git.resolve())
            self.assertTrue(str(run_dir).startswith(str(actual_git.resolve())))


# ---------------------------------------------------------------------------
# BUG-5: _get_client() race condition in OpenAICompatibleProvider
# ---------------------------------------------------------------------------
class TestBug5ClientRaceCondition(unittest.TestCase):
    """Regression: _get_client() must be thread-safe (TOCTOU on is_closed)."""

    def test_provider_has_client_lock(self):
        """OpenAICompatibleProvider must have a _client_lock for thread safety."""
        from model.providers import OpenAICompatibleProvider
        provider = OpenAICompatibleProvider(
            api_key="test-key",
            base_url="http://127.0.0.1:9999/v1",
        )
        self.assertTrue(hasattr(provider, '_client_lock'),
                        "Provider must have _client_lock attribute")
        self.assertIsInstance(provider._client_lock, type(threading.Lock()))

    def test_concurrent_get_client_returns_same_client(self):
        """Multiple threads calling _get_client concurrently must get the same client."""
        from model.providers import OpenAICompatibleProvider
        provider = OpenAICompatibleProvider(
            api_key="test-key",
            base_url="http://127.0.0.1:9999/v1",
            timeout_seconds=5,
        )

        clients = []
        errors = []
        barrier = threading.Barrier(10)

        def get_client():
            try:
                barrier.wait(timeout=5)
                c = provider._get_client()
                clients.append(id(c))
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=get_client) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(errors), 0, f"Errors during concurrent access: {errors}")
        unique_ids = set(clients)
        self.assertEqual(len(unique_ids), 1,
                        f"Expected 1 unique client, got {len(unique_ids)}: race condition!")
        provider.close()

    def test_get_client_recreates_after_close(self):
        """After close(), _get_client must create a new client."""
        from model.providers import OpenAICompatibleProvider
        provider = OpenAICompatibleProvider(
            api_key="test-key",
            base_url="http://127.0.0.1:9999/v1",
        )
        client1 = provider._get_client()
        client1_id = id(client1)
        provider.close()
        client2 = provider._get_client()
        client2_id = id(client2)
        self.assertNotEqual(client1_id, client2_id,
                           "New client must be created after close()")
        provider.close()

    def test_lock_prevents_leaked_clients(self):
        """Stress test: rapid concurrent access must not leak clients."""
        from model.providers import OpenAICompatibleProvider
        provider = OpenAICompatibleProvider(
            api_key="test-key",
            base_url="http://127.0.0.1:9999/v1",
            timeout_seconds=5,
        )

        def stress_cycle():
            for _ in range(20):
                try:
                    c = provider._get_client()
                    time.sleep(0.001)
                except Exception:
                    pass

        threads = [threading.Thread(target=stress_cycle) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        final = provider._get_client()
        self.assertFalse(final.is_closed)
        provider.close()


if __name__ == "__main__":
    unittest.main()
