from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


class TaskRole(str, Enum):
    """Who authored this task: the Dev agent (default) or the QA agent.

    Anti-reward-hacking v2.3 Checkpoint 5: only QA-authored tasks may touch
    test files; Dev tasks must never write/delete test files.
    """
    DEV = "dev"
    QA = "qa"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TaskDefinition(BaseModel):
    task_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    dependencies: List[str] = Field(default_factory=list)
    allowed_files: List[str] = Field(default_factory=list)
    max_lines_added: int = Field(default=50, ge=0, le=50000)
    max_lines_deleted: int = Field(default=20, ge=0, le=50000)
    target_symbols: List[str] = Field(default_factory=list)
    risk: RiskLevel = RiskLevel.MEDIUM
    role: TaskRole = TaskRole.DEV

    @field_validator("task_id", "title")
    @classmethod
    def validate_non_empty_string(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Task ID and title cannot be empty or whitespace-only.")
        return v.strip()


class PatchHunk(BaseModel):
    old_text: str = ""
    new_text: str = ""

    @model_validator(mode="after")
    def validate_hunk_semantics(self) -> "PatchHunk":
        if self.old_text == "" and self.new_text == "":
            raise ValueError("PatchHunk cannot be empty: both old_text and new_text are empty.")
        if self.old_text != "" and self.old_text == self.new_text:
            raise ValueError("PatchHunk is a no-op: old_text and new_text are identical.")
        return self


class FilePatch(BaseModel):
    file: str = Field(..., min_length=1)
    hunks: List[PatchHunk] = Field(default_factory=list)

    @field_validator("file")
    @classmethod
    def validate_file_path(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("File path cannot be empty or whitespace-only.")
        return v.strip()


class PatchProposal(BaseModel):
    patches: List[FilePatch]
    reason: str = ""
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class TransactionResult(BaseModel):
    task_id: str
    success: bool
    checkpoint: Optional[str] = None
    commit_hash: Optional[str] = None
    base_commit: Optional[str] = None
    worktree_path: Optional[str] = None
    integration_status: Optional[str] = None
    cleanup_error: Optional[str] = None
    baseline_details: Optional[Dict[str, Any]] = None
    failure_type: Optional[str] = None
    error_message: Optional[str] = None
    dry_run: bool = False
    events: List[str] = Field(default_factory=list)
