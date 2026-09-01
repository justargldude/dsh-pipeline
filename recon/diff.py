from typing import List, Set
from difflib import SequenceMatcher
from recon.database import EvidenceDatabase, SymbolRecord, CrossVersionMatchRecord


class CrossVersionMatcher:
    def __init__(self, db: EvidenceDatabase):
        self.db = db

    @staticmethod
    def compute_similarity(s1: str, s2: str) -> float:
        return SequenceMatcher(None, s1.lower(), s2.lower()).ratio()

    @staticmethod
    def compute_jaccard(set1: Set[str], set2: Set[str]) -> float:
        if not set1 and not set2:
            return 1.0
        if not set1 or not set2:
            return 0.0
        return len(set1.intersection(set2)) / len(set1.union(set2))

    def match_symbol(
        self,
        old_symbol: SymbolRecord,
        candidate_symbols: List[SymbolRecord],
        old_version: str,
        new_version: str,
    ) -> CrossVersionMatchRecord:
        best_match = None
        highest_conf = -1.0
        best_metrics = (0.0, 0.0, 0.0, 0.0)

        old_callers = set(self.db.get_callers(old_symbol.symbol_name, old_version))

        for cand in candidate_symbols:
            name_sim = self.compute_similarity(old_symbol.symbol_name, cand.symbol_name)
            sig_sim = self.compute_similarity(old_symbol.signature, cand.signature)

            cand_callers = set(self.db.get_callers(cand.symbol_name, new_version))
            caller_sim = self.compute_jaccard(old_callers, cand_callers)

            field_sim = self.compute_similarity(old_symbol.class_name, cand.class_name)

            # Weighted confidence score (Deterministic formula)
            confidence = (0.35 * name_sim) + (0.30 * sig_sim) + (0.20 * caller_sim) + (0.15 * field_sim)

            if confidence > highest_conf:
                highest_conf = confidence
                best_match = cand
                best_metrics = (name_sim, sig_sim, caller_sim, field_sim)

        if not best_match:
            return CrossVersionMatchRecord(
                old_symbol=old_symbol.symbol_name,
                new_symbol="",
                old_version=old_version,
                new_version=new_version,
                name_similarity=0.0,
                signature_similarity=0.0,
                caller_similarity=0.0,
                field_similarity=0.0,
                confidence=0.0,
                verified=False,
            )

        match_record = CrossVersionMatchRecord(
            old_symbol=old_symbol.symbol_name,
            new_symbol=best_match.symbol_name,
            old_version=old_version,
            new_version=new_version,
            name_similarity=best_metrics[0],
            signature_similarity=best_metrics[1],
            caller_similarity=best_metrics[2],
            field_similarity=best_metrics[3],
            confidence=round(highest_conf, 4),
            verified=highest_conf >= 0.85,
        )
        self.db.insert_mapping(match_record)
        return match_record
