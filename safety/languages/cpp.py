import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from tree_sitter import Language, Parser, Node

try:
    import tree_sitter_cpp
    _CPP_LANG = Language(tree_sitter_cpp.language())
except Exception:
    _CPP_LANG = None

from safety.languages.base import BaseLanguageDriver, ASTViolationError, PurityReport


@dataclass
class CppSymbol:
    kind: str  # "function", "class", "struct"
    name: str
    enclosing_type: str = ""
    parameters: List[str] = field(default_factory=list)
    body_text: str = ""
    statement_count: int = 0
    first_stmt_type: str = ""
    full_text: str = ""

    @property
    def qualified_name(self) -> str:
        if self.enclosing_type:
            return f"{self.enclosing_type}::{self.name}"
        return self.name

    @property
    def identity_key(self) -> str:
        return f"{self.kind}:{self.qualified_name}"

    def matches_target(self, target_pattern: str) -> bool:
        target = target_pattern.strip()
        names = {self.name, self.qualified_name}
        if "(" in target:
            target_name = target.split("(")[0].strip()
            return target_name in names
        return target in names


class CppDriver(BaseLanguageDriver):
    """C and C++ language driver leveraging Tree-Sitter and AddressSanitizer."""

    IMPURE_CALL_PATTERNS = [
        (re.compile(r"(\bfopen|\bfread|\bfwrite|\bifstream|\bofstream|\bfstream|\bremove|\brename)"), "C/C++ File System I/O"),
        (re.compile(r"(\bsocket|\bconnect|\bbind|\blisten|\baccept|\bsend|\brecv|\bgetaddrinfo)"), "POSIX / BSD Network socket call"),
        (re.compile(r"(\btime\s*\(|\bclock\s*\(|\bchrono::system_clock|\bchrono::steady_clock)"), "Non-deterministic system clock access"),
        (re.compile(r"(\brand\s*\(|\brandom_device|\bmt19937)"), "Unseeded pseudo-random generator access"),
        (re.compile(r"(\bexit\s*\(|\babort\s*\(|\bsystem\s*\(|\bfork\s*\(|\bexec[a-z]*\s*\()"), "Process exit, system shell or fork/exec"),
        (re.compile(r"(\bprintf\s*\(|\bcout\s*<<|\bcerr\s*<<)"), "Standard output / error stream access"),
    ]

    def __init__(self):
        if _CPP_LANG is None:
            raise RuntimeError("tree-sitter-cpp is not installed or failed to load.")
        self.language = _CPP_LANG
        self.parser = Parser(self.language)

    @property
    def language_id(self) -> str:
        return "cpp"

    @property
    def supported_extensions(self) -> List[str]:
        return [".cpp", ".c", ".cc", ".cxx", ".h", ".hpp", ".hxx"]

    def parse_ast(self, code: str) -> Any:
        return self.parser.parse(code.encode("utf-8"))

    def get_default_build_cmd(self) -> str:
        return "g++ -fsanitize=address,undefined -g -O1 -std=c++17"

    def get_default_test_cmd(self) -> str:
        return "ctest --output-on-failure"

    def get_pbt_runner_cmd(self) -> str:
        return "./build/test_fuzz"

    def _extract_symbols(self, root: Node, code_bytes: bytes) -> List[CppSymbol]:
        symbols: List[CppSymbol] = []
        stack: List[Tuple[Node, str]] = [(root, "")]

        while stack:
            curr, enclosing = stack.pop()

            if curr.type in ("class_specifier", "struct_specifier"):
                name = ""
                for child in curr.children:
                    if child.type in ("type_identifier", "identifier"):
                        name = code_bytes[child.start_byte:child.end_byte].decode("utf-8")
                        break
                kind = "class" if curr.type == "class_specifier" else "struct"
                sym = CppSymbol(
                    kind=kind,
                    name=name,
                    enclosing_type=enclosing,
                    full_text=code_bytes[curr.start_byte:curr.end_byte].decode("utf-8", errors="replace"),
                )
                symbols.append(sym)
                for child in curr.children:
                    if child.type == "field_declaration_list":
                        stack.append((child, name))

            elif curr.type == "function_definition":
                name = ""
                body_text = ""
                stmt_count = 0
                first_stmt_type = ""

                # Extract declarator
                for child in curr.children:
                    if child.type in ("function_declarator", "declarator"):
                        for sub in child.children:
                            if sub.type in ("identifier", "field_identifier"):
                                name = code_bytes[sub.start_byte:sub.end_byte].decode("utf-8")
                                break
                    elif child.type == "compound_statement":
                        body_text = code_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
                        stmts = [c for c in child.children if c.type.endswith("_statement")]
                        stmt_count = len(stmts)
                        if stmts:
                            first_stmt_type = stmts[0].type

                sym = CppSymbol(
                    kind="function",
                    name=name,
                    enclosing_type=enclosing,
                    body_text=body_text,
                    statement_count=stmt_count,
                    first_stmt_type=first_stmt_type,
                    full_text=code_bytes[curr.start_byte:curr.end_byte].decode("utf-8", errors="replace"),
                )
                symbols.append(sym)

            else:
                for child in curr.children:
                    stack.append((child, enclosing))

        return symbols

    def validate_transition(
        self,
        old_code: str,
        new_code: str,
        file_path: str = "",
        target_symbols: Optional[List[str]] = None,
    ) -> None:
        old_bytes = old_code.encode("utf-8")
        new_bytes = new_code.encode("utf-8")

        old_tree = self.parser.parse(old_bytes)
        new_tree = self.parser.parse(new_bytes)

        if new_tree.root_node.has_error and not old_tree.root_node.has_error:
            raise ASTViolationError(f"Patch introduces C/C++ syntax error in '{file_path}'.")

        old_symbols = self._extract_symbols(old_tree.root_node, old_bytes)
        new_symbols = self._extract_symbols(new_tree.root_node, new_bytes)

        old_sym_map = {s.identity_key: s for s in old_symbols}
        new_sym_map = {s.identity_key: s for s in new_symbols}

        # Check deleted classes/structs
        old_types = {s.identity_key: s for s in old_symbols if s.kind in ("class", "struct")}
        new_types = {s.identity_key: s for s in new_symbols if s.kind in ("class", "struct")}
        deleted_types = set(old_types.keys()) - set(new_types.keys())
        if deleted_types:
            deleted_names = [old_types[k].qualified_name for k in deleted_types]
            raise ASTViolationError(f"Disallowed type deletion in '{file_path}': {deleted_names}")

        # Check deleted functions
        old_funcs = {s.identity_key: s for s in old_symbols if s.kind == "function"}
        new_funcs = {s.identity_key: s for s in new_symbols if s.kind == "function"}
        deleted_funcs = set(old_funcs.keys()) - set(new_funcs.keys())
        if deleted_funcs:
            deleted_names = [old_funcs[k].qualified_name for k in deleted_funcs]
            raise ASTViolationError(f"Disallowed function deletion in '{file_path}': {deleted_names}")

        # Target symbol enforcement
        if target_symbols:
            changed_symbols: List[CppSymbol] = []
            for k, new_sym in new_sym_map.items():
                if k not in old_sym_map:
                    changed_symbols.append(new_sym)
                else:
                    old_sym = old_sym_map[k]
                    if new_sym.full_text != old_sym.full_text:
                        changed_symbols.append(new_sym)

            for sym in changed_symbols:
                if sym.kind in ("class", "struct"):
                    continue
                if not any(sym.matches_target(ts) for ts in target_symbols):
                    raise ASTViolationError(
                        f"Target symbol violation in '{file_path}': Symbol '{sym.qualified_name}' "
                        f"was modified but is not in allowed target_symbols: {target_symbols}"
                    )

    def check_purity(
        self,
        code: str,
        file_path: str = "",
        pure_symbols: Optional[List[str]] = None,
    ) -> PurityReport:
        violations: List[str] = []
        for pattern, desc in self.IMPURE_CALL_PATTERNS:
            match = pattern.search(code)
            if match:
                violations.append(f"Forbidden impure call '{match.group(0)}' in '{file_path}': {desc}")
        return PurityReport(is_pure=len(violations) == 0, violations=violations)
