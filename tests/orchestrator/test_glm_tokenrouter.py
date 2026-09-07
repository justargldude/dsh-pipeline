import pytest
from core.config import get_model_family
from orchestrator.subagents import create_qa_client, create_dev_provider
from orchestrator.coordinator import AutonomousCoordinator
from model.providers import OpenAICompatibleProvider
from pathlib import Path


def test_glm_model_family():
    assert get_model_family("glm") == "zhipu"
    assert get_model_family("glm-5.3") == "zhipu"
    assert get_model_family("z-ai/glm-5.3-free") == "zhipu"
    assert get_model_family("tokenrouter") == "zhipu"


def test_glm_qa_client_resolution():
    client = create_qa_client("glm")
    assert client.name == "glm"
    assert any("ask-glm" in cmd for cmd in client.cli_command)


def test_glm_dev_provider_resolution():
    provider = create_dev_provider("glm")
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.base_url == "https://api.tokenrouter.com/v1"
    assert provider.fast_model == "z-ai/glm-5.3-free"
    assert provider.reasoning_model == "z-ai/glm-5.3-free"
    assert provider.api_key.startswith("sk-")


def test_glm_cross_family_separation(tmp_path):
    # GLM (zhipu) + DeepSeek (deepseek) -> PASSES cross-family
    coord = AutonomousCoordinator(
        target_repo=tmp_path,
        qa_name="glm",
        dev_name="deepseek",
        test_mode=True,
    )
    assert coord.qa_client is not None
    assert coord.dev_provider is not None

    # GLM (zhipu) + GLM (zhipu) -> FAILS cross-family
    with pytest.raises(ValueError, match="Cross-family enforcement failed"):
        AutonomousCoordinator(
            target_repo=tmp_path,
            qa_name="glm",
            dev_name="z-ai/glm-5.3-free",
            test_mode=True,
        )
