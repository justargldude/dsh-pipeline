import re
from typing import Dict, List, Optional, Tuple
import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Parser
from context.budget import TokenBudgetManager


class ExtractedSourceResult:
    def __init__(
        self,
        extracted_code: str,
        symbols_found: List[str],
        extraction_method: str,
        is_truncated: bool = False,
    ):
        self.extracted_code = extracted_code
        self.symbols_found = symbols_found
        self.extraction_method = extraction_method
        self.is_truncated = is_truncated


class SymbolExtractor:
    """Deterministic extractor for targeted C# source symbols using Tree-sitter with regex fallback."""

    def __init__(self):
        self.language = Language(tscsharp.language())
        self.parser = Parser(self.language)

    @staticmethod
    def _normalize_symbol_name(symbol: str) -> str:
        """Extract the base identifier or member name from fully qualified names."""
        parts = symbol.replace("::", ".").split(".")
        return parts[-1].strip()

    def extract_relevant_source(
        self,
        source_code: str,
        target_symbols: Optional[List[str]] = None,
        max_tokens: Optional[int] = None,
        file_name: str = "",
    ) -> ExtractedSourceResult:
        """Extracts targeted symbol definitions and enclosing context from source code.
        
        If target_symbols is empty or full source fits within max_tokens, returns full source.
        Otherwise extracts relevant methods/classes and surrounding context.
        """
        if not source_code or not source_code.strip():
            return ExtractedSourceResult(
                extracted_code=source_code,
                symbols_found=[],
                extraction_method="empty",
                is_truncated=False,
            )

        full_tokens = TokenBudgetManager.estimate_tokens(source_code)
        if (not target_symbols) and (max_tokens is None or full_tokens <= max_tokens):
            return ExtractedSourceResult(
                extracted_code=source_code,
                symbols_found=[],
                extraction_method="full_source",
                is_truncated=False,
            )

        if not target_symbols and max_tokens is not None and full_tokens > max_tokens:
            # Deterministic chunking when no target symbols specified but file exceeds budget
            lines = source_code.splitlines(keepends=True)
            accumulated = []
            cur_tokens = 0
            omitted_count = 0
            for idx, line in enumerate(lines):
                t = TokenBudgetManager.estimate_tokens(line)
                if cur_tokens + t <= max_tokens - 50:  # reserve header/footer tokens
                    accumulated.append(line)
                    cur_tokens += t
                else:
                    omitted_count = len(lines) - idx
                    break

            result_text = "".join(accumulated) + f"\n// ... [{omitted_count} lines truncated due to budget limit] ...\n"
            return ExtractedSourceResult(
                extracted_code=result_text,
                symbols_found=[],
                extraction_method="deterministic_chunk",
                is_truncated=True,
            )

        # We have target_symbols to locate
        normalized_targets = {self._normalize_symbol_name(s): s for s in (target_symbols or [])}
        extracted_sections = []
        found_symbols = []

        # 1. Try Tree-sitter extraction
        try:
            tree = self.parser.parse(source_code.encode("utf-8"))
            tb = source_code.encode("utf-8")

            # Extract imports / using directives
            using_lines = []
            for line in source_code.splitlines():
                stripped = line.strip()
                if stripped.startswith("using ") and stripped.endswith(";"):
                    using_lines.append(stripped)

            # Locate symbol AST nodes
            matched_nodes = []

            def find_symbols(node):
                if node.type in ("method_declaration", "constructor_declaration", "property_declaration", "class_declaration", "struct_declaration", "interface_declaration", "enum_declaration"):
                    name = ""
                    for child in node.children:
                        if child.type == "identifier":
                            name = tb[child.start_byte:child.end_byte].decode("utf-8", errors="ignore")
                            break
                    if name in normalized_targets:
                        matched_nodes.append((name, node))
                for child in node.children:
                    find_symbols(child)

            find_symbols(tree.root_node)

            if matched_nodes:
                for name, node in matched_nodes:
                    found_symbols.append(normalized_targets.get(name, name))
                    # Get enclosing class / namespace if available
                    parent = node.parent
                    parent_header = ""
                    while parent:
                        if parent.type in ("class_declaration", "struct_declaration", "interface_declaration"):
                            # Get class declaration line
                            p_bytes = tb[parent.start_byte:parent.end_byte].decode("utf-8", errors="ignore")
                            first_line = p_bytes.split("\n", 1)[0]
                            parent_header = first_line + " { ... }"
                            break
                        parent = parent.parent

                    snippet = tb[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")
                    extracted_sections.append({
                        "name": name,
                        "parent": parent_header,
                        "code": snippet,
                    })

                # Assemble extracted code
                parts = []
                if using_lines:
                    parts.append("\n".join(using_lines))
                    parts.append("")

                for sec in extracted_sections:
                    if sec["parent"]:
                        parts.append(f"// Enclosing: {sec['parent']}")
                    parts.append(sec["code"])
                    parts.append("")

                combined_code = "\n".join(parts).strip()
                return ExtractedSourceResult(
                    extracted_code=combined_code,
                    symbols_found=found_symbols,
                    extraction_method="tree_sitter_symbol",
                    is_truncated=True,
                )
        except Exception:
            pass

        # 2. Fallback regex extraction if Tree-sitter did not match or failed
        lines = source_code.splitlines()
        found_blocks = []
        for target_norm, full_name in normalized_targets.items():
            pattern = re.compile(rf"\b(class|struct|interface|void|int|string|bool|float|double|Task|ValueTask|public|private|protected|internal|static|async)\s+.*?\b{re.escape(target_norm)}\b", re.IGNORECASE)
            for idx, line in enumerate(lines):
                if pattern.search(line):
                    # Capture surrounding block (up to 30 lines or closing brace)
                    start_idx = max(0, idx - 3)
                    end_idx = min(len(lines), idx + 35)
                    block = "\n".join(lines[start_idx:end_idx])
                    found_blocks.append(f"// Target Symbol Excerpt: {full_name}\n{block}")
                    found_symbols.append(full_name)
                    break

        if found_blocks:
            combined = "\n\n// ... [omitted lines] ...\n\n".join(found_blocks)
            return ExtractedSourceResult(
                extracted_code=combined,
                symbols_found=found_symbols,
                extraction_method="regex_fallback_symbol",
                is_truncated=True,
            )

        # 3. Last fallback: return truncated or full source
        if max_tokens and full_tokens > max_tokens:
            truncated = source_code[: int(max_tokens * 3.5)] + "\n// ... [truncated] ..."
            return ExtractedSourceResult(
                extracted_code=truncated,
                symbols_found=[],
                extraction_method="fallback_truncate",
                is_truncated=True,
            )

        return ExtractedSourceResult(
            extracted_code=source_code,
            symbols_found=[],
            extraction_method="full_source",
            is_truncated=False,
        )
