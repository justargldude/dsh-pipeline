import json
import sys
from pathlib import Path
from unittest.mock import MagicMock
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestrator.coordinator import AutonomousCoordinator
from orchestrator.planner import PlannedTask


class _ScriptedQA:
    name = "scripted-qa"

    def __init__(self, response):
        self._response = response

    def query(self, prompt, timeout=None):
        return self._response


def test_review_diff_with_qa_parses_valid_json_verdict(tmp_path: Path):
    payload = json.dumps({
        "verdict": "APPROVED",
        "flagged_risks": [],
        "summary": "Diff correctly implements functionality without regressions.",
    })
    coord = AutonomousCoordinator(
        target_repo=tmp_path,
        qa_client=_ScriptedQA(payload),
        dev_provider=MagicMock(),
        test_mode=True,
    )
    task = PlannedTask(task_id="T1", title="title", description="desc")
    verdict = coord._review_diff_with_qa(task, "diff content")
    assert isinstance(verdict, dict)
    assert verdict["verdict"] == "APPROVED"
    assert verdict["flagged_risks"] == []
    assert "regressions" in verdict["summary"]


def test_review_diff_with_qa_rejects_legacy_prose_format(tmp_path: Path):
    coord = AutonomousCoordinator(
        target_repo=tmp_path,
        qa_client=_ScriptedQA("APPROVED: Clean hook and no regression."),
        dev_provider=MagicMock(),
        test_mode=True,
    )
    task = PlannedTask(task_id="T2", title="title", description="desc")
    with pytest.raises(ValueError):
        coord._review_diff_with_qa(task, "diff content")


def test_review_diff_with_qa_validates_required_fields_and_types(tmp_path: Path):
    coord = AutonomousCoordinator(
        target_repo=tmp_path,
        qa_client=_ScriptedQA(json.dumps({"verdict": "MAYBE", "flagged_risks": [], "summary": "ok"})),
        dev_provider=MagicMock(),
        test_mode=True,
    )
    task = PlannedTask(task_id="T3", title="title", description="desc")
    with pytest.raises(ValueError, match="verdict"):
        coord._review_diff_with_qa(task, "diff content")

    coord.qa_client = _ScriptedQA(json.dumps({"verdict": "APPROVED", "flagged_risks": "none", "summary": "ok"}))
    with pytest.raises(ValueError, match="flagged_risks"):
        coord._review_diff_with_qa(task, "diff content")
