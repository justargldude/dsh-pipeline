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

    def _derive_lesson(self, failure_type: FailureType, error_message: str) -> str:
        if failure_type == FailureType.SYNTAX:
            return "Ensure all C# syntax, braces, semicolons, and type declarations are strictly valid."
        if failure_type == FailureType.TYPE_SEMANTIC:
            return "Verify all method signatures, parameter types, and member identifiers match existing code."
        if failure_type == FailureType.BEHAVIORAL:
            return "Do not break existing behavior or remove required logic/assertions."
        if failure_type == FailureType.REGRESSION:
            return "Ensure all regression tests continue to pass; preserve original method invariants."
        if failure_type == FailureType.SCOPE_VIOLATION:
            return "Stay strictly within allowed_files and line budgets; do not modify unauthorized files."
        if failure_type == FailureType.PATCH_INVALID:
            return "Provide exact matching old_text from the source file."
        if failure_type == FailureType.MODEL_FORMAT_ERROR:
            return "Output strictly valid JSON matching the PatchProposal schema without conversational wrapper."
        if failure_type == FailureType.MODEL_CONTENT_INVALID:
            return "Ensure all required PatchProposal fields (file, hunks, old_text, new_text) are populated and non-empty."
        if failure_type in (FailureType.RATE_LIMIT, FailureType.NETWORK_ERROR, FailureType.SERVER_ERROR, FailureType.TIMEOUT):
            return "Retry with simplified prompt or wait for provider capacity."
        return "Carefully review the error message and avoid repeating the same modification."

    def format_history_for_prompt(self, max_history_chars: int = 1500) -> str:
        if not self.attempts:
            return ""

        lines = ["## PREVIOUS FAILED ATTEMPTS (AVOID REPEATING THESE MISTAKES):"]
        for att in self.attempts:
            lines.append(f"### Attempt {att.attempt_index} (Model: {att.model_type_used})")
            lines.append(f"- What Failed: [{att.failure_type.value}]")
            
            # Truncate error message concisely to root cause
            err_summary = att.error_message.strip()
            if len(err_summary) > 200:
                err_summary = err_summary[:200] + "..."
            lines.append(f"- Why It Failed: {err_summary}")

            # Concise summary of what was attempted without dumping raw full patch JSON
            if att.patch_attempted:
                files_touched = [p.file for p in att.patch_attempted.patches]
                hunk_total = sum(len(p.hunks) for p in att.patch_attempted.patches)
                lines.append(f"- Attempted Action: Modified {len(files_touched)} file(s) ({', '.join(files_touched)}) across {hunk_total} hunk(s)")
            else:
                lines.append("- Attempted Action: Invalid schema or unparseable JSON output")

            lesson = self._derive_lesson(att.failure_type, att.error_message)
            lines.append(f"- Key Constraint & Lesson: {lesson}")
            lines.append("")

        result = "\n".join(lines).strip()
        if len(result) > max_history_chars:
            result = result[:max_history_chars] + "\n...[history truncated to preserve token budget]"
        return result

