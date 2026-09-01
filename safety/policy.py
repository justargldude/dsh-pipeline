from typing import List, Set
from pydantic import BaseModel, Field


class SafetyPolicy(BaseModel):
    # Task level defaults
    default_max_lines_added: int = 100
    default_max_lines_deleted: int = 50
    max_hunks_per_file: int = 10

    # Session / DAG cumulative limits
    max_cumulative_lines_added: int = 500
    max_cumulative_lines_deleted: int = 200

    # Forbidden file patterns (sensitive configs, keys, VCS)
    forbidden_file_patterns: List[str] = Field(
        default_factory=lambda: [
            ".git",
            ".env",
            "id_rsa",
            "id_ed25519",
            "*.pem",
            "*.key",
            "*.pfx",
            "*.kdbx",
            "passwd",
            "shadow",
        ]
    )


class SessionBudgetTracker:
    def __init__(self, policy: SafetyPolicy):
        self.policy = policy
        self.cumulative_lines_added = 0
        self.cumulative_lines_deleted = 0
        self.tasks_executed = 0

    def check_and_add(self, lines_added: int, lines_deleted: int):
        projected_added = self.cumulative_lines_added + lines_added
        projected_deleted = self.cumulative_lines_deleted + lines_deleted

        if projected_added > self.policy.max_cumulative_lines_added:
            raise ValueError(
                f"Session cumulative budget exceeded: added lines ({projected_added}) > limit ({self.policy.max_cumulative_lines_added})"
            )

        if projected_deleted > self.policy.max_cumulative_lines_deleted:
            raise ValueError(
                f"Session cumulative budget exceeded: deleted lines ({projected_deleted}) > limit ({self.policy.max_cumulative_lines_deleted})"
            )

        self.cumulative_lines_added = projected_added
        self.cumulative_lines_deleted = projected_deleted
        self.tasks_executed += 1

    def reset(self):
        self.cumulative_lines_added = 0
        self.cumulative_lines_deleted = 0
        self.tasks_executed += 0
