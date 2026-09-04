import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional
import pytest
from typer.testing import CliRunner

from build.sandbox import MockBuildRunner
from context.builder import ContextBuilder
from context.extractor import SymbolExtractor
from core.journal import TransactionJournal, JournalRecord
from core.runtime import DSHRuntime
from core.state import TransactionState
from core.workspace import WorkspaceManager, TransactionWorktree
from memory.episodic import EpisodicMemoryStore, EpisodeRecord, EpisodeStatus, EpisodeValidation
from memory.retrieval import EpisodeRetriever
from model.providers import MockModelProvider
from model.schemas import ModelResponse
from recovery.manager import RecoveryManager
import safety.patch_engine as pe
from safety.ast_guard import ASTGuard
from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk, TransactionResult


@pytest.fixture
def temp_git_repo(tmp_path: Path):
    """Creates an isolated clean Git repository for testing."""
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "agent@test.local"], cwd=repo, check=True)

    # Initial commit
    test_file = repo / "Player.cs"
    test_file.write_text("public class Player {\n    public void Update() {}\n}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)
    return repo


# ==============================================================================
# Hạng mục 1 — Bug 7a: Nối Episodic Memory vào luồng chạy thật
# ==============================================================================

class TestCategory1_EpisodicMemoryIntegration:
    """Bug 7a: Nối Episodic Memory vào luồng chạy thật của DSHRuntime & RecoveryManager."""

    def test_runtime_init_has_episodic_memory_parameters_and_attributes(self, temp_git_repo: Path):
        """Kiểm tra DSHRuntime.__init__ chấp nhận episodic_store và episode_retriever và lưu thành thuộc tính."""
        store = EpisodicMemoryStore()
        retriever = EpisodeRetriever(store)
        try:
            runtime = DSHRuntime(
                temp_git_repo,
                episodic_store=store,
                episode_retriever=retriever,
                test_mode=True,
            )
        except TypeError as e:
            pytest.fail(f"MISSING: episodic_store/episode_retriever in DSHRuntime.__init__: {e}")

        if not hasattr(runtime, "episodic_store") or runtime.episodic_store is not store:
            pytest.fail("MISSING: DSHRuntime.episodic_store attribute")
        if not hasattr(runtime, "episode_retriever") or runtime.episode_retriever is not retriever:
            pytest.fail("MISSING: DSHRuntime.episode_retriever attribute")

    def test_store_episode_on_integrated_success(self, temp_git_repo: Path):
        """Happy path & Contract: Chạy 1 transaction INTEGRATED -> store.get_all_episodes() có đúng 1 episode mới."""
        store = EpisodicMemoryStore()
        retriever = EpisodeRetriever(store)
        try:
            runtime = DSHRuntime(
                temp_git_repo,
                episodic_store=store,
                episode_retriever=retriever,
                test_mode=True,
                dry_run=False,
            )
        except TypeError as e:
            pytest.fail(f"MISSING: episodic_store/episode_retriever in DSHRuntime.__init__: {e}")

        task = TaskDefinition(
            task_id="T010",
            title="Hook Player.Update",
            allowed_files=["Player.cs"],
            target_symbols=["Player.Update"],
            max_lines_added=10,
            max_lines_deleted=5,
        )
        proposal = PatchProposal(
            patches=[
                FilePatch(
                    file="Player.cs",
                    hunks=[
                        PatchHunk(
                            old_text="    public void Update() {}\n",
                            new_text="    public void Update() {\n        // Hooked\n    }\n",
                        )
                    ],
                )
            ],
            reason="Add hook",
            confidence=0.95,
        )

        res = runtime.execute_transaction(task, proposal)
        assert res.success is True, f"Transaction failed: {res.error_message}"

        episodes = store.get_all_episodes()
        if not episodes:
            pytest.fail("MISSING: EpisodeRecord not stored into episodic_store after INTEGRATED transaction")

        assert len(episodes) == 1
        ep = episodes[0]
        assert ep.task_id == "T010"
        assert "Player.Update" in ep.symbol or ep.symbol == "Player.Update"
        assert ep.version == res.base_commit
        assert ep.validation.build is True
        assert ep.validation.behavior is True
        assert ep.validation.regression is True

    def test_execute_with_model_injects_advisory_episodes_to_context_builder(self, temp_git_repo: Path):
        """Happy path & Contract: execute_with_model truy xuất episodes từ retriever và truyền vào build_context."""
        store = EpisodicMemoryStore()
        retriever = EpisodeRetriever(store)
        try:
            runtime = DSHRuntime(
                temp_git_repo,
                episodic_store=store,
                episode_retriever=retriever,
                test_mode=True,
                dry_run=True,
            )
        except TypeError as e:
            pytest.fail(f"MISSING: episodic_store/episode_retriever in DSHRuntime.__init__: {e}")

        head = runtime.ws.get_head_commit()
        patch = PatchProposal(
            patches=[
                FilePatch(
                    file="Player.cs",
                    hunks=[
                        PatchHunk(
                            old_text="    public void Update() {}\n",
                            new_text="    public void Update() {\n        // Hooked\n    }\n",
                        )
                    ],
                )
            ],
            reason="Prior successful hook",
            confidence=0.98,
        )
        existing_ep = EpisodeRecord(
            task_id="T_PRIOR",
            symbol="Player.Update",
            solution_patch=patch,
            version=head,
            environment="linux",
            confidence=0.98,
            status=EpisodeStatus.VALIDATED,
            validation=EpisodeValidation(build=True, behavior=True, regression=True),
        )
        store.store_episode(existing_ep)

        task = TaskDefinition(
            task_id="T011",
            title="Hook Player.Update model",
            allowed_files=["Player.cs"],
            target_symbols=["Player.Update"],
        )

        captured_episodes = []

        class SpyContextBuilder(ContextBuilder):
            def build_context(self, *args, **kwargs):
                advisory = kwargs.get("advisory_episodes")
                captured_episodes.append(advisory)
                return super().build_context(*args, **kwargs)

        cb = SpyContextBuilder()
        mock_provider = MockModelProvider(
            canned_response=ModelResponse(
                raw_content="{}",
                patch_proposal=patch,
                tokens_used=50,
            )
        )

        runtime.execute_with_model(task, mock_provider, cb)

        if not captured_episodes:
            pytest.fail("ContextBuilder.build_context was not called during execute_with_model")

        advisory = captured_episodes[0]
        if advisory is None:
            pytest.fail("MISSING: advisory_episodes was None in execute_with_model -> build_context call")
        assert len(advisory) > 0, "advisory_episodes should contain the retrieved episode for Player.Update"
        assert advisory[0].task_id == "T_PRIOR"

    def test_store_failure_does_not_break_transaction(self, temp_git_repo: Path):
        """Contract: Lỗi lưu episodic_store (vd ném ngoại lệ) không làm hỏng transaction."""
        class FaultyStore(EpisodicMemoryStore):
            def store_episode(self, episode: EpisodeRecord):
                raise RuntimeError("Simulated episodic store disk error")

        faulty_store = FaultyStore()
        try:
            runtime = DSHRuntime(
                temp_git_repo,
                episodic_store=faulty_store,
                test_mode=True,
                dry_run=False,
            )
        except TypeError as e:
            pytest.fail(f"MISSING: episodic_store in DSHRuntime.__init__: {e}")

        task = TaskDefinition(
            task_id="T012",
            title="Resilience check on store failure",
            allowed_files=["Player.cs"],
            target_symbols=["Player.Update"],
        )
        proposal = PatchProposal(
            patches=[
                FilePatch(
                    file="Player.cs",
                    hunks=[
                        PatchHunk(
                            old_text="    public void Update() {}\n",
                            new_text="    public void Update() {\n        // Hooked\n    }\n",
                        )
                    ],
                )
            ],
            reason="Add hook",
            confidence=0.95,
        )

        res = runtime.execute_transaction(task, proposal)
        assert res.success is True, f"Transaction should succeed despite store_episode error: {res.error_message}"

    def test_risk_r1a_empty_target_symbols_does_not_call_retriever(self, temp_git_repo: Path):
        """R1a: task.target_symbols == [] -> không gọi retriever với symbol rỗng, trả về [] an toàn."""
        store = EpisodicMemoryStore()
        retriever = EpisodeRetriever(store)
        try:
            runtime = DSHRuntime(
                temp_git_repo,
                episodic_store=store,
                episode_retriever=retriever,
                test_mode=True,
                dry_run=True,
            )
        except TypeError as e:
            pytest.fail(f"MISSING: episodic_store/episode_retriever in DSHRuntime.__init__: {e}")

        called_symbols = []
        original_retrieve = retriever.retrieve_advisory_episodes
        def spy_retrieve(symbol, *args, **kwargs):
            called_symbols.append(symbol)
            return original_retrieve(symbol, *args, **kwargs)

        retriever.retrieve_advisory_episodes = spy_retrieve

        task = TaskDefinition(
            task_id="T013",
            title="Empty target symbols task",
            allowed_files=["Player.cs"],
            target_symbols=[],
        )
        patch = PatchProposal(
            patches=[
                FilePatch(
                    file="Player.cs",
                    hunks=[
                        PatchHunk(
                            old_text="    public void Update() {}\n",
                            new_text="    public void Update() {\n        // Hooked\n    }\n",
                        )
                    ],
                )
            ],
            reason="Hook",
            confidence=0.95,
        )
        mock_provider = MockModelProvider(
            canned_response=ModelResponse(raw_content="{}", patch_proposal=patch, tokens_used=10)
        )
        cb = ContextBuilder()

        runtime.execute_with_model(task, mock_provider, cb)
        assert len(called_symbols) == 0, f"retrieve_advisory_episodes should not be called for empty target_symbols: {called_symbols}"

    def test_risk_r1c_recovery_loop_injects_advisory_episodes(self, temp_git_repo: Path):
        """R1c: RecoveryManager.run_recovery_loop truy xuất retriever qua runtime và truyền advisory_episodes vào build_context."""
        store = EpisodicMemoryStore()
        retriever = EpisodeRetriever(store)
        try:
            runtime = DSHRuntime(
                temp_git_repo,
                episodic_store=store,
                episode_retriever=retriever,
                test_mode=True,
                dry_run=True,
            )
        except TypeError as e:
            pytest.fail(f"MISSING: episodic_store/episode_retriever in DSHRuntime.__init__: {e}")

        head = runtime.ws.get_head_commit()
        patch = PatchProposal(
            patches=[
                FilePatch(
                    file="Player.cs",
                    hunks=[
                        PatchHunk(
                            old_text="    public void Update() {}\n",
                            new_text="    public void Update() {\n        // Hooked\n    }\n",
                        )
                    ],
                )
            ],
            reason="Hook",
            confidence=0.95,
        )
        ep = EpisodeRecord(
            task_id="T_RECOV_EP",
            symbol="Player.Update",
            solution_patch=patch,
            version=head,
            environment="linux",
            confidence=0.95,
            status=EpisodeStatus.VALIDATED,
            validation=EpisodeValidation(build=True, behavior=True, regression=True),
        )
        store.store_episode(ep)

        task = TaskDefinition(
            task_id="T014",
            title="Recovery loop with episodes",
            allowed_files=["Player.cs"],
            target_symbols=["Player.Update"],
        )

        captured_episodes = []

        class SpyContextBuilder(ContextBuilder):
            def build_context(self, *args, **kwargs):
                captured_episodes.append(kwargs.get("advisory_episodes"))
                return super().build_context(*args, **kwargs)

        cb = SpyContextBuilder()
        mock_provider = MockModelProvider(
            canned_response=ModelResponse(raw_content="{}", patch_proposal=patch, tokens_used=10)
        )
        rm = RecoveryManager()
        rm.run_recovery_loop(task, runtime, mock_provider, cb)

        if not captured_episodes:
            pytest.fail("build_context was not called during recovery loop")
        advisory = captured_episodes[0]
        if advisory is None:
            pytest.fail("MISSING: RecoveryManager.run_recovery_loop did not pass advisory_episodes to build_context")
        assert len(advisory) > 0
        assert advisory[0].task_id == "T_RECOV_EP"


# ==============================================================================
# Hạng mục 2 — Bug 7b: CLI entrypoints cho model/recovery/DAG
# ==============================================================================

class TestCategory2_CLIEntrypoints:
    """Bug 7b: CLI entrypoints cho model/recovery/DAG."""

    def test_cli_commands_registered(self):
        """Kiểm tra các lệnh CLI mới model, recover, dag được đăng ký trong cli.app."""
        import cli
        cmd_names = [cmd.name for cmd in cli.app.registered_commands]
        for expected in ["model", "recover", "dag"]:
            if expected not in cmd_names:
                pytest.fail(f"MISSING: CLI command '{expected}' in cli.app")

    def test_cli_model_command_success_and_failure_exit_codes(self, temp_git_repo: Path, tmp_path: Path, monkeypatch):
        """Contract & R2c: Lệnh cli model chạy thành công exit 0, thất bại exit 1."""
        import cli
        cmd_names = [cmd.name for cmd in cli.app.registered_commands]
        if "model" not in cmd_names:
            pytest.fail("MISSING: CLI command 'model'")

        task_file = tmp_path / "task.json"
        task_file.write_text(json.dumps({
            "task_id": "T_CLI_M",
            "title": "CLI Model Task",
            "allowed_files": ["Player.cs"],
        }), encoding="utf-8")

        monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy_test_key")
        runner = CliRunner()

        with monkeypatch.context() as m:
            m.setattr(
                "core.runtime.DSHRuntime.execute_with_model",
                lambda self, task, provider, context_builder: TransactionResult(
                    task_id=task.task_id, success=True, dry_run=True, events=[]
                ),
            )
            res = runner.invoke(cli.app, ["model", "-t", str(task_file), "-r", str(temp_git_repo), "--dry-run"])
            assert res.exit_code == 0, f"Expected exit 0 on success, got {res.exit_code}: {res.output}"

        with monkeypatch.context() as m:
            m.setattr(
                "core.runtime.DSHRuntime.execute_with_model",
                lambda self, task, provider, context_builder: TransactionResult(
                    task_id=task.task_id, success=False, error_message="Model patch failed", dry_run=True, events=[]
                ),
            )
            res = runner.invoke(cli.app, ["model", "-t", str(task_file), "-r", str(temp_git_repo), "--dry-run"])
            assert res.exit_code == 1, f"Expected exit 1 on failure, got {res.exit_code}: {res.output}"

    def test_cli_recover_command_invokes_recovery_pipeline(self, temp_git_repo: Path, tmp_path: Path, monkeypatch):
        """Contract: Lệnh cli recover gọi execute_with_recovery."""
        import cli
        cmd_names = [cmd.name for cmd in cli.app.registered_commands]
        if "recover" not in cmd_names:
            pytest.fail("MISSING: CLI command 'recover'")

        task_file = tmp_path / "task.json"
        task_file.write_text(json.dumps({
            "task_id": "T_CLI_R",
            "title": "CLI Recover Task",
            "allowed_files": ["Player.cs"],
        }), encoding="utf-8")

        monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy_test_key")
        runner = CliRunner()
        called = []

        def mock_exec_rec(self, task, provider, context_builder):
            called.append(True)
            return TransactionResult(task_id=task.task_id, success=True, dry_run=True, events=[])

        monkeypatch.setattr("core.runtime.DSHRuntime.execute_with_recovery", mock_exec_rec)
        res = runner.invoke(cli.app, ["recover", "-t", str(task_file), "-r", str(temp_git_repo), "--dry-run"])
        assert res.exit_code == 0, f"Recover CLI failed: {res.output}"
        assert len(called) == 1, "execute_with_recovery was not invoked"

    def test_cli_dag_sequential_and_parallel(self, temp_git_repo: Path, tmp_path: Path):
        """Contract & R2b: Lệnh cli dag hỗ trợ cả sequential và parallel mode với 2 task độc lập."""
        import cli
        cmd_names = [cmd.name for cmd in cli.app.registered_commands]
        if "dag" not in cmd_names:
            pytest.fail("MISSING: CLI command 'dag'")

        file2 = temp_git_repo / "Enemy.cs"
        file2.write_text("public class Enemy { public void Attack() {} }\n", encoding="utf-8")
        subprocess.run(["git", "add", "Enemy.cs"], cwd=temp_git_repo, check=True)
        subprocess.run(["git", "commit", "-m", "Add Enemy.cs"], cwd=temp_git_repo, check=True)

        tasks_file = tmp_path / "tasks.json"
        tasks_file.write_text(json.dumps([
            {"task_id": "T1", "title": "Task 1", "allowed_files": ["Player.cs"], "dependencies": []},
            {"task_id": "T2", "title": "Task 2", "allowed_files": ["Enemy.cs"], "dependencies": []},
        ]), encoding="utf-8")

        patches_file = tmp_path / "patches.json"
        patches_file.write_text(json.dumps({
            "T1": {
                "patches": [{"file": "Player.cs", "hunks": [{"old_text": "    public void Update() {}\n", "new_text": "    public void Update() { /* T1 */ }\n"}]}],
                "reason": "r1",
                "confidence": 1.0,
            },
            "T2": {
                "patches": [{"file": "Enemy.cs", "hunks": [{"old_text": "public void Attack() {}", "new_text": "public void Attack() { /* T2 */ }"}]}],
                "reason": "r2",
                "confidence": 1.0,
            },
        }), encoding="utf-8")

        runner = CliRunner()
        # Sequential mode
        res_seq = runner.invoke(cli.app, ["dag", "-t", str(tasks_file), "-p", str(patches_file), "-r", str(temp_git_repo), "--mode", "sequential"])
        assert res_seq.exit_code == 0, f"Sequential DAG failed: {res_seq.output}"

        # Parallel mode
        res_par = runner.invoke(cli.app, ["dag", "-t", str(tasks_file), "-p", str(patches_file), "-r", str(temp_git_repo), "--mode", "parallel"])
        assert res_par.exit_code == 0, f"Parallel DAG failed: {res_par.output}"

    def test_cli_dag_failure_aborts_downstream(self, temp_git_repo: Path, tmp_path: Path):
        """Contract: Khi task upstream thất bại trong DAG, task downstream bị huỷ và CLI exit non-zero."""
        import cli
        cmd_names = [cmd.name for cmd in cli.app.registered_commands]
        if "dag" not in cmd_names:
            pytest.fail("MISSING: CLI command 'dag'")

        tasks_file = tmp_path / "tasks_dep.json"
        tasks_file.write_text(json.dumps([
            {"task_id": "T1", "title": "Task 1", "allowed_files": ["Player.cs"], "dependencies": []},
            {"task_id": "T2", "title": "Task 2 (Depends on T1)", "allowed_files": ["Player.cs"], "dependencies": ["T1"]},
        ]), encoding="utf-8")

        patches_file = tmp_path / "patches_dep.json"
        patches_file.write_text(json.dumps({
            "T1": {
                "patches": [{"file": "Player.cs", "hunks": [{"old_text": "NON_EXISTENT_PATTERN_XYZ", "new_text": "foo"}]}],
                "reason": "Will fail",
                "confidence": 1.0,
            },
            "T2": {
                "patches": [{"file": "Player.cs", "hunks": [{"old_text": "    public void Update() {}\n", "new_text": "    public void Update() { /* T2 */ }\n"}]}],
                "reason": "Should be aborted",
                "confidence": 1.0,
            },
        }), encoding="utf-8")

        runner = CliRunner()
        res = runner.invoke(cli.app, ["dag", "-t", str(tasks_file), "-p", str(patches_file), "-r", str(temp_git_repo)])
        assert res.exit_code != 0, "DAG with failing task must exit non-zero"

    def test_cli_invalid_json_schema_error_handling(self, temp_git_repo: Path, tmp_path: Path):
        """Contract: File JSON sai schema dẫn đến exit non-zero và in thông báo lỗi gọn gàng, không văng traceback."""
        import cli
        cmd_names = [cmd.name for cmd in cli.app.registered_commands]
        if "dag" not in cmd_names:
            pytest.fail("MISSING: CLI command 'dag'")

        bad_file = tmp_path / "bad.json"
        bad_file.write_text('{"unknown_key": 123}', encoding="utf-8")

        runner = CliRunner()
        res = runner.invoke(cli.app, ["dag", "-t", str(bad_file), "-p", str(bad_file), "-r", str(temp_git_repo)])
        assert res.exit_code != 0
        assert "Traceback (most recent call last)" not in res.output, "Invalid schema should not cause raw Python traceback"

    def test_risk_r2a_missing_api_key_exits_gracefully(self, temp_git_repo: Path, tmp_path: Path, monkeypatch):
        """R2a: Thiếu API key trong env và .env -> exit non-zero với thông báo cấu hình rõ ràng, không crash sâu."""
        import cli
        cmd_names = [cmd.name for cmd in cli.app.registered_commands]
        if "model" not in cmd_names:
            pytest.fail("MISSING: CLI command 'model'")

        task_file = tmp_path / "task.json"
        task_file.write_text(json.dumps({
            "task_id": "T_NOKEY",
            "title": "No Key Task",
            "allowed_files": ["Player.cs"],
        }), encoding="utf-8")

        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        empty_env = tmp_path / "empty.env"
        empty_env.write_text("", encoding="utf-8")

        runner = CliRunner()
        res = runner.invoke(cli.app, ["model", "-t", str(task_file), "-r", str(temp_git_repo), "--env-file", str(empty_env)])
        assert res.exit_code != 0, "CLI must exit non-zero when API key is unconfigured"
        assert "Traceback (most recent call last)" not in res.output, "Missing API key must exit cleanly without raw traceback"


# ==============================================================================
# Hạng mục 3 — Opt 8.1: Journal lazy-write cho update_state
# ==============================================================================

class TestCategory3_JournalLazyWrite:
    """Opt 8.1: Journal lazy-write cho update_state."""

    def test_journal_has_flush_and_pending_states_interface(self, temp_git_repo: Path):
        """Kiểm tra TransactionJournal có attribute _pending_states và method flush()."""
        journal = TransactionJournal(temp_git_repo)
        if not hasattr(journal, "flush"):
            pytest.fail("MISSING: TransactionJournal.flush() method")
        if not hasattr(journal, "_pending_states"):
            pytest.fail("MISSING: TransactionJournal._pending_states attribute")
        assert callable(getattr(journal, "flush"))
        assert isinstance(getattr(journal, "_pending_states"), dict)

    def test_journal_lazy_write_reduces_io_calls(self, temp_git_repo: Path):
        """Contract: 1 transaction thành công giảm số lần _write_records từ 9 xuống 2 (chỉ start + end)."""
        journal = TransactionJournal(temp_git_repo)
        if not hasattr(journal, "flush") or not hasattr(journal, "_pending_states"):
            pytest.fail("MISSING: TransactionJournal.flush or _pending_states")

        write_calls = []
        orig_write = journal._write_records
        def spy_write(records):
            write_calls.append(len(records))
            return orig_write(records)

        journal._write_records = spy_write

        # 1. record_start (ghi đĩa lần 1)
        journal.record_start("tx100", "T100", "base_commit", "/tmp/wt100")
        assert len(write_calls) == 1, "record_start must write to disk"

        # 2. update_state 7 lần (chỉ cập nhật in-memory, KHÔNG ghi đĩa)
        states = [
            TransactionState.VALIDATING,
            TransactionState.BASELINE_CAPTURED,
            TransactionState.PATCH_APPLIED,
            TransactionState.VALIDATED,
            TransactionState.STAGED,
            TransactionState.COMMITTED,
            TransactionState.INTEGRATED,
        ]
        for s in states:
            journal.update_state("tx100", s)

        assert len(write_calls) == 1, (
            f"update_state must not write to disk directly; expected 1 write call so far, got {len(write_calls)}"
        )

        # 3. record_end (ghi đĩa lần 2)
        journal.record_end("tx100")
        assert len(write_calls) == 2, (
            f"A complete transaction must only call _write_records twice (start + end), got {len(write_calls)}"
        )

    def test_journal_flush_persists_pending_states_to_disk(self, temp_git_repo: Path):
        """Contract & R3b: Trước flush đọc đĩa thấy state CREATED, sau flush đọc đĩa thấy state đã cập nhật."""
        journal = TransactionJournal(temp_git_repo)
        if not hasattr(journal, "flush") or not hasattr(journal, "_pending_states"):
            pytest.fail("MISSING: TransactionJournal.flush() method or _pending_states")

        journal.record_start("tx200", "T200", "base", str(temp_git_repo / "wt200"))
        journal.update_state("tx200", TransactionState.PATCH_APPLIED)

        # Risk R3b: Đọc trực tiếp từ đĩa trước khi flush phải thấy state ban đầu (CREATED)
        disk_records = journal._read_records()
        assert disk_records["tx200"].state == TransactionState.CREATED, (
            "Before flush(), disk records should still show CREATED due to lazy write"
        )

        # Gọi flush()
        journal.flush()

        # Sau flush(), đĩa đã cập nhật state mới
        disk_records_after = journal._read_records()
        assert disk_records_after["tx200"].state == TransactionState.PATCH_APPLIED, (
            "After flush(), disk records must reflect pending state PATCH_APPLIED"
        )
        assert len(journal._pending_states) == 0, "_pending_states must be reset after flush()"

    def test_crash_recovery_preserves_orphaned_detection(self, temp_git_repo: Path):
        """Contract: Crash giữa chừng (chỉ có record_start + update_state, không record_end) -> get_orphaned vẫn tìm thấy."""
        journal = TransactionJournal(temp_git_repo)
        if not hasattr(journal, "_pending_states"):
            pytest.fail("MISSING: TransactionJournal._pending_states")

        wt_dir = temp_git_repo / "orphan_wt"
        wt_dir.mkdir()

        journal.record_start("tx_orphan", "T_ORPHAN", "base", str(wt_dir))
        journal.update_state("tx_orphan", TransactionState.VALIDATING)
        journal.update_state("tx_orphan", TransactionState.PATCH_APPLIED)
        del journal  # Mô phỏng crash/drop object

        recovery_journal = TransactionJournal(temp_git_repo)
        orphans = recovery_journal.get_orphaned()
        assert len(orphans) == 1
        assert orphans[0].tx_id == "tx_orphan"

    def test_risk_r3c_concurrent_update_and_record_end_thread_safety(self, temp_git_repo: Path):
        """R3c: Thread safety giữa update_state và record_end trên các tx_id khác nhau trong cùng _lock."""
        journal = TransactionJournal(temp_git_repo)
        if not hasattr(journal, "_pending_states") or not hasattr(journal, "flush"):
            pytest.fail("MISSING: TransactionJournal lazy-write features")

        errors = []
        for i in range(16):
            journal.record_start(f"tx_{i}", f"T_{i}", "base", f"/tmp/wt_{i}")

        def worker(tx_id: str, do_end: bool):
            try:
                journal.update_state(tx_id, TransactionState.PATCH_APPLIED)
                journal.update_state(tx_id, TransactionState.VALIDATED)
                if do_end:
                    journal.record_end(tx_id)
                else:
                    journal.flush()
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(16):
            t = threading.Thread(target=worker, args=(f"tx_{i}", i % 2 == 0))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        assert len(errors) == 0, f"Concurrent operations failed: {errors}"


# ==============================================================================
# Hạng mục 4 — Opt 8.2: apply_hunks dùng find + slicing
# ==============================================================================

class TestCategory4_PatchEngineLocateUniqueMatch:
    """Opt 8.2: apply_hunks dùng find + slicing (_locate_unique_match)."""

    def test_locate_unique_match_interface_and_happy_path(self):
        """Happy path & Contract: _locate_unique_match trả về vị trí duy nhất và slicing cho kết quả giống replace(1)."""
        if not hasattr(pe, "_locate_unique_match"):
            pytest.fail("MISSING: safety.patch_engine._locate_unique_match")

        content = "public class Player { public void Update() {} }"
        old_text = "public void Update() {}"
        pos = pe._locate_unique_match(content, old_text, "Player.cs", 1)
        assert pos == 22

        new_text = "public void Update() { Hook(); }"
        replaced = content[:pos] + new_text + content[pos + len(old_text):]
        assert replaced == content.replace(old_text, new_text, 1)

    def test_locate_unique_match_not_found_error_format(self):
        """Contract: Không tìm thấy old_text ném PatchValidationError với đúng snippet 100 ký tự."""
        if not hasattr(pe, "_locate_unique_match"):
            pytest.fail("MISSING: safety.patch_engine._locate_unique_match")

        content = "public class Player {}"
        old_text = "public void MissingMethod()"
        with pytest.raises(pe.PatchValidationError) as exc_info:
            pe._locate_unique_match(content, old_text, "Player.cs", 2)

        msg = str(exc_info.value)
        assert "Hunk 2 old_text not found in target file: Player.cs" in msg
        assert "Snippet searched: public void MissingMethod()" in msg

    def test_locate_unique_match_ambiguous_error_format(self):
        """Contract: old_text xuất hiện >1 lần ném PatchValidationError với thông báo count chính xác."""
        if not hasattr(pe, "_locate_unique_match"):
            pytest.fail("MISSING: safety.patch_engine._locate_unique_match")

        content = "foo bar foo baz foo qux"
        old_text = "foo"
        with pytest.raises(pe.PatchValidationError) as exc_info:
            pe._locate_unique_match(content, old_text, "test.txt", 3)

        msg = str(exc_info.value)
        assert "Ambiguous hunk match: old_text appears 3 times in test.txt (hunk 3)" in msg

    def test_risk_r4a_overlapping_pattern_non_overlapping_semantics(self):
        """R4a: Xử lý pattern chồng lấp: aaa với old_text aa chỉ có 1 match non-overlapping -> không bị coi là ambiguous."""
        if not hasattr(pe, "_locate_unique_match"):
            pytest.fail("MISSING: safety.patch_engine._locate_unique_match")

        # 'aaa' có đúng 1 non-overlapping match của 'aa' tại index 0
        pos = pe._locate_unique_match("aaa", "aa", "overlap.txt", 1)
        assert pos == 0

        # 'aaaa' có 2 non-overlapping matches của 'aa' tại index 0 và 2 -> ném ambiguous
        with pytest.raises(pe.PatchValidationError) as exc_info:
            pe._locate_unique_match("aaaa", "aa", "overlap.txt", 1)
        assert "Ambiguous hunk match: old_text appears 2 times in overlap.txt (hunk 1)" in str(exc_info.value)


# ==============================================================================
# Hạng mục 5 — Opt 8.3: Chia sẻ tree-sitter Language instance
# ==============================================================================

class TestCategory5_TreeSitterLanguageInstanceSharing:
    """Opt 8.3: Chia sẻ tree-sitter Language instance."""

    def test_get_csharp_language_function_exists_and_cached(self):
        """Kiểm tra hàm singleton get_csharp_language tồn tại và trả về cùng một Language instance."""
        try:
            from safety.tree_sitter_shared import get_csharp_language
        except ImportError:
            try:
                from safety.ast_guard import get_csharp_language
            except ImportError:
                pytest.fail("MISSING: get_csharp_language function in safety module")

        lang1 = get_csharp_language()
        lang2 = get_csharp_language()
        assert lang1 is lang2, "get_csharp_language() must return cached Language instance"

    def test_astguard_instances_share_same_language(self):
        """Contract: Hai đối tượng ASTGuard khác nhau phải chia sẻ cùng Language instance nhưng Parser khác nhau."""
        g1 = ASTGuard()
        g2 = ASTGuard()
        assert g1.language is g2.language, "ASTGuard instances must share the same Language instance"
        assert g1.parser is not g2.parser, "ASTGuard instances should each have their own Parser"

    def test_astguard_and_symbol_extractor_share_same_language(self):
        """Contract: ASTGuard và SymbolExtractor phải chia sẻ cùng Language instance."""
        g = ASTGuard()
        ext = SymbolExtractor()
        assert ext.language is g.language, "ASTGuard and SymbolExtractor must share the same Language instance"

    def test_risk_r5a_no_circular_import_safety_and_context(self):
        """R5a: Kiểm chứng không có circular import giữa safety và context theo cả 2 chiều."""
        cmd1 = [
            sys.executable,
            "-c",
            "import safety.ast_guard; import context.extractor; print('OK')",
        ]
        res1 = subprocess.run(cmd1, capture_output=True, text=True)
        assert res1.returncode == 0 and "OK" in res1.stdout, f"Import order 1 failed: {res1.stderr}"

        cmd2 = [
            sys.executable,
            "-c",
            "import context.extractor; import safety.ast_guard; print('OK')",
        ]
        res2 = subprocess.run(cmd2, capture_output=True, text=True)
        assert res2.returncode == 0 and "OK" in res2.stdout, f"Import order 2 failed: {res2.stderr}"

        try:
            import safety.tree_sitter_shared
        except ImportError:
            pytest.fail("MISSING: safety.tree_sitter_shared module")

    def test_risk_r5b_concurrent_language_access_thread_safety(self):
        """R5b: Khởi tạo ASTGuard đồng thời từ nhiều thread luôn nhận cùng Language object."""
        languages = []
        def init_worker():
            g = ASTGuard()
            languages.append(g.language)

        threads = [threading.Thread(target=init_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(languages) == 10
        first_lang = languages[0]
        for lang in languages[1:]:
            assert lang is first_lang, "All concurrently initialized ASTGuard instances must share the same Language object"


# ==============================================================================
# Hạng mục 6 — Opt 8.4: Cache git status trong TransactionWorktree
# ==============================================================================

class TestCategory6_TransactionWorktreeStatusCache:
    """Opt 8.4: Cache git status trong TransactionWorktree."""

    def test_worktree_status_cache_interface_and_single_invocation(self, temp_git_repo: Path):
        """Happy path & Contract: TransactionWorktree có _status_cache và chuỗi get_status/is_clean/verify chỉ gọi git status 1 lần."""
        ws = WorkspaceManager(temp_git_repo)
        tx_wt = ws.create_transaction_worktree("tx_cache_1", base_commit=ws.get_head_commit())
        try:
            if not hasattr(tx_wt, "_status_cache"):
                pytest.fail("MISSING: TransactionWorktree._status_cache attribute")
            if not hasattr(tx_wt, "_invalidate_status_cache"):
                pytest.fail("MISSING: TransactionWorktree._invalidate_status_cache method")

            status_calls = []
            orig_run_git_bytes = tx_wt._run_git_bytes
            def spy_run_git_bytes(*args, **kwargs):
                if args and args[0] == "status":
                    status_calls.append(args)
                return orig_run_git_bytes(*args, **kwargs)

            tx_wt._run_git_bytes = spy_run_git_bytes

            tx_wt.get_status()
            tx_wt.is_clean()
            tx_wt.verify_changed_paths({"Player.cs"})

            assert len(status_calls) == 1, (
                f"Expected exactly 1 git status invocation due to caching, got {len(status_calls)}"
            )
        finally:
            ws.remove_transaction_worktree(tx_wt)

    def test_stage_exact_invalidates_cache(self, temp_git_repo: Path):
        """Contract & R6a: stage_exact phải invalidate cache (set None) và get_status phản ánh thay đổi mới."""
        ws = WorkspaceManager(temp_git_repo)
        tx_wt = ws.create_transaction_worktree("tx_cache_2", base_commit=ws.get_head_commit())
        try:
            if not hasattr(tx_wt, "_status_cache"):
                pytest.fail("MISSING: TransactionWorktree._status_cache attribute")

            tx_wt.get_status()
            assert tx_wt._status_cache is not None, "Cache should be populated after get_status()"

            p_file = tx_wt.worktree_path / "Player.cs"
            p_file.write_text("public class Player { /* modified */ }\n", encoding="utf-8")

            tx_wt.stage_exact(["Player.cs"])
            assert tx_wt._status_cache is None, "stage_exact must invalidate _status_cache (set to None)"

            st = tx_wt.get_status()
            assert st.dirty is True
            assert "Player.cs" in st.modified_files
        finally:
            ws.remove_transaction_worktree(tx_wt)

    def test_risk_r6b_commit_invalidates_cache_before_post_commit_is_clean(self, temp_git_repo: Path):
        """R6b: commit() phải invalidate cache trước khi gọi is_clean() nội bộ, đảm bảo commit sạch thành công."""
        ws = WorkspaceManager(temp_git_repo)
        tx_wt = ws.create_transaction_worktree("tx_cache_3", base_commit=ws.get_head_commit())
        try:
            if not hasattr(tx_wt, "_status_cache"):
                pytest.fail("MISSING: TransactionWorktree._status_cache attribute")

            p_file = tx_wt.worktree_path / "Player.cs"
            p_file.write_text("public class Player { /* commit hook */ }\n", encoding="utf-8")
            tx_wt.stage_exact(["Player.cs"])

            tx_wt.get_status()
            assert tx_wt._status_cache is not None

            head = tx_wt.commit(
                task_id="T_COMMIT",
                message="Test commit invalidation",
                expected_paths=["Player.cs"],
            )
            assert head is not None
            assert tx_wt.is_clean() is True, "Worktree must be clean after successful commit"
        finally:
            ws.remove_transaction_worktree(tx_wt)

    def test_risk_r6a_adversarial_mutation_invalidation(self, temp_git_repo: Path):
        """R6a: Khi có thao tác đột biến, invalidate cache đảm bảo get_status phản ánh trạng thái thực tế."""
        ws = WorkspaceManager(temp_git_repo)
        tx_wt = ws.create_transaction_worktree("tx_cache_4", base_commit=ws.get_head_commit())
        try:
            if not hasattr(tx_wt, "_status_cache"):
                pytest.fail("MISSING: TransactionWorktree._status_cache attribute")

            tx_wt.get_status()
            assert tx_wt.is_clean() is True

            new_file = tx_wt.worktree_path / "NewFile.cs"
            new_file.write_text("class NewFile {}", encoding="utf-8")

            if hasattr(tx_wt, "_invalidate_status_cache"):
                tx_wt._invalidate_status_cache()
            assert tx_wt._status_cache is None

            st = tx_wt.get_status()
            assert st.dirty is True
            assert any("NewFile.cs" in f for f in st.modified_files)
        finally:
            ws.remove_transaction_worktree(tx_wt)
