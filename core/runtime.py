import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from rich.logging import RichHandler

from task.schema import TaskDefinition, PatchProposal, TransactionResult
from core.workspace import WorkspaceManager, WorkspaceError
from core.state import PipelineEvent, EventLogEntry
from safety.policy import SafetyPolicy, SessionBudgetTracker
from safety.patch_validator import PatchValidationError
from safety.scope_guard import ScopeGuard, ScopeViolationError
from build.sandbox import BaseBuildRunner, MockBuildRunner, BuildResult
from recovery.classifier import FailureClassifier, FailureType
from model.schemas import ModelRequest, ModelResponse, ModelType
from model.providers import BaseModelProvider
from model.router import ModelRouter
from context.builder import ContextBuilder
from validation.pipeline import ValidationPipeline, ValidationReport, ValidationTier
from validation.behavioral import BaseBehavioralValidator
from validation.regression import BaseRegressionValidator

logging.basicConfig(
    level="INFO",
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, markup=True)]
)
logger = logging.getLogger("dsh.runtime")


class DSHRuntime:
    SYSTEM_PROMPT = """You are a deterministic code patch engine.
Your sole job is to produce a valid JSON object matching the PatchProposal schema.
Do NOT wrap your output in explanations. Output ONLY valid JSON with this structure:
{
  "patches": [
    {
      "file": "src/Player.cs",
      "hunks": [
        {
          "old_text": "...",
          "new_text": "..."
        }
      ]
    }
  ],
  "reason": "...",
  "confidence": 0.95
}
"""

    def __init__(
        self,
        workspace_path: Path,
        build_runner: Optional[BaseBuildRunner] = None,
        behavioral_validator: Optional[BaseBehavioralValidator] = None,
        regression_validator: Optional[BaseRegressionValidator] = None,
        policy: Optional[SafetyPolicy] = None,
        dry_run: bool = False,
        allowed_untracked_paths: Optional[List[str]] = None,
    ):
        self.workspace_path = workspace_path.resolve()
        self.ws = WorkspaceManager(self.workspace_path)
        self.build_runner = build_runner or MockBuildRunner(should_succeed=True)
        self.policy = policy or SafetyPolicy()
        self.session_tracker = SessionBudgetTracker(self.policy)
        self.scope_guard = ScopeGuard(policy=self.policy, session_tracker=self.session_tracker)
        self.validation_pipeline = ValidationPipeline(
            behavioral_validator=behavioral_validator,
            regression_validator=regression_validator,
        )
        self.dry_run = dry_run
        self.allowed_untracked_paths = allowed_untracked_paths or []
        self.event_log: List[EventLogEntry] = []

    def _log_event(self, event: PipelineEvent, task_id: str, message: str = "", details: Optional[dict] = None):
        entry = EventLogEntry(
            event=event,
            task_id=task_id,
            message=message,
            details=details or {},
        )
        self.event_log.append(entry)
        logger.info(f"[bold cyan][{event.value}][/bold cyan] {message}")

    def execute_with_recovery(
        self,
        task: TaskDefinition,
        provider: BaseModelProvider,
        context_builder: ContextBuilder,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> TransactionResult:
        """Executes a task with the 3-attempt recovery loop."""
        from recovery.manager import RecoveryManager
        recovery_mgr = RecoveryManager()
        return recovery_mgr.run_recovery_loop(
            task=task,
            runtime=self,
            provider=provider,
            context_builder=context_builder,
            evidence=evidence,
        )

    def execute_with_model(
        self,
        task: TaskDefinition,
        provider: BaseModelProvider,
        context_builder: ContextBuilder,
        evidence: Optional[Dict[str, Any]] = None,
        previous_failure: Optional[str] = None,
    ) -> TransactionResult:
        """Single-attempt model-driven transaction."""
        self._log_event(PipelineEvent.TASK_STARTED, task.task_id, f"Model-driven task {task.task_id}: '{task.title}'")

        file_snippets = {}
        for rel_file in task.allowed_files:
            file_path = (self.workspace_path / rel_file.lstrip("/")).resolve()
            if file_path.exists():
                file_snippets[rel_file] = file_path.read_text(encoding="utf-8")

        context_str = context_builder.build_context(
            task=task,
            file_snippets=file_snippets,
            evidence=evidence,
            previous_failure=previous_failure,
        )
        self._log_event(PipelineEvent.CONTEXT_BUILT, task.task_id, "Context assembled and ranked.")

        model_type = ModelRouter.route(
            task=task,
            evidence_confidence=evidence.get("confidence", 1.0) if evidence else 1.0,
        )
        self._log_event(PipelineEvent.MODEL_REQUEST, task.task_id, f"Querying model [{model_type.value}]...")

        req = ModelRequest(
            system_prompt=self.SYSTEM_PROMPT,
            user_prompt=context_str,
            model_type=model_type,
        )
        resp = provider.generate(req)
        self._log_event(PipelineEvent.MODEL_RESPONSE, task.task_id, f"Model responded with {resp.tokens_used} tokens.")

        if not resp.patch_proposal:
            self._log_event(PipelineEvent.FAILURE_CLASSIFIED, task.task_id, f"Model output is not valid JSON PatchProposal: {resp.error or resp.raw_content[:200]}")
            return TransactionResult(
                task_id=task.task_id,
                success=False,
                failure_type=FailureType.PATCH_INVALID.value,
                error_message=f"Model failed to generate valid PatchProposal JSON. Raw: {resp.raw_content[:200]}",
                dry_run=self.dry_run,
                events=[PipelineEvent.TASK_STARTED.value, PipelineEvent.MODEL_RESPONSE.value, PipelineEvent.FAILURE_CLASSIFIED.value],
            )

        return self.execute_transaction(task, resp.patch_proposal)

    def execute_transaction(self, task: TaskDefinition, proposal: PatchProposal) -> TransactionResult:
        self._log_event(PipelineEvent.TASK_STARTED, task.task_id, f"Executing transaction for {task.task_id}: '{task.title}'")
        checkpoint = None
        events: List[str] = [PipelineEvent.TASK_STARTED.value]

        try:
            # 1. Clean verification & Checkpoint creation
            checkpoint = self.ws.create_checkpoint(task.task_id, allow_untracked=self.allowed_untracked_paths)
            self._log_event(PipelineEvent.CHECKPOINT_CREATED, task.task_id, f"Git checkpoint created: {checkpoint}")
            events.append(PipelineEvent.CHECKPOINT_CREATED.value)

            # 2. Pre-apply Validation (T0 Structural + T4 Risk)
            pre_report = self.validation_pipeline.validate_pre_apply(
                task=task,
                proposal=proposal,
                repo_path=self.workspace_path,
                scope_guard=self.scope_guard,
            )
            if not pre_report.success:
                f_type = pre_report.failure_type.value if pre_report.failure_type else FailureType.UNKNOWN.value
                self._log_event(PipelineEvent.FAILURE_CLASSIFIED, task.task_id, f"Pre-apply validation failed [{pre_report.failed_tier.value}]: {pre_report.error_message}")
                events.append(PipelineEvent.FAILURE_CLASSIFIED.value)
                self.ws.rollback(checkpoint)
                self._log_event(PipelineEvent.ROLLBACK, task.task_id, f"Rolled back to {checkpoint}")
                events.append(PipelineEvent.ROLLBACK.value)
                return TransactionResult(
                    task_id=task.task_id,
                    success=False,
                    checkpoint=checkpoint,
                    failure_type=f_type,
                    error_message=pre_report.error_message,
                    dry_run=self.dry_run,
                    events=events,
                )

            self._log_event(PipelineEvent.SCOPE_PASSED, task.task_id, "T0 Structural & T4 Risk validation passed.")
            events.append(PipelineEvent.SCOPE_PASSED.value)

            # 3. Apply Patch to Filesystem
            for file_patch in proposal.patches:
                target_file = (self.workspace_path / file_patch.file.lstrip("/")).resolve()
                target_file.parent.mkdir(parents=True, exist_ok=True)

                content = target_file.read_text(encoding="utf-8") if target_file.exists() else ""
                for hunk in file_patch.hunks:
                    if hunk.old_text:
                        if hunk.old_text not in content:
                            raise PatchValidationError(f"Target hunk old_text not found in {file_patch.file}")
                        content = content.replace(hunk.old_text, hunk.new_text, 1)
                    else:
                        content += hunk.new_text

                target_file.write_text(content, encoding="utf-8")

            self._log_event(PipelineEvent.PATCH_APPLIED, task.task_id, f"Applied {len(proposal.patches)} file patch(es).")
            events.append(PipelineEvent.PATCH_APPLIED.value)

            # 4. Post-apply Validation (T1 Build + T2 Behavioral + T3 Regression)
            self._log_event(PipelineEvent.BUILD_STARTED, task.task_id, "Running post-apply validation (T1 Build, T2 Behavioral, T3 Regression)...")
            events.append(PipelineEvent.BUILD_STARTED.value)

            post_report = self.validation_pipeline.validate_post_apply(
                task=task,
                repo_path=self.workspace_path,
                build_runner=self.build_runner,
            )
            if not post_report.success:
                f_type = post_report.failure_type.value if post_report.failure_type else FailureType.UNKNOWN.value
                self._log_event(PipelineEvent.BUILD_FAILED, task.task_id, f"Post-apply validation failed [{post_report.failed_tier.value}]: {post_report.error_message}")
                events.append(PipelineEvent.BUILD_FAILED.value)

                self.ws.rollback(checkpoint)
                self._log_event(PipelineEvent.ROLLBACK, task.task_id, f"Rolled back to {checkpoint}")
                events.append(PipelineEvent.ROLLBACK.value)

                return TransactionResult(
                    task_id=task.task_id,
                    success=False,
                    checkpoint=checkpoint,
                    failure_type=f_type,
                    error_message=post_report.error_message,
                    dry_run=self.dry_run,
                    events=events,
                )

            self._log_event(PipelineEvent.VALIDATION_PASSED, task.task_id, "All 5 validation tiers (T0-T4) passed.")
            events.append(PipelineEvent.VALIDATION_PASSED.value)

            # 5. Commit or Dry-run Rollback
            if self.dry_run:
                self.ws.rollback(checkpoint)
                self._log_event(PipelineEvent.ROLLBACK, task.task_id, f"[DRY-RUN] Changes verified and rolled back to {checkpoint}")
                events.append(PipelineEvent.ROLLBACK.value)
                return TransactionResult(
                    task_id=task.task_id,
                    success=True,
                    checkpoint=checkpoint,
                    dry_run=True,
                    events=events,
                )
            else:
                commit_hash = self.ws.commit(task.task_id, task.title)
                self._log_event(PipelineEvent.COMMIT_CREATED, task.task_id, f"Transaction committed: {commit_hash}")
                self._log_event(PipelineEvent.TASK_COMPLETED, task.task_id, f"Task {task.task_id} completed successfully.")
                events.append(PipelineEvent.COMMIT_CREATED.value)
                events.append(PipelineEvent.TASK_COMPLETED.value)
                return TransactionResult(
                    task_id=task.task_id,
                    success=True,
                    checkpoint=checkpoint,
                    commit_hash=commit_hash,
                    dry_run=False,
                    events=events,
                )

        except Exception as exc:
            failure_type = FailureClassifier.classify_exception(exc)
            err_msg = str(exc)
            self._log_event(PipelineEvent.FAILURE_CLASSIFIED, task.task_id, f"Error ({failure_type.value}): {err_msg}")
            events.append(PipelineEvent.FAILURE_CLASSIFIED.value)

            if checkpoint:
                self.ws.rollback(checkpoint)
                self._log_event(PipelineEvent.ROLLBACK, task.task_id, f"Workspace restored to {checkpoint}")
                events.append(PipelineEvent.ROLLBACK.value)

            return TransactionResult(
                task_id=task.task_id,
                success=False,
                checkpoint=checkpoint,
                failure_type=failure_type.value,
                error_message=err_msg,
                dry_run=self.dry_run,
                events=events,
            )
