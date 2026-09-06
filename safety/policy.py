import threading
from typing import Dict, List, Optional, Set
from pydantic import BaseModel, Field


class SafetyPolicy(BaseModel):
    # Task level defaults
    default_max_lines_added: int = Field(default=100, ge=0)
    default_max_lines_deleted: int = Field(default=50, ge=0)
    max_hunks_per_file: int = Field(default=10, ge=1)

    # Session / DAG cumulative limits
    max_cumulative_lines_added: int = Field(default=500, ge=0)
    max_cumulative_lines_deleted: int = Field(default=200, ge=0)

    # Allowed capabilities (capability-based security)
    allowed_capabilities: Set[str] = Field(default_factory=set)

    # Forbidden file patterns (sensitive configs, keys, VCS)
    forbidden_file_patterns: List[str] = Field(
        default_factory=lambda: [
            ".git",
            ".git/*",
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

    # v2.3 Phase A — Write-Scope isolation: patterns identifying TEST files
    # that a Dev-agent task must never write or delete. Only QA-authored
    # tasks (role='qa', e.g. red-test authoring) may touch these. The
    # ScopeGuard consults this policy list (not its own constants) so the
    # rule is configurable and introspectable at the policy layer.
    dev_forbidden_test_file_patterns: List[str] = Field(
        default_factory=lambda: [
            "*Test.cs",
            "*Tests.cs",
            "*Test.java",
            "*Tests.java",
            "*Test.ts",
            "*Tests.ts",
            "*Test.js",
            "*Tests.js",
            "test_*.py",
            "*_test.py",
            "*_test.go",
            "*_test.dart",
            "*.test.js",
            "*.spec.js",
            "*.test.ts",
            "*.spec.ts",
            # Directory patterns: anything INSIDE such a directory is a test file.
            "*Tests/*",
            "*Test/*",
            "tests/*",
            "test/*",
            "__tests__/*",
        ]
    )


class BudgetReservation(BaseModel):
    reservation_id: str
    lines_added: int
    lines_deleted: int
    committed: bool = False
    released: bool = False


class SessionBudgetTracker:
    def __init__(self, policy: SafetyPolicy):
        self.policy = policy
        self.cumulative_lines_added = 0
        self.cumulative_lines_deleted = 0
        self.tasks_executed = 0
        self._active_reservations: Dict[str, BudgetReservation] = {}
        self._lock = threading.Lock()

    def reserve(self, reservation_id: str, lines_added: int, lines_deleted: int) -> BudgetReservation:
        """Temporarily holds budget for an in-flight transaction attempt."""
        with self._lock:
            active_added = sum(r.lines_added for rid, r in self._active_reservations.items() if rid != reservation_id)
            active_deleted = sum(r.lines_deleted for rid, r in self._active_reservations.items() if rid != reservation_id)

            projected_added = self.cumulative_lines_added + active_added + lines_added
            projected_deleted = self.cumulative_lines_deleted + active_deleted + lines_deleted

            if projected_added > self.policy.max_cumulative_lines_added:
                raise ValueError(
                    f"Session cumulative budget exceeded: projected added lines ({projected_added}) > limit ({self.policy.max_cumulative_lines_added})"
                )

            if projected_deleted > self.policy.max_cumulative_lines_deleted:
                raise ValueError(
                    f"Session cumulative budget exceeded: projected deleted lines ({projected_deleted}) > limit ({self.policy.max_cumulative_lines_deleted})"
                )

            res = BudgetReservation(
                reservation_id=reservation_id,
                lines_added=lines_added,
                lines_deleted=lines_deleted,
            )
            self._active_reservations[reservation_id] = res
            return res

    def commit(self, reservation_id: str):
        """Permanently commits the reserved budget after successful integration."""
        with self._lock:
            res = self._active_reservations.pop(reservation_id, None)
            if res and not res.committed and not res.released:
                res.committed = True
                self.cumulative_lines_added += res.lines_added
                self.cumulative_lines_deleted += res.lines_deleted
                self.tasks_executed += 1

    def release(self, reservation_id: str):
        """Releases the reserved budget after a failed attempt, rollback, or dry-run."""
        with self._lock:
            res = self._active_reservations.pop(reservation_id, None)
            if res:
                res.released = True

    def check_and_add(self, lines_added: int, lines_deleted: int):
        """Directly checks and commits lines (legacy helper)."""
        with self._lock:
            active_added = sum(r.lines_added for r in self._active_reservations.values())
            active_deleted = sum(r.lines_deleted for r in self._active_reservations.values())

            projected_added = self.cumulative_lines_added + active_added + lines_added
            projected_deleted = self.cumulative_lines_deleted + active_deleted + lines_deleted

            if projected_added > self.policy.max_cumulative_lines_added:
                raise ValueError(
                    f"Session cumulative budget exceeded: added lines ({projected_added}) > limit ({self.policy.max_cumulative_lines_added})"
                )

            if projected_deleted > self.policy.max_cumulative_lines_deleted:
                raise ValueError(
                    f"Session cumulative budget exceeded: deleted lines ({projected_deleted}) > limit ({self.policy.max_cumulative_lines_deleted})"
                )

            self.cumulative_lines_added += lines_added
            self.cumulative_lines_deleted += lines_deleted
            self.tasks_executed += 1

    def reset(self):
        with self._lock:
            self.cumulative_lines_added = 0
            self.cumulative_lines_deleted = 0
            self.tasks_executed = 0
            self._active_reservations.clear()

