"""Base SPI contract for language drivers (anti-reward-hacking v2.3 Stage 1).

A language driver encapsulates every language-specific concern of AST-level
safety analysis behind a uniform interface, so `safety.ast_guard.ASTGuard`
can remain a thin router. This module is a leaf module (imports nothing from
the project) to avoid circular imports with `safety` and `context` packages.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


class ASTViolationError(Exception):
    """Raised when an AST-level safety or scope invariant is violated.

    Lives in the SPI base module (not in ast_guard) so drivers raise the
    same exception type the router re-exports, keeping a single canonical
    error class across languages.
    """
    pass


@dataclass
class LanguageDriverContext:
    """Container for symbols of a single parse pass.

    Kept deliberately generic (Dict-based) so drivers for different
    languages can attach whatever metadata they need without forcing a
    shared, ever-growing dataclass hierarchy.
    """
    file_path: str = ""
    target_symbols: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


class BaseLanguageDriver(ABC):
    """Contract each language driver must implement.

    Contract invariants (enforced by the router and tests):
    - `language` returns the SHARED, cached tree-sitter Language instance
      for the language (singleton; identical object across all drivers).
    - `parser` returns a PER-INSTANCE Parser (distinct objects across
      driver instances; the wrapped Language is expensive, the Parser is
      lightweight).
    - `validate_transition` raises ASTViolationError on any violation and
      returns None otherwise.
    """

    @property
    @abstractmethod
    def language_name(self) -> str:
        """Canonical driver name, e.g. 'csharp'."""
        ...

    @property
    @abstractmethod
    def language(self):
        """Shared cached tree-sitter Language instance for this language."""
        ...

    @property
    @abstractmethod
    def parser(self):
        """A Parser bound to the shared language (fresh per driver)."""
        ...

    @abstractmethod
    def extract_symbols(self, root, code_bytes: bytes) -> List[Any]:
        """Extracts declared symbols from a parsed tree's root node.

        The element type is driver-specific (CSharpSymbol for the C#
        driver); the router treats results as opaque except for the
        identity/matching operations it delegates back to the driver.
        """
        ...

    @abstractmethod
    def extract_assert_predicates(self, root, code_bytes: bytes) -> List[Tuple[str, str, bool]]:
        """Extracts assertion invocations as (call_text, predicate_text, is_tautology)."""
        ...

    @abstractmethod
    def check_early_return_and_stubs(self, old_symbols, new_symbols, file_path: str) -> None:
        """Detects dummy stubs or early-return bypasses replacing substantive logic.

        Raises ASTViolationError on violation.
        """
        ...

    @abstractmethod
    def validate_transition(
        self,
        old_code: str,
        new_code: str,
        file_path: str = "",
        target_symbols: Optional[List[str]] = None,
    ) -> None:
        """Full semantic validation of a code transition.

        Raises ASTViolationError on any violation; returns None otherwise.
        """
        ...
