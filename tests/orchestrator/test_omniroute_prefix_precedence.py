"""OmniRoute prefix precedence tests — branch OmniRoute đứng ĐẦU TIÊN trong
create_dev_provider() VÀ create_qa_client(), match startswith prefix thật.

Khác test_omniroute_provider.py (kiểm provider/key/family): file này kiểm
thứ tự branch + các case phủ định sai (bắt nhầm tokenrouter/xkiro qua bare
'/' in name).
"""
import os
import pytest
from orchestrator.subagents import (
    create_dev_provider,
    create_qa_client,
    _is_omniroute_name,
)
from model.providers import OpenAICompatibleProvider, OMNIROUTE_MODEL_PREFIXES


def test_is_omniroute_name_prefixes():
    for p in OMNIROUTE_MODEL_PREFIXES:
        assert _is_omniroute_name(p + "anything"), f"{p}anything phải match"
        # case-insensitive
        assert _is_omniroute_name(p.upper() + "anything")


def test_is_omniroute_name_negatives():
    # z-ai/glm-5.3-free: tokenrouter, KHÔNG phải prefix OmniRoute
    assert not _is_omniroute_name("z-ai/glm-5.3-free")
    # xkiro/... không phải prefix OmniRoute
    assert not _is_omniroute_name("xkiro/qwen3.7-plus:free")
    # không dùng bare "/" match — tên có slash nhưng prefix lạ thì không match
    assert not _is_omniroute_name("foo/bar")
    assert not _is_omniroute_name("glm-5.3")
    assert not _is_omniroute_name("muse-spark")
    assert not _is_omniroute_name("deepseek-chat")


def test_dev_branch_omniroute_first_qwen_local(monkeypatch):
    """qwen-local/qwen3.8-max KHÔNG rơi branch qwen 8200 (đứng sau)."""
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-test")
    monkeypatch.setenv("QWEN_BASE_URL", "http://127.0.0.1:8200/v1")
    provider = create_dev_provider("qwen-local/qwen3.8-max")
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.base_url == "http://127.0.0.1:20128/v1"
    assert provider.fast_model == "qwen-local/qwen3.8-max"


def test_dev_branch_omniroute_first_muse(monkeypatch):
    """oc/muse-spark-1.2-contributor-free KHÔNG rơi branch muse/ask-muse."""
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-test")
    from orchestrator.subagents import SubagentCLIModelProvider
    provider = create_dev_provider("oc/muse-spark-1.2-contributor-free")
    assert isinstance(provider, OpenAICompatibleProvider)
    assert not isinstance(provider, SubagentCLIModelProvider)
    assert provider.fast_model == "oc/muse-spark-1.2-contributor-free"


def test_dev_branch_omniroute_first_deepseek_web(monkeypatch):
    """deepseek-web/... là prefix OmniRoute — KHÔNG rơi default deepseek 8100."""
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-test")
    provider = create_dev_provider("deepseek-web/deepseek-v4-flash-free")
    assert provider.base_url == "http://127.0.0.1:20128/v1"
    assert provider.fast_model == "deepseek-web/deepseek-v4-flash-free"


def test_dev_branch_omniroute_first_glm_aug(monkeypatch):
    """aug/glm-5.2 là prefix OmniRoute — KHÔNG rơi branch glm/tokenrouter
    (đứng sau) dù chứa 'glm'."""
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-test")
    provider = create_dev_provider("aug/glm-5.2")
    assert provider.base_url == "http://127.0.0.1:20128/v1"
    assert provider.fast_model == "aug/glm-5.2"


def test_qa_branch_omniroute_first_all_prefixes(monkeypatch):
    """create_qa_client: mọi prefix OmniRoute → ask-omniroute wrapper."""
    orig_which = __import__("shutil").which

    def fake_which(n):
        return f"/fake/{n}" if n == "ask-omniroute" else orig_which(n)

    monkeypatch.setattr("orchestrator.subagents.shutil.which", fake_which)
    for name in [
        "agy/gemini-3.8-flash-high",
        "codex/gpt-5.5",
        "oc/muse-spark-1.3-contributor-free",
        "qwen-local/qwen3.8-max",
        "auto/best-chat",
    ]:
        client = create_qa_client(name)
        assert client.cli_command and "ask-omniroute" in client.cli_command[0], (
            f"{name} không qua ask-omniroute: {client.cli_command}"
        )


def test_qa_branch_omniroute_before_family_branches(monkeypatch):
    """agy/... và codex/... phải qua OmniRoute, KHÔNG rơi branch agy/codex CLI
    dù các branch đó đứng sẵn."""
    orig_which = __import__("shutil").which

    def fake_which(n):
        return f"/fake/{n}" if n == "ask-omniroute" else orig_which(n)

    monkeypatch.setattr("orchestrator.subagents.shutil.which", fake_which)
    client = create_qa_client("agy/gemini-3.8-flash-high")
    assert "ask-omniroute" in client.cli_command[0]
    assert client.name == "agy/gemini-3.8-flash-high"  # giữ nguyên tên model
    client2 = create_qa_client("codex/gpt-5.5")
    assert "ask-omniroute" in client2.cli_command[0]


def test_qa_non_omniroute_names_unaffected(monkeypatch):
    """Tên không prefix OmniRoute giữ behavior cũ (agy, glm, muse-spark...)."""
    client = create_qa_client("agy")
    assert client.name == "antigravity"
    client2 = create_qa_client("glm")
    assert client2.name == "glm"
    client3 = create_qa_client("muse-spark")
    assert client3.name == "muse-spark"
