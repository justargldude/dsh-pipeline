import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from tree_sitter import Language, Parser, Node

from safety.tree_sitter_shared import get_csharp_language
from safety.languages.base import BaseLanguageDriver, ASTViolationError, PurityReport


@dataclass
class CSharpSymbol:
    kind: str  # "method", "constructor", "property", "class", "struct", "interface", "record", "enum"
    name: str
    enclosing_type: str
    namespace: str
    parameter_types: List[str] = field(default_factory=list)
    parameters: List[str] = field(default_factory=list)
    generic_params: List[str] = field(default_factory=list)
    full_text: str = ""
    body_text: str = ""
    statement_count: int = 0
    has_not_implemented: bool = False
    first_stmt_type: str = ""
    first_stmt_text: str = ""

    @property
    def qualified_name(self) -> str:
        if self.enclosing_type:
            return f"{self.enclosing_type}.{self.name}"
        return self.name

    @property
    def full_qualified_name(self) -> str:
        parts = []
        if self.namespace:
            parts.append(self.namespace)
        if self.enclosing_type:
            parts.append(self.enclosing_type)
        parts.append(self.name)
        return ".".join(parts)

    @property
    def signature(self) -> str:
        gen = f"<{','.join(self.generic_params)}>" if self.generic_params else ""
        if self.kind in ("method", "constructor"):
            params_str = ",".join(self.parameter_types)
            return f"{self.qualified_name}{gen}({params_str})"
        return f"{self.qualified_name}{gen}"

    @property
    def identity_key(self) -> str:
        gen = f"<{','.join(self.generic_params)}>" if self.generic_params else ""
        if self.kind in ("method", "constructor"):
            params_str = ",".join(self.parameter_types)
            return f"{self.kind}:{self.full_qualified_name}{gen}({params_str})"
        return f"{self.kind}:{self.full_qualified_name}{gen}"

    def matches_target(self, target_pattern: str) -> bool:
        """Determines if this symbol matches a target symbol pattern."""
        target = target_pattern.strip().replace(" ", "")
        
        names = {
            self.name,
            self.qualified_name,
            self.full_qualified_name,
        }

        if "(" in target and target.endswith(")"):
            param_types_str = ",".join(self.parameter_types)
            param_full_str = ",".join(self.parameters).replace(" ", "")
            
            sig_candidates = {
                f"{self.name}({param_types_str})",
                f"{self.qualified_name}({param_types_str})",
                f"{self.full_qualified_name}({param_types_str})",
                f"{self.name}({param_full_str})",
                f"{self.qualified_name}({param_full_str})",
                f"{self.full_qualified_name}({param_full_str})",
            }
            if self.generic_params:
                gen = f"<{','.join(self.generic_params)}>"
                sig_candidates.update({
                    f"{self.name}{gen}({param_types_str})",
                    f"{self.qualified_name}{gen}({param_types_str})",
                    f"{self.full_qualified_name}{gen}({param_types_str})",
                })
            return target in sig_candidates

        if "<" in target and target.endswith(">"):
            if self.generic_params:
                gen = f"<{','.join(self.generic_params)}>"
                gen_candidates = {
                    f"{self.name}{gen}",
                    f"{self.qualified_name}{gen}",
                    f"{self.full_qualified_name}{gen}",
                }
                if target in gen_candidates:
                    return True

        return target in names


