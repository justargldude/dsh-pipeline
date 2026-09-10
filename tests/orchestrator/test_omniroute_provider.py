"""OmniRoute HTTP provider wiring tests — pattern test_glm_tokenrouter.py.

Covers (plan Phase 2):
- resolve_omniroute_api_key(): env -> credentials.yaml refs -> RuntimeError
  (KHÔNG hardcode fallback)
- Dev provider: OpenAICompatibleProvider base_url 127.0.0.1:20128/v1,
  fast_model=reasoning_model=name, timeout 300s (gateway free chậm)
- QA client: SubagentClient(name=name, cli_command=[ask-omniroute]) — thiếu
  wrapper → RuntimeError
- Branch precedence: OmniRoute đứng ĐẦU TIÊN, match startswith prefix thật
- Anti-cross-hijack: z-ai/glm-5.3-free (tokenrouter) và xkiro/... KHÔNG rơi
  branch OmniRoute
- get_model_family strip prefix OmniRoute: qwen-local/qwen3.8-max -> alibaba,
  agy/gemini-* -> google, codex/* -> openai, oc/muse-* -> muse,
  aug/glm-5.2 -> zhipu, auto/best-coding -> omni_auto (family riêng)
"""
import os
import shutil
import pytest
from core.config import get_model_family
from orchestrator.subagents import create_qa_client, create_dev_provider
from model.providers import (
    OpenAICompatibleProvider,
    resolve_omniroute_api_key,
    OMNIROUTE_MODEL_PREFIXES,
)
from model.schemas import ModelRequest, ModelType
from recovery.classifier import FailureType


# ── resolve_omniroute_api_key: thứ tự env → credentials → RuntimeError ──

def test_omniroute_key_env_wins(monkeypatch):
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-env-test")
    assert resolve_omniroute_api_key() == "sk-env-test"


def test_omniroute_key_from_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    fake = tmp_path / "credentials.yaml"
    fake.write_text(
        "version: 1\nrefs:\n  OMNIROUTE_API_KEY: sk-cred-test\n",
        encoding="utf-8",
    )
    import model.providers as P
    monkeypatch.setattr(P, "_omniroute_credentials_path", lambda: fake)
    assert resolve_omniroute_api_key() == "sk-cred-test"


def test_omniroute_key_missing_raises(monkeypatch, tmp_path):
    """KHÔNG hardcode fallback — thiếu key ở mọi nơi → RuntimeError rõ ràng."""
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    fake = tmp_path / "credentials.yaml"
    fake.write_text("version: 1\nrefs:\n  OTHER_KEY: x\n", encoding="utf-8")
    import model.providers as P
    monkeypatch.setattr(P, "_omniroute_credentials_path", lambda: fake)
    with pytest.raises(RuntimeError, match="(?i)omniroute"):
        resolve_omniroute_api_key()


def test_omniroute_key_no_hardcoded_fallback(monkeypatch, tmp_path):
    """Function KHÔNG trả key hardcode khi mọi nguồn trống (bug cũ pattern
    tokenrouter/xkiro — không tái tạo)."""
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    fake = tmp_path / "credentials.yaml"
    fake.write_text("version: 1\nrefs: {}\n", encoding="utf-8")
    import model.providers as P
    monkeypatch.setattr(P, "_omniroute_credentials_path", lambda: fake)
    try:
        key = resolve_omniroute_api_key()
        assert not key.startswith("sk-"), "không được trả key hardcode"
    except RuntimeError:
        pass  # đúng behavior kỳ vọng


# ── Dev provider: OpenAICompatibleProvider qua OmniRoute ──

def test_omniroute_dev_provider_resolution(monkeypatch):
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-test")
    provider = create_dev_provider("oc-local/muse-spark-1.3-contributor-free")
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.base_url == "http://127.0.0.1:20128/v1"
    assert provider.fast_model == "oc-local/muse-spark-1.3-contributor-free"
    assert provider.reasoning_model == "oc-local/muse-spark-1.3-contributor-free"
    assert provider.api_key == "sk-test"
    # gateway free chậm 60-180s/turn → timeout 300s
    assert provider.timeout >= 300


def test_omniroute_dev_provider_base_url_env_override(monkeypatch):
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-test")
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:29999/v1")
    provider = create_dev_provider("auto/best-coding")
    assert provider.base_url == "http://127.0.0.1:29999/v1"


def test_omniroute_dev_provider_missing_key_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    fake = tmp_path / "credentials.yaml"
    fake.write_text("version: 1\nrefs: {}\n", encoding="utf-8")
    import model.providers as P
    monkeypatch.setattr(P, "_omniroute_credentials_path", lambda: fake)
    with pytest.raises(RuntimeError, match="(?i)omniroute"):
        create_dev_provider("oc-local/muse-spark-1.3-contributor-free")


# ── QA client: ask-omniroute wrapper ──

def test_omniroute_qa_client_resolution():
    client = create_qa_client("oc/muse-spark-1.2-contributor-free")
    assert client.name == "oc/muse-spark-1.2-contributor-free"
    ask = shutil.which("ask-omniroute")
    if ask:  # wrapper có trong PATH (máy dev) — kiểm tra wiring + model argv
        assert client.cli_command == [ask, "oc/muse-spark-1.2-contributor-free"]
    else:
        assert "ask-omniroute" in " ".join(client.cli_command)


def test_omniroute_qa_client_missing_wrapper_fails_fast(monkeypatch):
    orig_which = shutil.which

    def fake_which(n):
        return None if n == "ask-omniroute" else orig_which(n)

    monkeypatch.setattr("orchestrator.subagents.shutil.which", fake_which)
    with pytest.raises(RuntimeError, match="ask-omniroute"):
        create_qa_client("auto/best-coding")


