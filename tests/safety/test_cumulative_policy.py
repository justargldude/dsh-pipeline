import pytest
from safety.policy import SafetyPolicy, SessionBudgetTracker
from safety.scope_guard import ScopeGuard, ScopeViolationError
from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk
from pathlib import Path


def test_cumulative_budget_overflow():
    policy = SafetyPolicy(max_cumulative_lines_added=50, max_cumulative_lines_deleted=30)
    tracker = SessionBudgetTracker(policy)

    # First task adds 30 lines -> OK
    tracker.check_and_add(30, 10)
    assert tracker.cumulative_lines_added == 30

    # Second task tries to add 30 lines -> 30 + 30 = 60 > 50 -> Raises ValueError
    with pytest.raises(ValueError) as exc:
        tracker.check_and_add(30, 5)
    assert "Session cumulative budget exceeded" in str(exc.value)


def test_scope_guard_blocks_forbidden_files(tmp_path: Path):
    policy = SafetyPolicy()
    scope_guard = ScopeGuard(policy=policy)

    task = TaskDefinition(
        task_id="T_SEC_01",
        title="Attempt .env modification",
        allowed_files=[".env"]
    )

    proposal = PatchProposal(
        patches=[
            FilePatch(
                file=".env",
                hunks=[PatchHunk(old_text="", new_text="SECRET=123")]
            )
        ]
    )

    with pytest.raises(ScopeViolationError) as exc:
        scope_guard.validate(task, proposal, tmp_path)
    assert "Forbidden file access detected" in str(exc.value)
