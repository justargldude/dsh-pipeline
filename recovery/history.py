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

        header = "## PREVIOUS FAILED ATTEMPTS (AVOID REPEATING THESE MISTAKES):"
        truncation_marker = "...[history truncated to preserve token budget]"

        formatted_blocks = []
        for att in self.attempts:
            lines = [f"### Attempt {att.attempt_index} (Model: {att.model_type_used})"]
            lines.append(f"- What Failed: [{att.failure_type.value}]")

            # Tầng 2: Balanced keep (150 chars head + \n...\n + 150 chars tail)
            err_summary = att.error_message.strip()
            if len(err_summary) > 300:
                err_summary = err_summary[:150] + "\n...\n" + err_summary[-150:]
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
            formatted_blocks.append("\n".join(lines))

        # Tầng 1: Block-granularity Tail-keep (iterate reversed from newest to oldest)
        kept_blocks = []
        current_len = len(header) + 1
        marker_len = len(truncation_marker) + 1

        for block in reversed(formatted_blocks):
            block_len = len(block) + 2  # separator
            if kept_blocks and (current_len + block_len + marker_len > max_history_chars):
                break
            kept_blocks.append(block)
            current_len += block_len

        truncated = len(kept_blocks) < len(formatted_blocks)
        kept_blocks.reverse()

        result_parts = [header]
        if truncated:
            result_parts.append(truncation_marker)
        result_parts.extend(kept_blocks)
        return "\n\n".join(result_parts).strip()

