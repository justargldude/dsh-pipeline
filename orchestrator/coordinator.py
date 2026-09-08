import json
import logging
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from core.config import PipelineConfig, get_model_family
from core.workspace import get_git_dir
from core.runtime import DSHRuntime
from context.builder import ContextBuilder
from task.schema import TaskDefinition, RiskLevel, TransactionResult
from orchestrator.subagents import (
    SubagentClient,
    AntigravityClient,
    DeepSeekClient,
    create_qa_client,
    create_dev_provider,
)
from model.providers import BaseModelProvider
from orchestrator.planner import AutonomousPlanner, AuditReport, PlannedTask

logger = logging.getLogger("dsh.orchestrator.coordinator")


class TaskExecutionRecord(BaseModel):
    task_id: str
    title: str
    success: bool
    commit_hash: Optional[str] = None
    diff_summary: Optional[str] = None
    review_verdict: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None


class OrchestrationResult(BaseModel):
    success: bool
    goal: str
    target_repo: str
    audit_summary: str
    tasks: List[TaskExecutionRecord] = Field(default_factory=list)
    final_report: str = ""


class AutonomousCoordinator:
    """End-to-end coordinator orchestrating a QA subagent (Audit/Review) and
    a Dev model provider (Coder/Patch) on top of the DSH Pipeline transaction sandbox.
    """

    def __init__(
        self,
        target_repo: Path,
        qa_client: Optional[SubagentClient] = None,
        dev_provider: Optional[BaseModelProvider] = None,
        antigravity: Optional[SubagentClient] = None,
        deepseek: Optional[Any] = None,
        qa_name: str = "agy",
        dev_name: str = "deepseek",
        dry_run: bool = True,
        test_mode: bool = False,
        prover_client: Optional[Any] = None,
        prover_name: str = "security-prover",
    ):
        self.target_repo = target_repo.resolve()
        self.test_mode = test_mode
        self.dry_run = dry_run

        # Resolve QA client
        self.qa_client = (
            qa_client
            or antigravity
            or create_qa_client(qa_name, test_mode=test_mode)
        )
        self.antigravity = self.qa_client  # backwards-compatible alias

        # Resolve Dev provider
        if dev_provider:
            self.dev_provider = dev_provider
        elif deepseek is not None:
            self.dev_provider = (
                deepseek.get_provider()
                if hasattr(deepseek, "get_provider")
                else deepseek
            )
        else:
            self.dev_provider = create_dev_provider(dev_name, test_mode=test_mode)
        self.deepseek = deepseek  # backwards-compatible alias
        # QA round-2 F-01: run manifest dir allowance; set in run().
        self._run_dir: Optional[Path] = None

        # v2.3 Phase D1: Security Prover — a third reviewer whose model
        # family must differ from Dev's (enforced below like QA's).
        self.prover_client = prover_client
        self.prover_name = prover_name

        # Cross-family enforcement (Anti-Reward Hacking v2.3 Section 0)
        resolved_qa = qa_name or (getattr(self.qa_client, "name", None) or "agy")
        resolved_dev = dev_name or (getattr(self.dev_provider, "model_name", None) or "deepseek")
        qa_fam = get_model_family(resolved_qa)
        dev_fam = get_model_family(resolved_dev)
        if qa_fam == dev_fam and not (qa_fam.startswith("mock") or dev_fam.startswith("mock") or qa_fam.startswith("unknown")):
            raise ValueError(
                f"Cross-family enforcement failed: QA model '{resolved_qa}' ({qa_fam}) "
                f"and Dev model '{resolved_dev}' ({dev_fam}) belong to the same family. "
                "QA and Dev must belong to different model families to prevent shared blind spots."
            )

        # v2.3 Phase D1: the prover reviews the diff with fresh eyes from a
        # DIFFERENT family than Dev — same-family would share Dev's blind
        # spots on security issues (mock/unknown families pass for tests).
        if self.prover_client is not None:
            prover_fam = get_model_family(prover_name)
            if (
                prover_fam == dev_fam
                and not prover_fam.startswith("mock")
                and not dev_fam.startswith("mock")
                and not prover_fam.startswith("unknown")
                and not dev_fam.startswith("unknown")
            ):
                raise ValueError(
                    f"Cross-family enforcement failed for prover: Security Prover "
                    f"'{prover_name}' ({prover_fam}) and Dev model '{resolved_dev}' "
                    f"({dev_fam}) belong to the same family. The prover must bring "
                    "an outside perspective on exploitability."
                )


    def _red_test_worktree_callback(self, task: PlannedTask):
        """Returns a callback that copies this task's red test into each
        transaction worktree right after creation (before baseline capture),
        fixing false-green validation that ran without the red test."""
        def _callback(worktree_path) -> None:
            try:
                self._ensure_red_test_in_worktree(task, Path(worktree_path))
            except Exception as e:
                logger.warning(f"[COORDINATOR] Red-test worktree propagation failed: {e}")
        return _callback

    def _untracked_allowance_for_manifest(self) -> Optional[List[str]]:
        """Untracked paths the manifest dir occupies (F-01)."""
        run_dir = getattr(self, "_run_dir", None)
        if run_dir is None:
            return None
        try:
            run_dir_path = Path(run_dir).resolve()
            target_repo_path = self.target_repo.resolve()
            rel = str(run_dir_path.relative_to(target_repo_path))
            if rel.startswith(".git/") or rel.startswith(".git\\") or rel == ".git":
                return None
            return [rel]
        except ValueError:
            # Fallback to absolute path if not relative (e.g., cross-drive on Windows)
            return [str(Path(run_dir).resolve())]

    def _manifest_init(self, run_dir: Path) -> Path:
        """Creates the run directory and an empty manifest.json (no .gitignore mutation)."""
        run_dir = Path(run_dir)
        if not run_dir.is_absolute():
            run_dir = self.target_repo / run_dir
        run_dir = run_dir.resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        self._run_dir = run_dir

        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            self._manifest_atomic_write(manifest_path, {"tasks": {}})
        return manifest_path

    def _manifest_update(
        self,
        run_dir: Path,
        task_id: str,
        attempt: int,
        status: str,
        failure_reason: Optional[str] = None,
        worktree_path: Optional[str] = None,
    ) -> None:
        """Updates one task's state in run_dir/manifest.json atomically."""
        manifest_path = Path(run_dir) / "manifest.json"
        data = {"tasks": {}}
        if manifest_path.exists():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                data = {"tasks": {}}
        entry = data["tasks"].get(task_id, {"task_id": task_id})
        entry["task_id"] = task_id
        entry["status"] = status
        entry["attempts"] = max(int(entry.get("attempts", 0)), int(attempt))
        entry["failure_reason"] = failure_reason
        entry["worktree_path"] = worktree_path
        data["tasks"][task_id] = entry
        self._manifest_atomic_write(manifest_path, data)

    @staticmethod
    def _manifest_atomic_write(manifest_path: Path, data: Dict[str, Any]) -> None:
        from safety.patch_engine import atomic_write_file
        atomic_write_file(manifest_path, json.dumps(data, indent=2))

    def _write_red_test_to_main(self, task: PlannedTask, worktree_path: Optional[Path] = None) -> bool:
        """Writes the QA red test directly into a transaction worktree instead of polluting main repo."""
        if not (task.test_file and task.test_code):
            return False
        if worktree_path is not None:
            dst = Path(worktree_path) / task.test_file
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(task.test_code, encoding="utf-8")
        return True

    def _ensure_red_test_in_worktree(self, task: PlannedTask, worktree_path: Path) -> bool:
        """Writes the QA red test directly into a transaction worktree."""
        return self._write_red_test_to_main(task, worktree_path=worktree_path)

    def _count_discovered_tests(self, count_cmd: List[str]) -> Optional[int]:
        """Runs a test-discovery counting command; returns parsed count or None."""
        try:
            proc = subprocess.run(
                count_cmd,
                cwd=self.target_repo,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc.returncode != 0:
                return None
            return int(proc.stdout.strip().splitlines()[-1])
        except Exception:
            return None

    def _verify_red_test_discovered(
        self, task: PlannedTask, count_cmd: Optional[List[str]] = None
    ) -> bool:
        """Discovery smoke-check: the runner must discover MORE tests after the
        red test file exists, otherwise the test is invisible (e.g. wrong
        naming pattern for node --test / pytest discovery) and the whole TDD
        phase would silently pass without ever running it.
        """
        if not (task.test_file and task.test_cmd):
            return True
        cmd = count_cmd or shlex.split(task.test_cmd)
        before = self._count_discovered_tests(cmd)
        if before is None:
            return True
        src = self.target_repo / task.test_file
        hidden = not src.exists()
        if hidden:
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(task.test_code or "", encoding="utf-8")
        try:
            after = self._count_discovered_tests(cmd)
        finally:
            if hidden:
                try:
                    src.unlink()
                except OSError:
                    pass
        return after is None or after > before

    def _review_diff_with_qa(
        self, task: "PlannedTask", diff_text: str
    ) -> Dict[str, Any]:
        """Prompts the QA subagent to review the final diff produced by Dev.

        The response is a strictly validated structured JSON verdict
        ({verdict, flagged_risks, summary}); legacy prose responses and
        responses with missing/invalid fields are rejected with ValueError.
        """
        prompt = f"""You are the Lead Gatekeeper and Security/Quality Reviewer (Role: {self.qa_client.name}).
A Dev subagent has implemented a patch for the following task:
Task: [{task.task_id}] {task.title}
Description: {task.description}

### Git Diff Produced in Sandbox:
```diff
{diff_text[:4000]}
```

Respond ONLY with a valid JSON object (no prose, no markdown fences) matching exactly:
{{
  "verdict": "APPROVED" or "REJECTED",
  "flagged_risks": ["list of zero or more risks"],
  "summary": "one-sentence rationale"
}}
"""
        response = self.qa_client.query(prompt)
        try:
            data = json.loads(response)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON response: {e}")
        if not isinstance(data, dict):
            raise ValueError("Invalid JSON response: expected a JSON object")

        verdict = data.get("verdict")
        if verdict not in ("APPROVED", "REJECTED"):
            raise ValueError("Invalid or missing required field: verdict (must be APPROVED or REJECTED)")

        flagged = data.get("flagged_risks")
        if not isinstance(flagged, list):
            raise ValueError("Invalid or missing required field: flagged_risks (must be an array)")

        summary = data.get("summary")
        if not isinstance(summary, str):
            raise ValueError("Invalid or missing required field: summary (must be a string)")

        return data

    def _review_diff_with_antigravity(self, task: PlannedTask, diff_text: str) -> Dict[str, Any]:
        """Backwards compatibility alias."""
        return self._review_diff_with_qa(task, diff_text)

    def _review_diff_with_prover(
        self, task: "PlannedTask", diff_text: str
    ) -> Dict[str, Any]:
        """v2.3 Phase D1: exploit-focused Security Prover review.

        The prover is a third model from a family different from Dev's
        (enforced in __init__ via get_model_family). It answers ONE
        question with fresh eyes: how could this diff be exploited?
        Returns the same strictly-validated verdict shape as QA review.
        """
        if self.prover_client is None:
            return {"verdict": "APPROVED", "flagged_risks": [], "summary": "no prover configured"}

        prompt = f"""You are the Security Prover (Role: {self.prover_name}).
A Dev subagent produced the patch below. Your ONLY job is adversarial: find how this diff could be EXPLOITED, ABUSED, or how it could CHEAT its own test suite. Think: injection (command/path/SQL), unsafe deserialization, information disclosure, race conditions, and reward hacking (hardcoded outputs keyed to test inputs, weakened assertions, special-case branches that only trigger under the known test data).

Task: [{task.task_id}] {task.title}
Description: {task.description}

### Git Diff to Prove:
```diff
{diff_text[:4000]}
```

Answer these explicitly:
1. How could this code be exploited if an attacker controlled the inputs?
2. Does any branch look like it exists ONLY to satisfy the known tests (hardcoding / lookup table keyed on test inputs)?
3. Are validation/guard clauses real (do they throw) or cosmetic (return defaults)?

Respond ONLY with a valid JSON object (no prose, no markdown fences) matching exactly:
{{
  "verdict": "APPROVED" or "REJECTED",
  "flagged_risks": ["list of zero or more concrete exploit/cheat risks"],
  "summary": "one-sentence rationale"
}}
"""
        response = self.prover_client.query(prompt)
        try:
            data = json.loads(response)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON response from prover: {e}")
        if not isinstance(data, dict):
            raise ValueError("Invalid JSON response from prover: expected a JSON object")

        verdict = data.get("verdict")
        if verdict not in ("APPROVED", "REJECTED"):
            raise ValueError("Invalid or missing required field: verdict (must be APPROVED or REJECTED)")

        flagged = data.get("flagged_risks")
        if not isinstance(flagged, list):
            raise ValueError("Invalid or missing required field: flagged_risks (must be an array)")

        summary = data.get("summary")
        if not isinstance(summary, str):
            raise ValueError("Invalid or missing required field: summary (must be a string)")

        return data

    def _verdicts_reject(self, verdicts: List[Dict[str, Any]]) -> bool:
        """v2.3 Phase D1: verdicts are GATES. True when any reviewer
        (QA, prover) returned REJECTED."""
        return any(v and v.get("verdict") == "REJECTED" for v in verdicts)

    def _run_metamorphic_check(self, task: "PlannedTask", test_cmd: str) -> Optional[Dict[str, Any]]:
        """v2.3 Phase D2: run the MetamorphicGate on the task's visible test
        file in the MAIN repo using the real configured test command.

        Returns a plain dict verdict ({success, failed_variant, ...}) or None
        when the gate cannot run (no test file on disk).
        """
        import shlex as _shlex
        from validation.metamorphic import MetamorphicGate

        test_path = self.target_repo / task.test_file
        if not test_path.exists():
            return None

        cmd = _shlex.split(test_cmd)

        def _runner(repo_path: Path):
            proc = subprocess.run(
                cmd,
                cwd=self.target_repo,
                capture_output=True,
                text=True,
                timeout=120,
            )
            return (proc.returncode, (proc.stdout or "") + (proc.stderr or ""))

        gate = MetamorphicGate(test_runner=_runner)
        res = gate.run_gate(self.target_repo, task.test_file)
        return {
            "success": res.success,
            "skipped": res.skipped,
            "failed_variant": res.failed_variant,
            "skipped_variants": res.skipped_variants,
        }


    def run(self, user_goal: str, max_tasks: int = 3) -> OrchestrationResult:
        """Executes the full autonomous TDD loop."""
        logger.info(f"[COORDINATOR] Starting autonomous loop on '{self.target_repo}' for goal: '{user_goal}'")

        # 1. PHA 0 & 1: Recon, Audit & Task Breakdown via QA Subagent
        planner = AutonomousPlanner(self.target_repo, self.qa_client)
        audit_report = planner.audit_and_plan(user_goal, max_tasks=max_tasks)
        logger.info(f"[COORDINATOR] Audit completed: {audit_report.summary}")

        execution_records: List[TaskExecutionRecord] = []
        overall_success = True

        # 2. PHA 2 & 3: Iterate over tasks with Dev Subagent + Pipeline Sandbox
        context_builder = ContextBuilder()
        self._run_dir = get_git_dir(self.target_repo) / "dsh_runs" / f"run_{int(time.time())}"
        self._manifest_init(self._run_dir)

        for task in audit_report.tasks:
            logger.info(f"[COORDINATOR] Executing task {task.task_id}: {task.title}")

            # Optional: If QA subagent generated test code, verify discovery
            # BEFORE writing to main (F-03): the smoke-check counts discovered
            # tests with the red test absent vs present, so it must run while
            # the red test is still hidden from the runner.
            if task.test_file and task.test_code:
                if not self._verify_red_test_discovered(task):
                    raise RuntimeError(
                        f"Red test discovery smoke-check FAILED for '{task.test_file}': "
                        f"the configured test command does not discover more tests after "
                        f"the red test exists (wrong file naming pattern?). "
                        f"Refusing to continue a TDD phase that would silently skip the red test."
                    )
            test_created = self._write_red_test_to_main(task)
            if test_created:
                logger.info(f"[COORDINATOR] Written Red QA test to '{task.test_file}'")

            # Setup DSH Runtime for target repo
            config = PipelineConfig(workspace_root=self.target_repo)
            if not self.test_mode:
                if task.build_cmd or audit_report.detected_build_cmd:
                    cmd = task.build_cmd or audit_report.detected_build_cmd
                    config.build_command = shlex.split(cmd) if cmd else None
                if task.test_cmd or audit_report.detected_test_cmd:
                    cmd = task.test_cmd or audit_report.detected_test_cmd
                    config.test_command = shlex.split(cmd) if cmd else None

            runtime = DSHRuntime(
                workspace_path=self.target_repo,
                config=config,
                dry_run=self.dry_run,
                test_mode=self.test_mode,
            )

            # Map PlannedTask to TaskDefinition
            risk_val = RiskLevel.MEDIUM
            try:
                risk_val = RiskLevel(task.risk.lower())
            except Exception:
                pass

            # Ensure test file AND holdout file are allowed in untracked paths if created
            _untracked = []
            if test_created and task.test_file:
                _untracked.append(task.test_file)
            if task.holdout_test_file and task.holdout_test_code:
                _untracked.append(task.holdout_test_file)
            allowed_untracked = _untracked or None
            runtime.allowed_untracked_paths = allowed_untracked

            task_def = TaskDefinition(
                task_id=task.task_id,
                title=task.title,
                allowed_files=task.allowed_files if task.allowed_files else [str(f) for f in self.target_repo.glob("*") if f.is_file()][:2],
                target_symbols=task.target_symbols,
                max_lines_added=task.max_lines_added,
                max_lines_deleted=task.max_lines_deleted,
                risk=risk_val,
            )

            # Execute via Recovery Loop (Dev Subagent)
            provider = self.dev_provider
            # F-02: wire red-test propagation into every transaction worktree
            # via the runtime's on_worktree_created callback.
            runtime.on_worktree_created = self._red_test_worktree_callback(task)
            # v2.3 Phase A: load this task's HIDDEN holdout test into the
            # runtime so the holdout_injector slot writes it into the
            # worktree immediately before T3 Regression. The holdout never
            # enters Dev context (context/builder filters it) and is never
            # merged into Dev code (worktree is discarded after validation).
            runtime._pending_holdouts = [
                {
                    "holdout_test_file": task.holdout_test_file,
                    "holdout_test_code": task.holdout_test_code,
                }
            ] if (task.holdout_test_file and task.holdout_test_code) else []
            if self._run_dir is not None:
                manifest_allowance = self._untracked_allowance_for_manifest()
                if manifest_allowance:
                    allowance = set(runtime.allowed_untracked_paths or [])
                    allowance.update(manifest_allowance)
                    runtime.allowed_untracked_paths = sorted(allowance)
            tx_res: TransactionResult = runtime.execute_with_recovery(
                task=task_def,
                provider=provider,
                context_builder=context_builder,
            )

            diff_content = ""
            review_verdict: Optional[Dict[str, Any]] = None

            if tx_res.success:
                # Capture diff for QA review: prefer the diff captured from
                # the worktree BEFORE it was discarded (dry-run), else git
                # diff of the commit; only fall back to a placeholder when
                # neither is available — reviewers see real changes.
                if getattr(tx_res, "worktree_diff", None):
                    diff_content = tx_res.worktree_diff
                elif tx_res.commit_hash:
                    try:
                        diff_proc = subprocess.run(
                            ["git", "diff", f"{tx_res.commit_hash}^!", "--"],
                            cwd=self.target_repo,
                            capture_output=True,
                            text=True,
                        )
                        diff_content = diff_proc.stdout
                    except Exception:
                        diff_content = f"Committed at: {tx_res.commit_hash}"
                elif tx_res.dry_run:
                    diff_content = "[DRY_RUN: Verified in sandbox worktree — no diff captured]"

                # 3. PHA 4: Review by QA Subagent (+ Security Prover, v2.3 D1)
                review_verdict = self._review_diff_with_qa(task, diff_content)
                logger.info(f"[COORDINATOR] QA review verdict: {json.dumps(review_verdict, ensure_ascii=False)[:200]}")

                prover_verdict: Optional[Dict[str, Any]] = None
                if self.prover_client is not None:
                    prover_verdict = self._review_diff_with_prover(task, diff_content)
                    logger.info(f"[COORDINATOR] Security Prover verdict: {json.dumps(prover_verdict, ensure_ascii=False)[:200]}")

                # v2.3 Phase D2: Metamorphic Check on the task's visible test
                # file — semantic-preserving variants (reseed/rename/reorder)
                # must behave identically. Only meaningful when a test command
                # is configured (production); test-mode mocks have no runner.
                if not self.test_mode and task.test_file:
                    test_cmd = task.test_cmd or audit_report.detected_test_cmd
                    if test_cmd:
                        try:
                            metamorphic_verdict = self._run_metamorphic_check(task, test_cmd)
                            if metamorphic_verdict is not None and not metamorphic_verdict["success"]:
                                overall_success = False
                                review_verdict = dict(review_verdict)
                                review_verdict["metamorphic"] = metamorphic_verdict
                                tx_res = tx_res.model_copy(update={
                                    "success": False,
                                    "failure_type": "MORPHIC_FAILURE",
                                    "error_message": (
                                        f"Metamorphic check failed: variant "
                                        f"'{metamorphic_verdict['failed_variant']}' failed while the "
                                        f"original suite passed (hardcoded/flaky/order-dependent)"
                                    ),
                                }) if hasattr(tx_res, "model_copy") else tx_res
                                logger.error(
                                    f"[COORDINATOR] Task {task.task_id} FAILED metamorphic check "
                                    f"(variant '{metamorphic_verdict['failed_variant']}') — rejecting."
                                )
                        except Exception as morph_err:
                            # Best-effort gate wiring: an infrastructure error in
                            # the metamorphic runner is logged, never silently
                            # ignored, but does not corrupt the pipeline state.
                            logger.warning(
                                f"[COORDINATOR] Metamorphic check could not run for {task.task_id}: {morph_err}"
                            )

                # v2.3 Phase D1: verdicts are enforced gates, not decorations.
                # A REJECTED from QA or the prover fails the task even though
                # the pipeline itself passed (Contract Failure check).
                if self._verdicts_reject([review_verdict, prover_verdict]):
                    overall_success = False
                    review_verdict = dict(review_verdict)
                    review_verdict["enforced"] = "TASK_FAILED_BY_REVIEW"
                    tx_res = tx_res.model_copy(update={
                        "success": False,
                        "failure_type": "CONTRACT_FAILURE",
                        "error_message": "Review verdict REJECTED by QA/Security-Prover gate",
                    }) if hasattr(tx_res, "model_copy") else tx_res
                    logger.error(
                        f"[COORDINATOR] Task {task.task_id} FAILED by review gate "
                        f"(QA/prover REJECTED) — refusing to count it as success."
                    )

            else:
                overall_success = False
                logger.error(f"[COORDINATOR] Task {task.task_id} failed: {tx_res.error_message}")

            self._manifest_update(
                self._run_dir,
                task_id=task.task_id,
                attempt=1,
                status="PASSED" if tx_res.success else "FAILED",
                failure_reason=None if tx_res.success else (tx_res.error_message or "unknown"),
                worktree_path=tx_res.worktree_path,
            )

            execution_records.append(
                TaskExecutionRecord(
                    task_id=task.task_id,
                    title=task.title,
                    success=tx_res.success,
                    commit_hash=tx_res.commit_hash,
                    diff_summary=diff_content[:200] if diff_content else None,
                    review_verdict=review_verdict,
                    error_message=tx_res.error_message if not tx_res.success else None,
                )
            )

        # 4. Generate Final Human-Readable Summary
        lines = [
            f"# AUTONOMOUS ORCHESTRATION REPORT: {user_goal}",
            f"**Target Repository:** `{self.target_repo}`",
            f"**QA Subagent:** `{self.qa_client.name}` | **Dev Provider:** `{type(self.dev_provider).__name__}`",
            f"**Framework:** {audit_report.detected_framework}",
            f"**Dry-run Mode:** {self.dry_run}",
            f"**Audit Summary:** {audit_report.summary}",
            "",
            "## TDD Task Execution Outcomes:",
        ]
        for rec in execution_records:
            status_icon = "✅ SUCCESS" if rec.success else "❌ FAILED"
            lines.append(f"### [{rec.task_id}] {rec.title} — {status_icon}")
            if rec.commit_hash:
                lines.append(f"- **Commit:** `{rec.commit_hash}`")
            if rec.error_message:
                lines.append(f"- **Error:** `{rec.error_message}`")
            if rec.review_verdict:
                verdict_str = json.dumps(rec.review_verdict, ensure_ascii=False, indent=2)
                lines.append(f"- **QA Review:**\n> {verdict_str.replace(chr(10), chr(10)+'> ')}")
            lines.append("")

        final_report = "\n".join(lines)


        return OrchestrationResult(
            success=overall_success,
            goal=user_goal,
            target_repo=str(self.target_repo),
            audit_summary=audit_report.summary,
            tasks=execution_records,
            final_report=final_report,
        )
