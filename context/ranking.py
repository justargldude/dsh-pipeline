from enum import IntEnum
from typing import Any, List, Tuple
from pydantic import BaseModel
from context.budget import TokenBudgetManager


class PriorityLevel(IntEnum):
    # MANDATORY (1 - 3)
    TASK_DEFINITION = 1
    TARGET_SOURCE = 2
    TARGET_SYMBOLS = 3

    # IMPORTANT (4 - 5)
    DIRECT_CALLERS_CALLEES = 4
    RELEVANT_EVIDENCE = 5

    # OPTIONAL (6 - 8)
    FAILURE_HISTORY = 6
    ADVISORY_MEMORY = 7
    DISTANT_REPO_CONTEXT = 8

    # Backward compatibility aliases
    TARGET_SYMBOL_EVIDENCE = 5
    RELATED_FIELDS_TYPES = 4
    CONSTANTS_STRINGS = 5
    RELEVANT_DIFF = 6
    PREVIOUS_VALIDATED_FIX = 7


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
    def is_mandatory(priority: PriorityLevel) -> bool:
        return priority.value <= 3

    @staticmethod
    def rank_and_trim_detailed(
        items: List[ContextItem],
        token_budget: int,
    ) -> Tuple[List[ContextItem], List[ContextItem]]:
        """Ranks items ensuring mandatory items (priority <= 3) are never dropped
        for optional items, and optional items are trimmed by priority within remaining budget.
        Returns (selected_items, omitted_items).
        """
        # Split into mandatory vs optional
        mandatory_items = [it for it in items if it.priority.value <= 3]
        optional_items = [it for it in items if it.priority.value > 3]

        mandatory_items.sort(key=lambda item: item.priority.value)
        optional_items.sort(key=lambda item: item.priority.value)

        selected_items: List[ContextItem] = []
        omitted_items: List[ContextItem] = []
        current_tokens = 0

        # Mandatory items are always included first
        for item in mandatory_items:
            selected_items.append(item)
            current_tokens += item.token_cost

        # Optional items fill remaining budget
        for item in optional_items:
            if current_tokens + item.token_cost <= token_budget:
                selected_items.append(item)
                current_tokens += item.token_cost
            else:
                omitted_items.append(item)

        return selected_items, omitted_items

    @classmethod
    def rank_and_trim(cls, items: List[ContextItem], token_budget: int) -> List[ContextItem]:
        """Convenience method returning only selected items."""
        selected, _ = cls.rank_and_trim_detailed(items, token_budget)
        return selected
