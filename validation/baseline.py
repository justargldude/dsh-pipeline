import hashlib
import platform
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field

from build.sandbox import BaseBuildRunner, BuildResult
from validation.behavioral import BaseBehavioralValidator, BehavioralCheckResult
from validation.regression import BaseRegressionValidator, RegressionCheckResult
from core.workspace import WorkspaceManager


class BaselineState(BaseModel):
    base_commit: str
    build_result: BuildResult
    regression_result: Optional[RegressionCheckResult] = None
    behavioral_result: Optional[BehavioralCheckResult] = None
    env_fingerprint: str
    config_fingerprint: str
    cache_key: str
    captured_at: float = Field(default_factory=time.time)


class BaselineManager:
    """Manages pre-patch baseline capturing, environment fingerprinting, and transaction-scoped caching."""

    def __init__(self, enable_cache: bool = True):
        self.enable_cache = enable_cache
        self._cache: Dict[str, BaselineState] = {}

    @classmethod
    def get_env_fingerprint(cls) -> str:
        """Captures hardware, OS, and runtime environment identity."""
        components = [
            f"os={platform.system()}",
            f"release={platform.release()}",
            f"machine={platform.machine()}",
            f"python_version={sys.version.split()[0]}",
            f"python_exec={sys.executable}",
        ]
        return ";".join(components)

    @classmethod
    def compute_config_fingerprint(
        cls,
        build_runner: Optional[BaseBuildRunner],
        regression_validator: Optional[BaseRegressionValidator],
        behavioral_validator: Optional[BaseBehavioralValidator],
    ) -> str:
        """Computes a deterministic fingerprint of the execution runner configurations."""
        build_repr = repr(getattr(build_runner, "build_cmd", type(build_runner).__name__ if build_runner else "None"))
        test_repr = repr(getattr(regression_validator, "test_cmd", type(regression_validator).__name__ if regression_validator else "None"))
        beh_repr = repr(getattr(behavioral_validator, "cmd", type(behavioral_validator).__name__ if behavioral_validator else "None"))
        return f"build:{build_repr}|test:{test_repr}|beh:{beh_repr}"

    def compute_cache_key(
        self,
        repo_head: str,
        config_fingerprint: str,
        env_fingerprint: str,
    ) -> str:
        raw_key = f"{repo_head}::{config_fingerprint}::{env_fingerprint}"
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    def get_or_capture_baseline(
        self,
        repo_path: Path,
        ws: WorkspaceManager,
        build_runner: BaseBuildRunner,
        regression_validator: Optional[BaseRegressionValidator] = None,
        behavioral_validator: Optional[BaseBehavioralValidator] = None,
        base_commit: Optional[str] = None,
    ) -> BaselineState:
        """Captures or retrieves cached baseline BEFORE candidate patch is applied."""
        repo_head = base_commit if base_commit is not None else ws.get_head_commit()
        env_fp = self.get_env_fingerprint()
        config_fp = self.compute_config_fingerprint(build_runner, regression_validator, behavioral_validator)
        cache_key = self.compute_cache_key(repo_head, config_fp, env_fp)

        if self.enable_cache and cache_key in self._cache:
            return self._cache[cache_key]

        # 1. Capture baseline build
        build_res = build_runner.build(repo_path)

        # 2. Capture baseline regression tests (if validator configured)
        reg_res = None
        if regression_validator is not None:
            reg_res = regression_validator.validate_regression(repo_path)

        # 3. Capture baseline behavioral tests (if validator configured)
        beh_res = None
        if behavioral_validator is not None:
            beh_res = behavioral_validator.validate_behavior(repo_path, "BASELINE")

        baseline = BaselineState(
            base_commit=repo_head,
            build_result=build_res,
            regression_result=reg_res,
            behavioral_result=beh_res,
            env_fingerprint=env_fp,
            config_fingerprint=config_fp,
            cache_key=cache_key,
        )

        if self.enable_cache:
            self._cache[cache_key] = baseline

        return baseline

    def clear_cache(self):
        self._cache.clear()

    def invalidate(self, cache_key: str):
        self._cache.pop(cache_key, None)

    @staticmethod
    def compare_build(
        baseline_build: BuildResult,
        post_build: BuildResult,
    ) -> Tuple[bool, bool, str]:
        """Compares post-patch build result against baseline build result.
        
        Returns:
            (is_regression, is_failure, message)
        """
        # Case B: Baseline passed, post-patch failed -> REGRESSION
        if baseline_build.success and not post_build.success:
            err = "\n".join([e.message for e in post_build.errors]) or post_build.raw_output
            return True, True, f"Build regression detected (baseline passed, post-patch failed): {err}"

        # Case A: Baseline failed, post-patch failed -> PRE-EXISTING FAILURE (not a regression)
        if not baseline_build.success and not post_build.success:
            err = "\n".join([e.message for e in post_build.errors]) or post_build.raw_output
            return False, True, f"Pre-existing build failure (baseline already failed): {err}"

        # Case: Baseline failed, post-patch passed -> FIXED
        if not baseline_build.success and post_build.success:
            return False, False, "Patch successfully resolved pre-existing build failure."

        # Case: Both passed
        return False, False, "Build passed (clean baseline and post-patch)."

    @staticmethod
    def compare_regression(
        baseline_regression: Optional[RegressionCheckResult],
        post_regression: RegressionCheckResult,
    ) -> Tuple[bool, List[str]]:
        """Compares post-patch test failures against baseline broken tests.
        
        Returns:
            (is_regression, new_broken_tests)
        """
        baseline_broken = set(baseline_regression.broken_tests) if baseline_regression else set()
        post_broken = set(post_regression.broken_tests)
        new_broken = list(post_broken - baseline_broken)

        is_regression = len(new_broken) > 0
        return is_regression, new_broken
