import os
from enum import Enum
from typing import Dict
from pydantic import BaseModel


class ContextComplexity(str, Enum):
    SIMPLE = "simple"
    NORMAL = "normal"
    COMPLEX = "complex"
    RECOVERY = "recovery"
    UNLIMITED = "unlimited"


class ContextBudgetExceededError(Exception):
    """Raised when mandatory context (task definition + target source) exceeds available token budget."""
    pass


class TokenBudgetManager:
    DEFAULT_BUDGETS: Dict[ContextComplexity, int] = {
        ContextComplexity.SIMPLE: 3000,
        ContextComplexity.NORMAL: 6000,
        ContextComplexity.COMPLEX: 10000,
        ContextComplexity.RECOVERY: 8000,
        ContextComplexity.UNLIMITED: 2_000_000,
    }

    RESERVED_SYSTEM_PROMPT_TOKENS: int = 200
    RESERVED_OUTPUT_TOKENS: int = 800

    # Temporarily set to unlimited mode per user requirement (bypasses token caps for heavy injection)
    UNLIMITED_MODE: bool = True

    @classmethod
    def get_budget(cls, complexity: ContextComplexity = ContextComplexity.NORMAL) -> int:
        if complexity == ContextComplexity.UNLIMITED:
            return cls.DEFAULT_BUDGETS[ContextComplexity.UNLIMITED]
        return cls.DEFAULT_BUDGETS.get(complexity, 6000)

    @classmethod
    def compute_available_budget(cls, complexity: ContextComplexity = ContextComplexity.NORMAL) -> int:
        """Computes available context budget after reserving tokens for system prompt and model output."""
        env_unlimited = os.environ.get("DSH_UNLIMITED_BUDGET")
        if env_unlimited is not None:
            is_unlimited = env_unlimited in ("1", "true", "True")
        else:
            is_unlimited = cls.UNLIMITED_MODE or (complexity == ContextComplexity.UNLIMITED)

        if is_unlimited:
            return 2_000_000

        total = cls.get_budget(complexity)
        reserved = cls.RESERVED_SYSTEM_PROMPT_TOKENS + cls.RESERVED_OUTPUT_TOKENS
        return max(500, total - reserved)

    @staticmethod
    def estimate_tokens(text: str) -> int:
        # Standard conservative approximation: 1 token ≈ 3.5 characters for code/symbols
        if not text:
            return 0
        return max(1, int(len(text) / 3.5))
