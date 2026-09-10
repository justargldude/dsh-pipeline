"""Bug 2026-09-11 smoke 9: QA red test bị gán cho Dev trong review diff.

Chuỗi sự kiện thật (qa=auto/best-coding, dev=oc-local/muse-spark-1.3-contributor-free):
1. Coordinator inject red test của QA (test_task_001_visible.py) vào worktree
   qua on_worktree_created callback — ĐÚNG thiết kế (Bug #9 fix).
2. Dev patch chỉ util.py — T0/T1/T2/T3 ĐỀU PASS, RECOVERY_SUCCEEDED.
3. Dry-run: capture_diff() thu TOÀN BỘ worktree changes — gồm cả red test
   file của QA (untracked).
4. QA reviewer thấy diff có "new file test_task_001_visible.py" → REJECTED
   với lý do "Dev subagent created a new test file, violating scope"!

Red test là CỦA QA, không phải patch của Dev — review diff phải loại nó
(và cả holdout test QA-injected) ra, nếu không mọi orchestrate dry-run đều
bị QA gate từ chối sai.
"""
import subprocess
from pathlib import Path

from core.workspace import TransactionWorktree


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


def _worktree_for(repo: Path) -> TransactionWorktree:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    return TransactionWorktree(
        tx_id="tx_test",
        base_commit=head,
        worktree_path=repo,
        main_repo_path=repo,
    )


def test_capture_diff_excludes_injected_red_test(tmp_path):
    """Red test QA-injected (exclude_paths) không được xuất hiện trong review diff."""
    repo = _git_repo(tmp_path)
    # Dev thay đổi thật
    (repo / "util.py").write_text("x = 2\n", encoding="utf-8")
    # QA inject red test (untracked)
    (repo / "test_task_001_visible.py").write_text(
        "from util import multiply\n\ndef test_m():\n    assert multiply(3,4)==12\n",
        encoding="utf-8",
    )
    wt = _worktree_for(repo)
    diff = wt.capture_diff(exclude_paths={"test_task_001_visible.py"})
    assert "test_task_001_visible.py" not in diff, (
        "red test QA-injected bị gán cho Dev trong review diff"
    )
    assert "util.py" in diff and "x = 2" in diff, "thay đổi thật của Dev phải còn"


def test_capture_diff_excludes_holdout_test(tmp_path):
    """Holdout QA-injected cũng vậy."""
    repo = _git_repo(tmp_path)
    (repo / "util.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "tests_holdout_qa.py").write_text("def test_hidden():\n    assert True\n", encoding="utf-8")
    wt = _worktree_for(repo)
    diff = wt.capture_diff(exclude_paths={"tests_holdout_qa.py"})
    assert "tests_holdout_qa.py" not in diff
    assert "x = 2" in diff


def test_capture_diff_default_includes_everything(tmp_path):
    """Không truyền exclude_paths → behavior cũ (toàn bộ) — backward compat."""
    repo = _git_repo(tmp_path)
    (repo / "util.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "test_task_001_visible.py").write_text("y = 1\n", encoding="utf-8")
    wt = _worktree_for(repo)
    diff = wt.capture_diff()
    assert "test_task_001_visible.py" in diff


def test_capture_diff_exclusion_applies_to_untracked_noise_too(tmp_path):
    """Exclude hoạt động cho cả untracked lẫn tracked-modified paths."""
    repo = _git_repo(tmp_path)
    # Dev sửa 2 file, 1 trong đó bị exclude
    (repo / "util.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "extra_dev.py").write_text("z = 9\n", encoding="utf-8")  # untracked Dev file
    wt = _worktree_for(repo)
    diff = wt.capture_diff(exclude_paths={"extra_dev.py"})
    assert "extra_dev.py" not in diff
    assert "x = 2" in diff
