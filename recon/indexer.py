import re
from pathlib import Path
from typing import Dict, List, Optional
import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Parser
from recon.database import EvidenceDatabase, SymbolRecord


class SymbolIndexer:
    def __init__(self, db: EvidenceDatabase):
        self.db = db
        self.language = Language(tscsharp.language())
        self.parser = Parser(self.language)

    def index_symbol_dict(self, symbol_data: dict, version: str):
        record = SymbolRecord(
            symbol_name=symbol_data["symbol_name"],
            class_name=symbol_data.get("class_name", ""),
            namespace=symbol_data.get("namespace", ""),
            signature=symbol_data.get("signature", ""),
            rva=symbol_data.get("rva", "0x0"),
            version=version,
            return_type=symbol_data.get("return_type", "void"),
            parameters=symbol_data.get("parameters", []),
        )
        self.db.insert_symbol(record)

    def index_call_edges(self, edges: List[dict], version: str):
        for edge in edges:
            self.db.insert_call_edge(edge["caller"], edge["callee"], version)

    def parse_il2cpp_dump_snippet(self, dump_text: str, version: str):
        """Deterministic Tree-sitter based parser for C# header snippets from Il2CppDumper.
        
        Handles:
        - Modifiers (public, private, static, async, virtual, override, etc.)
        - Generic return types (Task<Dictionary<string, List<T>>>, T[], Nullable<int>)
        - Attributes ([CustomAttribute(...)])
        - Generic methods (<U>, <T, U>)
        - Parameter modifiers (in, out, ref, params)
        - Nested types and namespaces
        - RVA metadata comments (// RVA: 0x1A2B3C)
        """
        if not dump_text or not dump_text.strip():
            return

        wrapped = dump_text
        has_class_decl = any(kw in dump_text for kw in ("class ", "struct ", "interface ", "record "))
        if not has_class_decl:
            wrapped = f"public class DumpClass {{\n{dump_text}\n}}"

        tree = self.parser.parse(wrapped.encode("utf-8"))
        tb = wrapped.encode("utf-8")
        parsed_count = 0

        # Check for top-level Namespace comment if not in AST
        top_ns_match = re.search(r"//\s*Namespace:\s*([\w\.]+)", dump_text)
        top_ns = top_ns_match.group(1) if top_ns_match else ""

        def walk(node, current_class="DumpClass", current_ns=top_ns):
            nonlocal parsed_count
            if node.type in ("class_declaration", "struct_declaration", "interface_declaration", "record_declaration"):
                for c in node.children:
                    if c.type == "identifier":
                        current_class = tb[c.start_byte:c.end_byte].decode("utf-8")
                        break
            elif node.type in ("namespace_declaration", "file_scoped_namespace_declaration"):
                for c in node.children:
                    if c.type in ("identifier", "qualified_name"):
                        current_ns = tb[c.start_byte:c.end_byte].decode("utf-8")
                        break
            elif node.type == "method_declaration":
                m_name = ""
                ret_type = "void"
                params = []
                gen_params = []

                # Find RVA from preceding comment / lines
                prefix = tb[max(0, node.start_byte - 200):node.start_byte].decode("utf-8", errors="ignore")
                rva_match = re.search(r"//\s*RVA:\s*(0x[0-9a-fA-F]+)", prefix)
                rva = rva_match.group(1) if rva_match else "0x0"

                for c in node.children:
                    if c.type == "identifier":
                        m_name = tb[c.start_byte:c.end_byte].decode("utf-8")
                    elif c.type == "type_parameter_list":
                        for tp in c.children:
                            if tp.type == "type_parameter":
                                gen_params.append(tb[tp.start_byte:tp.end_byte].decode("utf-8"))
                    elif c.type in ("predefined_type", "generic_name", "nullable_type", "array_type", "qualified_name") and not m_name:
                        ret_type = tb[c.start_byte:c.end_byte].decode("utf-8")
                    elif c.type == "parameter_list":
                        for p in c.children:
                            if p.type == "parameter":
                                params.append(tb[p.start_byte:p.end_byte].decode("utf-8").strip())

                if m_name:
                    gen_str = "<" + ",".join(gen_params) + ">" if gen_params else ""
                    params_str = ", ".join(params)
                    sig = f"{ret_type} {m_name}{gen_str}({params_str})"
                    sym_name = f"{current_class}.{m_name}" if current_class != "DumpClass" else m_name
                    
                    self.index_symbol_dict(
                        {
                            "symbol_name": sym_name,
                            "class_name": current_class,
                            "namespace": current_ns,
                            "signature": sig,
                            "rva": rva,
                            "return_type": ret_type,
                            "parameters": params,
                        },
                        version=version,
                    )
                    parsed_count += 1

            for c in node.children:
                walk(c, current_class, current_ns)

        walk(tree.root_node)

        # Fallback regex if Tree-sitter didn't find methods
        if parsed_count == 0:
            method_pattern = re.compile(
                r"//\s*RVA:\s*(0x[0-9a-fA-F]+).*?\n\s*(?:public|private|protected|internal)?\s*(?:static)?\s*([\w<>\[\], ]+?)\s+([\w]+)\s*\((.*?)\)",
                re.MULTILINE,
            )
            for match in method_pattern.finditer(dump_text):
                rva, ret_type, method_name, params_raw = match.groups()
                params = [p.strip() for p in params_raw.split(",") if p.strip()]
                sig = f"{ret_type.strip()} {method_name}({params_raw})"
                self.index_symbol_dict(
                    {
                        "symbol_name": method_name,
                        "class_name": "DumpClass",
                        "namespace": top_ns,
                        "signature": sig,
                        "rva": rva,
                        "return_type": ret_type.strip(),
                        "parameters": params,
                    },
                    version=version,
                )
