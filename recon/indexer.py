import re
from pathlib import Path
from typing import Dict, List, Optional
from recon.database import EvidenceDatabase, SymbolRecord


class SymbolIndexer:
    def __init__(self, db: EvidenceDatabase):
        self.db = db

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
        """Simple deterministic parser for C# header snippets from Il2CppDumper."""
        method_pattern = re.compile(
            r"//\s*RVA:\s*(0x[0-9a-fA-F]+).*?\n\s*(?:public|private|protected|internal)?\s*(?:static)?\s*([\w<>\[\]]+)\s+([\w]+)\s*\((.*?)\)",
            re.MULTILINE,
        )
        for match in method_pattern.finditer(dump_text):
            rva, ret_type, method_name, params_raw = match.groups()
            params = [p.strip() for p in params_raw.split(",") if p.strip()]
            sig = f"{ret_type} {method_name}({params_raw})"
            self.index_symbol_dict(
                {
                    "symbol_name": method_name,
                    "class_name": "DumpClass",
                    "signature": sig,
                    "rva": rva,
                    "return_type": ret_type,
                    "parameters": params,
                },
                version=version,
            )
