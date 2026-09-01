import subprocess
from pathlib import Path
from typing import List, Optional
from core.state import WorkspaceState


class WorkspaceError(Exception):
    pass


class WorkspaceManager:
    def __init__(self, repo_path: Path):
        self.repo_path = repo_path.resolve()
        if not (self.repo_path / ".git").exists():
            raise WorkspaceError(f"Directory {self.repo_path} is not a valid Git repository.")

    def _run_git(self, *args) -> str:
        cmd = ["git", *args]
        res = subprocess.run(
            cmd,
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            raise WorkspaceError(
                f"Git command failed ({' '.join(args)}):\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}"
            )
        return res.stdout.strip()

    def get_head_commit(self) -> str:
        return self._run_git("rev-parse", "HEAD")

    def get_status(self) -> WorkspaceState:
        status_raw = self._run_git("status", "--porcelain")
        lines = [line.strip() for line in status_raw.splitlines() if line.strip()]
        modified = [line[3:] for line in lines]
        head = self.get_head_commit()
        return WorkspaceState(
            git_head=head,
            dirty=len(lines) > 0,
            modified_files=modified,
        )

    def is_clean(self, allowed_untracked_paths: Optional[List[str]] = None) -> bool:
        status_raw = self._run_git("status", "--porcelain")
        lines = [line.strip() for line in status_raw.splitlines() if line.strip()]
        if not lines:
            return True
        if allowed_untracked_paths:
            remaining = []
            for line in lines:
                path = line[3:]
                if not any(path.startswith(allowed) for allowed in allowed_untracked_paths):
                    remaining.append(line)
            return len(remaining) == 0
        return False

    def create_checkpoint(self, task_id: str, allow_untracked: Optional[List[str]] = None) -> str:
        if not self.is_clean(allowed_untracked_paths=allow_untracked):
            state = self.get_status()
            raise WorkspaceError(
                f"Cannot create checkpoint for {task_id}: Workspace is dirty.\n"
                f"Modified/Untracked files: {state.modified_files}"
            )
        head = self.get_head_commit()
        tag_name = f"ckpt_{task_id}_{head[:7]}"
        self._run_git("tag", "-f", tag_name, head)
        return tag_name

    def rollback(self, checkpoint_tag: str):
        try:
            self._run_git("reset", "--hard", checkpoint_tag)
            self._run_git("clean", "-fd")
        finally:
            try:
                self._run_git("tag", "-d", checkpoint_tag)
            except Exception:
                pass

    def commit(self, task_id: str, message: str) -> str:
        self._run_git("add", "-A")
        diff_staged = self._run_git("diff", "--staged")
        if not diff_staged:
            raise WorkspaceError("Cannot commit: No staged changes detected after applying patch.")
        commit_msg = f"[{task_id}] {message}"
        self._run_git("commit", "-m", commit_msg)
        return self.get_head_commit()
