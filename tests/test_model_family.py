import pytest
from pathlib import Path
from unittest.mock import MagicMock

from core.config import get_model_family
from orchestrator.coordinator import AutonomousCoordinator


def test_get_model_family_mappings():
    assert get_model_family("gemini-2.5-pro") == "google"
    assert get_model_family("agy") == "google"
    assert get_model_family("antigravity") == "google"
    assert get_model_family("qwen-2.5-coder") == "alibaba"
    assert get_model_family("qwen") == "alibaba"
    assert get_model_family("deepseek-chat") == "deepseek"
    assert get_model_family("deepseek") == "deepseek"
    assert get_model_family("claude-3-7-sonnet") == "anthropic"
    assert get_model_family("gpt-4o") == "openai"
    assert get_model_family("codex") == "openai"
    assert get_model_family("o1-preview") == "openai"


def test_cross_family_enforcement_raises_on_same_family(tmp_path: Path):
    with pytest.raises(ValueError, match="Cross-family enforcement failed"):
        AutonomousCoordinator(
            target_repo=tmp_path,
            qa_name="gemini",
            dev_name="agy",
            test_mode=False,
            qa_client=MagicMock(name="qa"),
            dev_provider=MagicMock(name="dev"),
        )

    with pytest.raises(ValueError, match="Cross-family enforcement failed"):
        AutonomousCoordinator(
            target_repo=tmp_path,
            qa_name="qwen",
            dev_name="qwen",
            test_mode=False,
            qa_client=MagicMock(name="qa"),
            dev_provider=MagicMock(name="dev"),
        )


def test_cross_family_enforcement_allows_different_families(tmp_path: Path):
    coord = AutonomousCoordinator(
        target_repo=tmp_path,
        qa_name="antigravity",
        dev_name="qwen",
        test_mode=False,
        qa_client=MagicMock(name="qa"),
        dev_provider=MagicMock(name="dev"),
    )
    assert coord is not None
