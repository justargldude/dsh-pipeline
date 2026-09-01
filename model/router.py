from typing import Optional
from task.schema import TaskDefinition, RiskLevel
from recovery.classifier import FailureType
from model.schemas import ModelType


class ModelRouter:
    @staticmethod
    def route(
        task: TaskDefinition,
        failure_type: Optional[FailureType] = None,
        attempt: int = 0,
        evidence_confidence: float = 1.0,
    ) -> ModelType:
        # Rule 1: High-risk task always gets Reasoning model
        if task.risk == RiskLevel.HIGH:
            return ModelType.REASONING

        # Rule 2: Escalation to Reasoning on recovery attempt >= 2
        if attempt >= 2:
            return ModelType.REASONING

        # Rule 3: Low confidence evidence requires Reasoning
        if evidence_confidence < 0.85:
            return ModelType.REASONING

        # Rule 4: Failure-based routing
        if failure_type:
            if failure_type == FailureType.SYNTAX:
                return ModelType.FAST
            if failure_type == FailureType.TYPE_SEMANTIC:
                return ModelType.FAST if attempt <= 1 else ModelType.REASONING
            if failure_type in (FailureType.BEHAVIORAL, FailureType.UNKNOWN):
                return ModelType.REASONING
            if failure_type == FailureType.PATCH_INVALID:
                return ModelType.FAST

        # Default for low/medium risk first attempts
        return ModelType.FAST
