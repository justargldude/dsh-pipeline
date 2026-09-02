import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, Tuple
import httpx
import yaml
from pydantic import ValidationError
from model.schemas import ModelRequest, ModelResponse, ModelType
from recovery.classifier import FailureType
from task.schema import PatchProposal

logger = logging.getLogger("dsh.model")


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

    @classmethod
    def extract_patch_proposal_structured(
        cls, raw_text: str
    ) -> Tuple[Optional[PatchProposal], Optional[FailureType], Optional[str]]:
        """Robustly extracts, parses JSON, and validates PatchProposal schema.

        Distinguishes:
        - Valid schema -> (proposal, None, None)
        - Format / unparseable JSON error -> (None, FailureType.MODEL_FORMAT_ERROR, err)
        - Invalid schema / validation error -> (None, FailureType.MODEL_CONTENT_INVALID, err)
        """
        if not raw_text or not raw_text.strip():
            return None, FailureType.MODEL_FORMAT_ERROR, "Empty model response."

        data = None
        # 1. Direct JSON parse
        try:
            data = json.loads(raw_text)
        except Exception:
            pass

        # 2. Markdown code block extraction
        if data is None:
            json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group(1))
                except Exception:
                    pass

        # 3. Outer brace extraction
        if data is None:
            start_idx = raw_text.find("{")
            end_idx = raw_text.rfind("}")
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                candidate = raw_text[start_idx : end_idx + 1]
                try:
                    data = json.loads(candidate)
                except Exception:
                    try:
                        data = json.loads(candidate, strict=False)
                    except Exception:
                        pass

        if data is None:
            snippet = raw_text[:200] if len(raw_text) > 200 else raw_text
            return (
                None,
                FailureType.MODEL_FORMAT_ERROR,
                f"Model response is not valid JSON. Snippet: {snippet}",
            )

        # 4. Schema validation
        try:
            if not isinstance(data, dict):
                return (
                    None,
                    FailureType.MODEL_CONTENT_INVALID,
                    f"Parsed JSON is not an object/dict (got {type(data).__name__}).",
                )
            proposal = PatchProposal(**data)
            return proposal, None, None
        except (ValidationError, ValueError) as e:
            return (
                None,
                FailureType.MODEL_CONTENT_INVALID,
                f"Model generated invalid PatchProposal content: {str(e)}",
            )
        except Exception as e:
            return (
                None,
                FailureType.MODEL_CONTENT_INVALID,
                f"Unexpected error validating PatchProposal: {str(e)}",
            )

    @classmethod
    def extract_patch_proposal(cls, raw_text: str) -> Optional[PatchProposal]:
        """Convenience method returning PatchProposal or None."""
        proposal, _, _ = cls.extract_patch_proposal_structured(raw_text)
        return proposal


