import pytest
from task.schema import TaskDefinition
from context.budget import ContextComplexity, TokenBudgetManager
from context.ranking import ContextItem, ContextRanker, PriorityLevel
from context.builder import ContextBuilder


def test_token_budget_estimation():
    text = "public class Player { public void Update() {} }"
    tokens = TokenBudgetManager.estimate_tokens(text)
    assert tokens > 0
    assert TokenBudgetManager.get_budget(ContextComplexity.SIMPLE) == 3000
    assert TokenBudgetManager.get_budget(ContextComplexity.COMPLEX) == 10000


def test_context_ranking_and_trimming():
    items = [
        ContextItem(priority=PriorityLevel.RELEVANT_DIFF, category="DIFF", content="A" * 500),
        ContextItem(priority=PriorityLevel.TARGET_SYMBOL_EVIDENCE, category="EVIDENCE", content="B" * 200),
        ContextItem(priority=PriorityLevel.DIRECT_CALLERS_CALLEES, category="CALLERS", content="C" * 200),
    ]

    # With a small budget that only fits ~100 tokens, priority 1 should be selected
    trimmed = ContextRanker.rank_and_trim(items, token_budget=100)
    assert len(trimmed) >= 1
    assert trimmed[0].priority == PriorityLevel.TARGET_SYMBOL_EVIDENCE


def test_context_builder_output():
    task = TaskDefinition(
        task_id="T017",
        title="Port Player.Update hook",
        allowed_files=["Player.cs"],
        max_lines_added=30,
        max_lines_deleted=10,
    )

    builder = ContextBuilder()
    context = builder.build_context(
        task=task,
        file_snippets={"Player.cs": "public class Player { void Update() {} }"},
        evidence={"symbol": "Player.Update", "confidence": 0.95},
        complexity=ContextComplexity.NORMAL,
    )

    assert "[T017] Port Player.Update hook" in context
    assert "RECON EVIDENCE (FACTS)" in context
    assert "SOURCE FILE: Player.cs" in context
