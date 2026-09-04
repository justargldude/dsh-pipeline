import json
import logging
import shlex
import subprocess
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

        for task in audit_report.tasks:
            logger.info(f"[COORDINATOR] Executing task {task.task_id}: {task.title}")

            # Optional: If QA subagent generated test code, inject it into the target repo
            test_created = False
            if task.test_file and task.test_code:
                test_path = self.target_repo / task.test_file
                test_path.parent.mkdir(parents=True, exist_ok=True)
                test_path.write_text(task.test_code, encoding="utf-8")
                test_created = True
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
            f"# BÁO CÁO ĐIỀU PHỐI TỰ ĐỘNG: {user_goal}",
            f"**Target Repository:** `{self.target_repo}`",
            f"**Framework:** {audit_report.detected_framework}",
            f"**Chế độ (Dry-run):** {self.dry_run}",
            f"**Tổng kết Khảo sát:** {audit_report.summary}",
            "",
            "## Kết quả từng đầu việc (TDD Tasks):",
        ]
        for rec in execution_records:
            status_icon = "✅ THÀNH CÔNG" if rec.success else "❌ THẤT BẠI"
            lines.append(f"### [{rec.task_id}] {rec.title} — {status_icon}")
            if rec.commit_hash:
                lines.append(f"- **Commit:** `{rec.commit_hash}`")
            if rec.error_message:
                lines.append(f"- **Lỗi:** `{rec.error_message}`")
            if rec.review_verdict:
                lines.append(f"- **Antigravity Review:**\n> {rec.review_verdict.strip().replace(chr(10), chr(10)+'> ')}")
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
