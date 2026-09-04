import io
import logging
import os
import signal
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from pydantic import BaseModel, Field

logger = logging.getLogger("dsh.build")

DEFAULT_MAX_OUTPUT_BYTES = 512 * 1024  # 512 KB


class BuildErrorDetail(BaseModel):
    file: Optional[str] = None
    line: Optional[int] = None
    column: Optional[int] = None
    code: Optional[str] = None
    message: str


class BuildResult(BaseModel):
    success: bool
    exit_code: int = 0
    timed_out: bool = False
    terminated_by_signal: Optional[int] = None
    truncated: bool = False
    errors: List[BuildErrorDetail] = Field(default_factory=list)
    raw_output: str = ""


def terminate_process_tree(proc: subprocess.Popen, timeout_grace: float = 0.5):
    """Terminates an entire process group safely to prevent orphan child/worker processes."""
    if proc.poll() is not None:
        return

    pid = proc.pid
    pgid = None
    try:
        # On POSIX systems with start_new_session=True, kill the process group
        if hasattr(os, "killpg") and hasattr(os, "getpgid"):
            try:
                # Capture the pgid up-front: the direct child may exit (and be
                # reaped by proc.wait below) while grandchildren in the same
                # group are still alive, at which point os.getpgid(pid) would
                # already raise ProcessLookupError.
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
        else:
            proc.terminate()

        try:
            proc.wait(timeout=timeout_grace)
        except subprocess.TimeoutExpired:
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
            else:
                proc.kill()
            proc.wait(timeout=1.0)

        # Final confirmation: the direct child may exit on SIGTERM while
        # grandchildren in the same process group survive it (e.g. SIG_IGN).
        # Verify the whole group is gone and, if not, escalate with one more
        # best-effort SIGKILL to the group, then wait briefly (bounded, no
        # infinite loop). Never raise from this confirmation step.
        if hasattr(os, "killpg") and hasattr(os, "getpgid") and pgid is not None:
            group_alive = True
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                group_alive = False
            except PermissionError:
                # Group exists but is owned by another user; we cannot
                # verify or signal it further. Treat as cleaned up.
                group_alive = False

            if group_alive:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                # Bounded wait (<=1s) for the group to settle; poll with
                # killpg(pgid, 0) until it raises ProcessLookupError.
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    try:
                        os.killpg(pgid, 0)
                    except ProcessLookupError:
                        break
                    except PermissionError:
                        break
                    time.sleep(0.1)
                else:
                    logger.warning(
                        f"Orphan processes may remain in process group {pgid} after SIGKILL"
                    )
    except (ProcessLookupError, PermissionError):
        pass
    except Exception as e:
        logger.warning(f"Error terminating process tree for pid {pid}: {e}")


def run_hardened_command(
    cmd: List[str],
    cwd: Path,
    timeout_seconds: Optional[int] = 30,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, bool, Optional[int], bool]:
    """Runs a subprocess with process-group isolation, bounded memory output, and deterministic timeouts.

    Returns:
        (returncode, raw_output, timed_out, signal_num, truncated)
    """
    if not cmd:
        return -1, "Empty command.", False, None, False

    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    # Disable interactive prompts in subprocesses
    merged_env["GIT_TERMINAL_PROMPT"] = "0"
    merged_env["LC_ALL"] = "C"

    # Start in a new session / process group on POSIX
    popen_kwargs = {
        "cwd": cwd,
        "env": merged_env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
    }
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
    except FileNotFoundError as fnf:
        return 127, f"Executable not found: {cmd[0]} ({fnf})", False, None, False
    except Exception as exc:
        return -1, f"Failed to spawn process {cmd[0]}: {str(exc)}", False, None, False

    timed_out = False
    raw_bytes = b""
    try:
        raw_bytes, _ = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_tree(proc)
        try:
            raw_bytes, _ = proc.communicate(timeout=1.0)
        except Exception:
            pass

    returncode = proc.returncode if proc.returncode is not None else -1
    sig_num = None
    if returncode < 0:
        sig_num = -returncode

    # Bounded output handling
    truncated = False
    if len(raw_bytes) > max_output_bytes:
        truncated = True
        half = max_output_bytes // 2
        head = raw_bytes[:half].decode("utf-8", errors="replace")
        tail = raw_bytes[-half:].decode("utf-8", errors="replace")
        output_str = f"{head}\n\n[... TRUNCATED: Output exceeded {max_output_bytes} bytes ({len(raw_bytes)} bytes total) ...]\n\n{tail}"
    else:
        output_str = raw_bytes.decode("utf-8", errors="replace")

    return returncode, output_str, timed_out, sig_num, truncated


class BaseBuildRunner(ABC):
    @abstractmethod
    def build(self, repo_path: Path) -> BuildResult:
        pass


class MockBuildRunner(BaseBuildRunner):
    def __init__(
        self,
        should_succeed: bool = True,
        errors: Optional[List[BuildErrorDetail]] = None,
        exit_code: Optional[int] = None,
        timed_out: bool = False,
        raw_output: Optional[str] = None,
    ):
        self.should_succeed = should_succeed
        self.errors = errors or []
        self.exit_code = exit_code if exit_code is not None else (0 if should_succeed else 1)
        self.timed_out = timed_out
        self.raw_output = raw_output

    def build(self, repo_path: Path) -> BuildResult:
        if self.should_succeed:
            return BuildResult(
                success=True,
                exit_code=0,
                raw_output=self.raw_output or "Build Succeeded (Mock).",
            )
        return BuildResult(
            success=False,
            exit_code=self.exit_code,
            timed_out=self.timed_out,
            errors=self.errors or [BuildErrorDetail(message="Mock compilation error CS1002: ; expected")],
            raw_output=self.raw_output or "Build Failed (Mock).",
        )


class SubprocessBuildRunner(BaseBuildRunner):
    def __init__(
        self,
        build_cmd: List[str],
        timeout_seconds: Optional[int] = 30,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ):
        self.build_cmd = build_cmd
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes

    def build(self, repo_path: Path) -> BuildResult:
        if not self.build_cmd:
            return BuildResult(
                success=False,
                exit_code=-1,
                errors=[BuildErrorDetail(message="No build command configured.")],
                raw_output="Empty build command.",
            )

        returncode, raw_output, timed_out, sig_num, truncated = run_hardened_command(
            cmd=self.build_cmd,
            cwd=repo_path,
            timeout_seconds=self.timeout_seconds,
            max_output_bytes=self.max_output_bytes,
        )

        if timed_out:
            msg = f"Build command timed out after {self.timeout_seconds}s: {' '.join(self.build_cmd)}"
            return BuildResult(
                success=False,
                exit_code=-1,
                timed_out=True,
                truncated=truncated,
                errors=[BuildErrorDetail(message=msg)],
                raw_output=f"{raw_output}\n{msg}".strip(),
            )

        if returncode == 0:
            return BuildResult(
                success=True,
                exit_code=0,
                truncated=truncated,
                raw_output=raw_output,
            )

        # Non-zero exit code: parse diagnostics
        error_lines = [line for line in raw_output.splitlines() if "error" in line.lower()]
        if not error_lines:
            error_lines = [line.strip() for line in raw_output.splitlines() if line.strip()]
        
        errors = [BuildErrorDetail(message=line) for line in error_lines] or [
            BuildErrorDetail(message=f"Build exited with code {returncode}")
        ]

        return BuildResult(
            success=False,
            exit_code=returncode,
            timed_out=False,
            terminated_by_signal=sig_num,
            truncated=truncated,
            errors=errors,
            raw_output=raw_output,
        )


