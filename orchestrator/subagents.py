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
)
from model.schemas import ModelRequest, ModelResponse, ModelType
from task.schema import PatchProposal, FilePatch, PatchHunk

logger = logging.getLogger("dsh.orchestrator.subagents")



class SubagentClient:
    """Generic client interface for querying autonomous subagents via CLI.
    
    Can wrap any subagent (Antigravity/Gemini, Claude, Codex/GPT, DeepSeek, etc.)
    for QA, auditing, planning, or review.
    """

    DEFAULT_QUERY_TIMEOUT = int(os.environ.get("DSH_SUBAGENT_TIMEOUT", "600"))

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

        cmd = list(self.cli_command) + [prompt]
        try:
            logger.info(f"[SUBAGENT:{self.name}] Executing command: {cmd[:3]}... (timeout={timeout}s)")
            # CRITICAL: stdin=subprocess.DEVNULL prevents hanging on interactive permission prompts!
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
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

    # 1. Antigravity / Gemini
    if name in ["agy", "antigravity", "gemini"] or "gemini" in name:
        # Prefer direct agy with --dangerously-skip-permissions to prevent permission hangs
        agy_bin = shutil.which("agy")
        if agy_bin:
            cmd = [agy_bin, "--dangerously-skip-permissions", "--print"]
            if "flash" in name or "pro" in name:
                cmd.extend(["--model", name])
            return SubagentClient(name="antigravity", cli_command=cmd, test_mode=False)
        ask_agy = shutil.which("ask-agy")
        if ask_agy:
            return SubagentClient(name="antigravity", cli_command=[ask_agy], test_mode=False)

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
        f"Available CLIs in PATH: ask, ask-agy, ask-claude, ask-codex, ask-ds, ask-qwen, ask-glm."
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

