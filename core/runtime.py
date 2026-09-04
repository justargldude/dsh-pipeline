import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from rich.logging import RichHandler

from task.schema import TaskDefinition, PatchProposal, TransactionResult
from core.config import PipelineConfig
from core.workspace import (
    WorkspaceManager,
    WorkspaceError,
    TransactionWorktree,
    WorktreeCleanupError,
    WorktreeStagingError,
    generate_transaction_id,
)
from core.state import PipelineEvent, EventLogEntry, TransactionState
from safety.policy import SafetyPolicy, SessionBudgetTracker
from safety.patch_engine import (
    PatchValidationError,
    validate_and_simulate_proposal,
    normalize_repo_path,
    atomic_write_file,
)
from safety.scope_guard import ScopeGuard, ScopeViolationError
from build.sandbox import BaseBuildRunner, MockBuildRunner, SubprocessBuildRunner, BuildResult
from recovery.classifier import FailureClassifier, FailureType
from model.schemas import ModelRequest, ModelResponse, ModelType
from model.providers import BaseModelProvider
from model.router import ModelRouter
from context.budget import ContextBudgetExceededError
from context.builder import ContextBuilder
from validation.pipeline import ValidationPipeline, ValidationReport, ValidationTier
from validation.behavioral import BaseBehavioralValidator, MockBehavioralValidator, SubprocessBehavioralValidator
from validation.regression import BaseRegressionValidator, MockRegressionValidator, SubprocessRegressionValidator
from validation.baseline import BaselineManager, BaselineState
from memory.episodic import EpisodicMemoryStore, EpisodeRecord, EpisodeValidation
from memory.retrieval import EpisodeRetriever

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
        config: Optional[PipelineConfig] = None,
        build_runner: Optional[BaseBuildRunner] = None,
        behavioral_validator: Optional[BaseBehavioralValidator] = None,
        regression_validator: Optional[BaseRegressionValidator] = None,
        baseline_manager: Optional[BaselineManager] = None,
        policy: Optional[SafetyPolicy] = None,
        dry_run: bool = False,
        allowed_untracked_paths: Optional[List[str]] = None,
        test_mode: bool = False,
        episodic_store: Optional[EpisodicMemoryStore] = None,
        episode_retriever: Optional[EpisodeRetriever] = None,
    ):
        self.workspace_path = workspace_path.resolve()
        self.ws = WorkspaceManager(self.workspace_path)
        self.config = config or PipelineConfig(workspace_root=self.workspace_path)
        self.test_mode = test_mode
        # Bug 7a: optional episodic memory wiring. Defaults keep legacy
        # behavior 100% unchanged (no new code path runs when unset).
        self.episodic_store = episodic_store
        self.episode_retriever = episode_retriever

        # Initialize build runner
        if build_runner is not None:
            self.build_runner = build_runner
        elif self.config.build_command:
            self.build_runner = SubprocessBuildRunner(
                build_cmd=self.config.build_command,
                timeout_seconds=self.config.default_timeout_seconds,
            )
        elif self.test_mode:
            self.build_runner = MockBuildRunner(should_succeed=True)
        else:
            self.build_runner = None  # Fail closed in production if unconfigured

        # Initialize behavioral validator
        if behavioral_validator is not None:
            self.behavioral_validator = behavioral_validator
        elif self.config.behavioral_command:
            self.behavioral_validator = SubprocessBehavioralValidator(
                cmd=self.config.behavioral_command,
                timeout_seconds=self.config.default_timeout_seconds,
            )
        elif self.test_mode:
            self.behavioral_validator = MockBehavioralValidator(should_succeed=True)
        else:
            self.behavioral_validator = None

        # Initialize regression validator
        if regression_validator is not None:
            self.regression_validator = regression_validator
        elif self.config.test_command:
            self.regression_validator = SubprocessRegressionValidator(
                test_cmd=self.config.test_command,
                timeout_seconds=self.config.default_timeout_seconds,
            )
        elif self.test_mode:
            self.regression_validator = MockRegressionValidator(should_succeed=True)
        else:
            self.regression_validator = None

        self.policy = policy or SafetyPolicy()
        self.session_tracker = SessionBudgetTracker(self.policy)
        self.scope_guard = ScopeGuard(policy=self.policy, session_tracker=self.session_tracker)
        self.validation_pipeline = ValidationPipeline(
            behavioral_validator=self.behavioral_validator,
            regression_validator=self.regression_validator,
        )
        self.baseline_manager = baseline_manager or BaselineManager(
            enable_cache=self.config.enable_baseline_cache
        )
        self.dry_run = dry_run
        self.allowed_untracked_paths = allowed_untracked_paths or self.config.allowed_untracked_paths or []
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

    def _safe_cleanup_worktree(self, tx_worktree: Optional[TransactionWorktree], task_id: str) -> Optional[str]:
        if tx_worktree is None:
            return None
        try:
            self.ws.remove_transaction_worktree(tx_worktree)
            return None
        except Exception as e:
            err_msg = f"Worktree cleanup failed for {tx_worktree.tx_id}: {str(e)}"
            logger.error(f"[CLEANUP_FAILED] {err_msg}")
            self._log_event(PipelineEvent.FAILURE_CLASSIFIED, task_id, err_msg)
            return err_msg

    def _retrieve_advisory_episodes(self, task: TaskDefinition) -> Optional[List[EpisodeRecord]]:
        """Bug 7a: retrieves advisory episodes for up to the first 2 target symbols.

        Returns None when no retriever is configured (legacy behavior) or no
        target symbols exist (R1a: never query with an empty symbol).
        Advisory memory is auxiliary: any retrieval error is logged and
        swallowed — it must never break the transaction.
        """
        if self.episode_retriever is None or not task.target_symbols:
            return None
        try:
            current_version = self.ws.get_head_commit()
            collected: List[EpisodeRecord] = []
            seen_ids = set()
            for symbol in task.target_symbols[:2]:
                episodes = self.episode_retriever.retrieve_advisory_episodes(
                    symbol=symbol,
                    current_version=current_version,
                    current_env="linux",
                    max_results=2,
                )
                for ep in episodes:
                    # Dedup by episode object identity (tests share records).
                    if id(ep) not in seen_ids:
                        seen_ids.add(id(ep))
                        collected.append(ep)
            return collected
        except Exception as e:
            logger.warning(f"[ADVISORY_MEMORY] Episode retrieval failed (continuing without): {e}")
            return None

    def _store_success_episode(
        self,
        task: TaskDefinition,
        proposal: PatchProposal,
        tx_worktree: TransactionWorktree,
    ) -> None:
        """Bug 7a: persists a validated episode after an INTEGRATED transaction.

        Memory is auxiliary: any error here is logged and swallowed so an
        episodic store failure never turns a success into a failure.
        """
        if self.episodic_store is None:
            return
        try:
            symbol = task.target_symbols[0] if task.target_symbols else task.task_id
            episode = EpisodeRecord(
                task_id=task.task_id,
                symbol=symbol,
                solution_patch=proposal,
                version=tx_worktree.base_commit,
                environment="linux",
                validation=EpisodeValidation(build=True, behavior=True, regression=True),
            )
            self.episodic_store.store_episode(episode)
        except Exception as e:
            logger.warning(f"[ADVISORY_MEMORY] Episode store failed (continuing without): {e}")

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
        for raw_file in task.allowed_files:
            rel_file = normalize_repo_path(raw_file)
            file_path = (self.workspace_path / rel_file).resolve()
            if file_path.exists():
                file_snippets[rel_file] = file_path.read_text(encoding="utf-8")

        try:
            context_str = context_builder.build_context(
                task=task,
                file_snippets=file_snippets,
                evidence=evidence,
                previous_failure=previous_failure,
                advisory_episodes=self._retrieve_advisory_episodes(task),
            )
        except ContextBudgetExceededError as cbe:
            self._log_event(PipelineEvent.FAILURE_CLASSIFIED, task.task_id, f"Context budget exceeded: {cbe}")
            return TransactionResult(
                task_id=task.task_id,
                success=False,
                failure_type="CONTEXT_BUDGET_EXCEEDED",
                error_message=str(cbe),
                dry_run=self.dry_run,
                events=[PipelineEvent.TASK_STARTED.value, PipelineEvent.FAILURE_CLASSIFIED.value],
            )
        self._log_event(PipelineEvent.CONTEXT_BUILT, task.task_id, "Context assembled and ranked.")

        evidence_conf = evidence.get("confidence") if evidence else None
        model_type = ModelRouter.route(
            task=task,
            evidence_confidence=evidence_conf,
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
            f_type = resp.failure_type or FailureType.MODEL_FORMAT_ERROR
            err_detail = resp.error or f"Model failed to generate valid PatchProposal JSON. Raw: {resp.raw_content[:200]}"
            self._log_event(PipelineEvent.FAILURE_CLASSIFIED, task.task_id, f"Model output failed [{f_type.value}]: {err_detail}")
            return TransactionResult(
                task_id=task.task_id,
                success=False,
                failure_type=f_type.value,
                error_message=err_detail,
                dry_run=self.dry_run,
                events=[PipelineEvent.TASK_STARTED.value, PipelineEvent.MODEL_RESPONSE.value, PipelineEvent.FAILURE_CLASSIFIED.value],
            )

        return self.execute_transaction(task, resp.patch_proposal)

    def execute_transaction(self, task: TaskDefinition, proposal: PatchProposal) -> TransactionResult:
        self._log_event(PipelineEvent.TASK_STARTED, task.task_id, f"Executing transaction for {task.task_id}: '{task.title}'")
        events: List[str] = [PipelineEvent.TASK_STARTED.value]
        tx_id = generate_transaction_id(task.task_id)
        tx_worktree: Optional[TransactionWorktree] = None
        base_commit: Optional[str] = None

        # Fail closed if production runtime has no build runner configured
        if self.build_runner is None:
            err_msg = "BUILD_CONFIGURATION_MISSING: No trusted build command or build runner configured for production runtime."
            self._log_event(PipelineEvent.FAILURE_CLASSIFIED, task.task_id, err_msg)
            events.append(PipelineEvent.FAILURE_CLASSIFIED.value)
            return TransactionResult(
                task_id=task.task_id,
                success=False,
                failure_type=FailureType.CONFIG_MISSING.value,
                error_message=err_msg,
                dry_run=self.dry_run,
                events=events,
            )

        try:
            # 1. Base Commit & Worktree Creation
            base_commit = self.ws.get_head_commit()
            tx_worktree = self.ws.create_transaction_worktree(
                tx_id=tx_id,
                base_commit=base_commit,
                worktree_dir=self.config.worktree_dir,
            )
            self._log_event(PipelineEvent.CHECKPOINT_CREATED, task.task_id, f"Isolated worktree created for {tx_id} at {tx_worktree.worktree_path}")
            events.append(PipelineEvent.CHECKPOINT_CREATED.value)

            # 2. Pre-apply Validation (T0 Structural + T4 Risk) executed in worktree
            self.ws.journal.update_state(tx_id, TransactionState.VALIDATING)
            pre_report = self.validation_pipeline.validate_pre_apply(
                task=task,
                proposal=proposal,
                repo_path=tx_worktree.worktree_path,
                scope_guard=self.scope_guard,
                reservation_id=tx_id,
            )
            if not pre_report.success:
                self.session_tracker.release(tx_id)
                self.ws.journal.update_state(tx_id, TransactionState.FAILED)
                f_type = pre_report.failure_type.value if pre_report.failure_type else FailureType.UNKNOWN.value
                self._log_event(PipelineEvent.FAILURE_CLASSIFIED, task.task_id, f"Pre-apply validation failed [{pre_report.failed_tier.value}]: {pre_report.error_message}")
                events.append(PipelineEvent.FAILURE_CLASSIFIED.value)

                cleanup_err = self._safe_cleanup_worktree(tx_worktree, task.task_id)
                self._log_event(PipelineEvent.ROLLBACK, task.task_id, f"Discarded worktree for {tx_id}")
                events.append(PipelineEvent.ROLLBACK.value)

                if cleanup_err:
                    f_type = FailureType.ROLLBACK_FAILED.value

                return TransactionResult(
                    task_id=task.task_id,
                    success=False,
                    base_commit=base_commit,
                    worktree_path=str(tx_worktree.worktree_path),
                    failure_type=f_type,
                    error_message=pre_report.error_message,
                    dry_run=self.dry_run,
                    cleanup_error=cleanup_err,
                    events=events,
                    integration_status="ROLLED_BACK",
                )

            self._log_event(PipelineEvent.SCOPE_PASSED, task.task_id, "T0 Structural & T4 Risk validation passed.")
            events.append(PipelineEvent.SCOPE_PASSED.value)

            # 3. Capture BASELINE inside isolated worktree (BEFORE candidate patch)
            self._log_event(PipelineEvent.BASELINE_CAPTURED, task.task_id, "Capturing pre-patch baseline build and validation state...")
            events.append(PipelineEvent.BASELINE_CAPTURED.value)
            self.ws.journal.update_state(tx_id, TransactionState.BASELINE_CAPTURED)

            baseline = self.baseline_manager.get_or_capture_baseline(
                repo_path=tx_worktree.worktree_path,
                ws=self.ws,
                build_runner=self.build_runner,
                regression_validator=self.validation_pipeline.regression_validator,
                behavioral_validator=self.validation_pipeline.behavioral_validator,
                base_commit=tx_worktree.base_commit,
            )

            # 4. Apply Patch to Filesystem in Isolated Worktree using Atomic Writes
            simulated_files = validate_and_simulate_proposal(
                proposal,
                tx_worktree.worktree_path,
                max_file_size_bytes=self.config.max_file_size_bytes,
                max_patch_size_bytes=self.config.max_patch_size_bytes,
                max_files=self.config.max_files_in_patch,
            )
            for rel_file, new_content in simulated_files.items():
                target_file = (tx_worktree.worktree_path / rel_file).resolve()
                atomic_write_file(target_file, new_content, encoding="utf-8", preserve_newline=True)

            self.ws.journal.update_state(tx_id, TransactionState.PATCH_APPLIED)
            self._log_event(PipelineEvent.PATCH_APPLIED, task.task_id, f"Applied {len(proposal.patches)} file patch(es) in worktree.")
            events.append(PipelineEvent.PATCH_APPLIED.value)

            # Final State Pre-Build Verification: Compare simulated content vs actual on-disk content
            for rel_file, expected_content in simulated_files.items():
                actual_file = (tx_worktree.worktree_path / rel_file).resolve()
                if not actual_file.exists():
                    raise PatchValidationError(f"Expected patched file '{rel_file}' missing from disk.")
                disk_content = actual_file.read_text(encoding="utf-8")
                # Normalize line endings for invariant check
                if disk_content.replace("\r\n", "\n") != expected_content.replace("\r\n", "\n"):
                    raise PatchValidationError(f"Pre-build state divergence detected on disk for '{rel_file}'.")

            # 5. Post-apply Validation (T1 Build + T2 Behavioral + T3 Regression) inside Worktree
            self._log_event(PipelineEvent.BUILD_STARTED, task.task_id, "Running post-apply validation (T1 Build, T2 Behavioral, T3 Regression) in worktree...")
            events.append(PipelineEvent.BUILD_STARTED.value)

            post_report = self.validation_pipeline.validate_post_apply(
                task=task,
                repo_path=tx_worktree.worktree_path,
                build_runner=self.build_runner,
                baseline=baseline,
            )
            if not post_report.success:
                self.session_tracker.release(tx_id)
                self.ws.journal.update_state(tx_id, TransactionState.FAILED)
                f_type = post_report.failure_type.value if post_report.failure_type else FailureType.UNKNOWN.value
                self._log_event(PipelineEvent.BUILD_FAILED, task.task_id, f"Post-apply validation failed [{post_report.failed_tier.value}]: {post_report.error_message}")
                events.append(PipelineEvent.BUILD_FAILED.value)

                cleanup_err = self._safe_cleanup_worktree(tx_worktree, task.task_id)
                self._log_event(PipelineEvent.ROLLBACK, task.task_id, f"Discarded worktree for {tx_id}")
                events.append(PipelineEvent.ROLLBACK.value)

                if cleanup_err:
                    f_type = FailureType.ROLLBACK_FAILED.value

                return TransactionResult(
                    task_id=task.task_id,
                    success=False,
                    base_commit=base_commit,
                    worktree_path=str(tx_worktree.worktree_path),
                    failure_type=f_type,
                    error_message=post_report.error_message,
                    dry_run=self.dry_run,
                    cleanup_error=cleanup_err,
                    events=events,
                    integration_status="ROLLED_BACK",
                )

            self._log_event(PipelineEvent.VALIDATION_PASSED, task.task_id, "All validation tiers passed without regression.")
            events.append(PipelineEvent.VALIDATION_PASSED.value)
            self.ws.journal.update_state(tx_id, TransactionState.VALIDATED)

            # 6. Verify Changed Paths in Worktree (No unexpected modified or untracked files)
            expected_paths = {normalize_repo_path(p.file) for p in proposal.patches}
            is_clean_paths, unexpected = tx_worktree.verify_changed_paths(
                expected_paths=expected_paths,
                allowed_untracked_paths=self.allowed_untracked_paths,
            )
            if not is_clean_paths:
                self.session_tracker.release(tx_id)
                self.ws.journal.update_state(tx_id, TransactionState.FAILED)
                err_msg = f"UNEXPECTED_CHANGES: Worktree contains unexpected modifications or generated files: {unexpected}"
                self._log_event(PipelineEvent.FAILURE_CLASSIFIED, task.task_id, err_msg)
                events.append(PipelineEvent.FAILURE_CLASSIFIED.value)

                cleanup_err = self._safe_cleanup_worktree(tx_worktree, task.task_id)
                self._log_event(PipelineEvent.ROLLBACK, task.task_id, f"Discarded worktree for {tx_id}")
                events.append(PipelineEvent.ROLLBACK.value)

                f_type = FailureType.ROLLBACK_FAILED.value if cleanup_err else FailureType.SCOPE_VIOLATION.value

                return TransactionResult(
                    task_id=task.task_id,
                    success=False,
                    base_commit=base_commit,
                    worktree_path=str(tx_worktree.worktree_path),
                    failure_type=f_type,
                    error_message=err_msg,
                    dry_run=self.dry_run,
                    cleanup_error=cleanup_err,
                    events=events,
                    integration_status="ROLLED_BACK",
                )

            # 7. Dry-Run Check
            if self.dry_run:
                self.session_tracker.release(tx_id)
                self.ws.journal.update_state(tx_id, TransactionState.DISCARDED)
                cleanup_err = self._safe_cleanup_worktree(tx_worktree, task.task_id)
                self._log_event(PipelineEvent.ROLLBACK, task.task_id, f"[DRY-RUN] Changes verified in worktree and discarded.")
                events.append(PipelineEvent.ROLLBACK.value)
                return TransactionResult(
                    task_id=task.task_id,
                    success=True,
                    base_commit=base_commit,
                    dry_run=True,
                    cleanup_error=cleanup_err,
                    events=events,
                    integration_status="DRY_RUN",
                )

            # 8. Exact Staging & Staging Verification
            self.ws.journal.update_state(tx_id, TransactionState.STAGED)
            tx_worktree.stage_exact(list(expected_paths))

            # 9. Commit inside Isolated Worktree
            commit_hash = tx_worktree.commit(
                task.task_id,
                task.title,
                expected_paths=list(expected_paths),
                allowed_untracked_paths=self.allowed_untracked_paths,
            )
            self.ws.journal.update_state(tx_id, TransactionState.COMMITTED)
            self._log_event(PipelineEvent.COMMIT_CREATED, task.task_id, f"Transaction committed in worktree: {commit_hash}")
            events.append(PipelineEvent.COMMIT_CREATED.value)

            # 10. Integration into Main Workspace
            integration_status = self.ws.integrate_transaction(
                tx_worktree, commit_hash, allowed_untracked_paths=self.allowed_untracked_paths
            )
            if integration_status == "INTEGRATED":
                self.ws.journal.update_state(tx_id, TransactionState.INTEGRATED)
            elif integration_status == "STALE_BASE":
                self.ws.journal.update_state(tx_id, TransactionState.STALE_BASE)
            else:
                self.ws.journal.update_state(tx_id, TransactionState.READY_TO_INTEGRATE)

            self._log_event(PipelineEvent.TASK_COMPLETED, task.task_id, f"Task {task.task_id} completed with integration status: {integration_status}")
            events.append(PipelineEvent.TASK_COMPLETED.value)

            if integration_status != "INTEGRATED":
                self.session_tracker.release(tx_id)
                return TransactionResult(
                    task_id=task.task_id,
                    success=False,
                    commit_hash=commit_hash,
                    base_commit=base_commit,
                    worktree_path=str(tx_worktree.worktree_path),
                    error_message=(
                        f"PENDING_INTEGRATION: {integration_status} — worktree retained "
                        "for manual/next-phase integration"
                    ),
                    dry_run=False,
                    events=events,
                    integration_status=integration_status,
                )

            # Bug 7a: persist validated episode to episodic memory on a fully
            # integrated success, BEFORE worktree cleanup (which needs the
            # base_commit/symbols still meaningful). Errors are swallowed
            # inside the helper — memory must never break the transaction.
            self._store_success_episode(task, proposal, tx_worktree)

            # 11. Cleanup Isolated Worktree
            cleanup_err = self._safe_cleanup_worktree(tx_worktree, task.task_id)
            if cleanup_err:
                self.session_tracker.release(tx_id)
                return TransactionResult(
                    task_id=task.task_id,
                    success=False,
                    commit_hash=commit_hash,
                    base_commit=base_commit,
                    worktree_path=str(tx_worktree.worktree_path),
                    cleanup_error=cleanup_err,
                    failure_type=FailureType.ROLLBACK_FAILED.value,
                    error_message=f"CLEANUP_FAILED: {cleanup_err}",
                    dry_run=False,
                    events=events,
                    integration_status=integration_status,
                )

            # Commit budget
            self.session_tracker.commit(tx_id)

            return TransactionResult(
                task_id=task.task_id,
                success=True,
                commit_hash=commit_hash,
                base_commit=base_commit,
                worktree_path=str(tx_worktree.worktree_path),
                dry_run=False,
                events=events,
                integration_status=integration_status,
            )

        except Exception as exc:
            self.session_tracker.release(tx_id)
            if tx_worktree is not None:
                self.ws.journal.update_state(tx_id, TransactionState.FAILED)
            failure_type = FailureClassifier.classify_exception(exc)
            err_msg = str(exc)
            self._log_event(PipelineEvent.FAILURE_CLASSIFIED, task.task_id, f"Error ({failure_type.value}): {err_msg}")
            events.append(PipelineEvent.FAILURE_CLASSIFIED.value)

            cleanup_err = None
            if tx_worktree is not None:
                cleanup_err = self._safe_cleanup_worktree(tx_worktree, task.task_id)
                self._log_event(PipelineEvent.ROLLBACK, task.task_id, f"Worktree discarded for {tx_id}")
                events.append(PipelineEvent.ROLLBACK.value)

            if cleanup_err:
                failure_type = FailureType.ROLLBACK_FAILED

            return TransactionResult(
                task_id=task.task_id,
                success=False,
                base_commit=base_commit if base_commit else (self.ws.get_head_commit() if (self.workspace_path / ".git").exists() else None),
                worktree_path=str(tx_worktree.worktree_path) if tx_worktree else None,
                failure_type=failure_type.value,
                error_message=err_msg,
                dry_run=self.dry_run,
                cleanup_error=cleanup_err,
                events=events,
                integration_status="FAILED",
            )
