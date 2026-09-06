"""Language SPI (Service Provider Interface) for AST safety analysis.

Anti-reward-hacking v2.3 Stage 1: decouples language-specific AST analysis
(C# today) from the generic ASTGuard router so additional language drivers
can be plugged in without touching the guard itself (no fragmentation).
"""
from safety.languages.base import (
    ASTViolationError,
    BaseLanguageDriver,
    LanguageDriverContext,
)
from safety.languages.csharp import CSharpDriver
from safety.languages.registry import LanguageRegistry, get_language_driver

__all__ = [
    "ASTViolationError",
    "BaseLanguageDriver",
    "LanguageDriverContext",
    "CSharpDriver",
    "LanguageRegistry",
    "get_language_driver",
]
