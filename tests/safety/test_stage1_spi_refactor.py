"""Stage 1 (v2.3) — Language SPI refactor + Validation pipeline standard slots.

Verifies the anti-fragmentation refactor:
1. safety/languages/ SPI exists (base.py, csharp.py, registry.py).
2. safety/ast_guard.py is a THIN ROUTER delegating to the SPI (no C#
   hardcode left) while preserving the entire legacy public surface.
3. Opt 8.3 contracts survive: shared Language singleton, per-instance Parser.
4. validation/pipeline.py exposes the T2_5_MUTATION tier and standard slots
   (mutation_gate, holdout_injector) with legacy behavior when unset.
"""
import pytest
from pathlib import Path

from tree_sitter import Parser

from safety.ast_guard import (
    ASTGuard,
    ASTViolationError,
    CSharpSymbol,
    get_csharp_language,
)
from safety.languages.base import BaseLanguageDriver, ASTViolationError as SPIViolationError
from safety.languages.csharp import CSharpDriver, CSharpSymbol as DriverSymbol
from safety.languages.registry import (
    LanguageRegistry,
    get_language_driver,
    get_default_registry,
)
from context.extractor import SymbolExtractor

from recovery.classifier import FailureType
from validation.pipeline import (
    ValidationPipeline,
    ValidationTier,
    MutationGateResult,
    HoldoutInjectionResult,
)


# ==============================================================================
# 1. SPI module structure
# ==============================================================================

class TestStage1SPIExists:
    def test_languages_package_files_exist(self):
        base = Path("safety/languages/base.py")
        csharp = Path("safety/languages/csharp.py")
        registry = Path("safety/languages/registry.py")
        assert base.exists() and csharp.exists() and registry.exists()

    def test_csharp_driver_implements_base(self):
        driver = CSharpDriver()
        assert isinstance(driver, BaseLanguageDriver)
        assert driver.language_name == "csharp"

    def test_registry_resolves_csharp_by_extension(self):
        driver = get_language_driver("src/Player.cs")
        assert isinstance(driver, CSharpDriver)

    def test_registry_rejects_unknown_extension(self):
        with pytest.raises(ValueError, match="Cannot detect language"):
            get_language_driver("src/main.cpp")

    def test_default_registry_has_csharp(self):
        assert "csharp" in get_default_registry().available()


# ==============================================================================
# 2. ast_guard is a thin router over the SPI
# ==============================================================================

class TestStage1ThinRouter:
    def test_ast_guard_is_router_no_csharp_hardcode(self):
        """ast_guard.py must not contain the C# symbol-extraction hardcode:
        tree-sitter node type strings live in the driver, not the router."""
        router_src = Path("safety/ast_guard.py").read_text(encoding="utf-8")
        # The heavy lifting markers must be gone from the router...
        for marker in (
            "class_declaration",          # extraction hardcode
            "invocation_expression",       # assert predicate hardcode
            "notimplementedexception",     # stub detection hardcode
        ):
            assert marker not in router_src, (
                f"Router still contains C# hardcode marker '{marker}' — "
                "extraction logic must live in safety/languages/csharp.py"
            )
        # ...and delegation must be present.
        assert "self._driver" in router_src

    def test_ast_guard_public_surface_preserved(self):
        guard = ASTGuard()
        assert hasattr(guard, "validate_csharp_transition")
        assert hasattr(guard, "_extract_symbols")
        assert hasattr(guard, "_extract_assert_predicates")
        assert hasattr(guard, "_check_early_return_and_stubs")
        assert hasattr(guard, "_get_enclosing_type")
        assert hasattr(guard, "language")
        assert hasattr(guard, "parser")

    def test_ast_violation_error_identity(self):
        """The router's error class IS the SPI error class (single canonical type)."""
        assert ASTViolationError is SPIViolationError

    def test_csharp_symbol_reexported_from_router(self):
        assert CSharpSymbol is DriverSymbol

    def test_driver_validate_transition_raises_same_error_type(self):
        driver = CSharpDriver()
        old = "public class Player { public int Hp { get; set; } }"
        # Delete the whole class -> must raise ASTViolationError from SPI
        with pytest.raises(ASTViolationError, match="class/type deletion"):
            driver.validate_transition(old, "public class Other { }", file_path="Player.cs")

    def test_guard_delegates_validate_csharp_transition(self):
        guard = ASTGuard()
        old = "public class Player {\n    public void Update() { int x = 1; int y = 2; DoWork(x + y); }\n}\n"
        new = "public class Player {\n    public void Update() { throw new System.NotImplementedException(); }\n}\n"
        with pytest.raises(ASTViolationError, match="dummy stub"):
            guard.validate_csharp_transition(old, new, "Player.cs")


