import re
import subprocess
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


from build.sandbox import run_hardened_command


class SubprocessRegressionValidator(BaseRegressionValidator):
    def __init__(self, test_cmd: List[str], timeout_seconds: Optional[int] = 30):
        self.test_cmd = test_cmd
        self.timeout_seconds = timeout_seconds

    def validate_regression(self, repo_path: Path) -> RegressionCheckResult:
        if not self.test_cmd:
            return RegressionCheckResult(success=False, broken_tests=["No test command configured."], output="")

        returncode, raw, timed_out, sig_num, truncated = run_hardened_command(
            cmd=self.test_cmd,
            cwd=repo_path,
            timeout_seconds=self.timeout_seconds,
        )

        if timed_out:
            msg = f"Test suite timed out after {self.timeout_seconds}s"
            return RegressionCheckResult(success=False, broken_tests=[msg], output=msg)

        if returncode == 0:
            return RegressionCheckResult(success=True, broken_tests=[], output=raw)

        broken = self._parse_broken_tests(raw)
        if not broken:
            broken = [f"Test runner exited with code {returncode}"]
        return RegressionCheckResult(success=False, broken_tests=broken, output=raw)


    def _parse_broken_tests(self, output: str) -> List[str]:
        broken = []
        for line in output.splitlines():
            line_str = line.strip()
            # Pytest format: FAILED tests/test_foo.py::test_bar - Error
            if line_str.startswith("FAILED "):
                broken.append(line_str)
            # Unittest format: FAIL: test_bar (test_foo.TestCase)
            elif line_str.startswith("FAIL:") or line_str.startswith("ERROR:"):
                broken.append(line_str)
            # Dotnet / xUnit / NUnit: Failed TestMethod [12 ms]
            elif "Failed " in line_str and ("[" in line_str or "::" in line_str or "(" in line_str):
                broken.append(line_str)
        return broken

