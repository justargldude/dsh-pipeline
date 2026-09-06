import pytest
from pathlib import Path
from task.schema import TaskDefinition
from build.sandbox import MockBuildRunner
from validation.behavioral import MockBehavioralValidator
from validation.pipeline import ValidationPipeline, ValidationTier
from validation.mutation import MockMutationGate, ASTMutationGate, MutationReport


def test_mock_mutation_gate_rejects_weak_tests(tmp_path: Path):
    """If mutation score is below threshold, ValidationPipeline must fail at T2.5."""
    gate = MockMutationGate(should_succeed=False, score=0.40)
    pipeline = ValidationPipeline(
        behavioral_validator=MockBehavioralValidator(should_succeed=True),
        mutation_gate=gate,
    )
    task = TaskDefinition(task_id="T_MUT_01", title="Mutation Test", allowed_files=["calc.py"])
    build_runner = MockBuildRunner(should_succeed=True)

    report = pipeline.validate_post_apply(
        task=task,
        repo_path=tmp_path,
        build_runner=build_runner,
    )

    assert not report.success
    assert report.failed_tier == ValidationTier.T2_5_MUTATION
    assert "below required threshold" in report.error_message


def test_ast_mutation_gate_catches_vacuous_tests(tmp_path: Path):
    """ASTMutationGate must generate mutants; a strong test kills them, a vacuous test lets them survive."""
    gate = ASTMutationGate(min_score=0.70)

    # Create target code
    code_file = tmp_path / "math_lib.py"
    code_file.write_text("def is_positive(x):\n    return x > 0\n", encoding="utf-8")

    task = TaskDefinition(task_id="T_MUT_02", title="AST Mut", allowed_files=["math_lib.py"])

    # 1. Vacuous test runner: always returns True regardless of mutants
    vacuous_runner = lambda p: True
    report_vacuous = gate.evaluate_mutation(tmp_path, task, test_runner_fn=vacuous_runner)
    assert not report_vacuous.success
    assert report_vacuous.score == 0.0
    assert report_vacuous.survived_mutants > 0

    # 2. Strong test runner: tests if is_positive(-5) is False
    def strong_runner(p: Path) -> bool:
        # Load the code dynamically
        content = (p / "math_lib.py").read_text(encoding="utf-8")
        namespace = {}
        exec(content, namespace)
        fn = namespace["is_positive"]
        # Must return True for 5 and False for -5 and False for 0
        return fn(5) is True and fn(-5) is False and fn(0) is False

    report_strong = gate.evaluate_mutation(tmp_path, task, test_runner_fn=strong_runner)
    assert report_strong.success
    assert report_strong.score == 1.0
    assert report_strong.killed_mutants > 0
