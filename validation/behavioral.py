import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field


class BehavioralCheckResult(BaseModel):
    success: bool
    failures: List[str] = Field(default_factory=list)
    output: str = ""


class BaseBehavioralValidator(ABC):
    """T2 - Behavioral Validation (Smoke test, runtime hook execution, state assertions)."""
    @abstractmethod
    def validate_behavior(self, repo_path: Path, task_id: str) -> BehavioralCheckResult:
        pass


class MockBehavioralValidator(BaseBehavioralValidator):
    def __init__(self, should_succeed: bool = True, failures: Optional[List[str]] = None):
        self.should_succeed = should_succeed
        self.failures = failures or []

    def validate_behavior(self, repo_path: Path, task_id: str) -> BehavioralCheckResult:
        if self.should_succeed:
            return BehavioralCheckResult(success=True, output="Behavioral smoke tests passed.")
        return BehavioralCheckResult(
            success=False,
            failures=self.failures or [f"Behavioral test for {task_id} failed: Expected hook state not met."],
            output="Behavioral test failure.",
        )


from build.sandbox import run_hardened_command


class SubprocessBehavioralValidator(BaseBehavioralValidator):
    def __init__(self, cmd: List[str], timeout_seconds: Optional[int] = 30):
        self.cmd = cmd
        self.timeout_seconds = timeout_seconds

    def validate_behavior(self, repo_path: Path, task_id: str) -> BehavioralCheckResult:
        if not self.cmd:
            return BehavioralCheckResult(success=False, failures=["No behavioral validation command configured."], output="")
        
        # Format task_id placeholder if present
        resolved_cmd = [arg.replace("{task_id}", task_id) for arg in self.cmd]
        returncode, raw, timed_out, sig_num, truncated = run_hardened_command(
            cmd=resolved_cmd,
            cwd=repo_path,
            timeout_seconds=self.timeout_seconds,
        )

        if timed_out:
            msg = f"Behavioral test command timed out after {self.timeout_seconds}s"
            return BehavioralCheckResult(success=False, failures=[msg], output=msg)

        if returncode == 0:
            return BehavioralCheckResult(success=True, output=raw)
        else:
            failures = [line.strip() for line in raw.splitlines() if "fail" in line.lower() or "error" in line.lower()]
            if not failures:
                failures = [f"Behavioral test command exited with code {returncode}"]
            return BehavioralCheckResult(success=False, failures=failures, output=raw)


