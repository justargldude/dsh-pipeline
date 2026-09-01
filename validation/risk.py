import re
from pathlib import Path
from typing import List
from pydantic import BaseModel, Field
from task.schema import TaskDefinition, PatchProposal, RiskLevel


class RiskCheckResult(BaseModel):
    success: bool
    violations: List[str] = Field(default_factory=list)


class RiskValidator:
    """T4 - Risk & Unsafe Code Validation (P/Invoke, unsafe memory, stackalloc, pointers)."""
    UNSAFE_PATTERNS = [
        (re.compile(r"\bunsafe\b"), "Detected unannotated 'unsafe' code block"),
        (re.compile(r"\bstackalloc\b"), "Detected unmanaged 'stackalloc' memory allocation"),
        (re.compile(r"\[DllImport\b"), "Detected unauthorized native '[DllImport]' P/Invoke declaration"),
        (re.compile(r"\bMarshal\.(AllocHGlobal|StringToHGlobalAnsi)\b"), "Detected raw unmanaged memory allocation"),
    ]

    @classmethod
    def validate_risk(cls, task: TaskDefinition, proposal: PatchProposal, repo_path: Path) -> RiskCheckResult:
        violations = []

        # If task is explicitly marked HIGH risk, allow some unsafe operations with caution
        is_high_risk_task = task.risk == RiskLevel.HIGH

        for file_patch in proposal.patches:
            for hunk in file_patch.hunks:
                if not hunk.new_text:
                    continue

                for pattern, msg in cls.UNSAFE_PATTERNS:
                    if pattern.search(hunk.new_text) and not is_high_risk_task:
                        violations.append(f"In '{file_patch.file}': {msg} (Task risk is {task.risk.value})")

        return RiskCheckResult(
            success=len(violations) == 0,
            violations=violations,
        )
