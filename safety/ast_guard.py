import re
from typing import Dict, List, Optional, Set
import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Parser, Node


class ASTViolationError(Exception):
    pass


class ASTGuard:
    def __init__(self):
        self.language = Language(tscsharp.language())
        self.parser = Parser(self.language)

    def _extract_declared_methods(self, root: Node, code_bytes: bytes) -> Set[str]:
        methods = set()
        stack = [root]
        while stack:
            curr = stack.pop()
            if curr.type == "method_declaration":
                # Find the method identifier
                for child in curr.children:
                    if child.type == "identifier":
                        name = code_bytes[child.start_byte : child.end_byte].decode("utf-8")
                        methods.add(name)
                        break
            stack.extend(curr.children)
        return methods

    def _extract_declared_classes(self, root: Node, code_bytes: bytes) -> Set[str]:
        classes = set()
        stack = [root]
        while stack:
            curr = stack.pop()
            if curr.type in ("class_declaration", "struct_declaration", "interface_declaration"):
                for child in curr.children:
                    if child.type == "identifier":
                        name = code_bytes[child.start_byte : child.end_byte].decode("utf-8")
                        classes.add(name)
                        break
            stack.extend(curr.children)
        return classes

    def _count_asserts(self, root: Node, code_bytes: bytes) -> int:
        count = 0
        stack = [root]
        while stack:
            curr = stack.pop()
            if curr.type == "invocation_expression":
                text = code_bytes[curr.start_byte : curr.end_byte].decode("utf-8", errors="ignore")
                if "assert" in text.lower():
                    count += 1
            stack.extend(curr.children)
        return count

    def _check_early_return_bypass(self, root: Node, code_bytes: bytes):
        """Detects if any method's first body statement is a dummy return or throw."""
        stack = [root]
        while stack:
            curr = stack.pop()
            if curr.type == "method_declaration":
                method_name = "unknown"
                for child in curr.children:
                    if child.type == "identifier":
                        method_name = code_bytes[child.start_byte : child.end_byte].decode("utf-8")
                    if child.type == "block":
                        # Examine statements in block
                        statements = [c for c in child.children if c.type not in ("{", "}", "comment")]
                        if statements:
                            first_stmt = statements[0]
                            # Check return or throw as first statement when original had more logic
                            if first_stmt.type in ("return_statement", "throw_statement"):
                                stmt_text = code_bytes[first_stmt.start_byte : first_stmt.end_byte].decode("utf-8")
                                if (
                                    "notimplementedexception" in stmt_text.lower()
                                    or len(statements) == 1
                                    and ("return null" in stmt_text or "return;" in stmt_text or "return false" in stmt_text)
                                ):
                                    raise ASTViolationError(
                                        f"Early return / dummy stub bypass detected in method '{method_name}': '{stmt_text.strip()}'"
                                    )
            stack.extend(curr.children)

    def validate_csharp_transition(self, old_code: str, new_code: str, file_path: str = ""):
        old_bytes = old_code.encode("utf-8")
        new_bytes = new_code.encode("utf-8")

        old_tree = self.parser.parse(old_bytes)
        new_tree = self.parser.parse(new_bytes)

        # 1. Check for syntax parsing errors in new code
        if new_tree.root_node.has_error:
            # Check if old code was already broken
            if not old_tree.root_node.has_error:
                raise ASTViolationError(f"Patch introduces syntax error in '{file_path}'.")

        # 2. Check for deleted classes / types
        old_classes = self._extract_declared_classes(old_tree.root_node, old_bytes)
        new_classes = self._extract_declared_classes(new_tree.root_node, new_bytes)
        deleted_classes = old_classes - new_classes
        if deleted_classes:
            raise ASTViolationError(
                f"Disallowed class/type deletion in '{file_path}': {deleted_classes}"
            )

        # 3. Check for deleted methods
        old_methods = self._extract_declared_methods(old_tree.root_node, old_bytes)
        new_methods = self._extract_declared_methods(new_tree.root_node, new_bytes)
        deleted_methods = old_methods - new_methods
        if deleted_methods:
            raise ASTViolationError(
                f"Disallowed method deletion in '{file_path}': {deleted_methods}"
            )

        # 4. Check for assertion removal / weakening
        old_asserts = self._count_asserts(old_tree.root_node, old_bytes)
        new_asserts = self._count_asserts(new_tree.root_node, new_bytes)
        if new_asserts < old_asserts:
            raise ASTViolationError(
                f"Assertion weakening/removal detected in '{file_path}': count dropped from {old_asserts} to {new_asserts}"
            )

        # 5. Check for early return / dummy stubs in new code
        self._check_early_return_bypass(new_tree.root_node, new_bytes)
