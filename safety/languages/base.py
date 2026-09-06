from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, List, Optional


class ASTViolationError(Exception):
    """Raised when an AST-level safety or scope invariant is violated."""
    pass


class PurityViolationError(Exception):
    """Raised when impure operations (I/O, non-deterministic state) are detected in pure Core code."""
    pass


@dataclass
class PurityReport:
    is_pure: bool
    violations: List[str] = field(default_factory=list)


class BaseLanguageDriver(ABC):
    """Abstract Base Class for language-specific AST validation, purity checking,
    and build/test toolchain commands.
    """

    @property
    @abstractmethod
    def language_id(self) -> str:
        """Language identifier, e.g. 'csharp', 'python', 'javascript', 'cpp'."""
        pass

    @property
    @abstractmethod
    def supported_extensions(self) -> List[str]:
        """List of file extensions, e.g. ['.cs'], ['.py'], ['.js', '.ts', '.jsx', '.tsx'], ['.cpp', '.c', '.cc', '.h', '.hpp']."""
        pass

    @abstractmethod
    def parse_ast(self, code: str) -> Any:
        """Parses source code into an AST representation."""
        pass

    @abstractmethod
    def validate_transition(
        self,
        old_code: str,
        new_code: str,
        file_path: str = "",
        target_symbols: Optional[List[str]] = None,
    ) -> None:
        """Validates transitions between old and new code (syntax, deletions, target symbols, asserts, bypasses)."""
        pass

    @abstractmethod
    def check_purity(
        self,
        code: str,
        file_path: str = "",
        pure_symbols: Optional[List[str]] = None,
    ) -> PurityReport:
        """Scans code for forbidden side-effects (file I/O, network, unseeded random, system clock)
        in accordance with the Functional Core, Imperative Shell (FCIS) pattern.
        """
        pass

    @abstractmethod
    def get_default_build_cmd(self) -> str:
        """Default build command for this language."""
        pass

    @abstractmethod
    def get_default_test_cmd(self) -> str:
        """Default test command for this language."""
        pass

    @abstractmethod
    def get_pbt_runner_cmd(self) -> str:
        """Command to run property-based tests (Hypothesis, Fast-Check, CsCheck, etc.)."""
        pass