class MockModelProvider(BaseModelProvider):
    def __init__(
        self,
        predefined_proposal: Optional[PatchProposal] = None,
        raw_response: Optional[str] = None,
        failure_type: Optional[FailureType] = None,
        error: Optional[str] = None,
        status_code: Optional[int] = None,
    ):
        self.predefined_proposal = predefined_proposal
        self.raw_response = raw_response
        self.failure_type = failure_type
        self.error = error
        self.status_code = status_code
        self.call_count = 0

    def generate(self, req: ModelRequest) -> ModelResponse:
        self.call_count += 1
        if self.failure_type is not None:
            return ModelResponse(
                raw_content=self.raw_response or "",
                patch_proposal=None,
                tokens_used=0,
                model_name=f"mock-{req.model_type.value.lower()}",
                error=self.error or f"Mock failure: {self.failure_type.value}",
                failure_type=self.failure_type,
                status_code=self.status_code,
            )

        if self.predefined_proposal:
            raw = self.predefined_proposal.model_dump_json(indent=2)
            return ModelResponse(
                raw_content=raw,
                patch_proposal=self.predefined_proposal,
                tokens_used=150,
                model_name=f"mock-{req.model_type.value.lower()}",
            )
        elif self.raw_response:
            proposal, f_type, err = self.extract_patch_proposal_structured(self.raw_response)
            return ModelResponse(
                raw_content=self.raw_response,
                patch_proposal=proposal,
                tokens_used=100,
                model_name=f"mock-{req.model_type.value.lower()}",
                failure_type=f_type,
                error=err,
            )
        return ModelResponse(
            raw_content="{}",
            patch_proposal=None,
            tokens_used=10,
            model_name="mock-empty",
            failure_type=FailureType.MODEL_FORMAT_ERROR,
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
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        max_backoff_seconds: float = 10.0,
        client: Optional[httpx.Client] = None,
    ):
        self.api_key = api_key or resolve_deepseek_api_key() or ""
        self.base_url = base_url.rstrip("/")
        self.fast_model = fast_model
        self.reasoning_model = reasoning_model
        self.timeout = timeout_seconds
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.max_backoff_seconds = max_backoff_seconds
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                timeout=self.timeout,
                limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            )
            self._owns_client = True
        return self._client

    def close(self):
        if self._client is not None and self._owns_client and not self._client.is_closed:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def generate(self, req: ModelRequest) -> ModelResponse:
        key = self.api_key or resolve_deepseek_api_key()
        if not key:
            return ModelResponse(
                raw_content="",
                patch_proposal=None,
                tokens_used=0,
                model_name=self.fast_model,
                failure_type=FailureType.AUTH_ERROR,
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

        client = self._get_client()
        last_failure_type = FailureType.UNKNOWN_PROVIDER_ERROR
        last_error = ""
        last_status_code: Optional[int] = None
        last_retry_after: Optional[float] = None

        for attempt in range(self.max_retries + 1):
            try:
                res = client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
                last_status_code = res.status_code

                if res.status_code in (401, 403):
                    # Hard stop: Auth error must not be retried
                    return ModelResponse(
                        raw_content=res.text,
                        patch_proposal=None,
                        tokens_used=0,
                        model_name=model_name,
                        failure_type=FailureType.AUTH_ERROR,
                        status_code=res.status_code,
                        error=f"Authentication failed ({res.status_code}): {res.text}",
                    )

                if res.status_code == 429:
                    last_failure_type = FailureType.RATE_LIMIT
                    retry_hdr = res.headers.get("Retry-After")
                    if retry_hdr:
                        try:
                            last_retry_after = float(retry_hdr)
                        except ValueError:
                            last_retry_after = None
                    last_error = f"Rate limit exceeded (429): {res.text}"
                    if attempt < self.max_retries:
                        sleep_time = last_retry_after if last_retry_after is not None else min(self.max_backoff_seconds, self.backoff_factor * (2 ** attempt))
                        logger.warning(f"Rate limit hit. Retrying in {sleep_time:.2f}s (attempt {attempt + 1}/{self.max_retries})...")
                        time.sleep(sleep_time)
                        continue
                    break

                if 500 <= res.status_code < 600:
                    last_failure_type = FailureType.SERVER_ERROR
                    last_error = f"Server error ({res.status_code}): {res.text}"
                    if attempt < self.max_retries:
                        sleep_time = min(self.max_backoff_seconds, self.backoff_factor * (2 ** attempt))
                        logger.warning(f"Server 5xx error ({res.status_code}). Retrying in {sleep_time:.2f}s (attempt {attempt + 1}/{self.max_retries})...")
                        time.sleep(sleep_time)
                        continue
                    break

                res.raise_for_status()

                # Successful HTTP call
                data = res.json()
                content = data["choices"][0]["message"]["content"]
                tokens = data.get("usage", {}).get("total_tokens", 0)

                proposal, f_type, err_msg = self.extract_patch_proposal_structured(content)
                return ModelResponse(
                    raw_content=content,
                    patch_proposal=proposal,
                    failure_type=f_type,
                    error=err_msg,
                    tokens_used=tokens,
                    model_name=model_name,
                    status_code=res.status_code,
                )

            except httpx.TimeoutException as te:
                last_failure_type = FailureType.TIMEOUT
                last_error = f"Request timed out after {self.timeout}s: {str(te)}"
                if attempt < self.max_retries:
                    sleep_time = min(self.max_backoff_seconds, self.backoff_factor * (2 ** attempt))
                    logger.warning(f"Timeout on model call. Retrying in {sleep_time:.2f}s...")
                    time.sleep(sleep_time)
                    continue
                break

            except (httpx.NetworkError, httpx.ConnectError, httpx.ReadError, httpx.WriteError) as ne:
                last_failure_type = FailureType.NETWORK_ERROR
                last_error = f"Network error connecting to {self.base_url}: {str(ne)}"
                if attempt < self.max_retries:
                    sleep_time = min(self.max_backoff_seconds, self.backoff_factor * (2 ** attempt))
                    logger.warning(f"Network error on model call. Retrying in {sleep_time:.2f}s...")
                    time.sleep(sleep_time)
                    continue
                break

            except Exception as e:
                last_failure_type = FailureType.UNKNOWN_PROVIDER_ERROR
                last_error = f"Unexpected provider error: {str(e)}"
                break

        return ModelResponse(
            raw_content="",
            patch_proposal=None,
            tokens_used=0,
            model_name=model_name,
            failure_type=last_failure_type,
            status_code=last_status_code,
            retry_after=last_retry_after,
            error=last_error,
        )

