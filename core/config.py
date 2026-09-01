from pathlib import Path
from pydantic_settings import BaseSettings


class PipelineConfig(BaseSettings):
    workspace_root: Path = Path(".").resolve()
    dry_run: bool = False
    default_timeout_seconds: int = 30
    log_level: str = "INFO"
    strict_git_check: bool = True

    class Config:
        env_prefix = "PIPELINE_"