class CSharpDriver(BaseLanguageDriver):
    TAUTOLOGICAL_ASSERT_PATTERNS = {
        "true",
        "true==true",
        "1==1",
        "0==0",
        "null==null",
        "\"\"!=null",
    }

    # Functional Core, Imperative Shell (FCIS) purity restrictions for C#
    IMPURE_CALL_PATTERNS = [
        (re.compile(r"\b(File|Directory|FileStream|StreamWriter|StreamReader|FileInfo|DirectoryInfo)\b"), "File/Directory I/O operation"),
        (re.compile(r"\b(Socket|TcpClient|UdpClient|HttpClient|HttpWebRequest|WebClient)\b"), "Network socket/HTTP call"),
        (re.compile(r"\bDateTime\.(Now|UtcNow|Today)\b"), "Non-deterministic system clock access (DateTime.Now/UtcNow)"),
        (re.compile(r"\bDateTimeOffset\.(Now|UtcNow)\b"), "Non-deterministic system clock access (DateTimeOffset.Now/UtcNow)"),
        (re.compile(r"\bnew\s+Random\s*\(\s*\)"), "Unseeded pseudo-random generator instantiation"),
        (re.compile(r"\bEnvironment\.(Exit|FailFast|TickCount)\b"), "Process termination or tick count access"),
        (re.compile(r"\bProcess\.Start\b"), "External process invocation"),
        (re.compile(r"\bConsole\.(Write|WriteLine|ReadLine|ReadKey)\b"), "Console standard I/O stream access"),
    ]

    def __init__(self):
        self.language = get_csharp_language()
        self.parser = Parser(self.language)

    @property
    def language_id(self) -> str:
        return "csharp"

    @property
    def supported_extensions(self) -> List[str]:
        return [".cs"]

    def parse_ast(self, code: str) -> Any:
        return self.parser.parse(code.encode("utf-8"))

    def get_default_build_cmd(self) -> str:
        return "dotnet build"

    def get_default_test_cmd(self) -> str:
        return "dotnet test"

    def get_pbt_runner_cmd(self) -> str:
        return "dotnet test --filter Category=PropertyBased"

    def _get_enclosing_type(self, node: Node, code_bytes: bytes) -> Tuple[str, str]:
        types = []
        namespaces = []
        curr = node.parent
        while curr:
            if curr.type in ("class_declaration", "struct_declaration", "interface_declaration", "record_declaration"):
                for child in curr.children:
                    if child.type == "identifier":
                        types.append(code_bytes[child.start_byte:child.end_byte].decode("utf-8"))
                        break
            elif curr.type in ("namespace_declaration", "file_scoped_namespace_declaration"):
                for child in curr.children:
                    if child.type in ("identifier", "qualified_name"):
                        namespaces.append(code_bytes[child.start_byte:child.end_byte].decode("utf-8"))
                        break
            curr = curr.parent
        return (".".join(reversed(types)), ".".join(reversed(namespaces)))

    def _extract_symbols(self, root: Node, code_bytes: bytes) -> List[CSharpSymbol]:
        symbols: List[CSharpSymbol] = []
        stack = [root]

        while stack:
            curr = stack.pop()

            if curr.type in ("class_declaration", "struct_declaration", "interface_declaration", "record_declaration", "enum_declaration"):
                name = ""
                gen_params = []
                for child in curr.children:
                    if child.type == "identifier":
                        name = code_bytes[child.start_byte:child.end_byte].decode("utf-8")
                    elif child.type == "type_parameter_list":
                        for tp in child.children:
                            if tp.type == "type_parameter":
                                gen_params.append(code_bytes[tp.start_byte:tp.end_byte].decode("utf-8"))
                
                enclosing_type, namespace = self._get_enclosing_type(curr, code_bytes)
                kind = curr.type.replace("_declaration", "")
                sym = CSharpSymbol(
                    kind=kind,
                    name=name,
                    enclosing_type=enclosing_type,
                    namespace=namespace,
                    generic_params=gen_params,
                    full_text=code_bytes[curr.start_byte:curr.end_byte].decode("utf-8", errors="replace"),
                )
                symbols.append(sym)

            elif curr.type in ("method_declaration", "constructor_declaration"):
                name = ""
                gen_params = []
                params = []
                param_types = []
                body_text = ""
                stmt_count = 0
                first_stmt_type = ""
                first_stmt_text = ""
                has_not_implemented = False

                for child in curr.children:
                    if child.type == "identifier":
                        name = code_bytes[child.start_byte:child.end_byte].decode("utf-8")
                    elif child.type == "type_parameter_list":
                        for tp in child.children:
                            if tp.type == "type_parameter":
                                gen_params.append(code_bytes[tp.start_byte:tp.end_byte].decode("utf-8"))
                    elif child.type == "parameter_list":
                        for p in child.children:
                            if p.type == "parameter":
                                p_str = code_bytes[p.start_byte:p.end_byte].decode("utf-8").strip()
                                params.append(p_str)
                                parts = p_str.split()
                                if len(parts) >= 2:
                                    param_types.append(parts[-2])
                                else:
                                    param_types.append(parts[0])
                    elif child.type in ("block", "arrow_expression_clause"):
                        body_text = code_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
                        if "NotImplementedException" in body_text:
                            has_not_implemented = True

                        if child.type == "block":
                            stmts = [c for c in child.children if c.type.endswith("_statement")]
                            stmt_count = len(stmts)
                            if stmts:
                                first_stmt_type = stmts[0].type
                                first_stmt_text = code_bytes[stmts[0].start_byte:stmts[0].end_byte].decode("utf-8", errors="replace").strip()
                        elif child.type == "arrow_expression_clause":
                            stmt_count = 1
                            first_stmt_type = "expression_arrow"
                            first_stmt_text = body_text.strip()

                enclosing_type, namespace = self._get_enclosing_type(curr, code_bytes)
                kind = "constructor" if curr.type == "constructor_declaration" else "method"

                sym = CSharpSymbol(
                    kind=kind,
                    name=name,
                    enclosing_type=enclosing_type,
                    namespace=namespace,
                    parameter_types=param_types,
                    parameters=params,
                    generic_params=gen_params,
                    full_text=code_bytes[curr.start_byte:curr.end_byte].decode("utf-8", errors="replace"),
                    body_text=body_text,
                    statement_count=stmt_count,
                    has_not_implemented=has_not_implemented,
                    first_stmt_type=first_stmt_type,
                    first_stmt_text=first_stmt_text,
                )
                symbols.append(sym)

            for child in curr.children:
                if child.type in (
                    "namespace_declaration", "file_scoped_namespace_declaration",
                    "class_declaration", "struct_declaration", "interface_declaration", "record_declaration",
                    "declaration_list", "method_declaration", "constructor_declaration"
                ):
                    stack.append(child)

        return symbols

    def _extract_assert_predicates(self, root: Node, code_bytes: bytes) -> List[Tuple[str, str, bool]]:
        asserts = []
        stack = [root]

        while stack:
            curr = stack.pop()

            if curr.type == "invocation_expression":
                full_call = code_bytes[curr.start_byte:curr.end_byte].decode("utf-8", errors="replace")
                
                is_assert_call = False
                for prefix in ("Assert.", "Debug.Assert", "Trace.Assert", "Contract.Assert"):
                    if prefix in full_call:
                        is_assert_call = True
                        break

                if is_assert_call:
                    arg_list = None
                    for child in curr.children:
                        if child.type == "argument_list":
                            arg_list = child
                            break

                    if arg_list and len(arg_list.children) > 1:
                        first_arg = None
                        for child in arg_list.children:
                            if child.type == "argument":
                                first_arg = child
                                break

                        if first_arg:
                            arg_text = code_bytes[first_arg.start_byte:first_arg.end_byte].decode("utf-8", errors="replace").strip()
                            norm_arg = arg_text.replace(" ", "").replace("(", "").replace(")", "").lower()
                            is_taut = norm_arg in self.TAUTOLOGICAL_ASSERT_PATTERNS
                            asserts.append((full_call, arg_text, is_taut))
                        else:
                            asserts.append((full_call, "", False))
                    else:
                        asserts.append((full_call, "", False))

            for child in curr.children:
                stack.append(child)

        return asserts

    def _check_early_return_and_stubs(
        self,
        old_symbols: List[CSharpSymbol],
        new_symbols: List[CSharpSymbol],
        file_path: str,
    ):
        old_sym_map = {s.identity_key: s for s in old_symbols}

        for new_sym in new_symbols:
            if new_sym.kind not in ("method", "constructor"):
                continue

            if new_sym.has_not_implemented:
                raise ASTViolationError(
                    f"Early return / dummy stub bypass detected in method '{new_sym.qualified_name}': NotImplementedException ({file_path})"
                )

            if new_sym.statement_count > 1 and new_sym.first_stmt_type == "return_statement":
                raise ASTViolationError(
                    f"Early return bypass detected in method '{new_sym.qualified_name}': injected '{new_sym.first_stmt_text}' ({file_path})"
                )

            old_sym = old_sym_map.get(new_sym.identity_key)
            if old_sym and old_sym.statement_count > 1:
                if new_sym.statement_count == 1 and new_sym.first_stmt_type == "return_statement":
                    norm_stmt = new_sym.first_stmt_text.replace(" ", "").rstrip(";").lower()
                    if norm_stmt in ("return", "returnnull", "returnfalse", "returntrue", "return0", "returndefault"):
                        raise ASTViolationError(
                            f"Early return / dummy stub bypass detected in method '{new_sym.qualified_name}': "
                            f"substantive logic replaced with '{new_sym.first_stmt_text}' ({file_path})"
                        )

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
            raise ASTViolationError(f"Patch introduces C# syntax error in '{file_path}'.")

        old_symbols = self._extract_symbols(old_tree.root_node, old_bytes)
        new_symbols = self._extract_symbols(new_tree.root_node, new_bytes)

        old_sym_map = {s.identity_key: s for s in old_symbols}
        new_sym_map = {s.identity_key: s for s in new_symbols}

        old_types = {s.identity_key: s for s in old_symbols if s.kind in ("class", "struct", "interface", "record")}
        new_types = {s.identity_key: s for s in new_symbols if s.kind in ("class", "struct", "interface", "record")}
        deleted_types = set(old_types.keys()) - set(new_types.keys())
        if deleted_types:
            deleted_names = [old_types[k].qualified_name for k in deleted_types]
            raise ASTViolationError(f"Disallowed class/type deletion in '{file_path}': {deleted_names}")

        old_methods = {s.identity_key: s for s in old_symbols if s.kind in ("method", "constructor")}
        new_methods = {s.identity_key: s for s in new_symbols if s.kind in ("method", "constructor")}
        deleted_methods = set(old_methods.keys()) - set(new_methods.keys())
        if target_symbols:
            new_method_fqns = {s.full_qualified_name for s in new_methods.values()}
            deleted_methods = {
                k for k in deleted_methods
                if not (
                    any(old_methods[k].matches_target(ts) for ts in target_symbols)
                    and old_methods[k].full_qualified_name in new_method_fqns
                )
            }
        if deleted_methods:
            deleted_names = [old_methods[k].signature for k in deleted_methods]
            raise ASTViolationError(f"Disallowed method deletion in '{file_path}': {deleted_names}")

        if target_symbols:
            changed_symbols: List[CSharpSymbol] = []

            for k, new_sym in new_sym_map.items():
                if k not in old_sym_map:
                    changed_symbols.append(new_sym)
                else:
                    old_sym = old_sym_map[k]
                    if new_sym.full_text != old_sym.full_text:
                        changed_symbols.append(new_sym)

            for sym in changed_symbols:
                if sym.kind in ("class", "struct", "interface", "record"):
                    continue

                matched = any(sym.matches_target(ts) for ts in target_symbols)
                if not matched:
                    raise ASTViolationError(
                        f"Target symbol violation in '{file_path}': Symbol '{sym.qualified_name}' "
                        f"({sym.signature}) was modified but is not in allowed target_symbols: {target_symbols}"
                    )

        old_asserts = self._extract_assert_predicates(old_tree.root_node, old_bytes)
        new_asserts = self._extract_assert_predicates(new_tree.root_node, new_bytes)

        old_non_trivial = [a for a in old_asserts if not a[2]]
        new_non_trivial = [a for a in new_asserts if not a[2]]

        for call_text, pred, is_taut in new_asserts:
            if is_taut:
                if old_non_trivial:
                    raise ASTViolationError(
                        f"Assertion semantic weakening detected in '{file_path}': "
                        f"Assertion weakened to tautology '{call_text}'"
                    )

        if len(new_non_trivial) < len(old_non_trivial):
            raise ASTViolationError(
                f"Assertion weakening/removal detected in '{file_path}': "
                f"non-trivial assertion count decreased from {len(old_non_trivial)} to {len(new_non_trivial)}"
            )

        self._check_early_return_and_stubs(old_symbols, new_symbols, file_path)

    def check_purity(
        self,
        code: str,
        file_path: str = "",
        pure_symbols: Optional[List[str]] = None,
    ) -> PurityReport:
        """Enforces Functional Core purity by verifying no side-effecting APIs are invoked."""
        violations: List[str] = []

        for pattern, desc in self.IMPURE_CALL_PATTERNS:
            match = pattern.search(code)
            if match:
                violations.append(f"Forbidden impure call '{match.group(0)}' in '{file_path}': {desc}")

        return PurityReport(is_pure=len(violations) == 0, violations=violations)
