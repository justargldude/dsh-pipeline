from typing import Any, Dict, List, Optional
from recon.database import EvidenceDatabase, SymbolRecord, CrossVersionMatchRecord, MatchState


class EvidenceService:
    def __init__(self, db: EvidenceDatabase):
        self.db = db

    def get_symbol_evidence(
        self,
        symbol_name: str,
        old_version: str,
        new_version: str,
    ) -> Dict[str, Any]:
        old_sym = self.db.get_symbol(symbol_name, old_version)
        mapping = self.db.get_best_mapping(symbol_name, old_version, new_version)

        new_sym_name = mapping.new_symbol if (mapping and mapping.new_symbol) else symbol_name
        new_sym = self.db.get_symbol(new_sym_name, new_version)

        old_callers = self.db.get_callers(symbol_name, old_version)
        new_callers = self.db.get_callers(new_sym_name, new_version) if new_sym_name else []

        match_state = mapping.match_state.value if mapping else MatchState.UNMAPPED.value
        is_fact = bool(mapping and mapping.verified and mapping.match_state == MatchState.MATCHED)

        return {
            "target_symbol": symbol_name,
            "old_version": old_version,
            "new_version": new_version,
            "old_symbol": old_sym.model_dump() if old_sym else None,
            "new_symbol": new_sym.model_dump() if new_sym else None,
            "mapping": mapping.model_dump() if mapping else None,
            "callers": {
                "old": old_callers,
                "new": new_callers,
            },
            "confidence": mapping.confidence if mapping else 0.0,
            "margin": mapping.margin if mapping else 0.0,
            "match_state": match_state,
            "is_fact": is_fact,
        }
