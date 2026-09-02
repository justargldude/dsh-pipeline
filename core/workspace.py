import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional, Set, Tuple

from core.state import FileStatus, TransactionState, WorkspaceState, WorktreeMetadata
from core.journal import TransactionJournal, JournalRecord
from safety.patch_engine import normalize_repo_path
from build.sandbox import terminate_process_tree

logger = logging.getLogger("dsh.workspace")


class WorkspaceError(Exception):
    pass


class WorktreeCleanupError(WorkspaceError):
    pass


class WorktreeStagingError(WorkspaceError):
    pass


def generate_transaction_id(task_id: str) -> str:
    """Generates a collision-resistant transaction identifier."""
    unique_suffix = uuid.uuid4().hex[:12]
    return f"tx_{task_id}_{unique_suffix}"


def parse_porcelain_v1_z(raw_bytes: bytes) -> List[FileStatus]:
    """Parses 'git status --porcelain=v1 -z' output safely into FileStatus objects.
    
    In -z mode, records are NUL-terminated.
    Normal format: XY <path>\0
    Rename/copy format: XY <new_path>\0<old_path>\0
    """
    if not raw_bytes:
        return []

    tokens = raw_bytes.split(b"\0")
    results: List[FileStatus] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token:
            i += 1
            continue
        if len(token) < 3:
            i += 1
            continue

        status_code = token[:2].decode("utf-8", errors="replace")
        path = token[3:].decode("utf-8", errors="replace")
        old_path = None

        if "R" in status_code or "C" in status_code:
            i += 1
            if i < len(tokens) and tokens[i]:
                old_path = tokens[i].decode("utf-8", errors="replace")

        results.append(FileStatus(status_code=status_code, path=path, old_path=old_path))
        i += 1

    return results


def parse_diff_cached_name_only_z(raw_bytes: bytes) -> List[str]:
    """Parses 'git diff --cached --name-only -z' output into a list of staged file paths."""
    if not raw_bytes:
        return []
    return [t.decode("utf-8", errors="replace") for t in raw_bytes.split(b"\0") if t]


def parse_diff_cached_name_status_z(raw_bytes: bytes) -> List[Tuple[str, str, Optional[str]]]:
    """Parses 'git diff --cached --name-status -z' output into (status, path, old_path) tuples."""
    if not raw_bytes:
        return []
    tokens = raw_bytes.split(b"\0")
    results: List[Tuple[str, str, Optional[str]]] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token:
            i += 1
            continue
        status_code = token.decode("utf-8", errors="replace")
        i += 1
        if i >= len(tokens):
            break
        path = tokens[i].decode("utf-8", errors="replace")
        old_path = None
        if status_code.startswith("R") or status_code.startswith("C"):
            old_path = path
            i += 1
            if i < len(tokens):
                path = tokens[i].decode("utf-8", errors="replace")
        results.append((status_code, path, old_path))
        i += 1
    return results


GIT_DETERMINISTIC_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "echo",
    "LC_ALL": "C",
}


def _run_git_subprocess(cwd: Path, *args, timeout: float = 30.0) -> Tuple[int, bytes, bytes]:
    """Runs a git command with process isolation, non-interactive environment, and timeout."""
    cmd = ["git", *args]
    merged_env = os.environ.copy()
    merged_env.update(GIT_DETERMINISTIC_ENV)

    popen_kwargs = {
        "cwd": cwd,
        "env": merged_env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
    except Exception as e:
        raise WorkspaceError(f"Failed to spawn git subprocess ({' '.join(args)}): {e}")

    try:
        stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_process_tree(proc)
        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=1.0)
        except Exception:
            stdout_bytes, stderr_bytes = b"", b""
        raise WorkspaceError(f"Git command timed out after {timeout}s: {' '.join(args)}")

    return proc.returncode, stdout_bytes, stderr_bytes


