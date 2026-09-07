"""Muse Spark (opencode provider) wiring tests — mirrors test_glm_tokenrouter.py.

Covers:
- Model family resolution (must be its own family for cross-family enforcement)
- QA client resolution via ask-muse wrapper
- Dev provider resolution via SubagentCLIModelProvider
- Anti-hijack: bare "spark" strings of other models (codex-spark) must NOT
  resolve to muse
- SubagentCLIModelProvider contract: system+user prompt concat, PatchProposal
  parsing, timeout handling
"""
import os
import shutil
import subprocess
import pytest
from core.config import get_model_family
from orchestrator.subagents import (
    create_qa_client,
    create_dev_provider,
    SubagentCLIModelProvider,
    SubagentClient,
)
from model.schemas import ModelRequest, ModelResponse, ModelType
from recovery.classifier import FailureType
from task.schema import PatchProposal, FilePatch, PatchHunk


def test_muse_model_family():
    assert get_model_family("muse-spark") == "muse"
    assert get_model_family("muse") == "muse"
    assert get_model_family("opencode/muse-spark-1.3-contributor-free") == "muse"
    # Anti-hijack: bare spark (e.g. codex-spark) is NOT muse
    assert get_model_family("spark") != "muse"


def test_muse_qa_client_resolution():
    client = create_qa_client("muse-spark")
    assert client.name == "muse-spark"
    assert any("ask-muse" in cmd for cmd in client.cli_command)


def test_muse_qa_client_resolution_short_alias():
    client = create_qa_client("muse")
    assert client.name == "muse-spark"
    assert any("ask-muse" in cmd for cmd in client.cli_command)


def test_muse_dev_provider_resolution():
    provider = create_dev_provider("muse-spark")
    assert isinstance(provider, SubagentCLIModelProvider)
    assert provider.model_name == "muse-spark"
    assert any("ask-muse" in cmd for cmd in provider.cli_command)


def test_codex_spark_not_hijacked_to_muse():
    """Regression: 'spark' substring in other model names (codex-spark) must
    not resolve QA to the muse wrapper."""
    client = create_qa_client("openai/gpt-5.3-codex-spark")
    # 'gpt'/'codex'/'openai' matches the codex branch, never muse
    assert client.name != "muse-spark"
    assert not any("ask-muse" in cmd for cmd in client.cli_command)


def test_bare_spark_not_hijacked_to_muse():
    """Regression: bare 'spark' must NOT resolve to muse (comment/behavior
    contract; get_model_family('spark') != 'muse' and factories agree)."""
    client = create_qa_client("spark")
    assert client.name != "muse-spark"
    assert not any("ask-muse" in cmd for cmd in client.cli_command)


def test_muse_named_other_family_models_not_hijacked():
    """Explicit-exclusion anti-hijack: a hypothetical model of another family
    containing 'muse'/'spark' (e.g. 'muse-spark-codex') stays with codex —
    never the ask-muse wrapper (independent of branch ordering)."""
    for name in ("codex-muse-spark", "claude-muse", "gemini-muse", "muse-qwen"):
        client = create_qa_client(name)
        assert client.name != "muse-spark", f"{name} hijacked to muse"
        assert not any("ask-muse" in cmd for cmd in client.cli_command), name


def test_muse_qa_missing_ask_muse_fails_fast(monkeypatch):
    """Missing ask-muse binary must raise a clear error instead of silently
    falling back to a different backend (e.g. the generic 'ask' router)."""
    orig_which = shutil.which

    def fake_which(n):
        return None if n == "ask-muse" else orig_which(n)

    monkeypatch.setattr("orchestrator.subagents.shutil.which", fake_which)
    with pytest.raises(RuntimeError, match="ask-muse CLI not found"):
        create_qa_client("muse-spark")


def test_muse_dev_missing_ask_muse_fails_fast(monkeypatch):
    """Dev provider: missing ask-muse must fail fast (no silent router)."""
    orig_which = shutil.which

    def fake_which(n):
        return None if n == "ask-muse" else orig_which(n)

    monkeypatch.setattr("orchestrator.subagents.shutil.which", fake_which)
    with pytest.raises(RuntimeError, match="ask-muse CLI not found"):
        create_dev_provider("muse-spark")


def _sample_proposal_json() -> str:
    return PatchProposal(
        patches=[
            FilePatch(
                file="Main.cs",
                hunks=[
                    PatchHunk(
                        old_text="public class Main { public static void Run() {} }",
                        new_text="public class Main {\n    public static void Run() { /* patched */ }\n}",
                    )
                ],
            )
        ],
        reason="muse test patch",
        confidence=0.9,
    ).model_dump_json(indent=2)


