import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestrator.coordinator import AutonomousCoordinator


class _StubQA:
    name = "stub-qa"

    def query(self, prompt, timeout=None):
        return "ok"


def _git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def test_manifest_init_does_not_mutate_gitignore(tmp_path: Path):
    """Bug #7: _manifest_init must not pollute target repo .gitignore."""
    repo = _git_repo(tmp_path)
    coord = AutonomousCoordinator(
        target_repo=repo,
        qa_client=_StubQA(),
        dev_provider=MagicMock(),
        dry_run=True,
        test_mode=True,
    )
    run_dir = repo / "run_1725540000"
    coord._manifest_init(run_dir)

    gitignore = repo / ".gitignore"
    assert not gitignore.exists(), ".gitignore must NOT be created or mutated by manifest_init"
    assert (run_dir / "manifest.json").exists()


def test_shadow_run_dir_outside_repo_does_not_crash_allowance(tmp_path: Path):
    repo = _git_repo(tmp_path)
    coord = AutonomousCoordinator(
        target_repo=repo,
        qa_client=_StubQA(),
        dev_provider=MagicMock(),
        dry_run=True,
        test_mode=True,
    )
    shadow_run_dir = tmp_path.parent / "shadow_runs" / "run_1725540000"
    coord._run_dir = shadow_run_dir
    allowance = coord._untracked_allowance_for_manifest()
    assert allowance is None or isinstance(allowance, list)
