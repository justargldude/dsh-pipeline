import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import duckdb
from pydantic import BaseModel, Field


class SymbolRecord(BaseModel):
    symbol_name: str
    class_name: str
    namespace: str = ""
    signature: str
    rva: str = "0x0"
    version: str
    return_type: str = "void"
    parameters: List[str] = Field(default_factory=list)


class CrossVersionMatchRecord(BaseModel):
    old_symbol: str
    new_symbol: str
    old_version: str
    new_version: str
    name_similarity: float
    signature_similarity: float
    caller_similarity: float
    field_similarity: float
    confidence: float
    verified: bool = False


class EvidenceDatabase:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = str(db_path) if db_path else ":memory:"
        self.conn = duckdb.connect(self.db_path)
        self._init_tables()

    def _init_tables(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS symbols (
                symbol_name VARCHAR,
                class_name VARCHAR,
                namespace VARCHAR,
                signature VARCHAR,
                rva VARCHAR,
                version VARCHAR,
                return_type VARCHAR,
                parameters JSON,
                PRIMARY KEY (symbol_name, version)
            );
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS call_graph (
                caller_symbol VARCHAR,
                callee_symbol VARCHAR,
                version VARCHAR,
                PRIMARY KEY (caller_symbol, callee_symbol, version)
            );
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS cross_version_mappings (
                old_symbol VARCHAR,
                new_symbol VARCHAR,
                old_version VARCHAR,
                new_version VARCHAR,
                name_similarity DOUBLE,
                signature_similarity DOUBLE,
                caller_similarity DOUBLE,
                field_similarity DOUBLE,
                confidence DOUBLE,
                verified BOOLEAN,
                PRIMARY KEY (old_symbol, new_symbol, old_version, new_version)
            );
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS evidence_records (
                task_id VARCHAR,
                target_symbol VARCHAR,
                evidence_json JSON,
                confidence DOUBLE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

    def insert_symbol(self, record: SymbolRecord):
        self.conn.execute(
            """
            INSERT OR REPLACE INTO symbols 
            (symbol_name, class_name, namespace, signature, rva, version, return_type, parameters)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            [
                record.symbol_name,
                record.class_name,
                record.namespace,
                record.signature,
                record.rva,
                record.version,
                record.return_type,
                json.dumps(record.parameters),
            ],
        )

    def insert_call_edge(self, caller: str, callee: str, version: str):
        self.conn.execute(
            """
            INSERT OR REPLACE INTO call_graph (caller_symbol, callee_symbol, version)
            VALUES (?, ?, ?);
            """,
            [caller, callee, version],
        )

    def insert_mapping(self, record: CrossVersionMatchRecord):
        self.conn.execute(
            """
            INSERT OR REPLACE INTO cross_version_mappings
            (old_symbol, new_symbol, old_version, new_version, name_similarity, signature_similarity, caller_similarity, field_similarity, confidence, verified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            [
                record.old_symbol,
                record.new_symbol,
                record.old_version,
                record.new_version,
                record.name_similarity,
                record.signature_similarity,
                record.caller_similarity,
                record.field_similarity,
                record.confidence,
                record.verified,
            ],
        )

    def get_symbol(self, symbol_name: str, version: str) -> Optional[SymbolRecord]:
        res = self.conn.execute(
            "SELECT symbol_name, class_name, namespace, signature, rva, version, return_type, parameters FROM symbols WHERE symbol_name = ? AND version = ?",
            [symbol_name, version],
        ).fetchone()
        if not res:
            return None
        return SymbolRecord(
            symbol_name=res[0],
            class_name=res[1],
            namespace=res[2],
            signature=res[3],
            rva=res[4],
            version=res[5],
            return_type=res[6],
            parameters=json.loads(res[7]) if res[7] else [],
        )

    def get_callers(self, symbol_name: str, version: str) -> List[str]:
        rows = self.conn.execute(
            "SELECT caller_symbol FROM call_graph WHERE callee_symbol = ? AND version = ?",
            [symbol_name, version],
        ).fetchall()
        return [r[0] for r in rows]

    def get_callees(self, symbol_name: str, version: str) -> List[str]:
        rows = self.conn.execute(
            "SELECT callee_symbol FROM call_graph WHERE caller_symbol = ? AND version = ?",
            [symbol_name, version],
        ).fetchall()
        return [r[0] for r in rows]

    def get_best_mapping(self, old_symbol: str, old_version: str, new_version: str) -> Optional[CrossVersionMatchRecord]:
        res = self.conn.execute(
            """
            SELECT old_symbol, new_symbol, old_version, new_version, name_similarity, signature_similarity, caller_similarity, field_similarity, confidence, verified
            FROM cross_version_mappings
            WHERE old_symbol = ? AND old_version = ? AND new_version = ?
            ORDER BY confidence DESC
            LIMIT 1
            """,
            [old_symbol, old_version, new_version],
        ).fetchone()
        if not res:
            return None
        return CrossVersionMatchRecord(
            old_symbol=res[0],
            new_symbol=res[1],
            old_version=res[2],
            new_version=res[3],
            name_similarity=res[4],
            signature_similarity=res[5],
            caller_similarity=res[6],
            field_similarity=res[7],
            confidence=res[8],
            verified=res[9],
        )

    def close(self):
        self.conn.close()
