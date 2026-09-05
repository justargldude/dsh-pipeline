"""TDD red tests: QA review round-2 remediation for coordinator + planner.

Findings from QA review (VERDICT: REJECTED) that must be fixed:
* F-01: run_<id>/ manifest dir must be tolerated in allowed_untracked_paths so
  non-dry-run transactions can still fast-forward (INTEGRATED).
* F-02: _ensure_red_test_in_worktree must actually be WIRED: the runtime gets
  an on_worktree_created callback the coordinator installs, so red tests are
  copied into every transaction worktree before baseline capture.
* F-03: _verify_red_test_discovered must run BEFORE _write_red_test_to_main,
  otherwise before==after always (inverted lifecycle).
* F-04: _count_discovered_tests must not assume stdout ends with an integer;
  it must return None (unknown) instead of crashing, and the smoke-check must
  stay inert when the runner output cannot be parsed.
* F-05: planner must tolerate malformed package.json / null scripts without
  TypeError and still provide a build command fallback.
* F-06: manifest must record the real attempt count from the recovery loop.
* F-08/F-09: manifest atomic write must reuse safety.patch_engine.atomic_write_file;
  module-scope import of time.
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from orchestrator.coordinator import AutonomousCoordinator
from orchestrator.planner import AutonomousPlanner
from orchestrator.subagents import SubagentClient


class _StubQA(SubagentClient):
    def query(self, prompt, timeout=None):  # pragma: no cover
        raise AssertionError("planner profile must not call QA")


def _git_repo(tmp_path: Path) -> Path:
    def run(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (tmp_path / "README.md").write_text("repo", encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "init")
    return tmp_path


def _coordinator(repo: Path) -> AutonomousCoordinator:
    class _Q:
        name = "stub-qa"
        def query(self, prompt, timeout=None):
            return "ok"
    return AutonomousCoordinator(
        target_repo=repo,
        qa_client=_Q(),
        dev_provider=MagicMock(),
        dry_run=False,
        test_mode=False,
    )


def _task(test_file="tests/test_red_01.test.js"):
    from orchestrator.planner import PlannedTask
    return PlannedTask(
        task_id="T_R2_01",
        title="probe",
        description="d",
        allowed_files=["lib/x.js"],
        test_file=test_file,
        test_code="// red",
        test_cmd="node --test tests/",
    )


class TestF01ManifestInAllowedUntracked:
    def test_run_dir_in_allowed_untracked_paths(self, tmp_path: Path):
        repo = _git_repo(tmp_path)
        coord = _coordinator(repo)
        # Simulate what run() does: allocate the run dir, then expose allowance
        coord._run_dir = repo / "run_123"
        coord._manifest_init(coord._run_dir)
        assert coord._untracked_allowance_for_manifest() is not None
        assert any("run_" in p for p in coord._untracked_allowance_for_manifest())

    def test_fresh_coordinator_allowance_is_none(self, tmp_path: Path):
        repo = _git_repo(tmp_path)
        coord = _coordinator(repo)
        assert coord._untracked_allowance_for_manifest() is None


class TestF02WorktreeCallbackWiring:
    def test_runtime_has_on_worktree_created_hook(self):
        from core.runtime import DSHRuntime
        assert hasattr(DSHRuntime, "on_worktree_created") or hasattr(DSHRuntime, "set_worktree_callback")

    def test_coordinator_installs_red_test_callback(self, tmp_path: Path):
        repo = _git_repo(tmp_path)
        coord = _coordinator(repo)
        task = _task()
        coord._write_red_test_to_main(task)

        wt = tmp_path / ".wt_probe"
        subprocess.run(["git", "worktree", "add", "--detach", str(wt), "HEAD"],
                       cwd=repo, check=True, capture_output=True)

        cb = coord._red_test_worktree_callback(task)
        cb(wt)
        assert (wt / task.test_file).exists(), (
            "coordinator must install a runtime callback copying red tests "
            "into each transaction worktree before validation"
        )


class TestF03SmokeCheckLifecycle:
    def test_smoke_check_counts_hidden_test_before_writing(self, tmp_path: Path):
        """The smoke-check must run its BEFORE count while the red test does
        NOT yet exist in main (i.e. before _write_red_test_to_main)."""
        repo = _git_repo(tmp_path)
        coord = _coordinator(repo)
        task = _task()

        # runner that counts files matching tests/*.test.js in cwd
        counter = 'find tests -name "*.test.js" 2>/dev/null | wc -l'
        (repo / "tests").mkdir()
        (repo / "tests" / "existing.test.js").write_text("//x", encoding="utf-8")

        visible = coord._verify_red_test_discovered(
            task, count_cmd=["bash", "-c", counter]
        )
        assert visible is True, (
            "With the red test NOT yet written, a discovery-counting runner "
            "must report +1 after the smoke-check temporarily writes it; "
            "ordering must be BEFORE _write_red_test_to_main."
        )


class TestF04ParserRobustness:
    def test_count_returns_none_on_unparseable_output(self, tmp_path: Path):
        repo = _git_repo(tmp_path)
        coord = _coordinator(repo)
        n = coord._count_discovered_tests(["bash", "-c", "echo 'no numbers here'"])
        assert n is None

    def test_smoke_check_inert_when_runner_output_unparseable(self, tmp_path: Path):
        repo = _git_repo(tmp_path)
        coord = _coordinator(repo)
        task = _task()
        ok = coord._verify_red_test_discovered(task, count_cmd=["bash", "-c", "echo nonsense"])
        assert ok is True, "unparseable runner output must not fail orchestration"


class TestF05PlannerRobustness:
    def test_malformed_package_json_still_yields_build_cmd(self, tmp_path: Path):
        (tmp_path / "package.json").write_text("{ not valid json", encoding="utf-8")
        planner = AutonomousPlanner(tmp_path, _StubQA(name="stub", test_mode=True))
        profile = planner.inspect_repo_profile()
        assert profile["default_build_cmd"], (
            "malformed package.json must still yield SOME build command "
            "(node --test fallback) instead of None -> BUILD_CONFIGURATION_MISSING"
        )

    def test_null_scripts_does_not_crash(self, tmp_path: Path):
        (tmp_path / "package.json").write_text('{"name": "x", "scripts": null}', encoding="utf-8")
        planner = AutonomousPlanner(tmp_path, _StubQA(name="stub", test_mode=True))
        profile = planner.inspect_repo_profile()
        assert profile["default_test_cmd"] == "node --test tests/"


class TestF06RealAttempts:
    def test_manifest_records_real_attempt_count(self, tmp_path: Path):
        repo = _git_repo(tmp_path)
        coord = _coordinator(repo)
        run_dir = tmp_path / "run_probe"
        coord._manifest_init(run_dir)
        coord._manifest_update(run_dir, task_id="T_X", attempt=3, status="FAILED",
                               failure_reason="x", worktree_path=None)
        m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        assert m["tasks"]["T_X"]["attempts"] == 3
