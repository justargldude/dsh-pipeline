from enum import Enum
from typing import Dict
from pydantic import BaseModel


class ContextComplexity(str, Enum):
    SIMPLE = "simple"
    NORMAL = "normal"
    COMPLEX = "complex"
    RECOVERY = "recovery"


class TokenBudgetManager:
    DEFAULT_BUDGETS: Dict[ContextComplexity, int] = {
        ContextComplexity.SIMPLE: 3000,
        ContextComplexity.NORMAL: 6000,
        ContextComplexity.COMPLEX: 10000,
        ContextComplexity.RECOVERY: 8000,
    }

    @classmethod
    def get_budget(cls, complexity: ContextComplexity = ContextComplexity.NORMAL) -> int:
        return cls.DEFAULT_BUDGETS.get(complexity, 6000)

    @staticmethod
    def estimate_tokens(text: str) -> int:
        # Standard conservative approximation: 1 token ≈ 3.5 characters for code/symbols
        if not text:
            return 0
        return max(1, int(len(text) / 3.5))
