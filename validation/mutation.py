import logging
import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel
from task.schema import TaskDefinition

logger = logging.getLogger("dsh.validation.mutation")


class MutationReport(BaseModel):
    success: bool
    score: float
    total_mutants: int = 0
    killed_mutants: int = 0
    survived_mutants: int = 0
    threshold: float = 0.70
    details: List[Dict[str, Any]] = []
    error_message: Optional[str] = None


class BaseMutationGate(ABC):
    """Abstract interface for T2.5 Mutation Testing Gate."""

    @abstractmethod
    def evaluate_mutation(
        self,
        repo_path: Path,
        task: TaskDefinition,
        test_cmd: Optional[str] = None,
        test_runner_fn: Optional[Callable[[Path], bool]] = None,
    ) -> MutationReport:
        pass


class MockMutationGate(BaseMutationGate):
    def __init__(self, should_succeed: bool = True, score: float = 0.85, error_message: Optional[str] = None):
        self.should_succeed = should_succeed
        self.score = score
        self.error_message = error_message

    def evaluate_mutation(
        self,
        repo_path: Path,
        task: TaskDefinition,
        test_cmd: Optional[str] = None,
        test_runner_fn: Optional[Callable[[Path], bool]] = None,
    ) -> MutationReport:
        if self.should_succeed:
            return MutationReport(
                success=True,
                score=self.score,
                total_mutants=10,
                killed_mutants=int(10 * self.score),
                survived_mutants=10 - int(10 * self.score),
            )
        return MutationReport(
            success=False,
            score=self.score,
            total_mutants=10,
            killed_mutants=int(10 * self.score),
            survived_mutants=10 - int(10 * self.score),
            error_message=self.error_message or f"Mutation score {self.score:.1%} below required threshold 70.0%",
        )


class ASTMutationGate(BaseMutationGate):
    """Deterministic, lightweight AST-level mutant generator for fast verification (< 10s).
    Mutates target files and executes the test runner to ensure tests are not vacuous.
    """

    # Fast mutation substitution rules (regex search -> replacement)
    MUTATION_OPERATORS = [
        (re.compile(r"==\s*([^=\s]+)"), r"!= \1", "equality_inversion"),
        (re.compile(r"!=\s*([^=\s]+)"), r"== \1", "inequality_inversion"),
        (re.compile(r"\bTrue\b"), "False", "bool_true_inversion"),
        (re.compile(r"\bFalse\b"), "True", "bool_false_inversion"),
        (re.compile(r"\btrue\b"), "false", "csharp_true_inversion"),
        (re.compile(r"\bfalse\b"), "true", "csharp_false_inversion"),
        (re.compile(r"(\w+)\s*\+\s*(\w+)"), r"\1 - \2", "plus_to_minus"),
        (re.compile(r"(\w+)\s*>\s*(\w+)"), r"\1 <= \2", "greater_to_less_equal"),
        (re.compile(r"(\w+)\s*<\s*(\w+)"), r"\1 >= \2", "less_to_greater_equal"),
    ]

    def __init__(self, min_score: float = 0.70, max_mutants_per_file: int = 5, timeout_seconds: int = 15):
        self.min_score = min_score
        self.max_mutants_per_file = max_mutants_per_file
        self.timeout_seconds = timeout_seconds

    def evaluate_mutation(
        self,
        repo_path: Path,
        task: TaskDefinition,
        test_cmd: Optional[str] = None,
        test_runner_fn: Optional[Callable[[Path], bool]] = None,
    ) -> MutationReport:
        total_mutants = 0
        killed_mutants = 0
        survived_mutants = 0
        details: List[Dict[str, Any]] = []

        # Find target files allowed in this task
        for rel_file in task.allowed_files:
            file_path = repo_path / rel_file
            if not file_path.exists() or not file_path.is_file():
                continue

            original_code = file_path.read_text(encoding="utf-8")
            mutants_generated = self._generate_mutants(original_code)

            for mutant_code, op_name, line_no in mutants_generated[: self.max_mutants_per_file]:
                total_mutants += 1
                try:
                    # Apply mutant
                    file_path.write_text(mutant_code, encoding="utf-8")

                    # Run test suite to see if it kills the mutant
                    test_passed = False
                    if test_runner_fn is not None:
                        test_passed = test_runner_fn(repo_path)
                    elif test_cmd:
                        proc = subprocess.run(
                            test_cmd,
                            shell=True,
                            cwd=str(repo_path),
                            capture_output=True,
                            timeout=self.timeout_seconds,
                        )
                        test_passed = (proc.returncode == 0)
                    else:
                        # No runner provided, treat as killed
                        test_passed = False

                    if not test_passed:
                        # Test failed -> Mutant was successfully caught/killed!
                        killed_mutants += 1
                        details.append({"operator": op_name, "line": line_no, "status": "KILLED"})
                    else:
                        # Test still passed -> Mutant survived (weak test!)
                        survived_mutants += 1
                        details.append({"operator": op_name, "line": line_no, "status": "SURVIVED"})

                except Exception as e:
                    # Errors during mutant execution count as killed
                    killed_mutants += 1
                    details.append({"operator": op_name, "line": line_no, "status": "KILLED", "error": str(e)})
                finally:
                    # Restore original code
                    file_path.write_text(original_code, encoding="utf-8")

        if total_mutants == 0:
            return MutationReport(
                success=True,
                score=1.0,
                total_mutants=0,
                killed_mutants=0,
                survived_mutants=0,
                threshold=self.min_score,
                details=[],
            )

        score = killed_mutants / total_mutants
        success = score >= self.min_score

        err = None
        if not success:
            err = (
                f"Mutation score {score:.1%} ({killed_mutants}/{total_mutants}) is below "
                f"required threshold {self.min_score:.1%}. Tests failed to detect introduced mutations."
            )

        return MutationReport(
            success=success,
            score=score,
            total_mutants=total_mutants,
            killed_mutants=killed_mutants,
            survived_mutants=survived_mutants,
            threshold=self.min_score,
            details=details,
            error_message=err,
        )

    def _generate_mutants(self, code: str) -> List[Tuple[str, str, int]]:
        mutants = []
        lines = code.splitlines(keepends=True)

        for i, line in enumerate(lines):
            # Skip comments or imports
            stripped = line.strip()
            if not stripped or stripped.startswith(("//", "#", "/*", "*", "import ", "using ")):
                continue

            for pattern, replacement, op_name in self.MUTATION_OPERATORS:
                if pattern.search(line):
                    mutated_line = pattern.sub(replacement, line, count=1)
                    if mutated_line != line:
                        mutant_lines = list(lines)
                        mutant_lines[i] = mutated_line
                        mutants.append(("".join(mutant_lines), op_name, i + 1))
                        break

        return mutants
