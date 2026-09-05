"""TDD red tests: real-time run manifest for observability (bug #6).

Pipeline round-2 TASK_005: the previous operational session spent ~56 minutes
and ~22% of its prompt tokens polling logs with tail/ps/grep because nothing
records task -> attempt -> status -> failure_reason -> worktree_path in a
machine-readable file. The coordinator must maintain run_<id>/manifest.json
updated after every task state transition.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock

from orchestrator.coordinator import AutonomousCoordinator


class _ScriptedQA:
    name = "stub-qa"

    def query(self, prompt, timeout=None):
        return "ok"


def _coordinator(tmp_path: Path) -> AutonomousCoordinator:
    return AutonomousCoordinator(
        target_repo=tmp_path,
        qa_client=_ScriptedQA(),
        dev_provider=MagicMock(),
        dry_run=True,
        test_mode=False,
    )


def test_manifest_updated_with_task_states(tmp_path: Path):
    coord = _coordinator(tmp_path)
    run_dir = tmp_path / "run_probe"
    coord._manifest_init(run_dir)

    coord._manifest_update(run_dir, task_id="T_A", attempt=1, status="VALIDATING",
                           failure_reason=None, worktree_path="/tmp/wtA")
    coord._manifest_update(run_dir, task_id="T_A", attempt=1, status="READY_TO_INTEGRATE",
                           failure_reason=None, worktree_path="/tmp/wtA")
    coord._manifest_update(run_dir, task_id="T_B", attempt=1, status="FAILED",
                           failure_reason="PATCH_INVALID: duplicate FilePatch",
                           worktree_path=None)

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["tasks"]["T_A"]["status"] == "READY_TO_INTEGRATE"
    assert manifest["tasks"]["T_B"]["status"] == "FAILED"
    assert manifest["tasks"]["T_B"]["failure_reason"] == "PATCH_INVALID: duplicate FilePatch"
    assert manifest["tasks"]["T_B"]["attempts"] == 1


def test_manifest_records_every_attempt(tmp_path: Path):
    coord = _coordinator(tmp_path)
    run_dir = tmp_path / "run_probe2"
    coord._manifest_init(run_dir)

    coord._manifest_update(run_dir, task_id="T_C", attempt=1, status="FAILED",
                           failure_reason="BUILD_FAILED", worktree_path="/tmp/wtC1")
    coord._manifest_update(run_dir, task_id="T_C", attempt=2, status="PASSED",
                           failure_reason=None, worktree_path="/tmp/wtC2")

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["tasks"]["T_C"]["attempts"] == 2
    assert manifest["tasks"]["T_C"]["status"] == "PASSED"
    assert manifest["tasks"]["T_C"]["worktree_path"] == "/tmp/wtC2"


def test_manifest_created_atomically_and_is_valid_json(tmp_path: Path):
    coord = _coordinator(tmp_path)
    run_dir = tmp_path / "run_probe3"
    coord._manifest_init(run_dir)
    coord._manifest_update(run_dir, task_id="T_D", attempt=1, status="VALIDATING",
                           failure_reason=None, worktree_path=None)

    raw = (run_dir / "manifest.json").read_text(encoding="utf-8")
    parsed = json.loads(raw)  # must never be half-written
    assert "tasks" in parsed
    assert parsed["tasks"]["T_D"]["task_id"] == "T_D"
