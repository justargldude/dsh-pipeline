import json
import re
from abc import ABC, abstractmethod
from typing import Optional
import httpx
from pydantic import ValidationError
from model.schemas import ModelRequest, ModelResponse, ModelType
from task.schema import PatchProposal


class BaseModelProvider(ABC):
    @abstractmethod
    def generate(self, req: ModelRequest) -> ModelResponse:
        pass

    @staticmethod
    def extract_patch_proposal(raw_text: str) -> Optional[PatchProposal]:
        """Robustly extracts and validates JSON PatchProposal from model response."""
        try:
            # 1. Try direct JSON parse
            data = json.loads(raw_text)
            return PatchProposal(**data)
        except Exception:
            pass

        # 2. Try markdown json fence extraction
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
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        fast_model: str = "deepseek-chat",
        reasoning_model: str = "deepseek-reasoner",
        timeout_seconds: int = 60,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.fast_model = fast_model
        self.reasoning_model = reasoning_model
        self.timeout = timeout_seconds

    def generate(self, req: ModelRequest) -> ModelResponse:
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
            "Authorization": f"Bearer {self.api_key}",
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
