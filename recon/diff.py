from typing import Dict, List, Optional, Set, Tuple
from difflib import SequenceMatcher
from recon.database import EvidenceDatabase, SymbolRecord, CrossVersionMatchRecord, MatchState


class CrossVersionMatcher:
    def __init__(
        self,
        db: EvidenceDatabase,
        min_confidence: float = 0.85,
        margin_threshold: float = 0.10,
    ):
        self.db = db
        self.min_confidence = min_confidence
        self.margin_threshold = margin_threshold

    @staticmethod
    def compute_similarity(s1: str, s2: str) -> float:
        if not s1 and not s2:
            return 1.0
        if not s1 or not s2:
            return 0.0
        return SequenceMatcher(None, s1.lower(), s2.lower()).ratio()

    @staticmethod
    def compute_jaccard(set1: Set[str], set2: Set[str]) -> float:
        """Computes Jaccard similarity. Empty sets return 0.0 because absence of evidence
        is not proof of similarity.
        """
        if not set1 or not set2:
            return 0.0
        union = set1.union(set2)
        if not union:
            return 0.0
        return len(set1.intersection(set2)) / len(union)

    @staticmethod
    def _extract_param_types(parameters: List[str]) -> List[str]:
        """Extracts normalized parameter types, ignoring parameter names and modifiers."""
        types = []
        for p in parameters:
            p_clean = p.strip()
            # Remove modifiers: in, out, ref, params, this
            for mod in ("in ", "out ", "ref ", "params ", "this "):
                if p_clean.startswith(mod):
                    p_clean = p_clean[len(mod):].strip()
            # Take type part (everything before the last word/identifier)
            parts = p_clean.split()
            if len(parts) > 1:
                types.append(" ".join(parts[:-1]).lower())
            else:
                types.append(parts[0].lower() if parts else "")
        return types

    def _compute_parameter_similarity(self, p1: List[str], p2: List[str]) -> float:
        types1 = self._extract_param_types(p1)
        types2 = self._extract_param_types(p2)
        if not types1 and not types2:
            return 1.0
        if not types1 or not types2:
            return 0.0
        if len(types1) != len(types2):
            # Penalize parameter count mismatch
            len_ratio = min(len(types1), len(types2)) / max(len(types1), len(types2))
            type_sim = self.compute_similarity(" ".join(types1), " ".join(types2))
            return 0.5 * len_ratio + 0.5 * type_sim
        matches = sum(1.0 for t1, t2 in zip(types1, types2) if t1 == t2)
        return matches / len(types1)

    def match_symbol(
        self,
        old_symbol: SymbolRecord,
        candidate_symbols: List[SymbolRecord],
        old_version: str,
        new_version: str,
    ) -> CrossVersionMatchRecord:
        if not candidate_symbols:
            unmapped = CrossVersionMatchRecord(
                old_symbol=old_symbol.symbol_name,
                new_symbol="",
                old_version=old_version,
                new_version=new_version,
                name_similarity=0.0,
                signature_similarity=0.0,
                caller_similarity=0.0,
                field_similarity=0.0,
                confidence=0.0,
                margin=0.0,
                match_state=MatchState.UNMAPPED,
                verified=False,
            )
            self.db.insert_mapping(unmapped)
            return unmapped

        old_callers = set(self.db.get_callers(old_symbol.symbol_name, old_version))

        # Cache candidate callers to avoid repeated queries
        caller_cache: Dict[str, Set[str]] = {}
        for cand in candidate_symbols:
            if cand.symbol_name not in caller_cache:
                caller_cache[cand.symbol_name] = set(self.db.get_callers(cand.symbol_name, new_version))

        scored_candidates: List[Tuple[SymbolRecord, float, Tuple[float, float, float, float]]] = []

        for cand in candidate_symbols:
            name_sim = self.compute_similarity(old_symbol.symbol_name, cand.symbol_name)
            sig_sim = self.compute_similarity(old_symbol.signature, cand.signature)
            param_sim = self._compute_parameter_similarity(old_symbol.parameters, cand.parameters)
            cand_callers = caller_cache.get(cand.symbol_name, set())
            caller_sim = self.compute_jaccard(old_callers, cand_callers)
            class_sim = self.compute_similarity(old_symbol.class_name, cand.class_name)
            ns_sim = self.compute_similarity(old_symbol.namespace, cand.namespace) if (old_symbol.namespace or cand.namespace) else 1.0

            # Composite field similarity combining class and namespace
            field_sim = 0.7 * class_sim + 0.3 * ns_sim

            # Weighted confidence score distinguishing names, signatures, overloads, callers, and containing types
            confidence = (0.30 * name_sim) + (0.25 * sig_sim) + (0.15 * param_sim) + (0.15 * caller_sim) + (0.15 * field_sim)
            confidence = round(confidence, 4)

            metrics = (name_sim, sig_sim, caller_sim, field_sim)
            scored_candidates.append((cand, confidence, metrics))

        # Sort candidates descending by confidence
        scored_candidates.sort(key=lambda x: x[1], reverse=True)

        best_cand, top1_conf, best_metrics = scored_candidates[0]

        # Evaluate margin and match state
        if len(scored_candidates) == 1:
            margin = top1_conf
            if top1_conf >= self.min_confidence:
                match_state = MatchState.MATCHED
                verified = True
            else:
                match_state = MatchState.UNMAPPED
                verified = False
        else:
            top2_conf = scored_candidates[1][1]
            margin = round(top1_conf - top2_conf, 4)

            if top1_conf < self.min_confidence:
                match_state = MatchState.UNMAPPED
                verified = False
            elif margin < self.margin_threshold:
                # Ambiguous: multiple close candidates
                match_state = MatchState.AMBIGUOUS
                verified = False
            else:
                match_state = MatchState.MATCHED
                verified = True

        match_record = CrossVersionMatchRecord(
            old_symbol=old_symbol.symbol_name,
            new_symbol=best_cand.symbol_name if match_state != MatchState.UNMAPPED else "",
            old_version=old_version,
            new_version=new_version,
            name_similarity=best_metrics[0],
            signature_similarity=best_metrics[1],
            caller_similarity=best_metrics[2],
            field_similarity=best_metrics[3],
            confidence=top1_conf,
            margin=margin,
            match_state=match_state,
            verified=verified,
        )

        self.db.insert_mapping(match_record)
        return match_record
