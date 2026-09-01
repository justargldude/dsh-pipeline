from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TaskDefinition(BaseModel):
    task_id: str
    title: str
    dependencies: List[str] = Field(default_factory=list)
    allowed_files: List[str]
    max_lines_added: int = 50
    max_lines_deleted: int = 20
    target_symbols: List[str] = Field(default_factory=list)
    risk: RiskLevel = RiskLevel.MEDIUM


class PatchHunk(BaseModel):
    old_text: str = ""
    new_text: str = ""


class FilePatch(BaseModel):
    file: str
    hunks: List[PatchHunk] = Field(default_factory=list)


class PatchProposal(BaseModel):
    patches: List[FilePatch]
    reason: str = ""
    confidence: float = 1.0


class TransactionResult(BaseModel):
    task_id: str
    success: bool
    checkpoint: Optional[str] = None
    commit_hash: Optional[str] = None
    failure_type: Optional[str] = None
    error_message: Optional[str] = None
    dry_run: bool = False
    events: List[str] = Field(default_factory=list)
