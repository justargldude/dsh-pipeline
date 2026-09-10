"""Bug mapping smoke10 (2026-09-11): red test vẫn lọt review diff qua
TaskDefinition thiếu test_file.

Coordinator tạo TaskDefinition KHÔNG copy test_file từ PlannedTask (field
không tồn tại trên TaskDefinition) → runtime.execute_transaction không biết
red test file để exclude → capture_diff(exclude_paths=...) rỗng phần red
test (chỉ có holdouts) → QA reviewer thấy "Dev modified test_util.py /
removed test_add" (thực chất là red test CỦA QA ghi đè) → REJECTED sai.

Fix: runtime nhận danh sách QA-injected paths qua attribute riêng
`_qa_injected_paths` (set bởi coordinator cùng _pending_holdouts) —
capture_diff exclude cả list này (ngoài test_file nếu có + holdouts).
"""
import subprocess
from pathlib import Path

from core.workspace import TransactionWorktree
from core.runtime import DSHRuntime
from core.config import PipelineConfig


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "util.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "test_util.py").write_text(
        "from util import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def test_runtime_excludes_qa_injected_paths_from_review_diff(tmp_path):
    """runtime._qa_injected_paths (do coordinator set) phải được truyền vào
    capture_diff exclude khi dry-run capture — không phụ thuộc task.test_file
    (TaskDefinition không có field đó)."""
    from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk

    repo = _git_repo(tmp_path)
    runtime = DSHRuntime(workspace_path=repo, config=PipelineConfig(workspace_root=repo), dry_run=True)

    # Coordinator-injected info: red test ghi đè test_util.py (QA-owned)
    runtime._qa_injected_paths = {"test_util.py"}
    runtime._pending_holdouts = []

    # Dev patch chỉ util.py (đúng scope)
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="util.py",
                hunks=[
                    PatchHunk(
                        old_text="def add(a, b):\n    return a + b\n",
                        new_text="def add(a, b):\n    return a + b\n\ndef multiply(a, b):\n    return a * b\n",
                    )
                ],
            )
        ],
        reason="add multiply",
        confidence=0.9,
    )
    # Simulate QA red test overwrite (callback sẽ làm điều này thật)
    (repo / "test_util.py").write_text(
        "from util import add, multiply\n\ndef test_multiply():\n    assert multiply(3, 4) == 12\n",
        encoding="utf-8",
    )
    task = TaskDefinition(
        task_id="TASK_001",
        title="add multiply",
        allowed_files=["util.py"],
    )
    config = PipelineConfig(workspace_root=repo)
    config.test_command = ["python3", "-m", "pytest", "-q"]
    config.build_command = ["python3", "-m", "pytest", "-q"]
    runtime_config = DSHRuntime(workspace_path=repo, config=config, dry_run=True)
    runtime_config._qa_injected_paths = {"test_util.py"}

    result = runtime_config.execute_transaction(task, proposal)
    # dry-run: success và diff PHẢI không chứa test_util.py
    assert result.dry_run is True
    assert result.worktree_diff is not None, "phải capture được diff"
    assert "test_util.py" not in result.worktree_diff, (
        "red test QA-injected (ghi đè test_util.py) lọt vào review diff — "
        "reviewer sẽ gán nhầm cho Dev"
    )
    assert "multiply" in result.worktree_diff, "thay đổi thật của Dev phải có trong diff"
