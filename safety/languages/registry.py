"""Language driver registry (anti-reward-hacking v2.3 Stage 1).

Maps file extensions / language names to SPI driver instances. Drivers are
created fresh per lookup so each carries its own lightweight Parser while
sharing the cached tree-sitter Language singleton (Opt 8.3 contract).
"""
from typing import Dict

from safety.languages.base import BaseLanguageDriver
from safety.languages.csharp import CSharpDriver


class LanguageRegistry:
    """Registry of language drivers keyed by canonical language name."""

    def __init__(self):
        self._drivers: Dict[str, BaseLanguageDriver] = {}

    def register(self, driver: BaseLanguageDriver) -> None:
        self._drivers[driver.language_name] = driver

    def get(self, language_name: str) -> BaseLanguageDriver:
        try:
            return self._drivers[language_name]
        except KeyError:
            raise ValueError(
                f"No language driver registered for '{language_name}'. "
                f"Available: {sorted(self._drivers.keys())}"
            )

    def available(self) -> list:
        return sorted(self._drivers.keys())

    def detect_language(self, file_path: str) -> str:
        """Maps a file path to its canonical language name."""
        lowered = str(file_path).lower()
        if lowered.endswith(".cs"):
            return "csharp"
        raise ValueError(
            f"Cannot detect language for file '{file_path}' — no driver registered "
            f"for its extension. Available: {self.available()}"
        )


# Default shared registry instance (drivers are stateless; the Language
# singleton lives in tree_sitter_shared, Parser is per-driver).
_registry = LanguageRegistry()
_registry.register(CSharpDriver())


def get_language_driver(file_path: str) -> BaseLanguageDriver:
    """Resolves the SPI driver for a source file path (fresh instance per
    call ⇒ per-instance Parser; shared Language singleton)."""
    lang = _registry.detect_language(file_path)
    return _registry.get(lang)


def get_default_registry() -> LanguageRegistry:
    """Returns the default shared registry (for registration of additional
    drivers by future stages)."""
    return _registry
