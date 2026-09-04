import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional
import pytest
from typer.testing import CliRunner

import cli
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
from safety.tree_sitter_shared import get_csharp_language
from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk, TransactionResult


@pytest.fixture
def temp_git_repo(tmp_path: Path):
    """Creates an isolated clean Git repository for testing."""
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "QA Adversarial Tester"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "qa@adversarial.local"], cwd=repo, check=True)

    test_file = repo / "Player.cs"
    test_file.write_text("public class Player {\n    public void Update() {}\n}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)
    return repo


# ==============================================================================
# Hạng mục 1 — Bug 7a: Episodic Memory Integration (R1a, R1b, R1c, 3c)
# ==============================================================================

class TestQA_Adversarial_R1_EpisodicMemory:
    """Adversarial tests for Bug 7a: Episodic Memory Integration."""

    def test_r1a_empty_target_symbols_avoids_retrieval_and_defaults_symbol_on_store(self, temp_git_repo: Path):
        """R1a: task.target_symbols rỗng -> không gọi retriever; khi store thì fallback task_id."""
        store = EpisodicMemoryStore()
        retriever = EpisodeRetriever(store)
        runtime = DSHRuntime(
            temp_git_repo,
            episodic_store=store,
            episode_retriever=retriever,
            test_mode=True,
            dry_run=False,
        )

        task = TaskDefinition(
            task_id="T_EMPTY_SYM",
            title="Empty Target Symbols Task",
            allowed_files=["Player.cs"],
            target_symbols=[],  # Empty
        )

        # 1. Verify helper returns None without querying retriever
        retrieved = runtime._retrieve_advisory_episodes(task)
        assert retrieved is None, "Empty target_symbols must return None without querying retriever"

        # 2. Execute transaction end-to-end
        proposal = PatchProposal(
            patches=[
                FilePatch(
                    file="Player.cs",
                    hunks=[
                        PatchHunk(
                            old_text="    public void Update() {}\n",
                            new_text="    public void Update() { /* EmptySymHook */ }\n",
                        )
                    ],
                )
            ],
            reason="Empty symbol patch",
            confidence=0.9,
        )

        res = runtime.execute_transaction(task, proposal)
        assert res.success is True, f"Transaction failed: {res.error_message}"

        # 3. Verify store received episode with fallback symbol == task_id
        episodes = store.get_all_episodes()
        assert len(episodes) == 1
        ep = episodes[0]
        assert ep.task_id == "T_EMPTY_SYM"
        assert ep.symbol == "T_EMPTY_SYM", "Episode symbol must fallback to task_id when target_symbols is empty"

    def test_r1a_execute_with_model_with_empty_target_symbols(self, temp_git_repo: Path):
        """R1a: execute_with_model khi target_symbols rỗng không crash và truyền advisory_episodes=None."""
        store = EpisodicMemoryStore()
        retriever = EpisodeRetriever(store)
        runtime = DSHRuntime(
            temp_git_repo,
            episodic_store=store,
            episode_retriever=retriever,
            test_mode=True,
            dry_run=True,
        )

        task = TaskDefinition(
            task_id="T_MODEL_EMPTY_SYM",
            title="Model Empty Symbol Task",
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
                            new_text="    public void Update() { /* ModelHook */ }\n",
                        )
                    ],
                )
            ],
            reason="Model empty symbol hook",
            confidence=0.95,
        )
        provider = MockModelProvider(canned_response=ModelResponse(patch_proposal=patch))
        context_builder = ContextBuilder()

        res = runtime.execute_with_model(task, provider, context_builder)
        assert res.success is True, f"execute_with_model failed: {res.error_message}"

    def test_r1b_episode_actually_persisted_with_complete_fields(self, temp_git_repo: Path):
        """R1b: Episode thật xuất hiện trong store với đầy đủ field hợp lệ sau transaction INTEGRATED."""
        store = EpisodicMemoryStore()
        retriever = EpisodeRetriever(store)
        runtime = DSHRuntime(
            temp_git_repo,
            episodic_store=store,
            episode_retriever=retriever,
            test_mode=True,
            dry_run=False,
        )

        task = TaskDefinition(
            task_id="T_VERIFY_R1B",
            title="Verify Episode Fields",
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
                            new_text="    public void Update() { /* VerifiedR1b */ }\n",
                        )
                    ],
                )
            ],
            reason="Verified R1b reason",
            confidence=0.99,
        )

        res = runtime.execute_transaction(task, proposal)
        assert res.success is True

        episodes = store.get_all_episodes()
        assert len(episodes) == 1, "Expected exactly 1 episode in store"
        ep = episodes[0]

        # Strict checks on fields
        assert ep.task_id == "T_VERIFY_R1B"
        assert ep.symbol == "Player.Update"
        assert ep.version == res.base_commit
        assert ep.environment == "linux"
        assert ep.status == EpisodeStatus.VALIDATED
        assert ep.validation.build is True
        assert ep.validation.behavior is True
        assert ep.validation.regression is True
        assert len(ep.solution_patch.patches) == 1
        assert ep.solution_patch.reason == "Verified R1b reason"

    def test_r1b_failed_transaction_does_not_persist_episode(self, temp_git_repo: Path):
        """R1b: Transaction thất bại (patch không khớp) TUYỆT ĐỐI KHÔNG lưu episode vào store."""
        store = EpisodicMemoryStore()
        retriever = EpisodeRetriever(store)
        runtime = DSHRuntime(
            temp_git_repo,
            episodic_store=store,
            episode_retriever=retriever,
            test_mode=True,
            dry_run=False,
        )

        task = TaskDefinition(
            task_id="T_FAIL_NO_EPISODE",
            title="Failing Task",
            allowed_files=["Player.cs"],
            target_symbols=["Player.Update"],
        )
        # Bad patch: old_text not found
        proposal = PatchProposal(
            patches=[
                FilePatch(
                    file="Player.cs",
                    hunks=[
                        PatchHunk(
                            old_text="NON_EXISTENT_TEXT_HERE",
                            new_text="new text",
                        )
                    ],
                )
            ],
            reason="Bad patch",
            confidence=0.5,
        )

        res = runtime.execute_transaction(task, proposal)
        assert res.success is False

        episodes = store.get_all_episodes()
        assert len(episodes) == 0, "Failing transaction must NOT persist any episode into store"

    def test_r1b_store_exception_swallowed_without_failing_transaction(self, temp_git_repo: Path):
        """R1b: Store raise exception -> transaction vẫn success=True (memory phụ trợ, không làm hỏng tx)."""
        class CrashingStore(EpisodicMemoryStore):
            def store_episode(self, episode: EpisodeRecord):
                raise RuntimeError("Simulated episodic storage disk failure!")

        store = CrashingStore()
        retriever = EpisodeRetriever(store)
        runtime = DSHRuntime(
            temp_git_repo,
            episodic_store=store,
            episode_retriever=retriever,
            test_mode=True,
            dry_run=False,
        )

        task = TaskDefinition(
            task_id="T_STORE_CRASH",
            title="Store Crash Resilient Task",
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
                            new_text="    public void Update() { /* ResilientHook */ }\n",
                        )
                    ],
                )
            ],
            reason="Resilient",
            confidence=1.0,
        )

        res = runtime.execute_transaction(task, proposal)
        assert res.success is True, "Storage failure must not convert successful transaction into failure"

    def test_r1c_recovery_loop_propagates_advisory_episodes(self, temp_git_repo: Path):
        """R1c: RecoveryManager.run_recovery_loop truy xuất và truyền advisory_episodes vào build_context."""
        store = EpisodicMemoryStore()
        retriever = EpisodeRetriever(store)
        runtime = DSHRuntime(
            temp_git_repo,
            episodic_store=store,
            episode_retriever=retriever,
            test_mode=True,
            dry_run=True,
        )

        head = runtime.ws.get_head_commit()
        # Seed an episode in store matching head and target symbol
        seed_episode = EpisodeRecord(
            task_id="T_PRIOR",
            symbol="Player.Update",
            version=head,
            environment="linux",
            solution_patch=PatchProposal(
                patches=[
                    FilePatch(
                        file="Player.cs",
                        hunks=[PatchHunk(old_text="old", new_text="new")],
                    )
                ],
                reason="Prior solution",
            ),
            validation=EpisodeValidation(build=True, behavior=True, regression=True),
        )
        store.store_episode(seed_episode)

        task = TaskDefinition(
            task_id="T_REC_MEM",
            title="Recovery With Memory Task",
            allowed_files=["Player.cs"],
            target_symbols=["Player.Update"],
        )

        patch = PatchProposal(
            patches=[
                FilePatch(
                    file="Player.cs",
                    hunks=[
                        PatchHunk(
                            old_text="    public void Update() {}\n",
                            new_text="    public void Update() { /* RecoverySuccess */ }\n",
                        )
                    ],
                )
            ],
            reason="Recovery success",
            confidence=0.9,
        )
        provider = MockModelProvider(canned_response=ModelResponse(patch_proposal=patch))

        captured_advisory = []
        real_build_context = ContextBuilder.build_context

        def spy_build_context(cb_self, *args, **kwargs):
            advisory = kwargs.get("advisory_episodes")
            captured_advisory.append(advisory)
            return real_build_context(cb_self, *args, **kwargs)

        context_builder = ContextBuilder()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ContextBuilder, "build_context", spy_build_context)
            res = runtime.execute_with_recovery(task, provider, context_builder)

        assert res.success is True, f"Recovery failed: {res.error_message}"
        assert len(captured_advisory) >= 1, "build_context was not called during recovery"
        assert captured_advisory[0] is not None, "advisory_episodes must be passed to build_context in recovery"
        assert len(captured_advisory[0]) == 1
        assert captured_advisory[0][0].task_id == "T_PRIOR"

    def test_r1c_recovery_loop_resilient_to_retriever_exception(self, temp_git_repo: Path):
        """R1c: Khi retriever raise exception trong recovery loop, lỗi được nuốt và recovery vẫn tiếp tục."""
        store = EpisodicMemoryStore()
        retriever = EpisodeRetriever(store)
        runtime = DSHRuntime(
            temp_git_repo,
            episodic_store=store,
            episode_retriever=retriever,
            test_mode=True,
            dry_run=True,
        )

        task = TaskDefinition(
            task_id="T_REC_CRASH_RET",
            title="Recovery With Crashing Retriever",
            allowed_files=["Player.cs"],
            target_symbols=["Player.Update"],
        )

        patch = PatchProposal(
            patches=[
                FilePatch(
                    file="Player.cs",
                    hunks=[
                        PatchHunk(
                            old_text="    public void Update() {}\n",
                            new_text="    public void Update() { /* RecNoCrash */ }\n",
                        )
                    ],
                )
            ],
            reason="Recovery patch",
            confidence=0.9,
        )
        provider = MockModelProvider(canned_response=ModelResponse(patch_proposal=patch))
        context_builder = ContextBuilder()

        def crashing_retrieve(*args, **kwargs):
            raise RuntimeError("Database connection timed out during episode retrieval!")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(retriever, "retrieve_advisory_episodes", crashing_retrieve)
            res = runtime.execute_with_recovery(task, provider, context_builder)

        assert res.success is True, f"Recovery should succeed even when retriever crashes: {res.error_message}"

    def test_3c_episodic_dedup_duplicate_symbols_behavior(self, temp_git_repo: Path):
        """Adversarial 3c: Task có 2 symbol cùng match 1 episode.
        Theo chữ SPEC 'dedup theo episode identity theo id object', EpisodeRetriever trả model_copy
        nên 2 copy có id khác nhau -> advisory có thể chứa 2 copy trùng lặp.
        Test xác nhận: ContextBuilder không crash, budget manifest vẫn tính toán đúng.
        """
        store = EpisodicMemoryStore()
        retriever = EpisodeRetriever(store)
        runtime = DSHRuntime(
            temp_git_repo,
            episodic_store=store,
            episode_retriever=retriever,
            test_mode=True,
            dry_run=True,
        )

        head = runtime.ws.get_head_commit()
        shared_ep = EpisodeRecord(
            task_id="T_SHARED",
            symbol="Player.Common",
            version=head,
            environment="linux",
            solution_patch=PatchProposal(
                patches=[FilePatch(file="Player.cs", hunks=[PatchHunk(old_text="a", new_text="b")])],
                reason="Shared solution",
            ),
        )
        store.store_episode(shared_ep)

        # Task có 2 symbols cùng query ra shared_ep
        task = TaskDefinition(
            task_id="T_DEDUP_TEST",
            title="Dedup test task",
            allowed_files=["Player.cs"],
            target_symbols=["Player.Common", "Player.Common"],
        )

        retrieved = runtime._retrieve_advisory_episodes(task)
        assert retrieved is not None
        # Verify Dev's note in dev3_notes: id(copy1) != id(copy2), so 2 copies are returned
        assert len(retrieved) == 2, "Expected 2 model_copy records due to id-based dedup per SPEC"

        # ContextBuilder must handle duplicate copies cleanly without crashing
        cb = ContextBuilder()
        snippets = {"Player.cs": (temp_git_repo / "Player.cs").read_text(encoding="utf-8")}
        context_str, manifest = cb.build_context_with_manifest(
            task=task,
            file_snippets=snippets,
            advisory_episodes=retrieved,
        )
        assert "### [ADVISORY MEMORY" in context_str
        assert "T_SHARED" in context_str
        assert manifest.used_tokens > 0
        assert manifest.available_budget >= manifest.used_tokens


