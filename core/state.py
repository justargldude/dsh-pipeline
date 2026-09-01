from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class PipelineEvent(str, Enum):
    TASK_STARTED = "TASK_STARTED"
    CHECKPOINT_CREATED = "CHECKPOINT_CREATED"
    CONTEXT_BUILT = "CONTEXT_BUILT"
    MODEL_REQUEST = "MODEL_REQUEST"
    MODEL_RESPONSE = "MODEL_RESPONSE"
    PATCH_VALIDATED = "PATCH_VALIDATED"
    SCOPE_PASSED = "SCOPE_PASSED"
    PATCH_APPLIED = "PATCH_APPLIED"
    BUILD_STARTED = "BUILD_STARTED"
    BUILD_PASSED = "BUILD_PASSED"
    BUILD_FAILED = "BUILD_FAILED"
    FAILURE_CLASSIFIED = "FAILURE_CLASSIFIED"
    RECOVERY_STARTED = "RECOVERY_STARTED"
    VALIDATION_PASSED = "VALIDATION_PASSED"
    COMMIT_CREATED = "COMMIT_CREATED"
    ROLLBACK = "ROLLBACK"
    TASK_COMPLETED = "TASK_COMPLETED"


class EventLogEntry(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    event: PipelineEvent
    task_id: str
    details: Dict[str, Any] = Field(default_factory=dict)
    message: str = ""


class WorkspaceState(BaseModel):
    git_head: str
    dirty: bool = False
    modified_files: List[str] = Field(default_factory=list)
    active_task: Optional[str] = None
    build_state: str = "UNKNOWN"
    validation_state: str = "UNKNOWN"
