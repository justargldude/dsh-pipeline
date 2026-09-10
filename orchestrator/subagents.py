import json
import logging
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional
from pathlib import Path

from model.providers import (
    OpenAICompatibleProvider,
    BaseModelProvider,
    MockModelProvider,
    resolve_tokenrouter_api_key,
    resolve_xkiro_api_key,
    resolve_omniroute_api_key,
    OMNIROUTE_MODEL_PREFIXES,
)
from model.schemas import ModelRequest, ModelResponse, ModelType
from recovery.classifier import FailureType
from task.schema import PatchProposal, FilePatch, PatchHunk


def _parse_subagent_timeout_env() -> int:
    """Parses DSH_SUBAGENT_TIMEOUT defensively: invalid values fall back to
    the default (600s) instead of crashing at import/class-definition time."""
    raw = os.environ.get("DSH_SUBAGENT_TIMEOUT", "600")
    try:
        val = int(raw)
        if val <= 0:
            raise ValueError
        return val
    except ValueError:
        logger.warning(
            "Invalid DSH_SUBAGENT_TIMEOUT=%r; falling back to default 600s.", raw
        )
        return 600


def _is_muse_name(name: str) -> bool:
    """True iff the model name is a Muse Spark model.

    Matches the "muse" substring, but explicitly excludes other families'
    names that merely contain "muse"/"spark" as a substring (e.g. a
    hypothetical codex or claude model named "...-spark"). This makes the
    anti-hijack guarantee explicit rather than relying on branch order.
    """
    other_family_markers = (
        "codex", "gpt", "openai", "claude", "anthropic",
        "gemini", "agy", "antigravity", "deepseek", "ds",
        "qwen", "glm", "zhipu", "z-ai", "tokenrouter", "xkiro",
    )
    if any(marker in name for marker in other_family_markers):
        return False
    return "muse" in name


def _is_omniroute_name(name: str) -> bool:
    """True iff name mang 1 prefix OmniRoute thật (agy/, codex/, oc/, ...).

    Match THEO PREFIX (startswith) — KHÔNG dùng bare '/' in name: sẽ bắt
    nhầm z-ai/glm-5.3-free (tokenrouter) và xkiro/... (xkiro cloud).
    Case-insensitive (name đã .lower() trước khi vào).
    """
    lowered = str(name).lower()
    return any(lowered.startswith(p) for p in OMNIROUTE_MODEL_PREFIXES)


