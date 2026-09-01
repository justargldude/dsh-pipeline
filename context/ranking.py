from enum import IntEnum
from typing import Any, List
from pydantic import BaseModel
from context.budget import TokenBudgetManager


class PriorityLevel(IntEnum):
    TARGET_SYMBOL_EVIDENCE = 1
    DIRECT_CALLERS_CALLEES = 2
    RELATED_FIELDS_TYPES = 3
    CONSTANTS_STRINGS = 4
    RELEVANT_DIFF = 5
    PREVIOUS_VALIDATED_FIX = 6


class ContextItem(BaseModel):
    priority: PriorityLevel
    category: str
    content: str
    token_cost: int = 0

    def __init__(self, **data):
        super().__init__(**data)
        if self.token_cost == 0:
            self.token_cost = TokenBudgetManager.estimate_tokens(self.content)


class ContextRanker:
    @staticmethod
    def rank_and_trim(items: List[ContextItem], token_budget: int) -> List[ContextItem]:
        # Sort items by priority ascending (1 is highest priority)
        sorted_items = sorted(items, key=lambda item: item.priority.value)
        selected_items = []
        current_tokens = 0

        for item in sorted_items:
            if current_tokens + item.token_cost <= token_budget:
                selected_items.append(item)
                current_tokens += item.token_cost
            else:
                # If cannot fit whole item, skip or stop
                continue

        return selected_items
