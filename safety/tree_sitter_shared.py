"""Shared tree-sitter Language instance for C#.

Placed in a neutral leaf module so both `safety` and `context` packages can
import it without creating circular imports (Q10 / R5a): this module imports
nothing from the project, only the tree-sitter libraries.
"""
from functools import lru_cache

import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language


@lru_cache(maxsize=1)
def get_csharp_language() -> Language:
    """Returns a cached shared C# tree-sitter Language instance.

    The Language object wraps a C data structure that is expensive to build;
    sharing one instance across ASTGuard / SymbolExtractor avoids rebuilding
    it per instance. Note (R5b): lru_cache is not strictly atomic — under
    first-access contention two threads may briefly each build an instance,
    but all subsequent calls return the same cached object. Worst case is a
    one-time redundant construction, never incorrect behavior.
    """
    return Language(tscsharp.language())