class SubagentCLIModelProvider(BaseModelProvider):
    """Dev provider backed by a CLI subagent (e.g. ask-muse for Muse Spark).

    Shells out to the CLI with "<system>\\n\\n<user>" as the prompt and parses
    the PatchProposal from stdout. Used for models with no OpenAI-compatible
    endpoint (opencode provider). stdin=DEVNULL prevents permission hangs.
    """

    def __init__(
        self,
        cli_command: List[str],
        model_name: str = "muse-spark",
        timeout_seconds: Optional[int] = None,
    ):
        self.cli_command = list(cli_command)
        self.model_name = model_name
        self.timeout = timeout_seconds or _parse_subagent_timeout_env()

    def generate(self, req: ModelRequest) -> ModelResponse:
        prompt = f"{req.system_prompt}\n\n{req.user_prompt}"
        try:
            # Prompt goes via stdin (input=), not argv: keeps large prompts
            # off the process table (ps) and avoids ARG_MAX truncation.
            # input= pipes stdin (never a TTY), preserving the no-hang contract.
            proc = subprocess.run(
                self.cli_command,
                input=prompt,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return ModelResponse(
                raw_content="",
                patch_proposal=None,
                tokens_used=0,
                model_name=self.model_name,
                failure_type=FailureType.TIMEOUT,
                error=f"CLI '{self.cli_command[0]}' timed out after {self.timeout}s.",
            )
        except Exception as e:
            return ModelResponse(
                raw_content="",
                patch_proposal=None,
                tokens_used=0,
                model_name=self.model_name,
                failure_type=FailureType.UNKNOWN_PROVIDER_ERROR,
                error=f"CLI '{self.cli_command[0]}' launch failed: {e}",
            )
        raw = (proc.stdout or "").strip()
        if proc.returncode != 0 and not raw:
            return ModelResponse(
                raw_content=raw,
                patch_proposal=None,
                tokens_used=0,
                model_name=self.model_name,
                failure_type=FailureType.UNKNOWN_PROVIDER_ERROR,
                error=f"CLI exited {proc.returncode}: {(proc.stderr or '')[:300]}",
            )
        proposal, f_type, err_msg = self.extract_patch_proposal_structured(raw)
        return ModelResponse(
            raw_content=raw,
            patch_proposal=proposal,
            failure_type=f_type,
            error=err_msg,
            tokens_used=0,
            model_name=self.model_name,
        )


logger = logging.getLogger("dsh.orchestrator.subagents")



class SubagentClient:
    """Generic client interface for querying autonomous subagents via CLI.
    
    Can wrap any subagent (Antigravity/Gemini, Claude, Codex/GPT, DeepSeek, etc.)
    for QA, auditing, planning, or review.
    """

    DEFAULT_QUERY_TIMEOUT = _parse_subagent_timeout_env()

    def __init__(
        self,
        name: str,
        cli_command: Optional[List[str]] = None,
        test_mode: bool = False,
    ):
        self.name = name
        self.cli_command = cli_command or []
        self.test_mode = test_mode
        self._mock_responses: Dict[str, Any] = {}

    def set_mock_response(self, prompt_keyword: str, response: Any):
        """Allows injecting mock responses during unit testing."""
        self._mock_responses[prompt_keyword] = response

    def query(self, prompt: str, timeout: Optional[int] = None) -> str:
        """Sends a prompt to the subagent CLI and returns the response string."""
        if timeout is None:
            timeout = self.DEFAULT_QUERY_TIMEOUT

        if self.test_mode:
            for k, v in self._mock_responses.items():
                if k in prompt:
                    return v if isinstance(v, str) else json.dumps(v)
            if any(term in prompt.lower() for term in ["schema", "json", "audit", "architect", "lead"]):
                # Structured QA review contract: prompts asking for a JSON
                # review verdict get a valid verdict object instead of prose.
                if "verdict" in prompt and "flagged_risks" in prompt:
                    return json.dumps({
                        "verdict": "APPROVED",
                        "flagged_risks": [],
                        "summary": f"Mock {self.name}: Diff reviewed and approved.",
                    })
                return json.dumps({
                    "summary": f"Mock {self.name}: Autonomous Audit Plan completed.",
                    "detected_framework": "generic",
                    "detected_build_cmd": "",
                    "detected_test_cmd": "",
                    "tasks": [
                        {
                            "task_id": "T_MOCK_01",
                            "title": "Mock Task",
                            "description": "Mock Description",
                            "allowed_files": ["Main.cs"],
                            "target_symbols": [],
                            "test_file": None,
                            "test_code": None,
                            "test_cmd": None,
                            "max_lines_added": 20,
                            "max_lines_deleted": 5,
                            "risk": "low",
                        }
                    ],
                })
            return f"Mock {self.name}: Response validated."

        if not self.cli_command:
            raise RuntimeError(
                f"No CLI command configured for subagent '{self.name}'. "
                "Ensure the required CLI is installed in PATH."
            )

        # Prompt goes via stdin (input=), not argv: keeps large prompts off
        # the process table (ps) and avoids ARG_MAX truncation. input= pipes
        # stdin (never a TTY), preventing interactive permission-prompt hangs.
        cmd = list(self.cli_command)
        try:
            logger.info(f"[SUBAGENT:{self.name}] Executing command: {cmd[:3]}... (timeout={timeout}s)")
            proc = subprocess.run(
                cmd,
                input=prompt,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
            if proc.returncode != 0:
                logger.warning(
                    f"Subagent '{self.name}' exited with code {proc.returncode}: {proc.stderr[:300]}"
                )
            return proc.stdout.strip()
        except subprocess.TimeoutExpired:
            logger.error(f"Subagent '{self.name}' timed out after {timeout}s.")
            raise TimeoutError(f"Subagent '{self.name}' query timed out after {timeout}s")
        except Exception as e:
            logger.error(f"Error querying subagent '{self.name}': {e}")
            raise

    def query_json(self, prompt: str, timeout: Optional[int] = None) -> Dict[str, Any]:
        """Sends a prompt expecting a JSON response and parses it."""
        raw = self.query(prompt, timeout=timeout)
        return self._extract_json(raw)

    @staticmethod
    def _extract_json(text: str) -> Dict[str, Any]:
        """Extracts JSON object from text, handling markdown fences and formatting."""
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try markdown code fence extraction
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # Try searching for outermost balanced braces
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass

        raise ValueError(f"Failed to extract valid JSON from subagent response: {text[:300]}")


def create_qa_client(model_or_cli: str, test_mode: bool = False) -> SubagentClient:
    """Factory creating a SubagentClient tailored to the requested QA model/CLI."""
    name = (model_or_cli or "agy").lower().strip()

    if test_mode:
        return SubagentClient(name=name, cli_command=["mock-qa"], test_mode=True)

    # 0. OmniRoute HTTP gateway — branch ĐẦU TIÊN, đứng trước mọi family branch
    # để agy/, codex/, oc/... (prefix OmniRoute) không rơi branch CLI cũ.
    # Model (prefix/...) truyền làm argv[1] cho ask-omniroute — đúng model
    # người dùng chọn, không rơi default auto/best-coding.
    if _is_omniroute_name(name):
        ask_omni = shutil.which("ask-omniroute")
        if not ask_omni:
            raise RuntimeError(
                f"ask-omniroute CLI not found in PATH for QA model '{name}' "
                "(OmniRoute). Install dsh-portable (bin/ask-omniroute symlinked "
                "to ~/.local/bin) or choose another model; refusing to silently "
                "fall back to a different backend."
            )
        return SubagentClient(name=name, cli_command=[ask_omni, name], test_mode=False)

    # 1. Antigravity / Gemini
    if name in ["agy", "antigravity", "gemini"] or "gemini" in name:
        # Prefer the ask-agy wrapper: it accepts prompts via stdin (the
        # pipeline sends prompts on stdin, not argv) and handles model
        # fallback chains. The direct agy binary's --print needs the prompt
        # as an argument and cannot read stdin, so it is only a fallback.
        ask_agy = shutil.which("ask-agy")
        if ask_agy:
            return SubagentClient(name="antigravity", cli_command=[ask_agy], test_mode=False)
        agy_bin = shutil.which("agy")
        if agy_bin:
            cmd = [agy_bin, "--dangerously-skip-permissions", "--print"]
            if "flash" in name or "pro" in name:
                cmd.extend(["--model", name])
            return SubagentClient(name="antigravity", cli_command=cmd, test_mode=False)

    # 2. Claude / Anthropic
    if name in ["claude", "sonnet", "opus"] or "claude" in name:
        ask_claude = shutil.which("ask-claude")
        if ask_claude:
            return SubagentClient(name="claude", cli_command=[ask_claude], test_mode=False)
        claude_bin = shutil.which("claude")
        if claude_bin:
            return SubagentClient(name="claude", cli_command=[claude_bin, "-p"], test_mode=False)

    # 3. Codex / GPT
    if name in ["codex", "gpt", "openai"] or "gpt" in name or "codex" in name:
        ask_codex = shutil.which("ask-codex")
        if ask_codex:
            return SubagentClient(name="codex", cli_command=[ask_codex], test_mode=False)
        codex_bin = shutil.which("codex")
        if codex_bin:
            return SubagentClient(
                name="codex",
                cli_command=[codex_bin, "exec", "--skip-git-repo-check", "-c", 'approval_policy="never"'],
                test_mode=False,
            )

    # 4. DeepSeek
    if name in ["deepseek", "ds"] or "deepseek" in name:
        ask_ds = shutil.which("ask-ds")
        if ask_ds:
            return SubagentClient(name="deepseek", cli_command=[ask_ds], test_mode=False)

    # 5. Qwen
    if name in ["qwen", "qwen3"] or "qwen" in name:
        ask_qwen = shutil.which("ask-qwen")
        if ask_qwen:
            return SubagentClient(name="qwen", cli_command=[ask_qwen], test_mode=False)

    # 5b. Xkiro (cloud qwen via api.xkiro.com)
    if any(k in name for k in ["xkiro", "xk"]):
        ask_xkiro = shutil.which("ask-xkiro")
        if ask_xkiro:
            return SubagentClient(name="xkiro", cli_command=[ask_xkiro], test_mode=False)
        # Fallback: use 'ask' router with xkiro model
        ask_router = shutil.which("ask")
        if ask_router:
            return SubagentClient(name="xkiro", cli_command=[ask_router, "-m", "xkiro"], test_mode=False)

    # 5c. Muse Spark (opencode provider, via ask-muse wrapper).
    # NOTE: matches "muse" only — bare "spark" (e.g. in other model names such
    # as openai/gpt-5.3-codex-spark) must NOT hijack to the muse wrapper, and
    # codex/claude-family names containing "muse" stay with their own family.
    if _is_muse_name(name):
        ask_muse = shutil.which("ask-muse")
        if not ask_muse:
            raise RuntimeError(
                f"ask-muse CLI not found in PATH for QA model '{name}' (muse-spark). "
                "Install the ask-muse wrapper or choose another model; refusing "
                "to silently fall back to a different backend."
            )
        return SubagentClient(name="muse-spark", cli_command=[ask_muse], test_mode=False)

    # 6. GLM / TokenRouter
    if any(k in name for k in ["glm", "tokenrouter", "z-ai"]):
        ask_glm = shutil.which("ask-glm")
        if ask_glm:
            return SubagentClient(name="glm", cli_command=[ask_glm], test_mode=False)

    # 7. Fallback router: 'ask' CLI
    ask_router = shutil.which("ask")
    if ask_router:
        return SubagentClient(name=name, cli_command=[ask_router, "-m", name], test_mode=False)

    # 8. Fallback to direct binary if named matches something in PATH
    bin_path = shutil.which(name)
    if bin_path:
        return SubagentClient(name=name, cli_command=[bin_path], test_mode=False)

    raise RuntimeError(
        f"Could not resolve CLI for QA model '{name}'. "
        f"Available CLIs in PATH: ask, ask-agy, ask-claude, ask-codex, ask-ds, ask-qwen, ask-xkiro, ask-glm, ask-muse."
    )


def create_dev_provider(
    model_or_cli: str,
    test_mode: bool = False,
    mock_provider: Optional[MockModelProvider] = None,
) -> BaseModelProvider:
    """Factory creating a BaseModelProvider tailored to the requested Dev model/CLI."""
    if mock_provider:
        return mock_provider

    if test_mode:
        return MockModelProvider(
            predefined_proposal=PatchProposal(
                patches=[
                    FilePatch(
                        file="Main.cs",
                        hunks=[
                            PatchHunk(
                                old_text="public class Main { public static void Run() {} }",
                                new_text="public class Main {\n    // [AUTO_PATCH]\n    public static void Run() {}\n}",
                            )
                        ],
                    )
                ],
                reason="Test Mode Mock Patch",
                confidence=1.0,
            )
        )

    name = (model_or_cli or "deepseek").lower().strip()

    # OmniRoute HTTP gateway — branch ĐẦU TIÊN: mọi model dạng prefix/...
    # (agy/, codex/, oc/, oc-local/, qwen-local/, auto/, ...) qua HTTP
    # 127.0.0.1:20128 thay vì CLI wrapper/proxy trực tiếp.
    if _is_omniroute_name(name):
        base_url = os.environ.get("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128/v1").rstrip("/")
        return OpenAICompatibleProvider(
            api_key=resolve_omniroute_api_key(),
            base_url=base_url,
            fast_model=name,
            reasoning_model=name,
            timeout_seconds=int(os.environ.get("OMNIROUTE_TIMEOUT", "300")),
        )

    if any(k in name for k in ["glm", "tokenrouter", "z-ai"]):
        base_url = os.environ.get("TOKENROUTER_BASE_URL", "https://api.tokenrouter.com/v1")
        api_key = os.environ.get("TOKENROUTER_API_KEY") or resolve_tokenrouter_api_key()
        model = "z-ai/glm-5.3-free"
        if "glm" in name and "/" in name:
            model = name
        return OpenAICompatibleProvider(
            api_key=api_key,
            base_url=base_url,
            fast_model=model,
            reasoning_model=model,
            timeout_seconds=int(os.environ.get("TOKENROUTER_TIMEOUT", "180")),
        )

    # Xkiro — cloud qwen endpoint (qwen/qwen3.8-max:free via api.xkiro.com)
    if any(k in name for k in ["xkiro", "xk"]):
        base_url = os.environ.get("XKIRO_BASE_URL", "https://api.xkiro.com/v1")
        api_key = os.environ.get("XKIRO_API_KEY") or resolve_xkiro_api_key()
        fast_model = "qwen/qwen3.8-max:free"
        reasoning_model = "qwen/qwen3.8-max:free"
        # Allow overriding the model by name, e.g. --dev xkiro/qwen3.7-plus:free
        if "/" in name and name != "xkiro":
            # e.g. "xkiro/qwen3.7-plus:free" -> model = "qwen3.7-plus:free"
            fast_model = name.split("/", 1)[1] if name.startswith("xkiro/") else name
            reasoning_model = fast_model
        return OpenAICompatibleProvider(
            api_key=api_key,
            base_url=base_url,
            fast_model=fast_model,
            reasoning_model=reasoning_model,
            timeout_seconds=int(os.environ.get("XKIRO_TIMEOUT", "180")),
        )

    # Muse Spark has no OpenAI-compatible endpoint (opencode provider only),
    # so Dev goes through the ask-muse CLI wrapper instead.
    # NOTE: matches "muse" only — bare "spark" (e.g. in other model names such
    # as openai/gpt-5.3-codex-spark) must NOT hijack to the muse wrapper.
    if _is_muse_name(name):
        ask_muse = shutil.which("ask-muse")
        if not ask_muse:
            raise RuntimeError(
                f"ask-muse CLI not found in PATH for Dev model '{name}' (muse-spark). "
                "Install the ask-muse wrapper or choose another model; refusing "
                "to silently fall back to a different backend."
            )
        return SubagentCLIModelProvider(
            cli_command=[ask_muse],
            model_name="muse-spark",
        )

    if "qwen" in name:
        base_url = os.environ.get("QWEN_BASE_URL", "http://127.0.0.1:8200/v1")
        api_key = os.environ.get("QWEN_API_KEY", "sk-justar-local-qwen")
        fast_model = "qwen3.7-plus"
        reasoning_model = "qwen3.8-max"
        if "max" in name or "3.8" in name:
            fast_model = "qwen3.8-max"
            reasoning_model = "qwen3.8-max"
        elif "plus" in name or "3.7" in name:
            fast_model = "qwen3.7-plus"
            reasoning_model = "qwen3.7-plus"
        return OpenAICompatibleProvider(
            api_key=api_key,
            base_url=base_url,
            fast_model=fast_model,
            reasoning_model=reasoning_model,
            timeout_seconds=int(os.environ.get("QWEN_TIMEOUT", "180")),
        )

    # Default: DeepSeek / OpenAI-compatible endpoint
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "http://127.0.0.1:8100/v1")
    api_key = os.environ.get("DEEPSEEK_API_KEY", "sk-justar-local-dsw2a")

    fast_model = "deepseek-chat"
    reasoning_model = "deepseek-reasoner"

    if "reasoner" in name or "r1" in name:
        fast_model = "deepseek-reasoner"
        reasoning_model = "deepseek-reasoner"
    elif "chat" in name or "v3" in name:
        fast_model = "deepseek-chat"
        reasoning_model = "deepseek-chat"
    elif name not in ["deepseek", "ds"]:
        fast_model = name
        reasoning_model = name

    return OpenAICompatibleProvider(
        api_key=api_key,
        base_url=base_url,
        fast_model=fast_model,
        reasoning_model=reasoning_model,
    )


# --- Backwards compatibility wrappers ---

class AntigravityClient(SubagentClient):
    """Backwards-compatible wrapper around SubagentClient for Antigravity."""

    def __init__(self, cli_path: Optional[str] = None, test_mode: bool = False):
        if test_mode:
            super().__init__(name="antigravity", cli_command=["mock-agy"], test_mode=True)
        elif cli_path:
            super().__init__(name="antigravity", cli_command=[cli_path], test_mode=False)
        else:
            client = create_qa_client("antigravity", test_mode=False)
            super().__init__(name="antigravity", cli_command=client.cli_command, test_mode=False)


class DeepSeekClient:
    """Backwards-compatible wrapper around Dev provider for DeepSeek."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        test_mode: bool = False,
    ):
        self.test_mode = test_mode
        self.base_url = base_url or os.environ.get("DEEPSEEK_BASE_URL", "http://127.0.0.1:8100/v1")
        self.api_key = (
            api_key
            or os.environ.get("DEEPSEEK_API_KEY")
            or "sk-justar-local-dsw2a"
        )
        self._mock_provider: Optional[MockModelProvider] = None

    def set_mock_provider(self, provider: MockModelProvider):
        self._mock_provider = provider

    def get_provider(self) -> BaseModelProvider:
        if self._mock_provider:
            return self._mock_provider
        return create_dev_provider(
            "deepseek",
            test_mode=self.test_mode,
            mock_provider=self._mock_provider,
        )