class TransactionWorktree:
    """Represents an isolated Git worktree dedicated to a single transaction."""

    def __init__(
        self,
        tx_id: str,
        base_commit: str,
        worktree_path: Path,
        main_repo_path: Path,
        created_at: Optional[float] = None,
        timeout: float = 30.0,
    ):
        self.tx_id = tx_id
        self.base_commit = base_commit
        self.worktree_path = worktree_path.resolve()
        self.main_repo_path = main_repo_path.resolve()
        self.created_at = created_at or time.time()
        self.timeout = timeout
        self.metadata = WorktreeMetadata(
            tx_id=tx_id,
            base_commit=base_commit,
            worktree_path=str(self.worktree_path),
            created_at=self.created_at,
            state=TransactionState.WORKTREE_READY,
        )

    def _run_git(self, *args) -> str:
        code, stdout_b, stderr_b = _run_git_subprocess(self.worktree_path, *args, timeout=self.timeout)
        stdout_str = stdout_b.decode("utf-8", errors="replace")
        stderr_str = stderr_b.decode("utf-8", errors="replace")
        if code != 0:
            raise WorkspaceError(
                f"Worktree Git command failed ({' '.join(args)}) [code {code}]:\nSTDOUT: {stdout_str}\nSTDERR: {stderr_str}"
            )
        return stdout_str.strip()

    def _run_git_bytes(self, *args) -> bytes:
        code, stdout_b, stderr_b = _run_git_subprocess(self.worktree_path, *args, timeout=self.timeout)
        if code != 0:
            raise WorkspaceError(
                f"Worktree Git command failed ({' '.join(args)}) [code {code}]:\nSTDOUT: {stdout_b.decode('utf-8', errors='replace')}\nSTDERR: {stderr_b.decode('utf-8', errors='replace')}"
            )
        return stdout_b

    def get_head_commit(self) -> str:
        return self._run_git("rev-parse", "HEAD")

    def get_status(self) -> WorkspaceState:
        raw_bytes = self._run_git_bytes("status", "--porcelain=v1", "-z")
        statuses = parse_porcelain_v1_z(raw_bytes)
        modified_paths = [s.path for s in statuses]
        head = self.get_head_commit()
        return WorkspaceState(
            git_head=head,
            dirty=len(statuses) > 0,
            modified_files=modified_paths,
            file_statuses=statuses,
        )

    def is_clean(self, allowed_untracked_paths: Optional[List[str]] = None) -> bool:
        raw_bytes = self._run_git_bytes("status", "--porcelain=v1", "-z")
        statuses = parse_porcelain_v1_z(raw_bytes)
        if not statuses:
            return True
        if allowed_untracked_paths:
            allowed_norm = []
            for a in allowed_untracked_paths:
                try:
                    allowed_norm.append(normalize_repo_path(a))
                except Exception:
                    allowed_norm.append(a.strip().replace("\\", "/").strip("/"))

            remaining = []
            for s in statuses:
                try:
                    norm_p = normalize_repo_path(s.path)
                except Exception:
                    norm_p = s.path.strip().replace("\\", "/").strip("/")

                if not any(norm_p.startswith(allowed) for allowed in allowed_norm if allowed):
                    remaining.append(s)
            return len(remaining) == 0
        return False

    def verify_changed_paths(
        self,
        expected_paths: Set[str],
        allowed_untracked_paths: Optional[List[str]] = None,
    ) -> Tuple[bool, List[str]]:
        """Verifies that actual changes in worktree strictly match expected paths."""
        raw_bytes = self._run_git_bytes("status", "--porcelain=v1", "-z")
        statuses = parse_porcelain_v1_z(raw_bytes)
        normalized_expected = {normalize_repo_path(p) for p in expected_paths}
        allowed = [normalize_repo_path(p) for p in (allowed_untracked_paths or [])]

        unexpected: List[str] = []
        for s in statuses:
            try:
                norm_path = normalize_repo_path(s.path)
            except Exception:
                norm_path = s.path.strip().replace("\\", "/").strip("/")

            if norm_path in normalized_expected:
                continue
            if any(norm_path.startswith(allow_p) for allow_p in allowed if allow_p):
                continue
            unexpected.append(s.path)

        return (len(unexpected) == 0, unexpected)

    def stage_exact(self, expected_paths: List[str]) -> None:
        """Stages ONLY the expected file paths and verifies staged state."""
        if not expected_paths:
            raise WorktreeStagingError("Cannot stage: Expected paths list is empty.")

        normalized_expected = {normalize_repo_path(p) for p in expected_paths}

        # Stage specific files
        try:
            self._run_git("add", "--", *list(normalized_expected))
        except WorkspaceError as e:
            raise WorktreeStagingError(f"Failed to stage expected paths {normalized_expected}: {e}")

        # Verify staged diff
        diff_bytes = self._run_git_bytes("diff", "--cached", "--name-only", "-z")
        staged_paths = set(parse_diff_cached_name_only_z(diff_bytes))
        norm_staged = {normalize_repo_path(p) for p in staged_paths}

        if not norm_staged:
            raise WorktreeStagingError("Cannot commit: No staged changes detected after staging expected files.")

        if not norm_staged.issubset(normalized_expected):
            unexpected = sorted(list(norm_staged - normalized_expected))
            raise WorktreeStagingError(
                f"Staging safety violation: Unexpected files were staged in worktree: {unexpected}"
            )

    def commit(
        self,
        task_id: str,
        message: str,
        expected_paths: Optional[List[str]] = None,
        allowed_untracked_paths: Optional[List[str]] = None,
    ) -> str:
        """Commits staged changes in the isolated worktree, performs post-commit verification,
        and verifies clean post-commit state.
        """
        commit_msg = f"[{task_id}] {message}"
        self._run_git("commit", "-m", commit_msg)

        head = self.get_head_commit()
        if head == self.base_commit:
            raise WorktreeStagingError("Commit failed: HEAD commit did not advance after commit command.")

        # Post-commit verification: verify exact files included in commit diff
        if expected_paths:
            norm_expected = {normalize_repo_path(p) for p in expected_paths}
            diff_tree_out = self._run_git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")
            committed_files = {normalize_repo_path(line.strip()) for line in diff_tree_out.splitlines() if line.strip()}
            if not committed_files.issubset(norm_expected):
                unexpected = committed_files - norm_expected
                raise WorktreeStagingError(f"Post-commit integrity error: Unexpected files entered commit: {unexpected}")

        # Detect unexpected post-hook mutations
        if not self.is_clean(allowed_untracked_paths=allowed_untracked_paths):
            raise WorktreeStagingError("Post-commit git hook unexpectedly mutated worktree state.")

        self.metadata.state = TransactionState.COMMITTED
        return head


