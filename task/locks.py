from typing import Dict, List, Optional, Set
from task.schema import TaskDefinition


class ResourceLockManager:
    def __init__(self):
        # Maps locked file path -> active task_id
        self.file_locks: Dict[str, str] = {}

    def can_acquire(self, task: TaskDefinition) -> bool:
        """Returns True if none of the task's allowed_files are currently locked by other tasks."""
        for f in task.allowed_files:
            norm_f = f.replace("\\", "/").lstrip("/")
            if norm_f in self.file_locks:
                return False
        return True

    def acquire(self, task: TaskDefinition) -> bool:
        if not self.can_acquire(task):
            return False
        for f in task.allowed_files:
            norm_f = f.replace("\\", "/").lstrip("/")
            self.file_locks[norm_f] = task.task_id
        return True

    def release(self, task: TaskDefinition):
        for f in task.allowed_files:
            norm_f = f.replace("\\", "/").lstrip("/")
            if self.file_locks.get(norm_f) == task.task_id:
                del self.file_locks[norm_f]