class TestSubagentCLIModelProvider:
    def _provider(self) -> SubagentCLIModelProvider:
        return SubagentCLIModelProvider(
            cli_command=["/bin/echo"],
            model_name="muse-spark",
            timeout_seconds=10,
        )

    def test_generate_parses_patch_proposal(self):
        # /bin/echo ignores stdin and echoes argv, so pass the JSON as an arg.
        provider = SubagentCLIModelProvider(
            cli_command=["/bin/echo", _sample_proposal_json()],
            model_name="muse-spark",
            timeout_seconds=10,
        )
        req = ModelRequest(system_prompt="SYS", user_prompt="USER")
        res = provider.generate(req)
        assert isinstance(res, ModelResponse)
        assert res.model_name == "muse-spark"
        assert res.patch_proposal is not None
        assert res.patch_proposal.patches[0].file == "Main.cs"
        assert res.failure_type is None

    def test_generate_concatenates_system_and_user(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=_sample_proposal_json(), stderr=""
            )

        monkeypatch.setattr("orchestrator.subagents.subprocess.run", fake_run)
        provider = self._provider()
        req = ModelRequest(system_prompt="SYSTEM-PROMPT", user_prompt="USER-PROMPT")
        res = provider.generate(req)
        # Prompt is passed via stdin (input=), not argv: keeps large prompts
        # off the process table and avoids ARG_MAX truncation.
        assert "SYSTEM-PROMPT" in captured["kwargs"]["input"]
        assert "USER-PROMPT" in captured["kwargs"]["input"]
        assert "SYSTEM-PROMPT" not in captured["cmd"][-1]
        assert res.patch_proposal is not None
        # input= pipes stdin (never an interactive TTY); no stdin= override
        # needed, and none may be passed alongside input=.
        assert "stdin" not in captured["kwargs"]

    def test_generate_timeout_returns_failure_response(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

        monkeypatch.setattr("orchestrator.subagents.subprocess.run", fake_run)
        provider = self._provider()
        res = provider.generate(ModelRequest(system_prompt="S", user_prompt="U"))
        assert res.failure_type == FailureType.TIMEOUT
        assert res.patch_proposal is None
        assert "timed out" in (res.error or "")

    def test_generate_nonzero_exit_no_stdout_is_error(self):
        provider = SubagentCLIModelProvider(
            cli_command=["/bin/false"],
            model_name="muse-spark",
            timeout_seconds=10,
        )
        res = provider.generate(ModelRequest(system_prompt="S", user_prompt="U"))
        assert res.patch_proposal is None
        assert res.failure_type == FailureType.UNKNOWN_PROVIDER_ERROR
        assert res.model_name == "muse-spark"

    def test_generate_stdout_forwarded_even_on_nonzero_exit(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout=_sample_proposal_json(), stderr=""
            )

        monkeypatch.setattr("orchestrator.subagents.subprocess.run", fake_run)
        provider = self._provider()
        res = provider.generate(ModelRequest(system_prompt="S", user_prompt="U"))
        # stdout with valid JSON is still parsed (wrapper forwards partial output)
        assert res.patch_proposal is not None

    def test_generate_unparseable_output_is_format_error(self):
        provider = SubagentCLIModelProvider(
            cli_command=["/bin/echo", "not json at all"],
            model_name="muse-spark",
            timeout_seconds=10,
        )
        res = provider.generate(ModelRequest(system_prompt="S", user_prompt="U"))
        assert res.patch_proposal is None
        assert res.failure_type == FailureType.MODEL_FORMAT_ERROR


class TestSubagentTimeoutEnvParsing:
    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        """DSH_SUBAGENT_TIMEOUT=garbage must not crash at import/definition
        time — falls back to the 600s default with a warning."""
        from orchestrator import subagents as S

        monkeypatch.setenv("DSH_SUBAGENT_TIMEOUT", "garbage")
        assert S._parse_subagent_timeout_env() == 600
        monkeypatch.setenv("DSH_SUBAGENT_TIMEOUT", "-5")
        assert S._parse_subagent_timeout_env() == 600
        monkeypatch.setenv("DSH_SUBAGENT_TIMEOUT", "0")
        assert S._parse_subagent_timeout_env() == 600

    def test_valid_env_is_used(self, monkeypatch):
        from orchestrator import subagents as S

        monkeypatch.setenv("DSH_SUBAGENT_TIMEOUT", "42")
        assert S._parse_subagent_timeout_env() == 42

    def test_invalid_env_does_not_break_import(self):
        """Regression: garbage DSH_SUBAGENT_TIMEOUT used to crash the whole
        module import (ValueError at class-body evaluation)."""
        import subprocess as sp

        r = sp.run(
            ["python3", "-c", "import orchestrator.subagents"],
            capture_output=True,
            text=True,
            env={**os.environ, "DSH_SUBAGENT_TIMEOUT": "garbage"},
        )
        assert r.returncode == 0, r.stderr

    def test_invalid_env_defaults_for_new_instances(self, monkeypatch):
        """New provider/client instances get the safe default, not a crash."""
        from orchestrator import subagents as S

        monkeypatch.setenv("DSH_SUBAGENT_TIMEOUT", "garbage")
        provider = S.SubagentCLIModelProvider(
            cli_command=["/bin/true"], model_name="muse-spark"
        )
        assert provider.timeout == 600


def test_muse_cross_family_vs_other_models(tmp_path):
    """muse (own family) + deepseek -> passes; muse + muse -> fails."""
    from orchestrator.coordinator import AutonomousCoordinator

    coord = AutonomousCoordinator(
        target_repo=tmp_path,
        qa_name="muse-spark",
        dev_name="deepseek",
        test_mode=True,
    )
    assert coord.qa_client is not None
    assert coord.dev_provider is not None

    with pytest.raises(ValueError, match="Cross-family enforcement failed"):
        AutonomousCoordinator(
            target_repo=tmp_path,
            qa_name="muse-spark",
            dev_name="opencode/muse-spark-1.3-contributor-free",
            test_mode=True,
        )
