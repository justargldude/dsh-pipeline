import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from tree_sitter import Language, Parser, Node

try:
    import tree_sitter_javascript
    _JS_LANG = Language(tree_sitter_javascript.language())
except Exception:
    _JS_LANG = None

from safety.languages.base import BaseLanguageDriver, ASTViolationError, PurityReport


@dataclass
class JSSymbol:
    kind: str  # "function", "class", "method"
    name: str
    enclosing_type: str = ""
    parameters: List[str] = field(default_factory=list)
    body_text: str = ""
    statement_count: int = 0
    first_stmt_type: str = ""
    has_not_implemented: bool = False
    full_text: str = ""

    @property
    def qualified_name(self) -> str:
        if self.enclosing_type:
            return f"{self.enclosing_type}.{self.name}"
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


class JavascriptDriver(BaseLanguageDriver):
    """JavaScript / TypeScript language driver leveraging Tree-Sitter."""

    IMPURE_CALL_PATTERNS = [
        (re.compile(r"(\bfs\.|\breadFile|\bwriteFile|\bcreateReadStream|\bcreateWriteStream)"), "Node.js File System I/O"),
        (re.compile(r"(\bfetch\s*\(|\baxios\.|\bhttp\.request|\bhttps\.request|\bnew\s+WebSocket)"), "Network / HTTP / WebSocket call"),
        (re.compile(r"(\bDate\.now\s*\(|\bnew\s+Date\b)"), "Non-deterministic system clock access (Date.now/new Date)"),
        (re.compile(r"(\bMath\.random\s*\(|\bcrypto\.randomBytes)"), "Non-deterministic pseudo-random generator"),
        (re.compile(r"(\bprocess\.exit\s*\(|\bchild_process\.|\bexecSync|\bspawnSync)"), "Process termination or child process spawn"),
        (re.compile(r"\bconsole\.(log|info|warn|error)"), "Console standard I/O stream access"),
    ]

    def __init__(self):
        if _JS_LANG is None:
            raise RuntimeError("tree-sitter-javascript is not installed or failed to load.")
        self.language = _JS_LANG
        self.parser = Parser(self.language)

    @property
    def language_id(self) -> str:
        return "javascript"

    @property
    def supported_extensions(self) -> List[str]:
        return [".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"]

    def parse_ast(self, code: str) -> Any:
        return self.parser.parse(code.encode("utf-8"))

    def get_default_build_cmd(self) -> str:
        return "node --check"

    def get_default_test_cmd(self) -> str:
        return "npx vitest run"

    def get_pbt_runner_cmd(self) -> str:
        return "npx vitest run -t fast-check"

    def _extract_symbols(self, root: Node, code_bytes: bytes) -> List[JSSymbol]:
        symbols: List[JSSymbol] = []
        stack: List[Tuple[Node, str]] = [(root, "")]

        while stack:
            curr, enclosing = stack.pop()

            if curr.type == "class_declaration":
                name = ""
                for child in curr.children:
                    if child.type == "identifier":
                        name = code_bytes[child.start_byte:child.end_byte].decode("utf-8")
                        break
                sym = JSSymbol(
                    kind="class",
                    name=name,
                    enclosing_type=enclosing,
                    full_text=code_bytes[curr.start_byte:curr.end_byte].decode("utf-8", errors="replace"),
                )
                symbols.append(sym)
                for child in curr.children:
                    if child.type == "class_body":
                        stack.append((child, name))

            elif curr.type in ("function_declaration", "method_definition"):
                name = ""
                body_text = ""
                stmt_count = 0
                first_stmt_type = ""
                has_ni = False

                for child in curr.children:
                    if child.type in ("identifier", "property_identifier"):
                        name = code_bytes[child.start_byte:child.end_byte].decode("utf-8")
                    elif child.type == "statement_block":
                        body_text = code_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
                        stmts = [c for c in child.children if c.type.endswith("_statement")]
                        stmt_count = len(stmts)
                        if stmts:
                            first_stmt_type = stmts[0].type
                        if "NotImplemented" in body_text or "throw new Error" in body_text:
                            has_ni = True

                kind = "method" if curr.type == "method_definition" else "function"
                sym = JSSymbol(
                    kind=kind,
                    name=name,
                    enclosing_type=enclosing,
                    body_text=body_text,
                    statement_count=stmt_count,
                    first_stmt_type=first_stmt_type,
                    has_not_implemented=has_ni,
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
            raise ASTViolationError(f"Patch introduces JavaScript/TypeScript syntax error in '{file_path}'.")

        old_symbols = self._extract_symbols(old_tree.root_node, old_bytes)
        new_symbols = self._extract_symbols(new_tree.root_node, new_bytes)

        old_sym_map = {s.identity_key: s for s in old_symbols}
        new_sym_map = {s.identity_key: s for s in new_symbols}

        # Check deleted classes
        old_classes = {s.identity_key: s for s in old_symbols if s.kind == "class"}
        new_classes = {s.identity_key: s for s in new_symbols if s.kind == "class"}
        deleted_classes = set(old_classes.keys()) - set(new_classes.keys())
        if deleted_classes:
            deleted_names = [old_classes[k].qualified_name for k in deleted_classes]
            raise ASTViolationError(f"Disallowed class deletion in '{file_path}': {deleted_names}")

        # Check deleted functions/methods
        old_funcs = {s.identity_key: s for s in old_symbols if s.kind in ("function", "method")}
        new_funcs = {s.identity_key: s for s in new_symbols if s.kind in ("function", "method")}
        deleted_funcs = set(old_funcs.keys()) - set(new_funcs.keys())
        if deleted_funcs:
            deleted_names = [old_funcs[k].qualified_name for k in deleted_funcs]
            raise ASTViolationError(f"Disallowed function/method deletion in '{file_path}': {deleted_names}")

        # Target symbol enforcement
        if target_symbols:
            changed_symbols: List[JSSymbol] = []
            for k, new_sym in new_sym_map.items():
                if k not in old_sym_map:
                    changed_symbols.append(new_sym)
                else:
                    old_sym = old_sym_map[k]
                    if new_sym.full_text != old_sym.full_text:
                        changed_symbols.append(new_sym)

            for sym in changed_symbols:
                if sym.kind == "class":
                    continue
                if not any(sym.matches_target(ts) for ts in target_symbols):
                    raise ASTViolationError(
                        f"Target symbol violation in '{file_path}': Symbol '{sym.qualified_name}' "
                        f"was modified but is not in allowed target_symbols: {target_symbols}"
                    )

        # Early return & dummy stub bypass
        for new_sym in new_symbols:
            old_sym = old_sym_map.get(new_sym.identity_key)
            if old_sym and old_sym.statement_count > 1 and new_sym.statement_count == 1:
                if new_sym.first_stmt_type == "return_statement":
                    raise ASTViolationError(
                        f"Early return / dummy stub bypass detected in method '{new_sym.qualified_name}': "
                        f"substantive logic replaced with return ({file_path})"
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
