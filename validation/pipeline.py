from enum import Enum
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from pydantic import BaseModel, Field

from task.schema import TaskDefinition, PatchProposal
from safety.scope_guard import ScopeGuard
from build.sandbox import BaseBuildRunner, BuildResult
from validation.structural import StructuralValidator
from validation.behavioral import BaseBehavioralValidator
from validation.regression import BaseRegressionValidator
from validation.risk import RiskValidator
from validation.baseline import BaselineState, BaselineManager
from recovery.classifier import FailureClassifier, FailureType


class MutationGateResult(BaseModel):
    """Outcome of the T2.5 Mutation Gate slot (v2.3 Stage 1 standard hook).

    The actual gate implementation lands in Stage 2 Phase A
    (validation/mutation.py); Stage 1 only standardizes the slot contract.
    """
    success: bool
    mutation_score: float = 0.0
    threshold: float = 0.70
    killed: int = 0
    total: int = 0
    surviving_mutants: List[str] = Field(default_factory=list)
    details: Dict[str, Any] = Field(default_factory=dict)


class HoldoutInjectionResult(BaseModel):
    """Outcome of the holdout test injection slot (v2.3 Stage 1 standard hook).

    Holdout tests are written into the worktree immediately BEFORE T3
    Regression runs (never merged into Dev code). Stage 1 only standardizes
    the slot; the planner-side holdout generation lands in Stage 2 Phase A.
    """
    injected_files: List[str] = Field(default_factory=list)
    details: Dict[str, Any] = Field(default_factory=dict)


class ValidationTier(str, Enum):
    T0_STRUCTURAL = "T0_STRUCTURAL"
    T1_BUILD = "T1_BUILD"
    T2_BEHAVIORAL = "T2_BEHAVIORAL"
    T2_5_MUTATION = "T2_5_MUTATION"
    T3_REGRESSION = "T3_REGRESSION"
    T4_RISK = "T4_RISK"


