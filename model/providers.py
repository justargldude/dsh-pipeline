import json
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional
import httpx
import yaml
from pydantic import ValidationError
from model.schemas import ModelRequest, ModelResponse, ModelType
from task.schema import PatchProposal


def resolve_deepseek_api_key() -> Optional[str]:
    """Auto-sync API key from DeepSeek Harness configuration or Environment."""
    # 1. Environment Variable
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"].strip()

    # 2. DeepSeek Harness settings.yaml (~/.dsh/settings.yaml)
    dsh_settings = Path.home() / ".dsh" / "settings.yaml"
    if dsh_settings.exists():
        try:
            with open(dsh_settings, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                if isinstance(data, dict):
                    if "deepseek_api_key" in data:
                        return str(data["deepseek_api_key"]).strip()
                    if "apiKey" in data:
                        return str(data["apiKey"]).strip()
                    models = data.get("models", {})
                    if isinstance(models, dict):
                        for k, v in models.items():
                            if "deepseek" in k.lower() and isinstance(v, dict) and "apiKey" in v:
                                return str(v["apiKey"]).strip()
        except Exception:
            pass

    # 3. DeepSeek Harness .env file (~/.dsh/.env)
    dsh_env = Path.home() / ".dsh" / ".env"
    if dsh_env.exists():
        try:
            for line in dsh_env.read_text(encoding="utf-8").splitlines():
                if line.startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip("\"' ")
        except Exception:
            pass

    # 4. Local workspace .env
    local_env = Path(".env")
    if local_env.exists():
        try:
            for line in local_env.read_text(encoding="utf-8").splitlines():
                if line.startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip("\"' ")
        except Exception:
            pass

    return None


class BaseModelProvider(ABC):
    @abstractmethod
    def generate(self, req: ModelRequest) -> ModelResponse:
        pass

    @staticmethod
    def extract_patch_proposal(raw_text: str) -> Optional[PatchProposal]:
        """Robustly extracts and validates JSON PatchProposal from model response."""
        try:
            data = json.loads(raw_text)
            return PatchProposal(**data)
        except Exception:
            pass

        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                return PatchProposal(**data)
            except Exception:
                pass

        return None


class MockModelProvider(BaseModelProvider):
    def __init__(self, predefined_proposal: Optional[PatchProposal] = None, raw_response: Optional[str] = None):
        self.predefined_proposal = predefined_proposal
        self.raw_response = raw_response

    def generate(self, req: ModelRequest) -> ModelResponse:
        if self.predefined_proposal:
            raw = self.predefined_proposal.model_dump_json(indent=2)
            return ModelResponse(
                raw_content=raw,
                patch_proposal=self.predefined_proposal,
                tokens_used=150,
                model_name=f"mock-{req.model_type.value.lower()}",
            )
        elif self.raw_response:
            proposal = self.extract_patch_proposal(self.raw_response)
            return ModelResponse(
                raw_content=self.raw_response,
                patch_proposal=proposal,
                tokens_used=100,
                model_name=f"mock-{req.model_type.value.lower()}",
            )
        return ModelResponse(
            raw_content="{}",
            patch_proposal=None,
            tokens_used=10,
            model_name="mock-empty",
            error="No predefined proposal set in mock",
        )


class OpenAICompatibleProvider(BaseModelProvider):
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://api.deepseek.com/v1",
        fast_model: str = "deepseek-chat",
        reasoning_model: str = "deepseek-reasoner",
        timeout_seconds: int = 60,
    ):
        self.api_key = api_key or resolve_deepseek_api_key() or ""
        self.base_url = base_url.rstrip("/")
        self.fast_model = fast_model
        self.reasoning_model = reasoning_model
        self.timeout = timeout_seconds

    def generate(self, req: ModelRequest) -> ModelResponse:
        key = self.api_key or resolve_deepseek_api_key()
        if not key:
            return ModelResponse(
                raw_content="",
                patch_proposal=None,
                tokens_used=0,
                model_name=self.fast_model,
                error="No DeepSeek API key found. Please set DEEPSEEK_API_KEY or configure in DeepSeek Harness (~/.dsh/settings.yaml).",
            )

        model_name = self.reasoning_model if req.model_type == ModelType.REASONING else self.fast_model

        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": req.system_prompt},
                {"role": "user", "content": req.user_prompt},
            ],
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
        }
        if req.json_mode and req.model_type != ModelType.REASONING:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                res = client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
                res.raise_for_status()
                data = res.json()
                content = data["choices"][0]["message"]["content"]
                tokens = data.get("usage", {}).get("total_tokens", 0)

                proposal = self.extract_patch_proposal(content)
                return ModelResponse(
                    raw_content=content,
                    patch_proposal=proposal,
                    tokens_used=tokens,
                    model_name=model_name,
                )
        except Exception as e:
            return ModelResponse(
                raw_content="",
                patch_proposal=None,
                tokens_used=0,
                model_name=model_name,
                error=str(e),
            )