class WorkspaceManager:
    """Manages the primary Git workspace and orchestrates isolated transaction worktrees."""

    def __init__(self, repo_path: Path, timeout: float = 30.0):
        self.repo_path = repo_path.resolve()
        self.timeout = timeout
        if not (self.repo_path / ".git").exists():
            raise WorkspaceError(f"Directory {self.repo_path} is not a valid Git repository.")
        self.journal = TransactionJournal(self.repo_path)
        self._integrate_lock = threading.Lock()

    def _run_git(self, *args) -> str:
        code, stdout_b, stderr_b = _run_git_subprocess(self.repo_path, *args, timeout=self.timeout)
        stdout_str = stdout_b.decode("utf-8", errors="replace")
        stderr_str = stderr_b.decode("utf-8", errors="replace")
        if code != 0:
            raise WorkspaceError(
                f"Git command failed ({' '.join(args)}) [code {code}]:\nSTDOUT: {stdout_str}\nSTDERR: {stderr_str}"
            )
        return stdout_str.strip()

    def _run_git_bytes(self, *args) -> bytes:
        code, stdout_b, stderr_b = _run_git_subprocess(self.repo_path, *args, timeout=self.timeout)
        if code != 0:
            raise WorkspaceError(
                f"Git command failed ({' '.join(args)}) [code {code}]:\nSTDOUT: {stdout_b.decode('utf-8', errors='replace')}\nSTDERR: {stderr_b.decode('utf-8', errors='replace')}"
            )
        return stdout_b

    def get_head_commit(self) -> str:
        return self._run_git("rev-parse", "HEAD")

    def get_status(self) -> WorkspaceState:
        raw_bytes = self._run_git_bytes("status", "--porcelain=v1", "-z")
        statuses = parse_porcelain_v1_z(raw_bytes)
        modified_paths = [s.path for s in statuses]
        head = self.get_head_commit()
        return WorkspaceState(
            git_head=head,
            dirty=len(statuses) > 0,
            modified_files=modified_paths,
            file_statuses=statuses,
        )

    def is_clean(self, allowed_untracked_paths: Optional[List[str]] = None) -> bool:
        raw_bytes = self._run_git_bytes("status", "--porcelain=v1", "-z")
        statuses = parse_porcelain_v1_z(raw_bytes)
        if not statuses:
            return True
        allowed = [".dsh", ".dsh_worktrees"]
        if allowed_untracked_paths:
            allowed.extend(allowed_untracked_paths)

        allowed_norm = []
        for a in allowed:
            try:
                allowed_norm.append(normalize_repo_path(a))
            except Exception:
                allowed_norm.append(a.strip().replace("\\", "/").strip("/"))

        remaining = []
        for s in statuses:
            try:
                norm_p = normalize_repo_path(s.path)
            except Exception:
                norm_p = s.path.strip().replace("\\", "/").strip("/")

            if not any(norm_p.startswith(allow) for allow in allowed_norm if allow):
                remaining.append(s)
        return len(remaining) == 0

    def create_transaction_worktree(
        self,
        tx_id: str,
        base_commit: Optional[str] = None,
        worktree_dir: Optional[Path] = None,
    ) -> TransactionWorktree:
        """Creates an isolated Git worktree detached at base_commit and logs to transaction journal."""
        commit = base_commit or self.get_head_commit()
        if worktree_dir is not None:
            wt_path = (worktree_dir / tx_id).resolve()
        else:
            wt_path = (self.repo_path / ".git" / "dsh_worktrees" / tx_id).resolve()

        wt_path.parent.mkdir(parents=True, exist_ok=True)
        if wt_path.exists():
            shutil.rmtree(wt_path, ignore_errors=True)

        try:
            self._run_git("worktree", "add", "--detach", str(wt_path), commit)
        except Exception as e:
            raise WorkspaceError(f"Failed to create isolated worktree for {tx_id} at {wt_path}: {e}")

        # Record in journal for crash safety
        self.journal.record_start(
            tx_id=tx_id,
            task_id=tx_id,
            base_commit=commit,
            worktree_path=str(wt_path),
            state=TransactionState.WORKTREE_READY,
        )

        return TransactionWorktree(
            tx_id=tx_id,
            base_commit=commit,
            worktree_path=wt_path,
            main_repo_path=self.repo_path,
        )

    def remove_transaction_worktree(
        self,
        tx_worktree: TransactionWorktree,
        force: bool = True,
    ) -> None:
        """Safely removes an isolated transaction worktree and updates crash journal."""
        wt_path = tx_worktree.worktree_path
        try:
            cmd = ["worktree", "remove"]
            if force:
                cmd.append("--force")
            cmd.append(str(wt_path))
            self._run_git(*cmd)
        except Exception as e:
            logger.warning(f"Git worktree remove failed: {e}. Attempting manual removal and prune.")
            try:
                if wt_path.exists():
                    shutil.rmtree(wt_path, ignore_errors=True)
                self._run_git("worktree", "prune")
            except Exception as e2:
                raise WorktreeCleanupError(
                    f"Failed to cleanly remove worktree {tx_worktree.tx_id} at {wt_path}: {e2}"
                )

        if wt_path.exists():
            try:
                shutil.rmtree(wt_path, ignore_errors=True)
            except Exception as e3:
                raise WorktreeCleanupError(
                    f"Failed to delete directory for worktree {tx_worktree.tx_id} at {wt_path}: {e3}"
                )

        try:
            self._run_git("worktree", "prune")
        except Exception:
            pass

        # Remove from journal
        self.journal.record_end(tx_worktree.tx_id)

    def get_orphaned_worktrees(self) -> List[JournalRecord]:
        """Returns orphaned transactions tracked in crash journal."""
        return self.journal.get_orphaned()

    def cleanup_orphaned_worktrees(self) -> List[str]:
        """Scans crash journal and prunes any stale/orphaned transaction worktrees."""
        orphaned = self.journal.get_orphaned()
        cleaned: List[str] = []
        for rec in orphaned:
            wt_path = Path(rec.worktree_path)
            try:
                if wt_path.exists():
                    shutil.rmtree(wt_path, ignore_errors=True)
                self._run_git("worktree", "prune")
                self.journal.record_end(rec.tx_id)
                cleaned.append(rec.tx_id)
            except Exception as e:
                logger.warning(f"Failed to cleanup orphaned worktree {rec.tx_id}: {e}")
        return cleaned

    def integrate_transaction(
        self,
        tx_worktree: TransactionWorktree,
        commit_hash: str,
    ) -> str:
        """Evaluates main HEAD and integrates the transaction commit if safe.
        
        Thread-safe: Uses an internal lock so concurrent worker threads do not
        race against main Git ref updates.

        Returns integration status:
            - 'INTEGRATED': Fast-forwarded main working tree safely.
            - 'READY_TO_INTEGRATE': Main tree has uncommitted user modifications or non-fast-forward divergence.
            - 'STALE_BASE': Main HEAD moved since transaction started.
        """
        with self._integrate_lock:
            current_head = self.get_head_commit()
            if current_head != tx_worktree.base_commit:
                logger.warning(
                    f"Main HEAD moved from {tx_worktree.base_commit[:7]} to {current_head[:7]}. "
                    f"Transaction {tx_worktree.tx_id} marked STALE_BASE."
                )
                return "STALE_BASE"

            # Check if main workspace is clean
            if not self.is_clean():
                logger.info(
                    f"Main workspace contains unrelated uncommitted changes. "
                    f"Preserving user changes; transaction {tx_worktree.tx_id} marked READY_TO_INTEGRATE."
                )
                return "READY_TO_INTEGRATE"

            # Main workspace is clean and at base_commit -> safe fast-forward
            try:
                self._run_git("merge", "--ff-only", commit_hash)
                return "INTEGRATED"
            except Exception as e:
                logger.warning(f"Fast-forward merge failed ({e}); marking READY_TO_INTEGRATE.")
                return "READY_TO_INTEGRATE"

    # Backward compatibility / legacy helpers
    def create_checkpoint(self, task_id: str, allow_untracked: Optional[List[str]] = None) -> str:
        head = self.get_head_commit()
        tag_name = f"ckpt_{generate_transaction_id(task_id)}"
        self._run_git("tag", "-f", tag_name, head)
        return tag_name

    def rollback(self, checkpoint_tag: str):
        try:
            self._run_git("tag", "-d", checkpoint_tag)
        except Exception:
            pass

    def commit(self, task_id: str, message: str, expected_paths: Optional[List[str]] = None) -> str:
        """Legacy in-place commit staging expected paths."""
        if expected_paths:
            self._run_git("add", "--", *expected_paths)
        else:
            status = self.get_status()
            if status.modified_files:
                self._run_git("add", "--", *status.modified_files)
        diff_staged = self._run_git("diff", "--staged")
        if not diff_staged:
            raise WorkspaceError("Cannot commit: No staged changes detected.")
        commit_msg = f"[{task_id}] {message}"
        self._run_git("commit", "-m", commit_msg)
        return self.get_head_commit()
