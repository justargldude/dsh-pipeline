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
