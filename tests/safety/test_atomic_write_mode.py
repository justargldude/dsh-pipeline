"""TDD red tests: atomic_write_file must preserve existing file mode (bug #8).

Pipeline round-2 TASK_001: patches applied to executable files (mode 100755)
were silently downgraded to 100644 because os.replace() propagates the temp
file's mode (created with default umask 644) onto the target. QA review in the
previous operational session caught this twice on bin/ask-ds.js.
"""
import os
import stat
from pathlib import Path

import pytest

from safety.patch_engine import atomic_write_file


def test_atomic_write_preserves_executable_mode(tmp_path: Path):
    """Patch engine must not downgrade 0755 executables to 0644."""
    target = tmp_path / "run_tool.sh"
    target.write_text("#!/bin/sh\necho v1\n", encoding="utf-8")
    os.chmod(target, 0o755)
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o755

    atomic_write_file(target, "#!/bin/sh\necho v2\n")

    assert stat.S_IMODE(os.stat(target).st_mode) == 0o755, (
        "atomic_write_file replaced a 0755 executable with a 0644 regular file; "
        "os.replace() must carry over the original st_mode onto the temp file first."
    )
    assert target.read_text(encoding="utf-8") == "#!/bin/sh\necho v2\n"


def test_atomic_write_preserves_readonly_mode(tmp_path: Path):
    """A non-executable but read-only target (0444) keeps its mode after write."""
    target = tmp_path / "config.lock"
    target.write_text("v1", encoding="utf-8")
    os.chmod(target, 0o444)

    atomic_write_file(target, "v2")

    assert stat.S_IMODE(os.stat(target).st_mode) == 0o444


def test_atomic_write_new_file_uses_default_mode(tmp_path: Path):
    """Creating a brand-new file must NOT inherit anything weird (umask default)."""
    target = tmp_path / "brand_new.txt"
    atomic_write_file(target, "hello")

    mode = stat.S_IMODE(os.stat(target).st_mode)
    # umask-dependent (usually 0o644); only assert it is NOT executable.
    assert not (mode & 0o111), f"new file unexpectedly executable: {oct(mode)}"


def test_atomic_write_mode_survives_crlf_normalization(tmp_path: Path):
    """Mode preservation and CRLF newline handling must compose."""
    target = tmp_path / "windows_tool.bat"
    target.write_bytes(b"@echo off\r\nv1\r\n")
    os.chmod(target, 0o755)

    atomic_write_file(target, "@echo off\nv2\n")

    assert stat.S_IMODE(os.stat(target).st_mode) == 0o755
    assert target.read_bytes() == b"@echo off\r\nv2\r\n"
