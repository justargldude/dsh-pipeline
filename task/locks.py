import threading
from typing import Dict, List, Optional, Set
from task.schema import TaskDefinition
from safety.patch_engine import normalize_repo_path


class ResourceLockManager:
    """Thread-safe resource manager for file-level locking during parallel DAG execution."""

    def __init__(self):
        # Maps locked file path -> active task_id
        self.file_locks: Dict[str, str] = {}
        self._lock = threading.Lock()

    def can_acquire(self, task: TaskDefinition) -> bool:
        """Returns True if none of the task's allowed_files are currently locked by other tasks."""
        with self._lock:
            for f in task.allowed_files:
                norm_f = normalize_repo_path(f)
                if norm_f in self.file_locks and self.file_locks[norm_f] != task.task_id:
                    return False
            return True

    def acquire(self, task: TaskDefinition) -> bool:
        """Atomically acquires locks for all allowed_files of the task."""
        with self._lock:
            for f in task.allowed_files:
                norm_f = normalize_repo_path(f)
                if norm_f in self.file_locks and self.file_locks[norm_f] != task.task_id:
                    return False

            for f in task.allowed_files:
                norm_f = normalize_repo_path(f)
                self.file_locks[norm_f] = task.task_id
            return True

    def release(self, task: TaskDefinition):
        """Atomically releases all locks held by the task."""
        with self._lock:
            for f in task.allowed_files:
                norm_f = normalize_repo_path(f)
                if self.file_locks.get(norm_f) == task.task_id:
                    del self.file_locks[norm_f]

    def reset(self):
        with self._lock:
            self.file_locks.clear()
