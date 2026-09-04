import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from tree_sitter import Language, Parser, Node
from safety.tree_sitter_shared import get_csharp_language


class ASTViolationError(Exception):
    """Raised when an AST-level safety or scope invariant is violated."""
    pass


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
        
        # Candidate identifiers for this symbol
        names = {
            self.name,
            self.qualified_name,
            self.full_qualified_name,
        }

        # If target has parameter specification: e.g. "Player.Update(int)" or "Update(int)" or "Player.Update(int delta)"
        if "(" in target and target.endswith(")"):
            # Construct candidate signatures
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

        # If target has generic arity / spec without parens: e.g. "Inventory<T>" or "Inventory`1"
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

        # Target is a name-level pattern: e.g. "Player.Update" or "Update" or "Player"
        return target in names


class ASTGuard:
    TAUTOLOGICAL_ASSERT_PATTERNS = {
        "true",
        "true==true",
        "1==1",
        "0==0",
        "null==null",
        "\"\"!=null",
    }

    def __init__(self):
        # Shared cached Language instance (Opt 8.3); Parser stays per-instance (lightweight).
        self.language = get_csharp_language()
        self.parser = Parser(self.language)

    def _get_enclosing_type(self, node: Node, code_bytes: bytes) -> Tuple[str, str]:
        """Returns (enclosing_type, namespace) for a given AST node."""
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
        """Extracts all declared symbols with detailed signatures from C# AST."""
        symbols: List[CSharpSymbol] = []
        stack = [root]

        while stack:
            curr = stack.pop()

            # Classes, structs, interfaces, records, enums
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

            # Methods and constructors
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
                                # Extract parameter type
                                for pchild in p.children:
                                    if pchild.type in ("predefined_type", "identifier", "generic_name", "nullable_type", "array_type"):
                                        param_types.append(code_bytes[pchild.start_byte:pchild.end_byte].decode("utf-8"))
                                        break
                    elif child.type in ("block", "arrow_expression_clause"):
                        body_text = code_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
                        if "notimplementedexception" in body_text.lower():
                            has_not_implemented = True

                        if child.type == "block":
                            stmts = [c for c in child.children if c.type not in ("{", "}", "comment")]
                            stmt_count = len(stmts)
                            if stmts:
                                first_stmt_type = stmts[0].type
                                first_stmt_text = code_bytes[stmts[0].start_byte:stmts[0].end_byte].decode("utf-8", errors="replace").strip()
                        elif child.type == "arrow_expression_clause":
                            stmt_count = 1
                            first_stmt_type = "arrow_expression"
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

            # Properties
            elif curr.type == "property_declaration":
                name = ""
                body_text = ""
                for child in curr.children:
                    if child.type == "identifier":
                        name = code_bytes[child.start_byte:child.end_byte].decode("utf-8")
                    elif child.type in ("accessor_list", "arrow_expression_clause"):
                        body_text = code_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace")

                enclosing_type, namespace = self._get_enclosing_type(curr, code_bytes)
                sym = CSharpSymbol(
                    kind="property",
                    name=name,
                    enclosing_type=enclosing_type,
                    namespace=namespace,
                    full_text=code_bytes[curr.start_byte:curr.end_byte].decode("utf-8", errors="replace"),
                    body_text=body_text,
                )
                symbols.append(sym)

            stack.extend(curr.children)

        return symbols

    def _extract_assert_predicates(self, root: Node, code_bytes: bytes) -> List[Tuple[str, str, bool]]:
        """Extracts assertion invocations, returning list of (call_text, predicate_text, is_tautology)."""
        asserts = []
        stack = [root]

        while stack:
            curr = stack.pop()
            if curr.type == "invocation_expression":
                call_text = code_bytes[curr.start_byte:curr.end_byte].decode("utf-8", errors="replace")
                # Only classify as an assertion by the CALLEE text, not by the
                # whole invocation (arguments may contain the word "assert" in
                # an unrelated string literal, e.g. Logger.Warn("removed assert check")).
                func_node = curr.child_by_field_name("function")
                if func_node is not None:
                    callee_text = code_bytes[func_node.start_byte:func_node.end_byte].decode("utf-8", errors="replace")
                    if "assert" in callee_text.lower():
                        # Extract argument list
                        predicate_text = ""
                        for child in curr.children:
                            if child.type == "argument_list":
                                args = [
                                    code_bytes[arg.start_byte:arg.end_byte].decode("utf-8", errors="replace").strip()
                                    for arg in child.children
                                    if arg.type == "argument"
                                ]
                                if args:
                                    predicate_text = args[0]
                                break

                        cleaned_pred = predicate_text.replace(" ", "").lower()
                        is_tautology = cleaned_pred in self.TAUTOLOGICAL_ASSERT_PATTERNS
                        asserts.append((call_text, predicate_text, is_tautology))

            stack.extend(curr.children)

        return asserts

    def _check_early_return_and_stubs(
        self,
        old_symbols: List[CSharpSymbol],
        new_symbols: List[CSharpSymbol],
        file_path: str,
    ):
        """Detects dummy stubs or early return bypasses replacing substantive logic."""
        old_sym_map = {s.identity_key: s for s in old_symbols}

        for new_sym in new_symbols:
            if new_sym.kind not in ("method", "constructor"):
                continue

            # 1. Reject NotImplementedException placeholders
            if new_sym.has_not_implemented:
                raise ASTViolationError(
                    f"Early return / dummy stub bypass detected in method '{new_sym.qualified_name}': NotImplementedException ({file_path})"
                )

            # 2. Check if first statement is injected return before multiple statements
            if new_sym.statement_count > 1 and new_sym.first_stmt_type == "return_statement":
                raise ASTViolationError(
                    f"Early return bypass detected in method '{new_sym.qualified_name}': injected '{new_sym.first_stmt_text}' ({file_path})"
                )

            # 3. Check if substantive existing method (>1 statement or substantive work) was stubbed out
            old_sym = old_sym_map.get(new_sym.identity_key)
            if old_sym and old_sym.statement_count > 1:
                if new_sym.statement_count == 1 and new_sym.first_stmt_type == "return_statement":
                    norm_stmt = new_sym.first_stmt_text.replace(" ", "").rstrip(";").lower()
                    if norm_stmt in ("return", "returnnull", "returnfalse", "returntrue", "return0", "returndefault"):
                        raise ASTViolationError(
                            f"Early return / dummy stub bypass detected in method '{new_sym.qualified_name}': "
                            f"substantive logic replaced with '{new_sym.first_stmt_text}' ({file_path})"
                        )

    def validate_csharp_transition(
        self,
        old_code: str,
        new_code: str,
        file_path: str = "",
        target_symbols: Optional[List[str]] = None,
    ):
        """Comprehensive AST validation across code transitions.
        
        Enforces:
        1. Valid C# syntax without introduced parse errors.
        2. Disallowed deletion of classes, interfaces, structs, or methods.
        3. Target symbol boundary enforcement (if target_symbols is non-empty, only matching symbols may change).
        4. Overload distinction and precision.
        5. Assertion semantic validation (rejects weakening/removal/tautologies like Assert(true)).
        6. Early return & dummy stub bypass detection.
        """
        old_bytes = old_code.encode("utf-8")
        new_bytes = new_code.encode("utf-8")

        old_tree = self.parser.parse(old_bytes)
        new_tree = self.parser.parse(new_bytes)

        # 1. Syntax error check
        if new_tree.root_node.has_error and not old_tree.root_node.has_error:
            raise ASTViolationError(f"Patch introduces C# syntax error in '{file_path}'.")

        old_symbols = self._extract_symbols(old_tree.root_node, old_bytes)
        new_symbols = self._extract_symbols(new_tree.root_node, new_bytes)

        old_sym_map = {s.identity_key: s for s in old_symbols}
        new_sym_map = {s.identity_key: s for s in new_symbols}

        # 2. Check for deleted types/classes
        old_types = {s.identity_key: s for s in old_symbols if s.kind in ("class", "struct", "interface", "record")}
        new_types = {s.identity_key: s for s in new_symbols if s.kind in ("class", "struct", "interface", "record")}
        deleted_types = set(old_types.keys()) - set(new_types.keys())
        if deleted_types:
            deleted_names = [old_types[k].qualified_name for k in deleted_types]
            raise ASTViolationError(f"Disallowed class/type deletion in '{file_path}': {deleted_names}")

        # 3. Check for deleted methods
        old_methods = {s.identity_key: s for s in old_symbols if s.kind in ("method", "constructor")}
        new_methods = {s.identity_key: s for s in new_symbols if s.kind in ("method", "constructor")}
        deleted_methods = set(old_methods.keys()) - set(new_methods.keys())
        if target_symbols:
            # An identity_key that vanished solely because a target symbol's
            # signature changed is not a real deletion: exclude keys whose OLD
            # symbol matches a target pattern AND a new method with the same
            # fully-qualified name still exists (re-declared with a new
            # signature). True deletions (no new method of that name) still
            # raise below, and step 4 keeps enforcing the target boundary for
            # the newly added signature.
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

        # 4. Target symbol enforcement
        if target_symbols:
            # Find changed or added symbols
            changed_symbols: List[CSharpSymbol] = []

            for k, new_sym in new_sym_map.items():
                if k not in old_sym_map:
                    changed_symbols.append(new_sym)
                else:
                    old_sym = old_sym_map[k]
                    if new_sym.full_text != old_sym.full_text:
                        changed_symbols.append(new_sym)

            for sym in changed_symbols:
                # If symbol is a container class/struct and only its children changed, that is checked at child level
                if sym.kind in ("class", "struct", "interface", "record"):
                    continue

                matched = any(sym.matches_target(ts) for ts in target_symbols)
                if not matched:
                    raise ASTViolationError(
                        f"Target symbol violation in '{file_path}': Symbol '{sym.qualified_name}' "
                        f"({sym.signature}) was modified but is not in allowed target_symbols: {target_symbols}"
                    )

        # 5. Assertion semantic validation
        old_asserts = self._extract_assert_predicates(old_tree.root_node, old_bytes)
        new_asserts = self._extract_assert_predicates(new_tree.root_node, new_bytes)

        old_non_trivial = [a for a in old_asserts if not a[2]]
        new_non_trivial = [a for a in new_asserts if not a[2]]

        # Check for introduced tautological assertions (e.g. Debug.Assert(true))
        for call_text, pred, is_taut in new_asserts:
            if is_taut:
                # Check if old code had a non-tautological assert that was replaced
                if old_non_trivial:
                    raise ASTViolationError(
                        f"Assertion semantic weakening detected in '{file_path}': "
                        f"Assertion weakened to tautology '{call_text}'"
                    )

        # Check for count drop in non-trivial assertions
        if len(new_non_trivial) < len(old_non_trivial):
            raise ASTViolationError(
                f"Assertion weakening/removal detected in '{file_path}': "
                f"non-trivial assertion count decreased from {len(old_non_trivial)} to {len(new_non_trivial)}"
            )

        # 6. Early return and dummy stub bypass check
        self._check_early_return_and_stubs(old_symbols, new_symbols, file_path)
