"""AST Guard — thin router delegating to the language SPI (v2.3 Stage 1).

Historically this module hard-coded every C#-specific AST concern. Per the
anti-reward-hacking v2.3 Stage 1 refactor it is now a thin router: all
language-specific logic lives in `safety/languages/` (base.py / csharp.py /
registry.py) and this module only resolves the driver, preserves the legacy
public surface, and delegates.

Backward-compat surface kept IDENTICAL (Stage 1 mandates zero behavior
change):
- `ASTGuard`, `ASTViolationError`, `CSharpSymbol`, `get_csharp_language`
  remain importable from this module.
- `ASTGuard().language` → shared cached tree-sitter Language singleton.
- `ASTGuard().parser` → stable per-instance Parser (distinct across guard
  instances, bound to the shared Language) — Opt 8.3 contract.
- `validate_csharp_transition(old, new, file_path, target_symbols)` — same
  signature, same ASTViolationError semantics.
- `_extract_symbols`, `_extract_assert_predicates`,
  `_check_early_return_and_stubs`, `_get_enclosing_type` — legacy method
  names preserved as thin delegating wrappers.
"""
from typing import List, Optional, Tuple

from safety.languages.base import ASTViolationError, BaseLanguageDriver
from safety.languages.csharp import CSharpDriver, CSharpSymbol
from safety.languages.registry import LanguageRegistry, get_default_registry, get_language_driver
from safety.tree_sitter_shared import get_csharp_language

__all__ = [
    "ASTViolationError",
    "ASTGuard",
    "CSharpSymbol",
    "CSharpDriver",
    "BaseLanguageDriver",
    "LanguageRegistry",
    "get_language_driver",
    "get_csharp_language",
]


class ASTGuard:
    """Thin router over the language SPI.

    Resolves the driver for C# (the only registered language today) and
    delegates all validation. Each guard instance snapshots one Parser at
    construction (legacy Opt 8.3 contract: shared Language singleton,
    per-instance Parser).
    """

    TAUTOLOGICAL_ASSERT_PATTERNS = CSharpDriver.TAUTOLOGICAL_ASSERT_PATTERNS

    def __init__(self, driver: Optional[BaseLanguageDriver] = None):
        # Shared, stateless driver from the registry (fresh Parser is created
        # per parse inside the driver; the Language singleton is shared).
        self._driver: BaseLanguageDriver = driver or get_default_registry().get("csharp")
        # Legacy compat attributes (Opt 8.3):
        # - `language` is the shared cached singleton (identical across all
        #   guards and the SymbolExtractor).
        # - `parser` is a stable per-guard snapshot (distinct per guard).
        self.language = self._driver.language
        self.parser = self._driver.parser

    # ------------------------------------------------------------------
    # Legacy public/private method names → thin delegation to the SPI.
    # ------------------------------------------------------------------
    def _get_enclosing_type(self, node, code_bytes: bytes) -> Tuple[str, str]:
        """Returns (enclosing_type, namespace) for a given AST node."""
        return self._driver._get_enclosing_type(node, code_bytes)

    def _extract_symbols(self, root, code_bytes: bytes) -> List[CSharpSymbol]:
        """Extracts all declared symbols with detailed signatures from C# AST."""
        return self._driver.extract_symbols(root, code_bytes)

    def _extract_assert_predicates(self, root, code_bytes: bytes) -> List[Tuple[str, str, bool]]:
        """Extracts assertion invocations, returning list of (call_text, predicate_text, is_tautology)."""
        return self._driver.extract_assert_predicates(root, code_bytes)

    def _check_early_return_and_stubs(
        self,
        old_symbols: List[CSharpSymbol],
        new_symbols: List[CSharpSymbol],
        file_path: str,
    ):
        """Detects dummy stubs or early return bypasses replacing substantive logic."""
        return self._driver.check_early_return_and_stubs(old_symbols, new_symbols, file_path)

    def validate_csharp_transition(
        self,
        old_code: str,
        new_code: str,
        file_path: str = "",
        target_symbols: Optional[List[str]] = None,
    ):
        """Comprehensive AST validation across code transitions (delegates to CSharpDriver).

        Enforces:
        1. Valid C# syntax without introduced parse errors.
        2. Disallowed deletion of classes, interfaces, structs, or methods.
        3. Target symbol boundary enforcement (if target_symbols is non-empty, only matching symbols may change).
        4. Overload distinction and precision.
        5. Assertion semantic validation (rejects weakening/removal/tautologies like Assert(true)).
        6. Early return & dummy stub bypass detection.
        """
        return self._driver.validate_transition(
            old_code=old_code,
            new_code=new_code,
            file_path=file_path,
            target_symbols=target_symbols,
        )
