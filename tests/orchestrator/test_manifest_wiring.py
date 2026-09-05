"""TDD red test: coordinator.run() must maintain a real-time manifest.

Pipeline round-2 TASK_005B: the manifest helpers exist (TASK_005) but run()
must actually initialize run_<id>/manifest.json and update it with each task
outcome so operators can read state from one file.
"""
import json
import subprocess
from pathlib import Path

from orchestrator.coordinator import AutonomousCoordinator
from orchestrator.planner import AuditReport, PlannedTask


class _ScriptedQA:
    """Stub QA that returns a fixed audit report without CLI calls."""

    name = "stub-qa"

    def __init__(self, report):
        self._report = report

    def query_json(self, prompt, timeout=None):
        return self._report.model_dump()

    def query(self, prompt, timeout=None):
        return "ok"


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def test_run_creates_and_updates_manifest(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    report = AuditReport(
        summary="s",
        tasks=[PlannedTask(task_id="T_M_1", title="m", description="d", allowed_files=["README.md"])],
    )
    coord = AutonomousCoordinator(
        target_repo=repo,
        qa_client=_ScriptedQA(report),
        dry_run=True,
        test_mode=True,
    )

    # Stub planner to return the fixed report, and stub the recovery loop.
    from unittest.mock import MagicMock
    from task.schema import TransactionResult

    coord.qa_client = _ScriptedQA(report)
    import orchestrator.planner as planner_mod
    monkeypatch.setattr(
        planner_mod.AutonomousPlanner, "audit_and_plan", lambda self, g, max_tasks=3: report
    )

    from core.runtime import DSHRuntime
    fake_tx = TransactionResult(
        task_id="T_M_1", success=True, commit_hash="abc1234", dry_run=True,
    )
    monkeypatch.setattr(
        DSHRuntime, "execute_with_recovery", lambda self, task, provider, context_builder: fake_tx
    )

    result = coord.run(user_goal="goal", max_tasks=1)

    run_dirs = [d for d in repo.glob("run_*") if d.is_dir()]
    assert run_dirs, "coordinator.run must create a run_<id>/ manifest directory"
    manifest = json.loads((run_dirs[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["tasks"]["T_M_1"]["status"] == "PASSED"
