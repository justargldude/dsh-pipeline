"""Regression: recovery loop must NOT retry tasks whose patch already
committed successfully and is only pending integration (dirty main tree).

Field observation (2026-09-05): TASK_001 passed all validation tiers and
committed in its worktree, but integration returned READY_TO_INTEGRATE
because the main tree held unrelated untracked files. The recovery loop
classified this as a failure and re-ran the model 3 times, producing 3
identical commits. Retrying cannot make the main tree cleaner.
"""
import sys
import logging
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from task.schema import TaskDefinition, PatchProposal, TransactionResult
from recovery.manager import RecoveryManager


def _make_task():
    return TaskDefinition(
        task_id="T_PEND_1",
        title="pending integration task",
        allowed_files=["a.py"],
    )


def test_recovery_stops_when_commit_pending_integration():
    runtime = MagicMock()
    runtime.dry_run = False
    runtime.SYSTEM_PROMPT = "sys"
    runtime._retrieve_advisory_episodes.return_value = None

    pending_result = TransactionResult(
        task_id="T_PEND_1",
        success=False,
        commit_hash="abc123",
        integration_status="READY_TO_INTEGRATE",
        error_message="PENDING_INTEGRATION: READY_TO_INTEGRATE — worktree retained for manual/next-phase integration",
    )
    # If the guard fails, the loop would call generate 3 times.
    # With the guard, exactly one model call happens and the pending
    # result is returned as-is.
    provider = MagicMock()
    provider.generate.return_value = MagicMock(
        patch_proposal=PatchProposal(patches=[]),
        failure_type=None,
        error=None,
        raw_content="",
    )
    runtime.execute_transaction.return_value = pending_result

    mgr = RecoveryManager()
    ctx = MagicMock()
    ctx.build_context.return_value = "context string"
    result = mgr.run_recovery_loop(
        task=_make_task(),
        runtime=runtime,
        provider=provider,
        context_builder=ctx,
    )

    assert result.commit_hash == "abc123"
    assert result.integration_status == "READY_TO_INTEGRATE"
    assert provider.generate.call_count == 1, (
        "Recovery must stop after the first pending-integration result; "
        "retrying duplicates identical commits."
    )


def test_recovery_retries_normal_build_failures_normally():
    runtime = MagicMock()
    runtime.dry_run = False
    runtime.SYSTEM_PROMPT = "sys"
    runtime._retrieve_advisory_episodes.return_value = None

    failing = TransactionResult(
        task_id="T_PEND_1",
        success=False,
        failure_type="T1_BUILD",
        error_message="CS0103: name not found",
    )
    runtime.execute_transaction.return_value = failing
    provider = MagicMock()
    provider.generate.return_value = MagicMock(
        patch_proposal=PatchProposal(patches=[]),
        failure_type=None,
        error=None,
        raw_content="",
    )

    mgr = RecoveryManager()
    ctx = MagicMock()
    ctx.build_context.return_value = "context string"
    result = mgr.run_recovery_loop(
        task=_make_task(),
        runtime=runtime,
        provider=provider,
        context_builder=ctx,
    )

    assert result.success is False
    assert provider.generate.call_count == 3, "Normal failures still get the full 3 attempts."
