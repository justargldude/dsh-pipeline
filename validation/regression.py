from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field


class RegressionCheckResult(BaseModel):
    success: bool
    broken_tests: List[str] = Field(default_factory=list)
    output: str = ""


class BaseRegressionValidator(ABC):
    """T3 - Regression Validation (Existing test suite pass, backward compatibility)."""
    @abstractmethod
    def validate_regression(self, repo_path: Path) -> RegressionCheckResult:
        pass


class MockRegressionValidator(BaseRegressionValidator):
    def __init__(self, should_succeed: bool = True, broken_tests: Optional[List[str]] = None):
        self.should_succeed = should_succeed
        self.broken_tests = broken_tests or []

    def validate_regression(self, repo_path: Path) -> RegressionCheckResult:
        if self.should_succeed:
            return RegressionCheckResult(success=True, output="Regression suite passed (0 broken tests).")
        return RegressionCheckResult(
            success=False,
            broken_tests=self.broken_tests or ["PlayerMovementTest.TestVelocityRegression: Assertion failed"],
            output="Regression failure detected.",
        )
