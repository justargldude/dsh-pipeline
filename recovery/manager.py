import logging
from typing import Any, Dict, Optional
from task.schema import TaskDefinition, TransactionResult
from recovery.history import RecoveryHistory
from recovery.classifier import FailureType
from model.schemas import ModelRequest, ModelType
from model.providers import BaseModelProvider
from model.router import ModelRouter
from context.budget import ContextComplexity
from context.builder import ContextBuilder

logger = logging.getLogger("dsh.recovery")


class RecoveryManager:
    MAX_ATTEMPTS = 3

    def __init__(self):
        pass

    def run_recovery_loop(
        self,
        task: TaskDefinition,
        runtime: Any,  # DSHRuntime
        provider: BaseModelProvider,
        context_builder: ContextBuilder,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> TransactionResult:
        history = RecoveryHistory(task_id=task.task_id)
        last_failure_type = FailureType.UNKNOWN
        last_error = ""

        # Collect source files once
        file_snippets = {}
        for rel_file in task.allowed_files:
            file_path = (runtime.workspace_path / rel_file.lstrip("/")).resolve()
            if file_path.exists():
                file_snippets[rel_file] = file_path.read_text(encoding="utf-8")

        for attempt in range(self.MAX_ATTEMPTS):
            # 1. Determine model and complexity based on attempt
            if attempt == 0:
                model_type = ModelRouter.route(task, evidence_confidence=evidence.get("confidence", 1.0) if evidence else 1.0)
                complexity = ContextComplexity.NORMAL
            elif attempt == 1:
                model_type = ModelRouter.route(task, failure_type=last_failure_type, attempt=1)
                complexity = ContextComplexity.NORMAL
            else:  # attempt 2
                model_type = ModelType.REASONING
                complexity = ContextComplexity.RECOVERY

            logger.info(f"[RECOVERY_STARTED] Attempt {attempt}/{self.MAX_ATTEMPTS - 1} using [{model_type.value}]")

            # 2. Build context including failure history
            prev_history_str = history.format_history_for_prompt() if attempt > 0 else None
            context_str = context_builder.build_context(
                task=task,
                file_snippets=file_snippets,
                evidence=evidence,
                previous_failure=prev_history_str,
                complexity=complexity,
            )

            # 3. Model generation
            req = ModelRequest(
                system_prompt=runtime.SYSTEM_PROMPT,
                user_prompt=context_str,
                model_type=model_type,
            )
            resp = provider.generate(req)

            if not resp.patch_proposal:
                last_failure_type = FailureType.PATCH_INVALID
                last_error = f"Model failed to generate valid PatchProposal schema. Raw: {resp.raw_content[:200]}"
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
