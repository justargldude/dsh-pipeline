from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from task.schema import PatchProposal


class EpisodeStatus(str, Enum):
    VALIDATED = "VALIDATED"
    STALE = "STALE"
    DEPRECATED = "DEPRECATED"


class EpisodeValidation(BaseModel):
    build: bool = True
    behavior: bool = True
    regression: bool = True


class EpisodeRecord(BaseModel):
    task_id: str
    symbol: str
    error_pattern: Optional[str] = None
    solution_patch: PatchProposal
    version: str = "v1"
    environment: str = "linux"
    evidence_used: Dict[str, Any] = Field(default_factory=dict)
    validation: EpisodeValidation = Field(default_factory=EpisodeValidation)
    confidence: float = 1.0
    status: EpisodeStatus = EpisodeStatus.VALIDATED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EpisodicMemoryStore:
    def __init__(self):
        self.episodes: List[EpisodeRecord] = []

    def store_episode(self, episode: EpisodeRecord):
        self.episodes.append(episode)

    def get_all_episodes(self) -> List[EpisodeRecord]:
        return self.episodes

    def get_by_symbol(self, symbol: str) -> List[EpisodeRecord]:
        return [ep for ep in self.episodes if ep.symbol.lower() == symbol.lower()]
