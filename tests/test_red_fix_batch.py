import subprocess
import threading
from pathlib import Path
import pytest

from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk
from core.runtime import DSHRuntime
from core.workspace import WorkspaceManager
from core.journal import TransactionJournal
from core.state import TransactionState
from validation.baseline import BaselineManager
from build.sandbox import MockBuildRunner


@pytest.fixture
def temp_git_repo(tmp_path: Path):
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test QA"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "qa@test.local"], cwd=repo, check=True)

    # Initial files
    (repo / "Player.cs").write_text("public class Player {\n    public void Update() {}\n}\n", encoding="utf-8")
    (repo / "UI.cs").write_text("public class UI {\n    public void Draw() {}\n}\n", encoding="utf-8")
    (repo / "Audio.cs").write_text("public class Audio {\n    public void Play() {}\n}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)
    return repo


class TestBug11QueueRename:
    def test_import_queue_is_stdlib(self):
        import queue
        assert "dsh-pipeline" not in str(getattr(queue, "__file__", "")), (
            f"queue should be from Python stdlib, but got {queue.__file__}"
        )
        assert hasattr(queue, "SimpleQueue"), "queue module missing SimpleQueue"

    def test_thread_pool_executor_instantiable(self):
        from concurrent.futures import ThreadPoolExecutor
        executor = ThreadPoolExecutor(max_workers=2)
        executor.shutdown(wait=False)

    def test_taskqueue_package_importable(self):
        from taskqueue.cloud_worker import CloudQueueWorker, CloudTaskItem
        assert CloudQueueWorker is not None
        assert CloudTaskItem is not None


class TestBug21BaselineCacheKey:
    def test_cache_key_uses_base_commit_not_moving_head(self, temp_git_repo: Path):
        mgr = BaselineManager(enable_cache=True)
        runner = MockBuildRunner(should_succeed=True)
        ws = WorkspaceManager(temp_git_repo)

        fixed_base = "AAA000111222333444555666777888999aaabbb"
        try:
            base1 = mgr.get_or_capture_baseline(temp_git_repo, ws, runner, base_commit=fixed_base)
        except TypeError:
            pytest.fail("base_commit param chưa được hỗ trợ")

        # Advance main repo HEAD with another commit
        (temp_git_repo / "new_commit.txt").write_text("advance head")
        subprocess.run(["git", "add", "."], cwd=temp_git_repo, check=True)
        subprocess.run(["git", "commit", "-m", "Advance main repo HEAD"], cwd=temp_git_repo, check=True)

        try:
            base2 = mgr.get_or_capture_baseline(temp_git_repo, ws, runner, base_commit=fixed_base)
        except TypeError:
            pytest.fail("base_commit param chưa được hỗ trợ")

        assert base1 is base2
        assert len(mgr._cache) == 1

    def test_different_base_commits_different_entries(self, temp_git_repo: Path):
        mgr = BaselineManager(enable_cache=True)
        runner = MockBuildRunner(should_succeed=True)
        ws = WorkspaceManager(temp_git_repo)

        try:
            base_a = mgr.get_or_capture_baseline(
                temp_git_repo, ws, runner, base_commit="AAA000111222333444555666777888999aaabbb"
            )
            base_b = mgr.get_or_capture_baseline(
                temp_git_repo, ws, runner, base_commit="BBB000111222333444555666777888999aaabbb"
            )
        except TypeError:
            pytest.fail("base_commit param chưa được hỗ trợ")

        assert base_a.cache_key != base_b.cache_key
        assert len(mgr._cache) == 2

    def test_default_uses_ws_head_fallback(self, temp_git_repo: Path):
        mgr = BaselineManager(enable_cache=True)
        runner = MockBuildRunner(should_succeed=True)
        ws = WorkspaceManager(temp_git_repo)

        base = mgr.get_or_capture_baseline(temp_git_repo, ws, runner)
        assert base is not None
        assert base.cache_key in mgr._cache
        assert base.base_commit == ws.get_head_commit()


class TestBug22NoCleanupNoFakeSuccess:
    def test_stale_base_retains_worktree_and_reports_failure(self, temp_git_repo: Path, monkeypatch):
        runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)

        task = TaskDefinition(task_id="T_STALE_1", title="Stale base task", allowed_files=["Player.cs"])
        proposal = PatchProposal(
            patches=[
                FilePatch(
                    file="Player.cs",
                    hunks=[
                        PatchHunk(
                            old_text="    public void Update() {}",
                            new_text="    public void Update() {\n        // Stale update\n    }",
                        )
                    ],
                )
            ]
        )

        orig_create_wt = runtime.ws.create_transaction_worktree

        def wrapped_create_wt(*args, **kwargs):
            wt = orig_create_wt(*args, **kwargs)
            # Advance main repo HEAD to simulate divergence while transaction is in flight
            (temp_git_repo / "divergent_head.txt").write_text("diverging main HEAD")
            subprocess.run(["git", "add", "."], cwd=temp_git_repo, check=True)
            subprocess.run(["git", "commit", "-m", "Advance main HEAD to make base stale"], cwd=temp_git_repo, check=True)
            return wt

        monkeypatch.setattr(runtime.ws, "create_transaction_worktree", wrapped_create_wt)

        result = runtime.execute_transaction(task, proposal)

        assert result.integration_status in ("STALE_BASE", "READY_TO_INTEGRATE")
        assert result.commit_hash is not None
        assert result.success is False
        assert Path(result.worktree_path).exists()

    def test_integrated_still_cleans_and_succeeds(self, temp_git_repo: Path):
        runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)

        task = TaskDefinition(task_id="T_HAPPY_1", title="Happy path task", allowed_files=["Player.cs"])
        proposal = PatchProposal(
            patches=[
                FilePatch(
                    file="Player.cs",
                    hunks=[
                        PatchHunk(
                            old_text="    public void Update() {}",
                            new_text="    public void Update() {\n        // Happy update\n    }",
                        )
                    ],
                )
            ]
        )

        result = runtime.execute_transaction(task, proposal)

        assert result.success is True
        assert result.integration_status == "INTEGRATED"
        assert result.commit_hash is not None
        assert not Path(result.worktree_path).exists()


class TestBug23JournalLock:
    def test_concurrent_record_start_no_lost_update(self, temp_git_repo: Path):
        journal = TransactionJournal(temp_git_repo)
        num_threads = 8
        barrier = threading.Barrier(num_threads)
        errors = []

        def worker(idx: int):
            try:
                barrier.wait(timeout=5)
                journal.record_start(
                    tx_id=f"tx_concurrent_{idx}",
                    task_id=f"T_CONC_{idx}",
                    base_commit="base_commit_hash",
                    worktree_path=str(temp_git_repo / f".worktrees/wt_{idx}"),
                )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors encountered during worker threads: {errors}"

        journal_reader = TransactionJournal(temp_git_repo)
        active_records = journal_reader.list_active()
        assert len(active_records) == num_threads

    def test_journal_api_unchanged(self, temp_git_repo: Path):
        journal = TransactionJournal(temp_git_repo)
        rec = journal.record_start(
            tx_id="tx_seq_01",
            task_id="T_SEQ_01",
            base_commit="base_commit_hash",
            worktree_path=str(temp_git_repo / ".worktrees/wt_seq_01"),
        )
        assert rec.tx_id == "tx_seq_01"
        assert rec.state == TransactionState.CREATED

        journal.update_state("tx_seq_01", TransactionState.COMMITTED)
        active = journal.list_active()
        assert len(active) == 1
        assert active[0].state == TransactionState.COMMITTED

        journal.record_end("tx_seq_01")
        assert len(journal.list_active()) == 0
