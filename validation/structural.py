from pathlib import Path
from typing import Optional
from task.schema import TaskDefinition, PatchProposal
from safety.patch_validator import PatchValidator
from safety.scope_guard import ScopeGuard


class StructuralValidator:
    """T0 - Structural Validation (Schema, diff budget, allowed files, AST guard)."""
    @staticmethod
    def validate(
        task: TaskDefinition,
        proposal: PatchProposal,
        repo_path: Path,
        scope_guard: ScopeGuard,
        reservation_id: Optional[str] = None,
    ):
        PatchValidator.validate_proposal(proposal, repo_path)
        scope_guard.validate(task, proposal, repo_path, reservation_id=reservation_id)

