from pathlib import Path
from typing import List, Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PipelineConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PIPELINE_")

    workspace_root: Path = Path(".").resolve()
    worktree_dir: Optional[Path] = None
    allowed_untracked_paths: List[str] = Field(default_factory=list)
    dry_run: bool = False
    default_timeout_seconds: int = Field(default=30, gt=0, le=3600)
    log_level: str = "INFO"
    strict_git_check: bool = True
    build_command: Optional[List[str]] = None
    test_command: Optional[List[str]] = None
    behavioral_command: Optional[List[str]] = None
    enable_baseline_cache: bool = True
    max_file_size_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    max_patch_size_bytes: int = Field(default=1 * 1024 * 1024, ge=1024)
    max_files_in_patch: int = Field(default=50, ge=1)


def get_model_family(model_name: str) -> str:
    """Returns canonical model family (e.g., google, alibaba, deepseek, anthropic, openai).

    Enforces cross-family separation for anti-reward-hacking (v2.3).
    """
    if not model_name:
        return "unknown"
    name = str(model_name).lower().strip()
    if any(k in name for k in ["gemini", "agy", "antigravity", "google"]):
        return "google"
    if any(k in name for k in ["qwen", "alibaba"]):
        return "alibaba"
    if any(k in name for k in ["deepseek", "ds"]):
        return "deepseek"
    if any(k in name for k in ["claude", "anthropic", "sonnet", "opus", "haiku"]):
        return "anthropic"
    if any(k in name for k in ["gpt", "openai", "codex", "o1", "o3", "o4"]):
        return "openai"
    if any(k in name for k in ["glm", "zhipu", "z-ai", "tokenrouter"]):
        return "zhipu"
    if "mock" in name or "test" in name:
        return f"mock_{name}"
    return f"unknown_{name}"

