from typing import List, Optional
from memory.episodic import EpisodeRecord, EpisodeStatus, EpisodicMemoryStore


class StaleDetector:
    @staticmethod
    def check_staleness(episode: EpisodeRecord, current_version: str, current_env: str = "linux") -> EpisodeStatus:
        """Flags an episode as STALE if the target version or environment has diverged."""
        if episode.version != current_version or episode.environment != current_env:
            return EpisodeStatus.STALE
        return EpisodeStatus.VALIDATED


class EpisodeRetriever:
    def __init__(self, store: EpisodicMemoryStore):
        self.store = store

    def retrieve_advisory_episodes(
        self,
        symbol: str,
        current_version: str,
        current_env: str = "linux",
        max_results: int = 3,
        include_stale: bool = False,
    ) -> List[EpisodeRecord]:
        """Retrieves matching episodes for symbol. Excludes STALE episodes by default."""
        matches = self.store.get_by_symbol(symbol)
        results = []

        for ep in matches:
            # Update staleness
            status = StaleDetector.check_staleness(ep, current_version=current_version, current_env=current_env)
            if not include_stale and status != EpisodeStatus.VALIDATED:
                # Stale memory excluded by default
                continue
            ep_copy = ep.model_copy(update={"status": status})
            results.append(ep_copy)

        # Sort by confidence descending
        results.sort(key=lambda x: x.confidence, reverse=True)
        return results[:max_results]
