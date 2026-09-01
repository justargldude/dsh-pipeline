import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field


class BuildErrorDetail(BaseModel):
    file: Optional[str] = None
    line: Optional[int] = None
    column: Optional[int] = None
    code: Optional[str] = None
    message: str


class BuildResult(BaseModel):
    success: bool
    exit_code: int = 0
    errors: List[BuildErrorDetail] = Field(default_factory=list)
    raw_output: str = ""


class BaseBuildRunner(ABC):
    @abstractmethod
    def build(self, repo_path: Path) -> BuildResult:
        pass


class MockBuildRunner(BaseBuildRunner):
    def __init__(self, should_succeed: bool = True, errors: Optional[List[BuildErrorDetail]] = None):
        self.should_succeed = should_succeed
        self.errors = errors or []

    def build(self, repo_path: Path) -> BuildResult:
        if self.should_succeed:
            return BuildResult(success=True, exit_code=0, raw_output="Build Succeeded (Mock).")
        return BuildResult(
            success=False,
            exit_code=1,
            errors=self.errors or [BuildErrorDetail(message="Mock compilation error CS1002: ; expected")],
            raw_output="Build Failed (Mock).",
        )


class SubprocessBuildRunner(BaseBuildRunner):
    def __init__(self, build_cmd: List[str]):
        self.build_cmd = build_cmd

    def build(self, repo_path: Path) -> BuildResult:
        try:
            res = subprocess.run(
                self.build_cmd,
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=False,
            )
            raw = res.stdout + "\n" + res.stderr
            if res.returncode == 0:
                return BuildResult(success=True, exit_code=0, raw_output=raw)
            else:
                errors = [BuildErrorDetail(message=line) for line in raw.splitlines() if "error" in line.lower()]
                return BuildResult(success=False, exit_code=res.returncode, errors=errors, raw_output=raw)
        except Exception as e:
            return BuildResult(
                success=False,
                exit_code=-1,
                errors=[BuildErrorDetail(message=str(e))],
                raw_output=str(e),
            )