# ── Branch precedence: OmniRoute ĐẦU TIÊN, prefix thật ──

OMNI_PREFIXES = [
    "agy/", "codex/", "oc/", "oc-local/", "qwen-local/", "auto/", "aug/",
    "cfp/", "cx/", "cxa/", "tllm/", "dva/", "gh/", "github/", "openrouter/",
    "opencode/", "opencode-zen/", "deepseek-web/", "ds-web/", "qwen-web/",
    "no-think/",
]


def test_omniroute_prefix_list_covers_all():
    for p in OMNI_PREFIXES:
        assert p in OMNIROUTE_MODEL_PREFIXES, f"thiếu prefix {p}"


@pytest.mark.parametrize("name", [
    "agy/gemini-3.8-flash-high",
    "codex/gpt-5.5",
    "oc/muse-spark-1.3-contributor-free",
    "oc-local/muse-spark-1.3-contributor-free",
    "qwen-local/qwen3.8-max",
    "auto/best-coding",
    "aug/glm-5.2",
    "cfp/zai-org/glm-5.2",
    "opencode-zen/muse-spark-1.2",
    "ds-web/deepseek-v4-flash-free",
])
def test_omniroute_names_route_to_omniroute_dev(monkeypatch, name):
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-test")
    provider = create_dev_provider(name)
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.base_url == "http://127.0.0.1:20128/v1"
    assert provider.fast_model == name
    assert provider.reasoning_model == name


@pytest.mark.parametrize("name", [
    "z-ai/glm-5.3-free",          # tokenrouter — KHÔNG rơi OmniRoute
    "glm-5.3-free",
    "xkiro/qwen3.7-plus:free",    # xkiro cloud — KHÔNG rơi OmniRoute
    "xkiro",
])
def test_non_omniroute_names_do_not_hijack(monkeypatch, name):
    """Bare '/' in name là sai: z-ai/glm-5.3-free phải về tokenrouter,
    xkiro/... phải về xkiro — không đụng branch OmniRoute."""
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-test")
    provider = create_dev_provider(name)
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.base_url != "http://127.0.0.1:20128/v1", (
        f"{name} bị hijack sang OmniRoute"
    )


def test_qwen_local_prefix_not_qwen_8200(monkeypatch):
    """qwen-local/qwen3.8-max là OmniRoute node (prefix qwen-local/), KHÔNG
    phải proxy 8200 trực tiếp (branch qwen cũ)."""
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-test")
    provider = create_dev_provider("qwen-local/qwen3.8-max")
    assert provider.base_url == "http://127.0.0.1:20128/v1"
    assert provider.fast_model == "qwen-local/qwen3.8-max"


def test_oc_muse_prefix_not_ask_muse(monkeypatch):
    """oc/muse-spark-1.2-contributor-free là OmniRoute HTTP, KHÔNG rơi branch
    muse/ask-muse (SubagentCLIModelProvider)."""
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-test")
    from orchestrator.subagents import SubagentCLIModelProvider
    provider = create_dev_provider("oc/muse-spark-1.2-contributor-free")
    assert isinstance(provider, OpenAICompatibleProvider)
    assert not isinstance(provider, SubagentCLIModelProvider)


def test_codex_prefix_not_codex_cli(monkeypatch):
    """codex/gpt-5.5 là OmniRoute HTTP, KHÔNG rơi branch codex CLI
    (create_qa_client)."""
    client = create_qa_client("codex/gpt-5.5")
    # phải là ask-omniroute wrapper, không phải ask-codex/codex CLI
    assert "ask-omniroute" in (client.cli_command[0] if client.cli_command else "")


# ── get_model_family: strip prefix OmniRoute trước substring-match ──

@pytest.mark.parametrize("name,family", [
    ("qwen-local/qwen3.8-max", "alibaba"),
    ("agy/gemini-3.8-flash-high", "google"),
    ("codex/gpt-5.5", "openai"),
    ("oc/muse-spark-1.3-contributor-free", "muse"),
    ("oc-local/muse-spark-1.2-contributor-free", "muse"),
    ("aug/glm-5.2", "zhipu"),
    ("auto/best-coding", "omni_auto"),
    ("auto/best-chat", "omni_auto"),
])
def test_model_family_strips_omniroute_prefix(name, family):
    assert get_model_family(name) == family, f"{name} phải là {family}"


def test_omni_auto_distinct_from_concrete_families():
    """auto/* là family riêng omni_auto — 2 model OmniRoute khác prefix
    (auto/best-coding vs oc/muse-*) không bị coi cùng family → anti-reward-
    hacking QA≠Dev vẫn đúng khi cả 2 qua OmniRoute."""
    assert get_model_family("auto/best-coding") == "omni_auto"
    assert get_model_family("auto/best-coding") != get_model_family("oc/muse-spark-1.3-contributor-free")
    # và ngược lại: oc/muse-* là muse thật, không phải omni_auto
    assert get_model_family("oc/muse-spark-1.3-contributor-free") == "muse"


def test_non_prefixed_family_unchanged():
    """Family matching cũ không prefix phải giữ nguyên behavior."""
    assert get_model_family("glm") == "zhipu"
    assert get_model_family("z-ai/glm-5.3-free") == "zhipu"
    assert get_model_family("muse-spark") == "muse"
    assert get_model_family("gemini-3-pro") == "google"
    assert get_model_family("qwen3.8-max") == "alibaba"
    assert get_model_family("deepseek-chat") == "deepseek"
