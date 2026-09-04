import logging
from typing import Any, Dict, Optional
from task.schema import TaskDefinition, TransactionResult
from recovery.history import RecoveryHistory
from recovery.classifier import FailureType
from model.schemas import ModelRequest, ModelType
from model.providers import BaseModelProvider
from model.router import ModelRouter
from context.budget import ContextComplexity, ContextBudgetExceededError
from context.builder import ContextBuilder
from safety.patch_engine import normalize_repo_path

logger = logging.getLogger("dsh.recovery")


from recovery.classifier import FailureClassifier, FailureType


class RecoveryManager:
    MAX_ATTEMPTS = 3

    def __init__(self):
        pass

    def run_recovery_loop(
        self,
        task: TaskDefinition,
        runtime: "DSHRuntime",
        provider: BaseModelProvider,
        context_builder: ContextBuilder,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> TransactionResult:
        """Executes up to MAX_ATTEMPTS with progressive reasoning & token budgets."""
        history = RecoveryHistory(task_id=task.task_id)
        last_failure_type = FailureType.UNKNOWN
        last_error = ""

        # Collect source files once
        file_snippets = {}
        for raw_file in task.allowed_files:
            rel_file = normalize_repo_path(raw_file)
            file_path = (runtime.workspace_path / rel_file).resolve()
            if file_path.exists():
                file_snippets[rel_file] = file_path.read_text(encoding="utf-8")

        for attempt in range(self.MAX_ATTEMPTS):
            # 1. Determine model and complexity based on attempt
            if attempt == 0:
                evidence_conf = evidence.get("confidence") if evidence else None
                model_type = ModelRouter.route(task, evidence_confidence=evidence_conf)
                complexity = ContextComplexity.NORMAL
            elif attempt == 1:
                model_type = ModelRouter.route(task, failure_type=last_failure_type, attempt=1)
                complexity = ContextComplexity.NORMAL
            else:  # attempt 2
                model_type = ModelType.REASONING
                complexity = ContextComplexity.RECOVERY

            logger.info(f"[RECOVERY_STARTED] Attempt {attempt}/{self.MAX_ATTEMPTS - 1} using [{model_type.value}]")

            # Bug 7a: pull advisory episodes from the runtime's episode
            # retriever (accessed via runtime attributes, not parameters) so
            # validated memory from prior runs informs the retry prompt.
            # Helper returns None when unconfigured or symbol set is empty.
            advisory_episodes = runtime._retrieve_advisory_episodes(task)

            # 2. Build context including failure history
            prev_history_str = history.format_history_for_prompt() if attempt > 0 else None
            try:
                context_str = context_builder.build_context(
                    task=task,
                    file_snippets=file_snippets,
                    evidence=evidence,
                    previous_failure=prev_history_str,
                    advisory_episodes=advisory_episodes,
                    complexity=complexity,
                )
            except ContextBudgetExceededError as cbe:
                logger.error(f"[CONTEXT_BUDGET_EXCEEDED] {cbe}")
                return TransactionResult(
                    task_id=task.task_id,
                    success=False,
                    failure_type="CONTEXT_BUDGET_EXCEEDED",
                    error_message=str(cbe),
                    dry_run=runtime.dry_run,
                    events=["RECOVERY_STARTED", "FAILURE_CLASSIFIED", "ROLLBACK"],
                )

            # 3. Model generation
            req = ModelRequest(
                system_prompt=runtime.SYSTEM_PROMPT,
                user_prompt=context_str,
                model_type=model_type,
            )
            resp = provider.generate(req)

            if not resp.patch_proposal:
                f_type = resp.failure_type or FailureType.MODEL_FORMAT_ERROR
                last_failure_type = f_type
                last_error = resp.error or f"Model failed to produce valid proposal ({f_type.value}). Raw: {resp.raw_content[:200]}"
                
                # Check for hard stop on provider failure (e.g. AUTH_ERROR)
                if FailureClassifier.is_hard_stop(f_type):
                    logger.error(f"[HARD_STOP] Non-recoverable provider failure: {f_type.value}")
                    return TransactionResult(
                        task_id=task.task_id,
                        success=False,
                        failure_type=f_type.value,
                        error_message=last_error,
                        dry_run=runtime.dry_run,
                        events=["RECOVERY_STARTED", "FAILURE_CLASSIFIED", "HARD_STOP"],
                    )

                history.record_attempt(
                    attempt_index=attempt,
                    model_type_used=model_type.value,
                    failure_type=last_failure_type,
                    error_message=last_error,
                    patch_attempted=None,
                )
                continue

            # 4. Execute transaction
            result = runtime.execute_transaction(task, resp.patch_proposal)
            if result.success:
                logger.info(f"[RECOVERY_SUCCEEDED] Task passed on Attempt {attempt}!")
                return result

            # Record failure in history
            try:
                last_failure_type = FailureType(result.failure_type)
            except Exception:
                last_failure_type = FailureType.UNKNOWN
            last_error = result.error_message or "Unknown failure"

            # Check for hard stop on execution failure (e.g. SCOPE_VIOLATION, ROLLBACK_FAILED, UNKNOWN_STATE)
            if FailureClassifier.is_hard_stop(last_failure_type):
                logger.error(f"[HARD_STOP] Non-recoverable execution failure: {last_failure_type.value}")
                return result

            history.record_attempt(
                attempt_index=attempt,
                model_type_used=model_type.value,
                failure_type=last_failure_type,
                error_message=last_error,
                patch_attempted=resp.patch_proposal,
            )

        # Reached limit without success -> HARD HALT
        logger.error(f"[HARD_HALT] Task {task.task_id} failed after {self.MAX_ATTEMPTS} attempts. Halting.")
        return TransactionResult(
            task_id=task.task_id,
            success=False,
            failure_type="HARD_HALT",
            error_message=f"Hard halt: Task failed all {self.MAX_ATTEMPTS} recovery attempts. Last error: {last_error}",
            dry_run=runtime.dry_run,
            events=["RECOVERY_STARTED", "HARD_HALT", "ROLLBACK"],
        )

