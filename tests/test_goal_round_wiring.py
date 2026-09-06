"""Goal-round verification tests — the three production-wiring gaps.

1. safety/policy.py owns dev_forbidden_test_file_patterns (policy layer is
   the single source of truth; ScopeGuard consults it).
2. Coordinator loads PlannedTask holdout into runtime._pending_holdouts
   (otherwise the holdout_injector slot never fires in production).
3. Coordinator runs the D2 MetamorphicCheck against the real test command
   when not in test_mode (gate wired, not dead code).
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from safety.policy import SafetyPolicy
from safety.scope_guard import ScopeGuard, ScopeViolationError
from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk
from orchestrator.planner import PlannedTask


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    def run(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (repo / "Player.cs").write_text("public class Player { public int Hp; }\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "init")
    return repo


# ==============================================================================
# Gap 1 — policy owns the test-file ban patterns
# ==============================================================================

class TestPolicyOwnsTestFileBan:
    def test_policy_field_exists_with_defaults(self):
        p = SafetyPolicy()
        assert "*Test.cs" in p.dev_forbidden_test_file_patterns
        assert "test_*.py" in p.dev_forbidden_test_file_patterns
        assert "tests/*" in p.dev_forbidden_test_file_patterns

    def test_scope_guard_uses_policy_patterns(self, tmp_path):
        """A custom policy pattern must be enforced by the ScopeGuard —
        proving the guard reads the policy, not its own constants."""
        repo = _git_repo(tmp_path)
        policy = SafetyPolicy(dev_forbidden_test_file_patterns=["*Spec.cs"])
        guard = ScopeGuard(policy=policy)
        task = TaskDefinition(task_id="T1", title="t", allowed_files=["PlayerSpec.cs"])
        proposal = PatchProposal(
            patches=[FilePatch(file="PlayerSpec.cs", hunks=[PatchHunk(old_text="", new_text="class S {}")])]
        )
        with pytest.raises(ScopeViolationError, match="Spec"):
            guard.validate(task, proposal, repo_path=repo)

    def test_scope_guard_respects_policy_relaxation_not_default(self, tmp_path):
        """A policy that bans only *Spec.cs must NOT reject *Test.cs files:
        the policy list fully replaces the defaults (single source of truth)."""
        repo = _git_repo(tmp_path)
        policy = SafetyPolicy(dev_forbidden_test_file_patterns=["*Spec.cs"])
        guard = ScopeGuard(policy=policy)
        (repo / "PlayerTest.cs").write_text("// t\n", encoding="utf-8")
        task = TaskDefinition(task_id="T2", title="t", allowed_files=["PlayerTest.cs"])
        proposal = PatchProposal(
            patches=[FilePatch(file="PlayerTest.cs", hunks=[PatchHunk(old_text="// t", new_text="// t2")])]
        )
        guard.validate(task, proposal, repo_path=repo)  # must NOT raise


# ==============================================================================
# Gap 2 — coordinator loads holdout into the runtime
# ==============================================================================

class TestCoordinatorHoldoutLoading:
    def test_coordinator_loads_holdout_into_runtime(self, tmp_path, monkeypatch):
        """run() must set runtime._pending_holdouts from the PlannedTask's
        holdout fields so the holdout_injector slot actually fires."""
        from orchestrator.coordinator import AutonomousCoordinator

        repo = _git_repo(tmp_path)

        class _Q:
            name = "mock-qa"
            def query(self, prompt, timeout=None):
                if "Git Diff" in prompt:
                    return json.dumps({"verdict": "APPROVED", "flagged_risks": [], "summary": "ok"})
                return json.dumps({
                    "summary": "s",
                    "detected_framework": "generic",
                    "detected_build_cmd": "",
                    "detected_test_cmd": "",
                    "tasks": [{
                        "task_id": "T_H1",
                        "title": "Holdout wiring",
                        "description": "d",
                        "allowed_files": ["Player.cs"],
                        "test_file": None,
                        "test_code": None,
                        "holdout_test_file": "tests/holdout_T_H1.py",
                        "holdout_test_code": "# DSH_HOLDOUT\nassert True",
                    }],
                })
            def query_json(self, prompt, timeout=None):
                return json.loads(self.query(prompt))

        # Intercept execute_with_recovery to capture the runtime state.
        captured = {}

        _APPROVED = json.dumps({"verdict": "APPROVED", "flagged_risks": [], "summary": "ok"})

        def _fake_exec_with_recovery(self, task, provider, context_builder):
            captured["holdouts"] = list(self._pending_holdouts)
            from task.schema import TransactionResult
            return TransactionResult(
                task_id=task.task_id, success=True, dry_run=True, events=[],
            )

        monkeypatch.setattr(
            "core.runtime.DSHRuntime.execute_with_recovery", _fake_exec_with_recovery
        )

        coord = AutonomousCoordinator(
            target_repo=repo,
            qa_client=_Q(),
            dev_provider=MagicMock(),
            dry_run=True,
            test_mode=False,
        )
        coord.run(user_goal="g", max_tasks=1)

        assert captured["holdouts"], "coordinator must load holdout into runtime._pending_holdouts"
        assert captured["holdouts"][0]["holdout_test_file"] == "tests/holdout_T_H1.py"
        assert "DSH_HOLDOUT" in captured["holdouts"][0]["holdout_test_code"]

    def test_coordinator_empty_holdout_loads_empty(self, tmp_path, monkeypatch):
        """No holdout fields on the task => empty list (injector inert)."""
        from orchestrator.coordinator import AutonomousCoordinator

        repo = _git_repo(tmp_path)

        class _Q:
            name = "mock-qa"
            def query(self, prompt, timeout=None):
                if "Git Diff" in prompt:
                    return json.dumps({"verdict": "APPROVED", "flagged_risks": [], "summary": "ok"})
                return json.dumps({
                    "summary": "s",
                    "detected_framework": "generic",
                    "detected_build_cmd": "",
                    "detected_test_cmd": "",
                    "tasks": [{
                        "task_id": "T_H2",
                        "title": "No holdout",
                        "description": "d",
                        "allowed_files": ["Player.cs"],
                    }],
                })
            def query_json(self, prompt, timeout=None):
                return json.loads(self.query(prompt))

        captured = {}

        def _fake_exec_with_recovery(self, task, provider, context_builder):
            captured["holdouts"] = list(self._pending_holdouts)
            from task.schema import TransactionResult
            return TransactionResult(
                task_id=task.task_id, success=True, dry_run=True, events=[],
            )

        monkeypatch.setattr(
            "core.runtime.DSHRuntime.execute_with_recovery", _fake_exec_with_recovery
        )

        coord = AutonomousCoordinator(
            target_repo=repo,
            qa_client=_Q(),
            dev_provider=MagicMock(),
            dry_run=True,
            test_mode=False,
        )
        coord.run(user_goal="g", max_tasks=1)
        assert captured["holdouts"] == []


# ==============================================================================
# Gap 3 — coordinator runs the D2 metamorphic check in production
# ==============================================================================

class TestCoordinatorMetamorphicWiring:
    def test_metamorphic_runs_when_test_cmd_configured(self, tmp_path, monkeypatch):
        """With a real test command and a test file, run() must invoke the
        metamorphic check (patched here to a stub gate) and record its
        verdict."""
        from orchestrator.coordinator import AutonomousCoordinator

        repo = _git_repo(tmp_path)
        (repo / "test_task.py").write_text("def test_x():\n    assert 1\n", encoding="utf-8")

        class _Q:
            name = "mock-qa"
            def query(self, prompt, timeout=None):
                if "Git Diff" in prompt:
                    return json.dumps({"verdict": "APPROVED", "flagged_risks": [], "summary": "ok"})
                return json.dumps({
                    "summary": "s",
                    "detected_framework": "pytest",
                    "detected_build_cmd": "",
                    "detected_test_cmd": "pytest -q",
                    "tasks": [{
                        "task_id": "T_M1",
                        "title": "Metamorphic wiring",
                        "description": "d",
                        "allowed_files": ["Player.cs"],
                        "test_file": "test_task.py",
                        "test_code": "def test_x():\n    assert 1\n",
                        "test_cmd": "pytest -q",
                    }],
                })
            def query_json(self, prompt, timeout=None):
                return json.loads(self.query(prompt))

        gate_calls = {}

        def _fake_run_metamorphic(self, task, test_cmd):
            gate_calls["task_id"] = task.task_id
            gate_calls["test_cmd"] = test_cmd
            return {"success": True, "skipped": False, "failed_variant": None, "skipped_variants": 3}

        def _fake_exec_with_recovery(self, task, provider, context_builder):
            from task.schema import TransactionResult
            return TransactionResult(
                task_id=task.task_id, success=True, dry_run=True, events=[],
            )

        monkeypatch.setattr(
            "orchestrator.coordinator.AutonomousCoordinator._run_metamorphic_check",
            _fake_run_metamorphic,
        )
        monkeypatch.setattr(
            "core.runtime.DSHRuntime.execute_with_recovery", _fake_exec_with_recovery
        )

        coord = AutonomousCoordinator(
            target_repo=repo,
            qa_client=_Q(),
            dev_provider=MagicMock(),
            dry_run=True,
            test_mode=False,
        )
        result = coord.run(user_goal="g", max_tasks=1)
        assert gate_calls.get("task_id") == "T_M1", "metamorphic check must run in production mode"
        assert gate_calls.get("test_cmd") == "pytest -q"

    def test_metamorphic_skipped_in_test_mode(self, tmp_path, monkeypatch):
        """test_mode must not invoke the metamorphic check (mocks have no
        real test runner)."""
        from orchestrator.coordinator import AutonomousCoordinator

        repo = _git_repo(tmp_path)

        class _Q:
            name = "mock-qa"
            def query(self, prompt, timeout=None):
                if "Git Diff" in prompt:
                    return json.dumps({"verdict": "APPROVED", "flagged_risks": [], "summary": "ok"})
                return json.dumps({
                    "summary": "s",
                    "detected_framework": "generic",
                    "detected_build_cmd": "",
                    "detected_test_cmd": "",
                    "tasks": [{
                        "task_id": "T_M2",
                        "title": "t",
                        "description": "d",
                        "allowed_files": ["Player.cs"],
                        "test_file": "test_task.py",
                        "test_code": "x",
                    }],
                })
            def query_json(self, prompt, timeout=None):
                return json.loads(self.query(prompt))

        def _fail_if_called(self, task, test_cmd):
            raise AssertionError("metamorphic check must NOT run in test_mode")

        monkeypatch.setattr(
            "orchestrator.coordinator.AutonomousCoordinator._run_metamorphic_check",
            _fail_if_called,
        )

        def _fake_exec_with_recovery(self, task, provider, context_builder):
            from task.schema import TransactionResult
            return TransactionResult(
                task_id=task.task_id, success=True, dry_run=True, events=[],
            )

        monkeypatch.setattr(
            "core.runtime.DSHRuntime.execute_with_recovery", _fake_exec_with_recovery
        )

        coord = AutonomousCoordinator(
            target_repo=repo,
            qa_client=_Q(),
            dev_provider=MagicMock(),
            dry_run=True,
            test_mode=True,
        )
        coord.run(user_goal="g", max_tasks=1)  # must not raise

    def test_metamorphic_failure_rejects_task(self, tmp_path, monkeypatch):
        """A failed metamorphic verdict must fail the task (gate, not log)."""
        from orchestrator.coordinator import AutonomousCoordinator

        repo = _git_repo(tmp_path)
        (repo / "test_task.py").write_text("def test_x():\n    assert 1\n", encoding="utf-8")

        class _Q:
            name = "mock-qa"
            def query(self, prompt, timeout=None):
                if "Git Diff" in prompt:
                    return json.dumps({"verdict": "APPROVED", "flagged_risks": [], "summary": "ok"})
                return json.dumps({
                    "summary": "s",
                    "detected_framework": "pytest",
                    "detected_build_cmd": "",
                    "detected_test_cmd": "pytest -q",
                    "tasks": [{
                        "task_id": "T_M3",
                        "title": "t",
                        "description": "d",
                        "allowed_files": ["Player.cs"],
                        "test_file": "test_task.py",
                        "test_code": "def test_x():\n    assert 1\n",
                        "test_cmd": "pytest -q",
                    }],
                })
            def query_json(self, prompt, timeout=None):
                return json.loads(self.query(prompt))

        def _failing_gate(self, task, test_cmd):
            return {"success": False, "skipped": False, "failed_variant": "reseed", "skipped_variants": 0}

        def _fake_exec_with_recovery(self, task, provider, context_builder):
            from task.schema import TransactionResult
            return TransactionResult(
                task_id=task.task_id, success=True, dry_run=True, events=[],
            )

        monkeypatch.setattr(
            "orchestrator.coordinator.AutonomousCoordinator._run_metamorphic_check",
            _failing_gate,
        )
        monkeypatch.setattr(
            "core.runtime.DSHRuntime.execute_with_recovery", _fake_exec_with_recovery
        )

        coord = AutonomousCoordinator(
            target_repo=repo,
            qa_client=_Q(),
            dev_provider=MagicMock(),
            dry_run=True,
            test_mode=False,
        )
        result = coord.run(user_goal="g", max_tasks=1)
        assert result.success is False, "metamorphic failure must reject the task"
        failed = [t for t in result.tasks if not t.success]
        assert failed, "the task record must be marked failed"
        assert "Metamorphic" in (failed[0].error_message or "")
