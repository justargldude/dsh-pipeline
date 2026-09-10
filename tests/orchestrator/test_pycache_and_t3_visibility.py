"""Bug 2026-09-11 smoke orchestrate (muse qua OmniRoute): 2 regression thật.

BUG A — __pycache__ *.pyc bị coi là UNEXPECTED_CHANGES:
  Worktree T3 chạy pytest → sinh __pycache__/*.pyc (untracked).
  verify_changed_paths() cờ đỏ → SCOPE_VIOLATION → hard-halt.
  pyc là artifact build VÔ TẠI, không phải model viết — phải bị bỏ qua
  như mọi build artifact (.gitignore-style noise), không cờ đỏ.

BUG B — T3 Regression chỉ báo "Test runner exited with code 2":
  pytest exit 2 = COLLECTION ERROR (ImportError: cannot import 'multiply').
  _parse_broken_tests() không bắt dòng "E   ImportError..." cũng không
  fallback kèm output → Dev retry 3 lần MÙ (context chỉ có câu chung chung,
  không có lỗi thật) → hard-halt thay vì Dev sửa đúng chỗ.

Smoke证据 (2026-09-11, /tmp/smoke4, qa=auto/best-coding,
dev=oc-local/muse-spark-1.3-contributor-free, T1/T2/T3 pipeline thật):
- attempt 1: PATCH_APPLIED → T3 "Test runner exited with code 2" (mù)
- repro: UNEXPECTED_CHANGES: ['__pycache__/test_util.cpython-312-pytest-9.1.1.pyc',
  '__pycache__/util.cpython-312.pyc']
"""
import subprocess
from pathlib import Path

import pytest

from core.workspace import TransactionWorktree
from validation.regression import SubprocessRegressionValidator


# ── BUG A: __pycache__ noise trong verify_changed_paths ──

def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "util.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def test_verify_changed_paths_ignores_pycache_artifacts(tmp_path):
    """pytest chạy trong worktree sinh __pycache__/*.pyc — KHÔNG được coi là
    unexpected (build artifact vô hại, không do model tạo chủ đích)."""
    repo = _git_repo(tmp_path)
    (repo / "util.py").write_text("x = 2\n", encoding="utf-8")
    pyc_dir = repo / "__pycache__"
    pyc_dir.mkdir()
    (pyc_dir / "util.cpython-312.pyc").write_bytes(b"\x00\x01")
    (pyc_dir / "test_util.cpython-312-pytest-9.1.1.pyc").write_bytes(b"\x00\x01")

    wt = TransactionWorktree(
        tx_id="tx_test",
        base_commit="HEAD",
        worktree_path=repo,
        main_repo_path=repo,
    )
    ok, unexpected = wt.verify_changed_paths(expected_paths={"util.py"})
    assert ok, f"__pycache__/*.pyc bị cờ đỏ là unexpected: {unexpected}"


def test_verify_changed_paths_ignores_pytest_cache_dir(tmp_path):
    """Tương tự: .pytest_cache/ cũng là artifact vô hại."""
    repo = _git_repo(tmp_path)
    (repo / "util.py").write_text("x = 2\n", encoding="utf-8")
    cache = repo / ".pytest_cache" / "v" / "cache"
    cache.mkdir(parents=True)
    (cache / "lastfailed").write_text("[]", encoding="utf-8")

    wt = TransactionWorktree(
        tx_id="tx_test",
        base_commit="HEAD",
        worktree_path=repo,
        main_repo_path=repo,
    )
    ok, unexpected = wt.verify_changed_paths(expected_paths={"util.py"})
    assert ok, f".pytest_cache/ bị cờ đỏ: {unexpected}"


# ── BUG B: T3 collection error không có output cho Dev ──

def _run_pytest_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "pyrepo"
    repo.mkdir()
    (repo / "util.py").write_text("def add(a,b):\n    return a+b\n", encoding="utf-8")
    (repo / "test_broken_import.py").write_text(
        "from util import multiply\n\ndef test_m():\n    assert multiply(3,4)==12\n",
        encoding="utf-8",
    )
    return repo


def test_regression_validator_reports_collection_error_details(tmp_path):
    """pytest exit 2 (collection error) phải báo ImportError cụ thể —
    không chỉ "Test runner exited with code 2" — để Dev retry có thông tin."""
    repo = _run_pytest_repo(tmp_path)
    v = SubprocessRegressionValidator(
        test_cmd=["python3", "-m", "pytest", "-q", "--no-header"],
        timeout_seconds=60,
    )
    r = v.validate_regression(repo)
    assert not r.success
    joined = "\n".join(r.broken_tests)
    assert "ImportError" in joined or "ModuleNotFoundError" in joined, (
        f"phải chứa lỗi import thật, got: {joined!r}"
    )


def test_regression_validator_output_captured_on_failure(tmp_path):
    """Output pytest thô phải được giữ lại (r.output) cho recovery context."""
    repo = _run_pytest_repo(tmp_path)
    v = SubprocessRegressionValidator(
        test_cmd=["python3", "-m", "pytest", "-q", "--no-header"],
        timeout_seconds=60,
    )
    r = v.validate_regression(repo)
    assert not r.success
    assert "multiply" in r.output, "raw output phải chứa tên symbol thiếu"