class ValidationReport(BaseModel):
    success: bool
    failed_tier: Optional[ValidationTier] = None
    failure_type: Optional[FailureType] = None
    error_message: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class ValidationPipeline:
    def __init__(
        self,
        behavioral_validator: Optional[BaseBehavioralValidator] = None,
        regression_validator: Optional[BaseRegressionValidator] = None,
        mutation_gate: Optional[Callable] = None,
        holdout_injector: Optional[Callable] = None,
    ):
        self.behavioral_validator = behavioral_validator
        self.regression_validator = regression_validator
        # v2.3 Stage 1 standard slots (None ⇒ legacy behavior, no gate runs):
        # - mutation_gate: callable(repo_path, task) -> MutationGateResult,
        #   executed between T2 and T3 (T2.5 Mutation Gate).
        # - holdout_injector: callable(repo_path, task) -> HoldoutInjectionResult,
        #   executed immediately before T3 Regression only.
        self.mutation_gate = mutation_gate
        self.holdout_injector = holdout_injector

    def validate_pre_apply(
        self,
        task: TaskDefinition,
        proposal: PatchProposal,
        repo_path: Path,
        scope_guard: ScopeGuard,
        reservation_id: Optional[str] = None,
    ) -> ValidationReport:
        """Runs T0 Structural and T4 Risk BEFORE modifying files."""
        # 1. T0 - Structural
        try:
            StructuralValidator.validate(task, proposal, repo_path, scope_guard, reservation_id=reservation_id)
        except Exception as e:

            f_type = FailureClassifier.classify_exception(e)
            return ValidationReport(
                success=False,
                failed_tier=ValidationTier.T0_STRUCTURAL,
                failure_type=f_type,
                error_message=f"[T0 Structural Error] {str(e)}",
            )

        # 2. T4 - Risk
        risk_res = RiskValidator.validate_risk(task, proposal, repo_path, policy=scope_guard.policy)
        if not risk_res.success:
            return ValidationReport(
                success=False,
                failed_tier=ValidationTier.T4_RISK,
                failure_type=FailureType.SCOPE_VIOLATION,
                error_message=f"[T4 Risk Violation] " + "; ".join(risk_res.violations),
                details={"violations": risk_res.violations},
            )

        return ValidationReport(success=True)

    def validate_post_apply(
        self,
        task: TaskDefinition,
        repo_path: Path,
        build_runner: BaseBuildRunner,
        baseline: Optional[BaselineState] = None,
    ) -> ValidationReport:
        """Runs T1 Build, T2 Behavioral, T3 Regression AFTER applying patch, comparing against baseline."""
        # 3. T1 - Build
        post_build: BuildResult = build_runner.build(repo_path)
        if baseline is not None:
            is_regression, has_error, msg = BaselineManager.compare_build(baseline.build_result, post_build)
            if is_regression:
                f_type = FailureClassifier.classify_build_failure(post_build)
                return ValidationReport(
                    success=False,
                    failed_tier=ValidationTier.T1_BUILD,
                    failure_type=f_type,
                    error_message=f"[T1 Build Failure] {msg}",
                    details={"is_regression": True, "baseline_passed": baseline.build_result.success},
                )
            elif has_error:
                f_type = FailureClassifier.classify_build_failure(post_build)
                return ValidationReport(
                    success=False,
                    failed_tier=ValidationTier.T1_BUILD,
                    failure_type=f_type,
                    error_message=f"[T1 Build Failure] {msg}",
                    details={"is_regression": False, "baseline_passed": baseline.build_result.success},
                )
        else:
            if not post_build.success:
                f_type = FailureClassifier.classify_build_failure(post_build)
                err = "\n".join([e.message for e in post_build.errors]) or post_build.raw_output
                return ValidationReport(
                    success=False,
                    failed_tier=ValidationTier.T1_BUILD,
                    failure_type=f_type,
                    error_message=f"[T1 Build Failure] {err}",
                )

        # 4. T2 - Behavioral (if validator configured)
        if self.behavioral_validator is not None:
            beh_res = self.behavioral_validator.validate_behavior(repo_path, task.task_id)
            if baseline is not None and baseline.behavioral_result is not None:
                # Compare against baseline: only NEW behavioral failures (i.e.
                # regressions introduced by the patch) fail T2. Pre-existing
                # failures already present in the baseline are tolerated.
                is_regression, new_failures = BaselineManager.compare_behavioral(
                    baseline.behavioral_result, beh_res
                )
                if is_regression:
                    return ValidationReport(
                        success=False,
                        failed_tier=ValidationTier.T2_BEHAVIORAL,
                        failure_type=FailureType.BEHAVIORAL,
                        error_message=f"[T2 Behavioral Failure] " + "; ".join(new_failures),
                        details={"failures": new_failures},
                    )
            elif not beh_res.success:
                # No baseline behavioral result captured: keep legacy behavior
                # (any post-patch behavioral failure fails T2).
                return ValidationReport(
                    success=False,
                    failed_tier=ValidationTier.T2_BEHAVIORAL,
                    failure_type=FailureType.BEHAVIORAL,
                    error_message=f"[T2 Behavioral Failure] " + "; ".join(beh_res.failures),
                    details={"failures": beh_res.failures},
                )

        # 5. T2.5 - Mutation Gate (v2.3; runs only when configured)
        if self.mutation_gate is not None:
            mut_res: MutationGateResult = self.mutation_gate(repo_path, task)
            if not mut_res.success:
                return ValidationReport(
                    success=False,
                    failed_tier=ValidationTier.T2_5_MUTATION,
                    failure_type=FailureType.MUTATION_COVERAGE,
                    error_message=(
                        f"[T2.5 Mutation Gate Failure] Mutation score "
                        f"{mut_res.mutation_score:.3f} below threshold {mut_res.threshold:.2f} "
                        f"({mut_res.killed}/{mut_res.total} mutants killed)"
                    ),
                    details={
                        "mutation_score": mut_res.mutation_score,
                        "threshold": mut_res.threshold,
                        "killed": mut_res.killed,
                        "total": mut_res.total,
                        "surviving_mutants": mut_res.surviving_mutants,
                    },
                )

        # 5.5 Holdout test injection (v2.3): write holdout tests into the
        # worktree IMMEDIATELY BEFORE T3 Regression so the baseline cannot
        # mask defects. Holdout tests are never merged into Dev code; they
        # only adjudicate T3. Injection is best-effort (a failed injection
        # is logged, not fatal) — holdout tests must not silently weaken a
        # gate they merely supplement.
        if self.holdout_injector is not None:
            holdout_res: HoldoutInjectionResult = self.holdout_injector(repo_path, task)
            if holdout_res.details:
                logging.getLogger("dsh.validation").info(
                    "Holdout injection complete: %s", holdout_res.details
                )

        # 6. T3 - Regression (if validator configured)
        if self.regression_validator is not None:
            post_reg = self.regression_validator.validate_regression(repo_path)
            if baseline is not None and baseline.regression_result is not None:
                is_regression, new_broken = BaselineManager.compare_regression(baseline.regression_result, post_reg)
                if is_regression:
                    return ValidationReport(
                        success=False,
                        failed_tier=ValidationTier.T3_REGRESSION,
                        failure_type=FailureType.REGRESSION,
                        error_message=f"[T3 Regression Failure] " + "; ".join(new_broken),
                        details={
                            "broken_tests": new_broken,
                            "baseline_broken": baseline.regression_result.broken_tests,
                            "post_broken": post_reg.broken_tests,
                        },
                    )
            else:
                if not post_reg.success:
                    return ValidationReport(
                        success=False,
                        failed_tier=ValidationTier.T3_REGRESSION,
                        failure_type=FailureType.REGRESSION,
                        error_message=f"[T3 Regression Failure] " + "; ".join(post_reg.broken_tests),
                        details={"broken_tests": post_reg.broken_tests},
                    )

        return ValidationReport(success=True)

