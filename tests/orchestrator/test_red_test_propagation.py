"""TDD red tests: coordinator must make QA red tests reach the Dev worktree
and must smoke-check that the test runner actually discovers them.

Pipeline round-2 TASK_004 (bugs #1 + #9 from the operational review):

* Bug #9 — `git worktree add --detach ... HEAD` snapshots committed state only.
  The coordinator writes the QA red test into the MAIN repo as an untracked
  file BEFORE creating the worktree, so the Dev worktree never contains the
  red test and validation inside the worktree runs without it (false green).

* Bug #1 — QA once wrote `tests/test_task_001.js` while `npm test` runs
  `node --test tests/` which only discovers `*.test.js`; the whole phase then
  "passed" while the red test never executed. A cheap discovery smoke-check
  (test count must grow) must fail the orchestration fast instead.
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from orchestrator.coordinator import AutonomousCoordinator
from orchestrator.planner import AuditReport, PlannedTask


def _git_repo(tmp_path: Path) -> Path:
    def run(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (tmp_path / "README.md").write_text("repo", encoding="utf-8")
    pkg = {"name": "x", "scripts": {"test": "node --test tests/"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "init")
    return tmp_path


class _ScriptedQA:
    """Minimal stub satisfying the coordinator's usage surface."""

    name = "stub-qa"

    def query(self, prompt, timeout=None):
        return "ok"


def _coordinator(repo: Path) -> AutonomousCoordinator:
    return AutonomousCoordinator(
        target_repo=repo,
        qa_client=_ScriptedQA(),
        dev_provider=MagicMock(),
        dry_run=False,
        test_mode=False,
    )


def _task_with_test(test_file: str) -> PlannedTask:
    return PlannedTask(
        task_id="T_RED_01",
        title="Red test propagation probe",
        description="probe",
        allowed_files=["lib/x.js"],
        test_file=test_file,
        test_code="// a red test that would never be discovered as test_task_NNN.js",
        test_cmd="node --test tests/",
    )


class TestRedTestReachesWorktree:
    """Bug #9: the coordinator must propagate the red test into the Dev worktree."""

    def test_prepare_red_test_copies_file_into_worktree(self, tmp_path: Path):
        repo = _git_repo(tmp_path)
        coord = _coordinator(repo)
        task = _task_with_test("tests/test_red_01.test.js")

        wt = tmp_path / ".wt_probe"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(wt), "HEAD"],
            cwd=repo, check=True, capture_output=True,
        )

        coord._ensure_red_test_in_worktree(task, wt)

        copied = wt / task.test_file
        assert copied.exists(), (
            "Red test written to main repo as untracked file never reaches the "
            "Dev worktree; validation there runs against an incomplete test set."
        )
        assert copied.read_text(encoding="utf-8") == task.test_code

    def test_prepare_red_test_main_repo_fallback(self, tmp_path: Path):
        """Main repo copy must still happen first (existing behaviour preserved)."""
        repo = _git_repo(tmp_path)
        coord = _coordinator(repo)
        task = _task_with_test("tests/test_red_02.test.js")

        coord._write_red_test_to_main(task)

        assert (repo / task.test_file).exists()


class TestDiscoverySmokeCheck:
    """Bug #1: a red test invisible to the runner must fail orchestration fast."""

    def test_count_discovered_tests_counts_runner_output(self, tmp_path: Path):
        repo = _git_repo(tmp_path)
        coord = _coordinator(repo)

        n = coord._count_discovered_tests(["bash", "-c", "echo 7"])
        assert n == 7

    def test_smoke_check_raises_when_count_does_not_grow(self, tmp_path: Path):
        repo = _git_repo(tmp_path)
        coord = _coordinator(repo)
        task = _task_with_test("tests/test_invisible_01.js")  # NOT *.test.js

        (repo / "tests").mkdir(exist_ok=True)
        (repo / task.test_file).write_text(task.test_code, encoding="utf-8")

        # Runner reports the SAME count before and after the red test exists.
        before = coord._count_discovered_tests(["bash", "-c", "echo 3"])
        after = coord._count_discovered_tests(["bash", "-c", "echo 3"])
        if after <= before:
            pytest.fail(
                "Discovery smoke-check did not detect that the new red test is "
                "invisible to the configured test command "
                "(tests/test_invisible_01.js vs node --test tests/)."
            )

    def test_verify_red_test_discovered_returns_false_for_invisible(self, tmp_path: Path):
        repo = _git_repo(tmp_path)
        coord = _coordinator(repo)
        task = _task_with_test("tests/test_invisible_02.js")

        ok = coord._verify_red_test_discovered(task, count_cmd=["bash", "-c", "echo 3"])
        assert ok is False