# ==============================================================================
# 3. Opt 8.3 contracts survive the refactor
# ==============================================================================

class TestStage1LanguageSharingContracts:
    def test_shared_language_singleton_across_guards(self):
        g1, g2 = ASTGuard(), ASTGuard()
        assert g1.language is g2.language
        assert g1.parser is not g2.parser
        assert isinstance(g1.parser, Parser)

    def test_driver_shares_language_with_extractor(self):
        driver = CSharpDriver()
        ext = SymbolExtractor()
        guard = ASTGuard()
        assert driver.language is ext.language
        assert guard.language is ext.language

    def test_registry_drivers_share_language_not_parser(self):
        d1 = get_language_driver("A.cs")
        d2 = get_language_driver("B.cs")
        assert d1.language is d2.language
        assert d1.parser is not d2.parser

    def test_router_reexports_get_csharp_language(self):
        from safety.tree_sitter_shared import get_csharp_language as shared
        assert get_csharp_language() is shared()


# ==============================================================================
# 4. Validation pipeline standard slots (T2.5 + holdout)
# ==============================================================================

class TestStage1ValidationSlots:
    def test_t2_5_mutation_tier_exists(self):
        assert ValidationTier.T2_5_MUTATION.value == "T2_5_MUTATION"
        # Tier ordering sanity: enum lists T2 before T2.5 before T3.
        names = [t.name for t in ValidationTier]
        assert names.index("T2_BEHAVIORAL") < names.index("T2_5_MUTATION") < names.index("T3_REGRESSION")

    def test_mutation_coverage_failure_type_exists_and_is_recoverable(self):
        """MUTATION_COVERAGE must NOT be a hard stop: a failed mutation gate
        triggers the recovery loop, not a permanent halt."""
        from recovery.classifier import FailureClassifier
        assert FailureType.MUTATION_COVERAGE.value == "MUTATION_COVERAGE"
        assert FailureClassifier.is_hard_stop(FailureType.MUTATION_COVERAGE) is False

    def test_pipeline_slots_default_none(self):
        pipeline = ValidationPipeline()
        assert pipeline.mutation_gate is None
        assert pipeline.holdout_injector is None

    def test_mutation_gate_result_schema(self):
        res = MutationGateResult(success=True, mutation_score=0.85, killed=17, total=20)
        assert res.threshold == 0.70
        assert res.surviving_mutants == []

    def test_holdout_injection_result_schema(self):
        res = HoldoutInjectionResult(injected_files=["tests/HoldoutTest.cs"])
        assert res.details == {}

    def test_mutation_gate_slot_fires_between_t2_and_t3(self, tmp_path):
        """Ordering contract: T2 → T2.5 → T3. A failing mutation gate must
        report failed_tier=T2_5_MUTATION BEFORE regression runs."""
        calls = []

        from validation.behavioral import BaseBehavioralValidator, BehavioralCheckResult
        from validation.regression import BaseRegressionValidator, RegressionCheckResult
        from build.sandbox import MockBuildRunner
        from validation.baseline import BaselineState

        class Recorder(BaseBehavioralValidator):
            def validate_behavior(self, repo_path, task_id):
                calls.append("T2")
                return BehavioralCheckResult(success=True)

        class RecorderReg(BaseRegressionValidator):
            def validate_regression(self, repo_path):
                calls.append("T3")
                return RegressionCheckResult(success=True)

        def gate(repo_path, task):
            calls.append("T2.5")
            return MutationGateResult(success=False, mutation_score=0.5, killed=5, total=10)

        pipeline = ValidationPipeline(
            behavioral_validator=Recorder(),
            regression_validator=RecorderReg(),
            mutation_gate=gate,
        )
        def _mk_baseline():
            from build.sandbox import BuildResult
            return BaselineState(
                base_commit="deadbeef",
                build_result=BuildResult(success=True, raw_output=""),
                regression_result=None,
                behavioral_result=None,
                env_fingerprint="env",
                config_fingerprint="cfg",
                cache_key="k1",
            )

        def _mk_task():
            from task.schema import TaskDefinition
            return TaskDefinition(task_id="T_STAGE1_X", title="Stage 1 slot test", allowed_files=[])

        # T1 passes with the mock runner (no baseline build to compare).
        report = pipeline.validate_post_apply(
            task=_mk_task(),
            repo_path=tmp_path,
            build_runner=MockBuildRunner(should_succeed=True),
            baseline=_mk_baseline(),
        )
        assert report.success is False
        assert report.failed_tier == ValidationTier.T2_5_MUTATION
        assert report.failure_type == FailureType.MUTATION_COVERAGE
        assert calls == ["T2", "T2.5"], f"Expected T2 then T2.5 gate; got {calls}"
        assert "T3" not in calls, "T3 must not run when T2.5 fails"

    def test_holdout_injector_slot_fires_before_t3(self, tmp_path):
        """Holdout injection must run IMMEDIATELY BEFORE T3 Regression."""
        calls = []

        from validation.regression import BaseRegressionValidator, RegressionCheckResult
        from build.sandbox import MockBuildRunner
        from validation.baseline import BaselineState

        class RecorderReg(BaseRegressionValidator):
            def validate_regression(self, repo_path):
                calls.append("T3")
                return RegressionCheckResult(success=True)

        def injector(repo_path, task):
            calls.append("HOLDOUT_INJECT")
            return HoldoutInjectionResult(injected_files=["tests/Holdout1.cs"])

        pipeline = ValidationPipeline(
            regression_validator=RecorderReg(),
            holdout_injector=injector,
        )

        def _mk_task():
            from task.schema import TaskDefinition
            return TaskDefinition(task_id="T_STAGE1_X", title="Stage 1 slot test", allowed_files=[])

        def _mk_baseline():
            from build.sandbox import BuildResult
            return BaselineState(
                base_commit="deadbeef",
                build_result=BuildResult(success=True, raw_output=""),
                regression_result=None,
                behavioral_result=None,
                env_fingerprint="env",
                config_fingerprint="cfg",
                cache_key="k2",
            )

        report = pipeline.validate_post_apply(
            task=_mk_task(),
            repo_path=tmp_path,
            build_runner=MockBuildRunner(should_succeed=True),
            baseline=_mk_baseline(),
        )
        assert report.success is True
        assert calls == ["HOLDOUT_INJECT", "T3"], f"Expected holdout inject then T3; got {calls}"

    def test_legacy_pipeline_no_slots_unchanged(self, tmp_path):
        """With both slots None, T1→T2→T3 ordering and success behavior are
        identical to the pre-v2.3 pipeline (zero behavior change)."""
        calls = []

        from validation.behavioral import BaseBehavioralValidator, BehavioralCheckResult
        from validation.regression import BaseRegressionValidator, RegressionCheckResult
        from build.sandbox import MockBuildRunner
        from validation.baseline import BaselineState

        class Recorder(BaseBehavioralValidator):
            def validate_behavior(self, repo_path, task_id):
                calls.append("T2")
                return BehavioralCheckResult(success=True)

        class RecorderReg(BaseRegressionValidator):
            def validate_regression(self, repo_path):
                calls.append("T3")
                return RegressionCheckResult(success=True)

        pipeline = ValidationPipeline(
            behavioral_validator=Recorder(),
            regression_validator=RecorderReg(),
        )

        def _mk_task():
            from task.schema import TaskDefinition
            return TaskDefinition(task_id="T_STAGE1_X", title="Stage 1 slot test", allowed_files=[])

        def _mk_baseline():
            from build.sandbox import BuildResult
            return BaselineState(
                base_commit="deadbeef",
                build_result=BuildResult(success=True, raw_output=""),
                regression_result=None,
                behavioral_result=None,
                env_fingerprint="env",
                config_fingerprint="cfg",
                cache_key="k3",
            )

        report = pipeline.validate_post_apply(
            task=_mk_task(),
            repo_path=tmp_path,
            build_runner=MockBuildRunner(should_succeed=True),
            baseline=_mk_baseline(),
        )
        assert report.success is True
        assert calls == ["T2", "T3"], f"Legacy ordering broken: {calls}"
