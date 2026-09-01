from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field

from task.schema import TaskDefinition, PatchProposal
from safety.scope_guard import ScopeGuard
from build.sandbox import BaseBuildRunner, BuildResult
from validation.structural import StructuralValidator
from validation.behavioral import BaseBehavioralValidator, MockBehavioralValidator
from validation.regression import BaseRegressionValidator, MockRegressionValidator
from validation.risk import RiskValidator
from recovery.classifier import FailureClassifier, FailureType


class ValidationTier(str, Enum):
    T0_STRUCTURAL = "T0_STRUCTURAL"
    T1_BUILD = "T1_BUILD"
    T2_BEHAVIORAL = "T2_BEHAVIORAL"
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
    ):
        self.behavioral_validator = behavioral_validator or MockBehavioralValidator(should_succeed=True)
        self.regression_validator = regression_validator or MockRegressionValidator(should_succeed=True)

    def validate_pre_apply(
        self,
        task: TaskDefinition,
        proposal: PatchProposal,
        repo_path: Path,
        scope_guard: ScopeGuard,
    ) -> ValidationReport:
        """Runs T0 Structural and T4 Risk BEFORE modifying files."""
        # 1. T0 - Structural
        try:
            StructuralValidator.validate(task, proposal, repo_path, scope_guard)
        except Exception as e:
            f_type = FailureClassifier.classify_exception(e)
            return ValidationReport(
                success=False,
                failed_tier=ValidationTier.T0_STRUCTURAL,
                failure_type=f_type,
                error_message=f"[T0 Structural Error] {str(e)}",
            )

        # 2. T4 - Risk
        risk_res = RiskValidator.validate_risk(task, proposal, repo_path)
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
    ) -> ValidationReport:
        """Runs T1 Build, T2 Behavioral, T3 Regression AFTER applying patch."""
        # 3. T1 - Build
        build_res: BuildResult = build_runner.build(repo_path)
        if not build_res.success:
            f_type = FailureClassifier.classify_build_failure(build_res)
            err = "\n".join([e.message for e in build_res.errors]) or build_res.raw_output
            return ValidationReport(
                success=False,
                failed_tier=ValidationTier.T1_BUILD,
                failure_type=f_type,
                error_message=f"[T1 Build Failure] {err}",
            )

        # 4. T2 - Behavioral
        beh_res = self.behavioral_validator.validate_behavior(repo_path, task.task_id)
        if not beh_res.success:
            return ValidationReport(
                success=False,
                failed_tier=ValidationTier.T2_BEHAVIORAL,
                failure_type=FailureType.BEHAVIORAL,
                error_message=f"[T2 Behavioral Failure] " + "; ".join(beh_res.failures),
                details={"failures": beh_res.failures},
            )

        # 5. T3 - Regression
        reg_res = self.regression_validator.validate_regression(repo_path)
        if not reg_res.success:
            return ValidationReport(
                success=False,
                failed_tier=ValidationTier.T3_REGRESSION,
                failure_type=FailureType.BEHAVIORAL,
                error_message=f"[T3 Regression Failure] " + "; ".join(reg_res.broken_tests),
                details={"broken_tests": reg_res.broken_tests},
            )

        return ValidationReport(success=True)
