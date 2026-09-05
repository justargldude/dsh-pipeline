import json
import logging
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from core.config import PipelineConfig
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
    review_verdict: Optional[str] = None
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
        if getattr(self, "_run_dir", None) is None:
            return None
        try:
            return [str(self._run_dir.relative_to(self.target_repo))]
        except ValueError:
            return None

    def _manifest_init(self, run_dir: Path) -> Path:
        """Creates the run directory and an empty manifest.json."""
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
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

    def _write_red_test_to_main(self, task: PlannedTask) -> bool:
        """Writes the QA red test into the main repo (unchanged legacy path)."""
        if not (task.test_file and task.test_code):
            return False
        test_path = self.target_repo / task.test_file
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(task.test_code, encoding="utf-8")
        return True

    def _ensure_red_test_in_worktree(self, task: PlannedTask, worktree_path: Path) -> bool:
        """Copies the QA red test into a transaction worktree.

        Worktrees are created from committed HEAD, so a red test written to the
        main repo as an untracked file never reaches the Dev worktree and its
        validation runs an incomplete test set (false green).
        """
        if not (task.test_file and task.test_code):
            return False
        src = self.target_repo / task.test_file
        if not src.exists():
            return False
        dst = Path(worktree_path) / task.test_file
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        return True

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
        self, task: PlannedTask, diff_text: str
    ) -> str:
        """Prompts the QA subagent to review the final diff produced by Dev."""
        prompt = f"""You are the Lead Gatekeeper and Security/Quality Reviewer (Role: {self.qa_client.name}).
A Dev subagent has implemented a patch for the following task:
Task: [{task.task_id}] {task.title}
Description: {task.description}

### Git Diff Produced in Sandbox:
```diff
{diff_text[:4000]}
```

Provide a concise 3-5 line code review evaluating:
1. Correctness: Does the diff address the task without unintended side effects?
2. Code Cleanliness & Security: Are there anti-patterns, stubs, or security leaks?
3. Final Verdict: APPROVED or REJECTED with a one-sentence reason.
"""
        try:
            return self.qa_client.query(prompt)
        except Exception as e:
            return f"Review skipped due to client error: {e}"

    def _review_diff_with_antigravity(self, task: PlannedTask, diff_text: str) -> str:
        """Backwards compatibility alias."""
        return self._review_diff_with_qa(task, diff_text)


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
        self._run_dir = self.target_repo / f"run_{int(time.time())}"
        self._manifest_init(self._run_dir)

        for task in audit_report.tasks:
            logger.info(f"[COORDINATOR] Executing task {task.task_id}: {task.title}")

            # Optional: If QA subagent generated test code, inject it into the target repo
            test_created = self._write_red_test_to_main(task)
            if test_created:
                logger.info(f"[COORDINATOR] Written Red QA test to '{task.test_file}'")
                if not self._verify_red_test_discovered(task):
                    raise RuntimeError(
                        f"Red test discovery smoke-check FAILED for '{task.test_file}': "
                        f"the configured test command does not discover more tests after "
                        f"the red test exists (wrong file naming pattern?). "
                        f"Refusing to continue a TDD phase that would silently skip the red test."
                    )

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

            # Ensure test file is allowed in untracked paths if created
            allowed_untracked = [task.test_file] if (test_created and task.test_file) else None
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
            if self._run_dir is not None:
                allowance = set(runtime.allowed_untracked_paths or [])
                allowance.add(str(self._run_dir.relative_to(self.target_repo)))
                runtime.allowed_untracked_paths = sorted(allowance)
            tx_res: TransactionResult = runtime.execute_with_recovery(
                task=task_def,
                provider=provider,
                context_builder=context_builder,
            )

            diff_content = ""
            review_verdict = ""

            if tx_res.success:
                # Capture diff for QA review
                if tx_res.commit_hash:
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
                    diff_content = "[DRY_RUN: Verified in sandbox worktree]"

                # 3. PHA 4: Review by QA Subagent
                review_verdict = self._review_diff_with_qa(task, diff_content)
                logger.info(f"[COORDINATOR] QA review verdict: {review_verdict[:100]}")

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
                lines.append(f"- **QA Review:**\n> {rec.review_verdict.strip().replace(chr(10), chr(10)+'> ')}")
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