# ==============================================================================
# Hạng mục 2 — Bug 7b: CLI Entrypoints (R2a, R2b, R2c, 3e)
# ==============================================================================

class TestQA_Adversarial_R2_CLIEntrypoints:
    """Adversarial tests for Bug 7b: CLI entrypoints."""

    def test_r2a_env_isolation_missing_key_clean_error(self, temp_git_repo: Path, tmp_path: Path, monkeypatch):
        """R2a: Thiếu API key và không có .env -> exit code 1 với message rõ, KHÔNG gọi mạng / crash sâu."""
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr("model.providers.resolve_deepseek_api_key", lambda: None)

        task_file = tmp_path / "task.json"
        task_file.write_text(json.dumps({
            "task_id": "T_ISOLATION_TEST",
            "title": "Isolation Test",
            "allowed_files": ["Player.cs"],
        }), encoding="utf-8")

        runner = CliRunner()
        # Test model command
        res_m = runner.invoke(cli.app, ["model", "-t", str(task_file), "-r", str(temp_git_repo)])
        assert res_m.exit_code == 1, f"Expected exit code 1, got {res_m.exit_code}"
        assert "No DEEPSEEK_API_KEY configured" in res_m.output
        assert "Traceback (most recent call last)" not in (res_m.output or "")

        # Test recover command
        res_r = runner.invoke(cli.app, ["recover", "-t", str(task_file), "-r", str(temp_git_repo)])
        assert res_r.exit_code == 1, f"Expected exit code 1, got {res_r.exit_code}"
        assert "No DEEPSEEK_API_KEY configured" in res_r.output
        assert "Traceback (most recent call last)" not in (res_r.output or "")

    def test_r2a_env_file_loads_key_without_calling_real_api(self, temp_git_repo: Path, tmp_path: Path, monkeypatch):
        """R2a: Cung cấp --env-file chứa key -> nạp thành công, không gọi API thật nhờ mock runtime."""
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setattr("model.providers.resolve_deepseek_api_key", lambda: None)

        env_file = tmp_path / "custom.env"
        env_file.write_text("DEEPSEEK_API_KEY=test_isolated_cli_key\n", encoding="utf-8")

        task_file = tmp_path / "task.json"
        task_file.write_text(json.dumps({
            "task_id": "T_ENVFILE_TEST",
            "title": "Env File Test",
            "allowed_files": ["Player.cs"],
        }), encoding="utf-8")

        called = []
        def mock_execute_model(self, task, provider, context_builder):
            called.append(provider.api_key)
            return TransactionResult(task_id=task.task_id, success=True, dry_run=True, events=[])

        monkeypatch.setattr("core.runtime.DSHRuntime.execute_with_model", mock_execute_model)

        runner = CliRunner()
        res = runner.invoke(cli.app, ["model", "-t", str(task_file), "-r", str(temp_git_repo), "--env-file", str(env_file)])
        assert res.exit_code == 0, f"Command failed: {res.output}"
        assert len(called) == 1
        assert called[0] == "test_isolated_cli_key"

    def test_r2b_cli_dag_parallel_three_concurrent_tasks(self, temp_git_repo: Path, tmp_path: Path):
        """R2b: dag --mode parallel với 3 task độc lập trên temp repo chạy đồng thời hoàn tất 3 task."""
        # Create 3 distinct files
        for name in ["FileA.cs", "FileB.cs", "FileC.cs"]:
            f = temp_git_repo / name
            f.write_text(f"public class {name.replace('.cs', '')} {{ public void Run() {{}} }}\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=temp_git_repo, check=True)
        subprocess.run(["git", "commit", "-m", "Add 3 test files"], cwd=temp_git_repo, check=True)

        tasks_file = tmp_path / "tasks_3.json"
        tasks_file.write_text(json.dumps([
            {"task_id": "T_A", "title": "Task A", "allowed_files": ["FileA.cs"], "dependencies": []},
            {"task_id": "T_B", "title": "Task B", "allowed_files": ["FileB.cs"], "dependencies": []},
            {"task_id": "T_C", "title": "Task C", "allowed_files": ["FileC.cs"], "dependencies": []},
        ]), encoding="utf-8")

        patches_file = tmp_path / "patches_3.json"
        patches_file.write_text(json.dumps({
            "T_A": {
                "patches": [{"file": "FileA.cs", "hunks": [{"old_text": "public void Run() {}", "new_text": "public void Run() { /* A */ }"}]}],
                "reason": "patch a",
            },
            "T_B": {
                "patches": [{"file": "FileB.cs", "hunks": [{"old_text": "public void Run() {}", "new_text": "public void Run() { /* B */ }"}]}],
                "reason": "patch b",
            },
            "T_C": {
                "patches": [{"file": "FileC.cs", "hunks": [{"old_text": "public void Run() {}", "new_text": "public void Run() { /* C */ }"}]}],
                "reason": "patch c",
            },
        }), encoding="utf-8")

        runner = CliRunner()
        res = runner.invoke(cli.app, [
            "dag",
            "-t", str(tasks_file),
            "-p", str(patches_file),
            "-r", str(temp_git_repo),
            "--mode", "parallel",
            "--max-workers", "3",
        ])
        assert res.exit_code == 0, f"Parallel 3-task DAG failed: {res.output}"
        assert "Completed" in res.output
        assert "3" in res.output

    def test_r2c_exit_code_branches_for_all_commands(self, temp_git_repo: Path, tmp_path: Path, monkeypatch):
        """R2c: Exit code chính xác cả 2 nhánh (0 khi success, 1 khi fail) cho model, recover, dag."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy_key")
        runner = CliRunner()

        task_file = tmp_path / "task.json"
        task_file.write_text(json.dumps({
            "task_id": "T_EXIT_CODE",
            "title": "Exit Code Task",
            "allowed_files": ["Player.cs"],
        }), encoding="utf-8")

        # 1. model: success -> 0, fail -> 1
        with monkeypatch.context() as m:
            m.setattr("core.runtime.DSHRuntime.execute_with_model", lambda *args, **kwargs: TransactionResult(
                task_id="T_EXIT_CODE", success=True, dry_run=True, events=[]
            ))
            res = runner.invoke(cli.app, ["model", "-t", str(task_file), "-r", str(temp_git_repo)])
            assert res.exit_code == 0

        with monkeypatch.context() as m:
            m.setattr("core.runtime.DSHRuntime.execute_with_model", lambda *args, **kwargs: TransactionResult(
                task_id="T_EXIT_CODE", success=False, error_message="Fail", dry_run=True, events=[]
            ))
            res = runner.invoke(cli.app, ["model", "-t", str(task_file), "-r", str(temp_git_repo)])
            assert res.exit_code == 1

        # 2. recover: success -> 0, fail -> 1
        with monkeypatch.context() as m:
            m.setattr("core.runtime.DSHRuntime.execute_with_recovery", lambda *args, **kwargs: TransactionResult(
                task_id="T_EXIT_CODE", success=True, dry_run=True, events=[]
            ))
            res = runner.invoke(cli.app, ["recover", "-t", str(task_file), "-r", str(temp_git_repo)])
            assert res.exit_code == 0

        with monkeypatch.context() as m:
            m.setattr("core.runtime.DSHRuntime.execute_with_recovery", lambda *args, **kwargs: TransactionResult(
                task_id="T_EXIT_CODE", success=False, error_message="Fail", dry_run=True, events=[]
            ))
            res = runner.invoke(cli.app, ["recover", "-t", str(task_file), "-r", str(temp_git_repo)])
            assert res.exit_code == 1

        # 3. dag invalid mode -> 1
        tasks_file = tmp_path / "valid_tasks.json"
        tasks_file.write_text(json.dumps([
            {"task_id": "T1", "title": "T1", "allowed_files": ["Player.cs"]}
        ]), encoding="utf-8")
        patches_file = tmp_path / "valid_patches.json"
        patches_file.write_text(json.dumps({
            "T1": {"patches": [{"file": "Player.cs", "hunks": [{"old_text": "public void Update() {}", "new_text": "public void Update() { /* 1 */ }"}]}]}
        }), encoding="utf-8")

        res_bad_mode = runner.invoke(cli.app, [
            "dag", "-t", str(tasks_file), "-p", str(patches_file), "-r", str(temp_git_repo), "--mode", "unsupported_mode"
        ])
        assert res_bad_mode.exit_code == 1
        assert "Invalid --mode" in res_bad_mode.output

    def test_3e_dag_sequential_then_parallel_on_same_repo_default_dry_run(self, temp_git_repo: Path, tmp_path: Path):
        """Adversarial 3e: Chạy sequential rồi parallel trên CÙNG repo (mặc định dry-run) đều 2 completed."""
        tasks_file = tmp_path / "tasks_seq_par.json"
        tasks_file.write_text(json.dumps([
            {"task_id": "T1", "title": "Task 1", "allowed_files": ["Player.cs"], "dependencies": []},
            {"task_id": "T2", "title": "Task 2", "allowed_files": ["Player.cs"], "dependencies": []},
        ]), encoding="utf-8")

        patches_file = tmp_path / "patches_seq_par.json"
        patches_file.write_text(json.dumps({
            "T1": {
                "patches": [{"file": "Player.cs", "hunks": [{"old_text": "    public void Update() {}\n", "new_text": "    public void Update() { /* T1 */ }\n"}]}],
                "reason": "r1",
            },
            "T2": {
                "patches": [{"file": "Player.cs", "hunks": [{"old_text": "    public void Update() {}\n", "new_text": "    public void Update() { /* T2 */ }\n"}]}],
                "reason": "r2",
            },
        }), encoding="utf-8")

        runner = CliRunner()
        # Sequential run
        res_seq = runner.invoke(cli.app, ["dag", "-t", str(tasks_file), "-p", str(patches_file), "-r", str(temp_git_repo), "--mode", "sequential"])
        assert res_seq.exit_code == 0, f"Sequential run failed: {res_seq.output}"
        assert "Completed" in res_seq.output

        # Parallel run on the SAME repo (must succeed because sequential was dry-run)
        res_par = runner.invoke(cli.app, ["dag", "-t", str(tasks_file), "-p", str(patches_file), "-r", str(temp_git_repo), "--mode", "parallel"])
        assert res_par.exit_code == 0, f"Parallel run failed on same repo: {res_par.output}"
        assert "Completed" in res_par.output

    def test_3e_dag_failure_causes_dependent_abort_and_nonzero_exit(self, temp_git_repo: Path, tmp_path: Path):
        """Adversarial 3e: 1 task upstream fail -> task downstream bị abort, exit code non-zero."""
        tasks_file = tmp_path / "tasks_dep_abort.json"
        tasks_file.write_text(json.dumps([
            {"task_id": "T_ROOT", "title": "Root Task", "allowed_files": ["Player.cs"], "dependencies": []},
            {"task_id": "T_CHILD", "title": "Child Task", "allowed_files": ["Player.cs"], "dependencies": ["T_ROOT"]},
        ]), encoding="utf-8")

        patches_file = tmp_path / "patches_dep_abort.json"
        patches_file.write_text(json.dumps({
            "T_ROOT": {
                "patches": [{"file": "Player.cs", "hunks": [{"old_text": "NON_EXISTENT_MATCH_TEXT", "new_text": "foo"}]}],
                "reason": "Must fail",
            },
            "T_CHILD": {
                "patches": [{"file": "Player.cs", "hunks": [{"old_text": "    public void Update() {}\n", "new_text": "    public void Update() { /* Child */ }\n"}]}],
                "reason": "Must abort",
            },
        }), encoding="utf-8")

        runner = CliRunner()
        res = runner.invoke(cli.app, ["dag", "-t", str(tasks_file), "-p", str(patches_file), "-r", str(temp_git_repo)])
        assert res.exit_code == 1, f"DAG must exit non-zero on failure: {res.output}"
        assert "Failed" in res.output
        assert "Aborted" in res.output
        assert "T_CHILD" in res.output

    def test_3e_dag_invalid_json_syntax_produces_clean_message(self, temp_git_repo: Path, tmp_path: Path):
        """Adversarial 3e: File JSON sai cú pháp -> message rõ không traceback."""
        bad_json = tmp_path / "corrupted.json"
        bad_json.write_text("{ unquoted_key: 123 ", encoding="utf-8")

        runner = CliRunner()
        res = runner.invoke(cli.app, ["dag", "-t", str(bad_json), "-p", str(bad_json), "-r", str(temp_git_repo)])
        assert res.exit_code != 0
        assert "Invalid JSON in" in res.output
        assert "Traceback (most recent call last)" not in (res.output or "")

    def test_3e_dag_invalid_pydantic_schema_produces_clean_message(self, temp_git_repo: Path, tmp_path: Path):
        """Adversarial 3e: File JSON thiếu trường bắt buộc (schema sai) -> message rõ không traceback."""
        bad_schema = tmp_path / "missing_fields.json"
        bad_schema.write_text(json.dumps([{"task_id": "T1"}]), encoding="utf-8")  # missing title

        dummy_patches = tmp_path / "dummy_p.json"
        dummy_patches.write_text(json.dumps({}), encoding="utf-8")

        runner = CliRunner()
        res = runner.invoke(cli.app, ["dag", "-t", str(bad_schema), "-p", str(dummy_patches), "-r", str(temp_git_repo)])
        assert res.exit_code != 0
        assert "Schema validation failed" in res.output
        assert "title: Field required" in res.output
        assert "Traceback (most recent call last)" not in (res.output or "")

    def test_3e_dag_json_non_dict_elements_handling(self, temp_git_repo: Path, tmp_path: Path):
        """Adversarial 3e: File JSON danh sách chứa phần tử nguyên thủy [123] thay vì dict.
        Kiểm tra hệ thống xử lý lỗi schema gọn gàng mà không ném TypeError uncaught.
        """
        bad_primitive_list = tmp_path / "int_list.json"
        bad_primitive_list.write_text("[123, 456]", encoding="utf-8")

        runner = CliRunner()
        res = runner.invoke(cli.app, ["dag", "-t", str(bad_primitive_list), "-p", str(bad_primitive_list), "-r", str(temp_git_repo)])
        assert res.exit_code != 0
        assert "Traceback (most recent call last)" not in (res.output or "")
        assert not isinstance(res.exception, TypeError), (
            f"Uncaught TypeError in CLI execution: {res.exception}. Expected typer.BadParameter / clean exit."
        )

    def test_3e_model_json_list_input_handling(self, temp_git_repo: Path, tmp_path: Path, monkeypatch):
        """Adversarial 3e: Lệnh model nhận file chứa list [TaskDefinition] thay vì single TaskDefinition.
        Phải báo lỗi schema/parameter rõ ràng thay vì crash AttributeError: 'list' object has no attribute 'task_id'.
        """
        monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy_key")
        list_task_file = tmp_path / "list_task.json"
        list_task_file.write_text(json.dumps([
            {"task_id": "T_LIST_IN_MODEL", "title": "List Task", "allowed_files": ["Player.cs"]}
        ]), encoding="utf-8")

        runner = CliRunner()
        res = runner.invoke(cli.app, ["model", "-t", str(list_task_file), "-r", str(temp_git_repo)])
        assert res.exit_code != 0
        assert not isinstance(res.exception, AttributeError), (
            f"Uncaught AttributeError: {res.exception}. Expected clean validation message for list passed to single task command."
        )

    def test_3e_recover_json_list_input_handling(self, temp_git_repo: Path, tmp_path: Path, monkeypatch):
        """Adversarial 3e: Lệnh recover nhận file chứa list [TaskDefinition] thay vì single TaskDefinition.
        Phải báo lỗi schema/parameter rõ ràng thay vì crash AttributeError: 'list' object has no attribute 'task_id'.
        """
        monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy_key")
        list_task_file = tmp_path / "list_task_rec.json"
        list_task_file.write_text(json.dumps([
            {"task_id": "T_LIST_IN_REC", "title": "List Task", "allowed_files": ["Player.cs"]}
        ]), encoding="utf-8")

        runner = CliRunner()
        res = runner.invoke(cli.app, ["recover", "-t", str(list_task_file), "-r", str(temp_git_repo)])
        assert res.exit_code != 0
        assert not isinstance(res.exception, AttributeError), (
            f"Uncaught AttributeError: {res.exception}. Expected clean validation message for list passed to single task command."
        )



# ==============================================================================
# Hạng mục 3 — Opt 8.1: Journal Lazy-Write (R3a, R3c, 3a, 3b)
# ==============================================================================

class TestQA_Adversarial_R3_JournalLazyWrite:
    """Adversarial tests for Opt 8.1: Journal lazy-write."""

    def test_r3a_crash_after_intermediate_states_preserves_orphan_detection(self, temp_git_repo: Path):
        """R3a: Crash giữa transaction sau nhiều update_state (không record_end/flush) -> orphan detection trên đĩa vẫn đúng."""
        journal = TransactionJournal(temp_git_repo)
        wt_dir = temp_git_repo / "orphan_test_wt"
        wt_dir.mkdir()

        journal.record_start("tx_crash_sim", "T_CRASH_SIM", "base_commit_123", str(wt_dir), state=TransactionState.CREATED)

        # Mutate intermediate states (lazy in-memory)
        journal.update_state("tx_crash_sim", TransactionState.WORKTREE_READY)
        journal.update_state("tx_crash_sim", TransactionState.PATCH_APPLIED)
        journal.update_state("tx_crash_sim", TransactionState.VALIDATING)

        # Simulate abrupt process crash: drop object without flush() or record_end()
        del journal

        # Recovery journal initializes fresh from disk
        recovery_journal = TransactionJournal(temp_git_repo)
        disk_records = recovery_journal._read_records()
        assert "tx_crash_sim" in disk_records
        # State on disk remains CREATED (by design R3a)
        assert disk_records["tx_crash_sim"].state == TransactionState.CREATED

        # Orphan detection works!
        orphans = recovery_journal.get_orphaned()
        assert any(o.tx_id == "tx_crash_sim" for o in orphans)

    def test_r3c_concurrent_multithreaded_journal_mutations(self, temp_git_repo: Path):
        """R3c: Xen kẽ update_state, record_end, flush từ nhiều thread không deadlock và không làm mất dữ liệu."""
        journal = TransactionJournal(temp_git_repo)
        num_tx = 12
        for i in range(num_tx):
            journal.record_start(f"tx_conc_{i}", f"T_CONC_{i}", "base_commit", f"/tmp/wt_{i}")

        errors = []
        barrier = threading.Barrier(num_tx)

        def worker(tx_idx: int):
            tx_id = f"tx_conc_{tx_idx}"
            try:
                barrier.wait()
                # Interleaved mutations
                journal.update_state(tx_id, TransactionState.VALIDATING)
                time.sleep(0.001)
                journal.update_state(tx_id, TransactionState.PATCH_APPLIED)
                if tx_idx % 2 == 0:
                    # Half of the transactions end
                    journal.record_end(tx_id)
                else:
                    # Other half updates further and flushes
                    journal.update_state(tx_id, TransactionState.INTEGRATED)
                    journal.flush()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_tx)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Concurrent multithreaded journal operations produced errors: {errors}"

        # Verify disk consistency
        disk_recs = journal._read_records()
        for i in range(num_tx):
            tx_id = f"tx_conc_{i}"
            if i % 2 == 0:
                assert tx_id not in disk_recs, f"Ended transaction {tx_id} must not be on disk"
            else:
                assert tx_id in disk_recs, f"Flushed transaction {tx_id} must be on disk"
                assert disk_recs[tx_id].state == TransactionState.INTEGRATED

    def test_3a_journal_overlay_two_tier_visibility(self, temp_git_repo: Path):
        """Adversarial 3a: list_active() thấy pending state MỚI (overlay),
        trong khi _read_records() trực tiếp thấy state TRƯỚC trên đĩa — đúng 2 mức thiết kế.
        """
        journal = TransactionJournal(temp_git_repo)
        journal.record_start("tx_overlay_1", "T_OV", "base_commit", "/tmp/wt_ov", state=TransactionState.CREATED)

        # Update in-memory
        journal.update_state("tx_overlay_1", TransactionState.STAGED)

        # Level 1: list_active() reflects fresh in-memory state
        active = journal.list_active()
        active_rec = next(r for r in active if r.tx_id == "tx_overlay_1")
        assert active_rec.state == TransactionState.STAGED, "list_active() must return fresh overlaid state"

        # Level 2: _read_records() reads directly from disk, sees CREATED
        disk_recs = journal._read_records()
        assert disk_recs["tx_overlay_1"].state == TransactionState.CREATED, "Disk records before flush must be CREATED"

        # After flush, disk reflects the updated state
        journal.flush()
        disk_recs_after = journal._read_records()
        assert disk_recs_after["tx_overlay_1"].state == TransactionState.STAGED, "Disk records after flush must be STAGED"

    def test_3b_journal_record_end_flushes_pending_state_of_other_tx(self, temp_git_repo: Path):
        """Adversarial 3b: record_end của tx A không làm mất pending state của tx B (flush ghi cả state B xuống đĩa)."""
        journal = TransactionJournal(temp_git_repo)
        journal.record_start("tx_A", "T_A", "base", "/tmp/wt_A")
        journal.record_start("tx_B", "T_B", "base", "/tmp/wt_B")

        # Mutate B into pending state
        journal.update_state("tx_B", TransactionState.INTEGRATED)

        # Check disk before tx_A ends: tx_B is CREATED on disk
        disk_before = journal._read_records()
        assert disk_before["tx_B"].state == TransactionState.CREATED

        # tx_A ends (full-rewrite on disk)
        journal.record_end("tx_A")

        # Disk check: tx_A is deleted, AND tx_B is persisted with state INTEGRATED!
        disk_after = journal._read_records()
        assert "tx_A" not in disk_after, "tx_A must be removed from disk"
        assert "tx_B" in disk_after, "tx_B must remain on disk"
        assert disk_after["tx_B"].state == TransactionState.INTEGRATED, (
            "tx_A's record_end must flush tx_B's pending state to disk"
        )


# ==============================================================================
# Hạng mục 4 — Opt 8.2: Patch Engine Locate Unique Match (R4a, R4b)
# ==============================================================================

class TestQA_Adversarial_R4_PatchEngineLocateMatch:
    """Adversarial tests for Opt 8.2: Patch Engine locate unique match."""

    def test_r4a_overlapping_aaa_with_aa_replacement_behavior(self):
        """R4a: Xử lý pattern chồng lấp: aaa với old_text aa chỉ có 1 match non-overlapping tại index 0."""
        content = "aaa"
        old_text = "aa"
        pos = pe._locate_unique_match(content, old_text, "test.cs", 1)
        assert pos == 0

        # Verify slicing matches str.replace(1)
        new_text = "bb"
        sliced = content[:pos] + new_text + content[pos + len(old_text):]
        assert sliced == "bba"
        assert sliced == content.replace(old_text, new_text, 1)

    def test_r4a_overlapping_pattern_in_apply_hunks(self):
        """R4a: apply_hunks trên nội dung 'aaa' thay 'aa' bằng 'bb' trả về 'bba' thành công."""
        hunks = [PatchHunk(old_text="aa", new_text="bb")]
        res = pe.apply_hunks(base_content="aaa", hunks=hunks, is_new_file=False, file_path="test.cs")
        assert res == "bba"

    def test_r4a_multiple_non_overlapping_matches_raise_ambiguous(self):
        """R4a: 'aaaa' có 2 match non-overlapping của 'aa' (0 và 2) -> raise ambiguous 2 times."""
        with pytest.raises(pe.PatchValidationError) as exc_info:
            pe._locate_unique_match("aaaa", "aa", "test.cs", 1)
        msg = str(exc_info.value)
        assert "Ambiguous hunk match: old_text appears 2 times in test.cs (hunk 1)" in msg

    def test_r4a_five_as_with_two_matches(self):
        """R4a: 'aaaaa' có 2 match non-overlapping của 'aa' (0 và 2, thừa 1 ký tự a) -> raise ambiguous 2 times."""
        with pytest.raises(pe.PatchValidationError) as exc_info:
            pe._locate_unique_match("aaaaa", "aa", "test.cs", 2)
        assert "Ambiguous hunk match: old_text appears 2 times in test.cs (hunk 2)" in str(exc_info.value)

    def test_r4b_empty_old_text_in_locate_unique_match_raises_ambiguous(self):
        """R4b: old_text rỗng '' trong _locate_unique_match luôn ném ambiguous hunk match."""
        content = "hello world"
        with pytest.raises(pe.PatchValidationError) as exc_info:
            pe._locate_unique_match(content, "", "test.cs", 1)
        msg = str(exc_info.value)
        expected_count = len(content) + 1
        assert f"Ambiguous hunk match: old_text appears {expected_count} times in test.cs (hunk 1)" in msg

    def test_r4b_empty_old_text_with_empty_content_raises_ambiguous(self):
        """R4b: old_text rỗng '' với content rỗng '' cũng ném ambiguous (appears 1 times)."""
        with pytest.raises(pe.PatchValidationError) as exc_info:
            pe._locate_unique_match("", "", "test.cs", 1)
        assert "Ambiguous hunk match: old_text appears 1 times in test.cs (hunk 1)" in str(exc_info.value)

    def test_r4b_apply_hunks_empty_old_text_on_existing_file_rejected(self):
        """R4b: apply_hunks trên file hiện có với hunk old_text='' bị từ chối ngay trước khi locate."""
        hunks = [PatchHunk(old_text="", new_text="new content")]
        with pytest.raises(pe.PatchValidationError) as exc_info:
            pe.apply_hunks(base_content="some content", hunks=hunks, is_new_file=False, file_path="existing.cs")
        assert "Empty old_text is not allowed on existing file: existing.cs" in str(exc_info.value)

    def test_r4b_apply_hunks_new_file_subsequent_empty_old_text_rejected(self):
        """R4b: apply_hunks trên file mới nhưng hunk thứ 2 có old_text='' bị từ chối."""
        hunks = [
            PatchHunk(old_text="", new_text="initial content\n"),
            PatchHunk(old_text="", new_text="appended content\n"),
        ]
        with pytest.raises(pe.PatchValidationError) as exc_info:
            pe.apply_hunks(base_content=None, hunks=hunks, is_new_file=True, file_path="new.cs")
        assert "Empty old_text is not allowed on existing file content in hunk 2 for 'new.cs'" in str(exc_info.value)


# ==============================================================================
# Hạng mục 5 — Opt 8.3: Tree-sitter Language Sharing (R5a)
# ==============================================================================

class TestQA_Adversarial_R5_TreeSitterSharing:
    """Adversarial tests for Opt 8.3: Tree-sitter Language sharing."""

    def test_r5a_bidirectional_import_safety_then_context(self):
        """R5a: Import theo thứ tự safety -> context trong process độc lập không bị circular import."""
        cmd = [sys.executable, "-c", "import safety.ast_guard; import context.extractor; print('IMPORT_OK')"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        assert res.returncode == 0
        assert "IMPORT_OK" in res.stdout

    def test_r5a_bidirectional_import_context_then_safety(self):
        """R5a: Import theo thứ tự context -> safety trong process độc lập không bị circular import."""
        cmd = [sys.executable, "-c", "import context.extractor; import safety.ast_guard; print('IMPORT_OK')"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        assert res.returncode == 0
        assert "IMPORT_OK" in res.stdout

    def test_r5a_import_shared_module_directly(self):
        """R5a: Import trực tiếp safety.tree_sitter_shared độc lập không bị lỗi."""
        cmd = [sys.executable, "-c", "import safety.tree_sitter_shared; print('SHARED_OK')"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        assert res.returncode == 0
        assert "SHARED_OK" in res.stdout

    def test_r5a_object_identity_across_guard_extractor_and_shared(self):
        """R5a: ASTGuard, SymbolExtractor và get_csharp_language() trả về đúng cùng một object Language."""
        guard = ASTGuard()
        extractor = SymbolExtractor()
        shared_lang = get_csharp_language()

        assert guard.language is extractor.language, "ASTGuard and SymbolExtractor must share language"
        assert guard.language is shared_lang, "ASTGuard must use cached singleton language"
        assert guard.parser is not extractor.parser, "ASTGuard and SymbolExtractor must have separate Parser instances"


# ==============================================================================
# Hạng mục 6 — Opt 8.4: Status Cache Trong TransactionWorktree (R6a, R6b, 3d)
# ==============================================================================

class TestQA_Adversarial_R6_WorktreeStatusCache:
    """Adversarial tests for Opt 8.4: Status cache in TransactionWorktree."""

    def test_r6a_stage_exact_invalidates_cache_and_reflects_staged_state(self, temp_git_repo: Path):
        """R6a: stage_exact invalidate cache -> get_status tiếp theo phản ánh đúng file staged."""
        ws = WorkspaceManager(temp_git_repo)
        tx_wt = ws.create_transaction_worktree("tx_qa_r6a", base_commit=ws.get_head_commit())
        try:
            # 1. Initial clean status cached
            st_initial = tx_wt.get_status()
            assert st_initial.dirty is False
            assert tx_wt._status_cache is not None

            # 2. Mutate file
            p_file = tx_wt.worktree_path / "Player.cs"
            p_file.write_text("public class Player { /* R6a mutation */ }\n", encoding="utf-8")

            # 3. Stage
            tx_wt.stage_exact(["Player.cs"])
            # Cache must be invalidated (None)
            assert tx_wt._status_cache is None, "stage_exact must set _status_cache to None"

            # 4. get_status must show staged modification
            st_after = tx_wt.get_status()
            assert st_after.dirty is True
            assert "Player.cs" in st_after.modified_files
            staged_entry = next((s for s in st_after.file_statuses if s.path == "Player.cs"), None)
            assert staged_entry is not None
            assert staged_entry.status_code.startswith("M"), f"Expected status_code to start with 'M', got '{staged_entry.status_code}'"
        finally:
            ws.remove_transaction_worktree(tx_wt)

    def test_r6b_commit_invalidates_cache_before_is_clean_and_shows_clean(self, temp_git_repo: Path):
        """R6b: commit() invalidate cache TRƯỚC khi gọi is_clean nội bộ -> sau commit worktree sạch thật."""
        ws = WorkspaceManager(temp_git_repo)
        tx_wt = ws.create_transaction_worktree("tx_qa_r6b", base_commit=ws.get_head_commit())
        try:
            p_file = tx_wt.worktree_path / "Player.cs"
            p_file.write_text("public class Player { /* R6b commit test */ }\n", encoding="utf-8")
            tx_wt.stage_exact(["Player.cs"])

            # Call get_status to ensure dirty status is in cache right before commit
            st_dirty = tx_wt.get_status()
            assert st_dirty.dirty is True
            assert tx_wt._status_cache is not None

            # Commit staged changes. Commit internally calls is_clean().
            # If cache wasn't invalidated at start of commit, internal is_clean would see dirty cache and raise WorktreeStagingError!
            commit_hash = tx_wt.commit(
                task_id="T_R6B",
                message="Commit test R6b",
                expected_paths=["Player.cs"],
            )
            assert commit_hash is not None

            # Post-commit check: is_clean() must be True
            assert tx_wt.is_clean() is True, "Worktree must be clean after commit"
            st_post = tx_wt.get_status()
            assert st_post.dirty is False, "get_status().dirty must be False after successful commit"
            assert len(st_post.modified_files) == 0
        finally:
            ws.remove_transaction_worktree(tx_wt)

    def test_3d_status_cache_call_count_through_mutation_lifecycle(self, temp_git_repo: Path):
        """Adversarial 3d: Chuỗi is_clean -> verify_changed_paths -> get_status trong window không đột biến
        chỉ spawn 1 git status; sau stage_exact và commit phải spawn mới.
        """
        ws = WorkspaceManager(temp_git_repo)
        tx_wt = ws.create_transaction_worktree("tx_qa_3d", base_commit=ws.get_head_commit())
        try:
            status_invocations = []
            orig_run_git_bytes = tx_wt._run_git_bytes

            def spy_run_git_bytes(*args, **kwargs):
                if args and args[0] == "status":
                    status_invocations.append(args)
                return orig_run_git_bytes(*args, **kwargs)

            tx_wt._run_git_bytes = spy_run_git_bytes

            # 1. Non-mutating window: is_clean -> verify_changed_paths -> get_status
            assert tx_wt.is_clean() is True
            valid, _ = tx_wt.verify_changed_paths(set())
            assert valid is True
            st = tx_wt.get_status()
            assert st.dirty is False

            assert len(status_invocations) == 1, (
                f"Non-mutating window must spawn git status exactly once, got {len(status_invocations)}"
            )

            # 2. Mutate file and stage
            p_file = tx_wt.worktree_path / "Player.cs"
            p_file.write_text("public class Player { /* Mutated 3d */ }\n", encoding="utf-8")

            tx_wt.stage_exact(["Player.cs"])
            # Cache is now invalidated. Querying get_status must spawn a 2nd git status
            st_staged = tx_wt.get_status()
            assert st_staged.dirty is True
            assert len(status_invocations) == 2, (
                f"After stage_exact, get_status must spawn a 2nd git status, got {len(status_invocations)}"
            )

            # 3. Commit mutation
            tx_wt.commit(task_id="T_3D", message="Commit 3d", expected_paths=["Player.cs"])
            # Commit invalidates cache and calls is_clean() post-commit, spawning 3rd git status
            assert len(status_invocations) == 3, (
                f"Commit must spawn a 3rd git status for post-commit verification, got {len(status_invocations)}"
            )

            # 4. Subsequent is_clean() in clean window must reuse commit's cached status
            assert tx_wt.is_clean() is True
            assert len(status_invocations) == 3, (
                f"Subsequent is_clean() should reuse cached post-commit status, got {len(status_invocations)}"
            )
        finally:
            ws.remove_transaction_worktree(tx_wt)
