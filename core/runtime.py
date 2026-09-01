import logging
from pathlib import Path
from typing import List, Optional
from rich.logging import RichHandler

from task.schema import TaskDefinition, PatchProposal, TransactionResult
from core.workspace import WorkspaceManager, WorkspaceError
from core.state import PipelineEvent, EventLogEntry
from safety.patch_validator import PatchValidator, PatchValidationError
from safety.scope_guard import ScopeGuard, ScopeViolationError
from build.sandbox import BaseBuildRunner, MockBuildRunner, BuildResult
from recovery.classifier import FailureClassifier, FailureType

logging.basicConfig(
    level="INFO",
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, markup=True)]
)
logger = logging.getLogger("dsh.runtime")


class DSHRuntime:
    def __init__(
        self,
        workspace_path: Path,
        build_runner: Optional[BaseBuildRunner] = None,
        dry_run: bool = False,
        allowed_untracked_paths: Optional[List[str]] = None,
    ):
        self.workspace_path = workspace_path.resolve()
        self.ws = WorkspaceManager(self.workspace_path)
        self.build_runner = build_runner or MockBuildRunner(should_succeed=True)
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

    def execute_transaction(self, task: TaskDefinition, proposal: PatchProposal) -> TransactionResult:
        self._log_event(PipelineEvent.TASK_STARTED, task.task_id, f"Starting task {task.task_id}: '{task.title}'")
        checkpoint = None
        events: List[str] = [PipelineEvent.TASK_STARTED.value]

        try:
            # 1. Clean verification & Checkpoint creation
            checkpoint = self.ws.create_checkpoint(task.task_id, allow_untracked=self.allowed_untracked_paths)
            self._log_event(PipelineEvent.CHECKPOINT_CREATED, task.task_id, f"Git checkpoint created: {checkpoint}")
            events.append(PipelineEvent.CHECKPOINT_CREATED.value)

            # 2. Patch Validator
            PatchValidator.validate_proposal(proposal, self.workspace_path)
            self._log_event(PipelineEvent.PATCH_VALIDATED, task.task_id, "Patch structure & hunk applicability verified.")
            events.append(PipelineEvent.PATCH_VALIDATED.value)

            # 3. Scope Guard (Deterministic firewall)
            ScopeGuard.validate(task, proposal, self.workspace_path)
            self._log_event(PipelineEvent.SCOPE_PASSED, task.task_id, "Diff limits, allowed_files & anti-bypass checks passed.")
            events.append(PipelineEvent.SCOPE_PASSED.value)

            # 4. Apply Patch to Filesystem
            for file_patch in proposal.patches:
                target_file = (self.workspace_path / file_patch.file.lstrip("/")).resolve()
                target_file.parent.mkdir(parents=True, exist_ok=True)

                if target_file.exists():
                    content = target_file.read_text(encoding="utf-8")
                else:
                    content = ""

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

            # 5. Sandbox Build
            self._log_event(PipelineEvent.BUILD_STARTED, task.task_id, "Invoking build runner...")
            events.append(PipelineEvent.BUILD_STARTED.value)
            build_res = self.build_runner.build(self.workspace_path)

            if not build_res.success:
                failure_type = FailureClassifier.classify_build_failure(build_res)
                err_msg = "\n".join([e.message for e in build_res.errors]) or build_res.raw_output
                self._log_event(PipelineEvent.BUILD_FAILED, task.task_id, f"Build failed with {failure_type.value}: {err_msg}")
                events.append(PipelineEvent.BUILD_FAILED.value)

                self.ws.rollback(checkpoint)
                self._log_event(PipelineEvent.ROLLBACK, task.task_id, f"Rolled back to {checkpoint}")
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

            self._log_event(PipelineEvent.BUILD_PASSED, task.task_id, "Build succeeded.")
            events.append(PipelineEvent.BUILD_PASSED.value)

            # 6. Commit or Dry-run Rollback
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
