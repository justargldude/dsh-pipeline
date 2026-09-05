"""TDD red tests: ModelRouter escalation policy for repeated patch failures (bug #2 gap).

Pipeline round-2 TASK_002: observed in the previous operational session that
Dev deepseek repeated the SAME error class across recovery attempts
(missing `rename` import twice, duplicate FilePatch, missed error.message),
because router.py routes PATCH_INVALID -> FAST, so the weak model was retried
on the very failure it just produced. Escalation must force the stronger
REASONING model as soon as a patch-structure failure occurred once.
"""
import pytest

from task.schema import TaskDefinition, RiskLevel
from recovery.classifier import FailureType
from model.schemas import ModelType
from model.router import ModelRouter


def _task(risk=RiskLevel.LOW):
    return TaskDefinition(
        task_id="T_ESC_01",
        title="Escalation probe",
        allowed_files=["lib/x.py"],
        risk=risk,
    )


def test_patch_invalid_on_first_retry_escalates_to_reasoning():
    """Attempt 1 with failure_type PATCH_INVALID must NOT stay on FAST."""
    routed = ModelRouter.route(
        _task(),
        failure_type=FailureType.PATCH_INVALID,
        attempt=1,
        evidence_confidence=0.95,
    )
    assert routed == ModelType.REASONING, (
        "A model that just produced an invalid patch (PATCH_INVALID) must be "
        "escalated to REASONING on the immediate retry; re-running the same "
        "FAST model on the same class of error was observed to loop."
    )


def test_type_semantic_on_first_retry_escalates_to_reasoning():
    """TYPE_SEMANTIC at attempt 1 currently stays FAST — same loop risk."""
    routed = ModelRouter.route(
        _task(),
        failure_type=FailureType.TYPE_SEMANTIC,
        attempt=1,
        evidence_confidence=0.95,
    )
    assert routed == ModelType.REASONING


def test_syntax_failure_first_retry_may_stay_fast():
    """SYNTAX failures are mechanical; FAST is acceptable at attempt 1."""
    routed = ModelRouter.route(
        _task(),
        failure_type=FailureType.SYNTAX,
        attempt=1,
        evidence_confidence=0.95,
    )
    assert routed == ModelType.FAST


def test_no_failure_high_confidence_stays_fast():
    """Control: happy path attempt 0 with verified evidence remains FAST."""
    routed = ModelRouter.route(_task(), attempt=0, evidence_confidence=0.95)
    assert routed == ModelType.FAST
