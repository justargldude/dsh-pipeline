import re
from pathlib import Path
from typing import List, Optional, Tuple
from pydantic import BaseModel, Field
from task.schema import TaskDefinition, PatchProposal, RiskLevel
from safety.policy import SafetyPolicy


class RiskCheckResult(BaseModel):
    success: bool
    violations: List[str] = Field(default_factory=list)


class RiskValidator:
    """T4 - Risk & Unsafe Code Validation (P/Invoke, unsafe memory, stackalloc, pointers)."""

    # (regex, error_message, required_capability)
    UNSAFE_PATTERNS: List[Tuple[re.Pattern, str, str]] = [
        (re.compile(r"\bunsafe\b"), "Detected unannotated 'unsafe' code block", "unsafe_code"),
        (re.compile(r"\bstackalloc\b"), "Detected unmanaged 'stackalloc' memory allocation", "unsafe_memory"),
        (re.compile(r"\[DllImport\b"), "Detected unauthorized native '[DllImport]' P/Invoke declaration", "native_pinvoke"),
        (re.compile(r"\bMarshal\.(AllocHGlobal|StringToHGlobalAnsi)\b"), "Detected raw unmanaged memory allocation", "unsafe_memory"),
    ]

    @classmethod
    def validate_risk(
        cls,
        task: TaskDefinition,
        proposal: PatchProposal,
        repo_path: Path,
        policy: Optional[SafetyPolicy] = None,
    ) -> RiskCheckResult:
        """Evaluates actual introduced risk across patch hunks against trusted capabilities."""
        violations = []
        active_policy = policy or SafetyPolicy()
        allowed_caps = active_policy.allowed_capabilities

        # Invariant: Higher risk level => stricter policy, never a bypass.
        for file_patch in proposal.patches:
            for hunk_idx, hunk in enumerate(file_patch.hunks, start=1):
                if not hunk.new_text:
                    continue

                for pattern, msg, required_cap in cls.UNSAFE_PATTERNS:
                    old_matches = len(pattern.findall(hunk.old_text)) if hunk.old_text else 0
                    new_matches = len(pattern.findall(hunk.new_text))

                    # Evaluate ACTUAL CHANGE: only flag if new unsafe constructs are introduced
                    if new_matches > old_matches:
                        if required_cap not in allowed_caps:
                            violations.append(
                                f"In '{file_patch.file}' (hunk {hunk_idx}): {msg}. "
                                f"Requires capability '{required_cap}' (Task risk: {task.risk.value})."
                            )

        return RiskCheckResult(
            success=len(violations) == 0,
            violations=violations,
        )
