from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from task.schema import PatchProposal


class ModelType(str, Enum):
    FAST = "FAST"
    REASONING = "REASONING"


class ModelRequest(BaseModel):
    system_prompt: str
    user_prompt: str
    model_type: ModelType = ModelType.FAST
    temperature: float = 0.2
    max_tokens: int = 4000
    json_mode: bool = True


class ModelResponse(BaseModel):
    raw_content: str
    patch_proposal: Optional[PatchProposal] = None
    tokens_used: int = 0
    model_name: str = ""
    error: Optional[str] = None
