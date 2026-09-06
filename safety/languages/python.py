import ast
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from safety.languages.base import BaseLanguageDriver, ASTViolationError, PurityReport


@dataclass
class PythonSymbol:
    kind: str  # "function", "async_function", "class"
    name: str
    enclosing_type: str = ""
    parameters: List[str] = field(default_factory=list)
    body_node_count: int = 0
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


class PythonDriver(BaseLanguageDriver):
    """Python language driver leveraging standard library `ast` for high-precision validation."""

    IMPURE_CALL_PATTERNS = [
        (re.compile(r"(\bopen\s*\(|\bos\.open|\bio\.open|\bPath\.write_|\bPath\.unlink)"), "File I/O filesystem operation"),
        (re.compile(r"(\brequests\.|\burllib\.|\bsocket\.|\bhttp\.client|\baiohttp\.|\bhttpx\.)"), "Network socket / HTTP call"),
        (re.compile(r"(\btime\.time\s*\(|\bdatetime\.datetime\.now|\bdatetime\.datetime\.utcnow|\bdatetime\.date\.today|\btime\.sleep)"), "Non-deterministic system clock access"),
        (re.compile(r"(\brandom\.random\s*\(|\brandom\.randint\s*\(|\brandom\.choice\s*\(|\bos\.urandom)"), "Unseeded non-deterministic random access"),
        (re.compile(r"(\bsubprocess\.|\bos\.system\s*\(|\bos\.popen\s*\(|\bsys\.exit\s*\()"), "Subprocess execution or system exit"),
        (re.compile(r"\bprint\s*\("), "Standard output stream print side-effect"),
    ]

    @property
    def language_id(self) -> str:
        return "python"

    @property
    def supported_extensions(self) -> List[str]:
        return [".py"]

    def parse_ast(self, code: str) -> Any:
        return ast.parse(code)

    def get_default_build_cmd(self) -> str:
        return "python3 -m py_compile"

    def get_default_test_cmd(self) -> str:
        return "pytest"

    def get_pbt_runner_cmd(self) -> str:
        return "pytest -k hypothesis"

    def _extract_symbols(self, tree: ast.AST, code: str) -> List[PythonSymbol]:
        symbols: List[PythonSymbol] = []
        code_lines = code.splitlines(keepends=True)

        class SymbolVisitor(ast.NodeVisitor):
            def __init__(self):
                self.class_stack = []

            def visit_ClassDef(self, node: ast.ClassDef):
                enclosing = ".".join(self.class_stack)
                sym = PythonSymbol(
                    kind="class",
                    name=node.name,
                    enclosing_type=enclosing,
                    body_node_count=len(node.body),
                )
                symbols.append(sym)
                self.class_stack.append(node.name)
                self.generic_visit(node)
                self.class_stack.pop()

            def _visit_func(self, node, kind: str):
                enclosing = ".".join(self.class_stack)
                params = [a.arg for a in node.args.args]
                first_stmt_type = type(node.body[0]).__name__ if node.body else ""
                has_ni = False
                for b in node.body:
                    if isinstance(b, ast.Raise):
                        if isinstance(b.exc, ast.Name) and b.exc.id == "NotImplementedError":
                            has_ni = True
                        elif isinstance(b.exc, ast.Call) and isinstance(b.exc.func, ast.Name) and b.exc.func.id == "NotImplementedError":
                            has_ni = True

                # Extract text lines if pos available
                full_text = ""
                if hasattr(node, "lineno") and hasattr(node, "end_lineno") and node.end_lineno is not None:
                    full_text = "".join(code_lines[node.lineno - 1 : node.end_lineno])

                sym = PythonSymbol(
                    kind=kind,
                    name=node.name,
                    enclosing_type=enclosing,
                    parameters=params,
                    body_node_count=len(node.body),
                    first_stmt_type=first_stmt_type,
                    has_not_implemented=has_ni,
                    full_text=full_text,
                )
                symbols.append(sym)
                self.generic_visit(node)

            def visit_FunctionDef(self, node: ast.FunctionDef):
                self._visit_func(node, "function")

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
                self._visit_func(node, "async_function")

        visitor = SymbolVisitor()
        visitor.visit(tree)
        return symbols

    def _extract_asserts(self, tree: ast.AST) -> List[Tuple[ast.Assert, bool]]:
        asserts = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert):
                is_taut = False
                if isinstance(node.test, ast.Constant) and bool(node.test.value) is True:
                    is_taut = True
                asserts.append((node, is_taut))
        return asserts

    def validate_transition(
        self,
        old_code: str,
        new_code: str,
        file_path: str = "",
        target_symbols: Optional[List[str]] = None,
    ) -> None:
        try:
            old_tree = ast.parse(old_code)
        except SyntaxError:
            old_tree = None

        try:
            new_tree = ast.parse(new_code)
        except SyntaxError as e:
            raise ASTViolationError(f"Patch introduces Python syntax error in '{file_path}': {e}")

        if old_tree is None:
            return

        old_symbols = self._extract_symbols(old_tree, old_code)
        new_symbols = self._extract_symbols(new_tree, new_code)

        old_sym_map = {s.identity_key: s for s in old_symbols}
        new_sym_map = {s.identity_key: s for s in new_symbols}

        # Check for deleted classes
        old_classes = {s.identity_key: s for s in old_symbols if s.kind == "class"}
        new_classes = {s.identity_key: s for s in new_symbols if s.kind == "class"}
        deleted_classes = set(old_classes.keys()) - set(new_classes.keys())
        if deleted_classes:
            deleted_names = [old_classes[k].qualified_name for k in deleted_classes]
            raise ASTViolationError(f"Disallowed class deletion in '{file_path}': {deleted_names}")

        # Check for deleted functions
        old_funcs = {s.identity_key: s for s in old_symbols if s.kind in ("function", "async_function")}
        new_funcs = {s.identity_key: s for s in new_symbols if s.kind in ("function", "async_function")}
        deleted_funcs = set(old_funcs.keys()) - set(new_funcs.keys())
        if deleted_funcs:
            deleted_names = [old_funcs[k].qualified_name for k in deleted_funcs]
            raise ASTViolationError(f"Disallowed function deletion in '{file_path}': {deleted_names}")

        # Target symbol enforcement
        if target_symbols:
            changed_symbols: List[PythonSymbol] = []
            for k, new_sym in new_sym_map.items():
                if k not in old_sym_map:
                    changed_symbols.append(new_sym)
                else:
                    old_sym = old_sym_map[k]
                    if new_sym.full_text and old_sym.full_text and new_sym.full_text != old_sym.full_text:
                        changed_symbols.append(new_sym)

            for sym in changed_symbols:
                if sym.kind == "class":
                    continue
                if not any(sym.matches_target(ts) for ts in target_symbols):
                    raise ASTViolationError(
                        f"Target symbol violation in '{file_path}': Symbol '{sym.qualified_name}' "
                        f"was modified but is not in allowed target_symbols: {target_symbols}"
                    )

        # Assertion semantic validation
        old_asserts = self._extract_asserts(old_tree)
        new_asserts = self._extract_asserts(new_tree)

        old_non_trivial = [a for a in old_asserts if not a[1]]
        new_non_trivial = [a for a in new_asserts if not a[1]]

        for a, is_taut in new_asserts:
            if is_taut and old_non_trivial:
                raise ASTViolationError(
                    f"Assertion semantic weakening detected in '{file_path}': Assertion weakened to tautology (assert True)"
                )

        if len(new_non_trivial) < len(old_non_trivial):
            raise ASTViolationError(
                f"Assertion weakening/removal detected in '{file_path}': "
                f"assertion count decreased from {len(old_non_trivial)} to {len(new_non_trivial)}"
            )

        # Check early return & dummy stub bypass
        for new_sym in new_symbols:
            if new_sym.has_not_implemented:
                raise ASTViolationError(
                    f"Early return / dummy stub bypass detected in method '{new_sym.qualified_name}': NotImplementedError ({file_path})"
                )
            old_sym = old_sym_map.get(new_sym.identity_key)
            if old_sym and old_sym.body_node_count > 1 and new_sym.body_node_count == 1:
                if new_sym.first_stmt_type in ("Pass", "Return"):
                    raise ASTViolationError(
                        f"Early return / dummy stub bypass detected in method '{new_sym.qualified_name}': "
                        f"substantive logic replaced with '{new_sym.first_stmt_type}' ({file_path})"
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
