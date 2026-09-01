import json
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from task.schema import PatchProposal
from recovery.classifier import FailureType


class AttemptRecord(BaseModel):
    attempt_index: int
    model_type_used: str
    failure_type: FailureType
    error_message: str
    patch_attempted: Optional[PatchProposal] = None
    approach_used: str = ""


class RecoveryHistory(BaseModel):
    task_id: str
    attempts: List[AttemptRecord] = Field(default_factory=list)

    def record_attempt(
        self,
        attempt_index: int,
        model_type_used: str,
        failure_type: FailureType,
        error_message: str,
        patch_attempted: Optional[PatchProposal] = None,
        approach_used: str = "",
    ):
        self.attempts.append(
            AttemptRecord(
                attempt_index=attempt_index,
                model_type_used=model_type_used,
                failure_type=failure_type,
                error_message=error_message,
                patch_attempted=patch_attempted,
                approach_used=approach_used,
            )
        )

    def format_history_for_prompt(self) -> str:
        if not self.attempts:
            return ""

        lines = ["## PREVIOUS FAILED ATTEMPTS (DO NOT REPEAT THESE APPROACHES):"]
        for att in self.attempts:
            lines.append(f"### Attempt {att.attempt_index} (Model: {att.model_type_used})")
            lines.append(f"- Failure Classification: {att.failure_type.value}")
            lines.append(f"- Error Output: {att.error_message}")
            if att.patch_attempted:
                lines.append(f"- Patch Tried:\n```json\n{att.patch_attempted.model_dump_json(indent=2)}\n```")
        return "\n".join(lines)
