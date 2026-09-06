"""Phase A (v2.3) — T2.5 Mutation Gate + Holdout isolation + Write-Scope + context filtering.

RED tests first (TDD): these encode the v2.3 Stage 2 Phase A contracts:
1. validation/mutation.py ASTMutationGate exists and kills vacuous tests.
2. Planner schema generates holdout tests (separate from visible tests).
3. ScopeGuard forbids Dev from writing/deleting test files.
4. ContextBuilder filters holdout_test_code from Dev context.
5. Runtime wires the mutation gate into the validation pipeline.
"""
import subprocess
from pathlib import Path

import pytest

from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk
from safety.scope_guard import ScopeGuard, ScopeViolationError
from context.builder import ContextBuilder
from orchestrator.planner import PlannedTask


# ==============================================================================
# 1. ASTMutationGate
# ==============================================================================

class TestMutationGate:
    def test_mutation_module_exists(self):
        import validation.mutation as M
        assert hasattr(M, "ASTMutationGate")
        assert hasattr(M, "MutationOperator")
        assert hasattr(M, "extract_diff_regions")

    def test_diff_regions_only_in_changed_area(self):
        from validation.mutation import extract_diff_regions
        old = "line1\nline2\nline3\nline4\nline5"
        new = "line1\nline2\nCHANGED\nline4\nline5"
        regions = extract_diff_regions(old, new, "f.txt")
        assert len(regions) == 1
        assert regions[0].start_line == 3 and regions[0].end_line == 3

    def test_operators_include_required_inversions(self):
        from validation.mutation import MutationOperator
        pairs = {op.value for op in MutationOperator}
        assert (("==", "!=") in pairs) and ((">", "<=") in pairs)
        assert (("+", "-") in pairs) and (("true", "false") in pairs)

    def test_generate_mutants_only_in_diff_region(self):
        from validation.mutation import ASTMutationGate
        gate = ASTMutationGate(test_runner=lambda p: (0, ""))
        old = "public class C {\n    int a = 1;\n}\n"
        new = (
            "public class C {\n"
            "    int a = 1;\n"
            "    int b = a + 5;\n"
            "    bool ok = (a == 1);\n"
            "}\n"
        )
        mutants = gate.generate_mutants(old, new, "C.cs")
        assert mutants, "must generate at least one mutant"
        assert all(m.line_no in (3, 4) for m in mutants), (
            f"mutants must stay inside diff lines 3-4, got {[m.line_no for m in mutants]}"
        )
        plus_mutants = [m for m in mutants if m.operator.name == "PLUS_TO_MINUS"]
        assert any("a - 5" in m.mutated_text for m in plus_mutants)
        eq_mutants = [m for m in mutants if m.operator.name == "EQ_TO_NE"]
        assert any("a != 1" in m.mutated_text for m in eq_mutants)

    def test_string_literals_never_mutated(self):
        from validation.mutation import ASTMutationGate
        gate = ASTMutationGate(test_runner=lambda p: (0, ""))
        old = "class C { }\n"
        new = 'class C { string s = "a == b + true"; }\n'
        mutants = gate.generate_mutants(old, new, "C.cs")
        assert mutants == [], f"must not mutate inside string literals, got {mutants}"

    def test_comments_never_mutated(self):
        from validation.mutation import ASTMutationGate
        gate = ASTMutationGate(test_runner=lambda p: (0, ""))
        old = "class C { }\n"
        new = "class C {\n    // x == y + z\n}\n"
        mutants = gate.generate_mutants(old, new, "C.cs")
        assert mutants == []

    def test_gate_fails_when_tests_vacuous(self, tmp_path):
        """A test runner that always passes lets every mutant survive => REJECT."""
        from validation.mutation import ASTMutationGate
        repo = tmp_path / "repo"
        repo.mkdir()
        f = repo / "MathUtil.cs"
        post_patch_code = "public class MathUtil {\n    public static int Add(int a, int b) { return a + b; }\n}\n"
        f.write_text(post_patch_code, encoding="utf-8")
        old_code = "public class MathUtil {\n    public static int Add(int a, int b) { throw new System.NotImplementedException(); }\n}\n"

        always_pass = lambda p: (0, "ok")  # vacuous test suite
        gate = ASTMutationGate(test_runner=always_pass, threshold=0.7)
        res = gate.run_gate(repo, old_code, post_patch_code, "MathUtil.csv" if False else "MathUtil.cs")
        assert res.success is False
        assert res.total >= 1
        assert res.killed == 0
        assert res.mutation_score < 0.7
        assert f.read_text(encoding="utf-8") == post_patch_code, (
            "gate must restore the exact post-patch content afterwards"
        )

    def test_gate_passes_when_tests_kill_all(self, tmp_path):
        from validation.mutation import ASTMutationGate
        repo = tmp_path / "repo"
        repo.mkdir()
        f = repo / "MathUtil.cs"
        post_patch_code = "public class MathUtil {\n    public static int Add(int a, int b) { return a + b; }\n}\n"
        f.write_text(post_patch_code, encoding="utf-8")
        old_code = "public class MathUtil { }\n"

        def killing_runner(p):
            # A real suite fails (rc != 0) under mutation => mutant killed.
            return (1, "assertion failed")

        gate = ASTMutationGate(test_runner=killing_runner, threshold=0.7)
        res = gate.run_gate(repo, old_code, post_patch_code, "MathUtil.cs")
        assert res.success is True
        assert res.killed == res.total and res.total >= 1
        assert res.mutation_score == 1.0
        assert f.read_text(encoding="utf-8") == post_patch_code

    def test_gate_skips_when_no_mutable_tokens(self, tmp_path):
        from validation.mutation import ASTMutationGate
        repo = tmp_path / "repo"
        repo.mkdir()
        f = repo / "Doc.cs"
        post = "public class Doc {\n    public string Name { get; set; }\n}\n"
        f.write_text(post, encoding="utf-8")
        old = "public class Doc { }\n"
        gate = ASTMutationGate(test_runner=lambda p: (0, ""))
        res = gate.run_gate(repo, old, post, "Doc.cs")
        assert res.success is True and res.total == 0

    def test_max_mutants_cap(self, tmp_path):
        from validation.mutation import ASTMutationGate
        repo = tmp_path / "repo"
        repo.mkdir()
        f = repo / "Big.cs"
        post = "class Big {\n" + "\n".join(f"    int v{i} = {i} + {i};" for i in range(10)) + "\n}\n"
        f.write_text(post, encoding="utf-8")
        old = "class Big { }\n"
        gate = ASTMutationGate(test_runner=lambda p: (0, ""), threshold=0.7, max_mutants=5)
        res = gate.run_gate(repo, old, post, "Big.cs")
        assert res.total == 5

    def test_threshold_out_of_range_rejected(self):
        from validation.mutation import ASTMutationGate
        with pytest.raises(ValueError):
            ASTMutationGate(test_runner=lambda p: (0, ""), threshold=1.5)
        with pytest.raises(ValueError):
            ASTMutationGate(test_runner=lambda p: (0, ""), threshold=-0.1)


# ==============================================================================
# 2. Planner holdout schema
# ==============================================================================

class TestPlannerHoldout:
    def test_planned_task_has_holdout_fields(self):
        fields = PlannedTask.model_fields
        assert "holdout_test_code" in fields
        assert "holdout_test_file" in fields
        # visible test remains the legacy field
        assert "test_code" in fields

    def test_planner_prompt_requests_holdout(self):
        import orchestrator.planner as P
        src = Path(P.__file__).read_text(encoding="utf-8")
        assert "holdout" in src.lower(), "planner prompt must ask QA for a holdout test"

    def test_planner_prompt_requires_pbt(self):
        import orchestrator.planner as P
        src = Path(P.__file__).read_text(encoding="utf-8")
        assert "100" in src and ("property" in src.lower() or "invariant" in src.lower()), (
            "planner prompt must require Native PBT Generative Loop (100 iterations)"
        )


# ==============================================================================
# 3. Write-Scope: Dev cannot write test files
# ==============================================================================

def _git_repo_with_tests(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    def run(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (repo / "Player.cs").write_text("public class Player { public int Hp; }\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "PlayerTest.cs").write_text("// existing test\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "init")
    return repo


class TestWriteScopeIsolation:
    def test_scope_guard_rejects_dev_writing_csharp_test_file(self, tmp_path):
        repo = _git_repo_with_tests(tmp_path)
        task = TaskDefinition(
            task_id="T_WS_01",
            title="Write test file as dev",
            allowed_files=["Player.cs", "tests/PlayerTest.cs"],
        )
        proposal = PatchProposal(
            patches=[
                FilePatch(
                    file="tests/PlayerTest.cs",
                    hunks=[PatchHunk(old_text="// existing test", new_text="// dev-tampered test")],
                )
            ]
        )
        guard = ScopeGuard()
        with pytest.raises(ScopeViolationError, match="test file"):
            guard.validate(task, proposal, repo_path=repo)

    def test_scope_guard_rejects_dev_writing_new_test_file(self, tmp_path):
        repo = _git_repo_with_tests(tmp_path)
        task = TaskDefinition(
            task_id="T_WS_02",
            title="Create test file as dev",
            allowed_files=["Player.cs", "tests/NewTests.cs"],
        )
        proposal = PatchProposal(
            patches=[
                FilePatch(
                    file="tests/NewTests.cs",
                    hunks=[PatchHunk(old_text="", new_text="[Test]\npublic class NewTests { }\n")],
                )
            ]
        )
        guard = ScopeGuard()
        with pytest.raises(ScopeViolationError, match="test file"):
            guard.validate(task, proposal, repo_path=repo)

    def test_scope_guard_rejects_dev_writing_pytest_test_file(self, tmp_path):
        repo = _git_repo_with_tests(tmp_path)
        task = TaskDefinition(
            task_id="T_WS_03",
            title="Create pytest file as dev",
            allowed_files=["Player.cs", "tests/test_dev.py"],
        )
        proposal = PatchProposal(
            patches=[
                FilePatch(
                    file="tests/test_dev.py",
                    hunks=[PatchHunk(old_text="", new_text="def test_x():\n    assert True\n")],
                )
            ]
        )
        guard = ScopeGuard()
        with pytest.raises(ScopeViolationError, match="test file"):
            guard.validate(task, proposal, repo_path=repo)

    def test_scope_guard_rejects_dev_writing_nested_test_pattern(self, tmp_path):
        """Files under any *Tests/ directory must be rejected for Dev even
        when the basename is not a test name."""
        repo = _git_repo_with_tests(tmp_path)
        task = TaskDefinition(
            task_id="T_WS_04",
            title="Dev writes helper inside Tests dir",
            allowed_files=["Player.cs", "MyTests/helper.cs"],
        )
        proposal = PatchProposal(
            patches=[
                FilePatch(
                    file="MyTests/helper.cs",
                    hunks=[PatchHunk(old_text="", new_text="public class Helper { }\n")],
                )
            ]
        )
        guard = ScopeGuard()
        with pytest.raises(ScopeViolationError, match="test file"):
            guard.validate(task, proposal, repo_path=repo)

    def test_scope_guard_allows_normal_dev_writes(self, tmp_path):
        repo = _git_repo_with_tests(tmp_path)
        task = TaskDefinition(
            task_id="T_WS_05",
            title="Normal dev write",
            allowed_files=["Player.cs"],
        )
        proposal = PatchProposal(
            patches=[
                FilePatch(file="Player.cs", hunks=[PatchHunk(old_text="public int Hp;", new_text="public int Hp = 100;")])
            ]
        )
        guard = ScopeGuard()
        guard.validate(task, proposal, repo_path=repo)  # must NOT raise

    def test_qa_role_task_can_write_test_files(self, tmp_path):
        """Tasks carrying role='qa' (red-test authoring) are exempt from the
        test-file write ban; Dev tasks default to role='dev'.

        This test drives the additive schema change: TaskDefinition must
        ACCEPT and PERSIST a role field (extra kwargs must not be silently
        dropped, otherwise the exemption cannot be enforced reliably)."""
        repo = _git_repo_with_tests(tmp_path)
        task = TaskDefinition(
            task_id="T_WS_06",
            title="QA writes test",
            allowed_files=["tests/QaTest.cs"],
            role="qa",
        )
        # The role must actually persist on the model, not be silently dropped.
        assert task.model_dump().get("role") == "qa", (
            "TaskDefinition must persist role='qa' (extra kwargs are being "
            "silently dropped, so the QA exemption cannot be enforced)"
        )
        proposal = PatchProposal(
            patches=[
                FilePatch(
                    file="tests/QaTest.cs",
                    hunks=[PatchHunk(old_text="", new_text="[Test] public class QaTest { }\n")],
                )
            ]
        )
        guard = ScopeGuard()
        guard.validate(task, proposal, repo_path=repo)  # must NOT raise

    def test_dev_role_default_writes_test_files_rejected(self, tmp_path):
        """Default role is 'dev': test-file writes are rejected without any
        explicit role."""
        repo = _git_repo_with_tests(tmp_path)
        task = TaskDefinition(
            task_id="T_WS_07",
            title="Default role write",
            allowed_files=["tests/QaTest.cs"],
        )
        assert task.model_dump().get("role") == "dev"
        proposal = PatchProposal(
            patches=[
                FilePatch(
                    file="tests/QaTest.cs",
                    hunks=[PatchHunk(old_text="", new_text="[Test] public class QaTest { }\n")],
                )
            ]
        )
        guard = ScopeGuard()
        with pytest.raises(ScopeViolationError, match="test file"):
            guard.validate(task, proposal, repo_path=repo)


# ==============================================================================
# 4. Context isolation: holdout never reaches Dev
# ==============================================================================

class TestContextIsolation:
    def test_context_builder_filters_holdout_test_code(self):
        """file_snippets containing holdout content must not leak into the
        Dev prompt."""
        builder = ContextBuilder()
        task = TaskDefinition(
            task_id="T_CI_01",
            title="Context isolation",
            allowed_files=["src/Player.cs"],
            target_symbols=["Player"],
        )
        holdout_code = "SECRET_HOLDOUT_MARKER_assert_PBT_roundtrip_9173"
        snippets = {
            "src/Player.cs": "public class Player { public int Hp; }",
            "tests/HoldoutTests.cs": f"[Test] public class Holdout {{ void H() {{ Assert.True({holdout_code}); }} }}",
        }
        ctx_str, manifest = builder.build_context_with_manifest(
            task=task,
            file_snippets=snippets,
        )
        assert "SECRET_HOLDOUT_MARKER" not in ctx_str

    def test_context_builder_filters_holdout_via_previous_failure(self):
        """previous_failure may quote holdout test output; it must be
        sanitized before entering Dev context."""
        builder = ContextBuilder()
        task = TaskDefinition(
            task_id="T_CI_02",
            title="Failure history isolation",
            allowed_files=["src/Player.cs"],
        )
        ctx_str, _ = builder.build_context_with_manifest(
            task=task,
            file_snippets={"src/Player.cs": "public class Player { }"},
            previous_failure=(
                "FAILED tests/HoldoutTests.cs::TestHoldout - Expected: 42 But was: 13\n"
                "SECRET_HOLDOUT_MARKER in assertion"
            ),
        )
        assert "SECRET_HOLDOUT_MARKER" not in ctx_str
        assert "HoldoutTests" not in ctx_str


# ==============================================================================
# 5. Runtime wiring
# partial-class syntax errors above removed; consolidated here.
# ==============================================================================

class TestRuntimeWiring:
    def _runtime_repo(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        def run(*args):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
        run("init", "-q")
        run("config", "user.email", "t@t")
        run("config", "user.name", "t")
        (repo / "README.md").write_text("repo", encoding="utf-8")
        run("add", ".")
        run("commit", "-qm", "init")
        return repo

    def test_runtime_wires_mutation_gate_when_test_command_configured(self, tmp_path):
        from core.runtime import DSHRuntime
        from core.config import PipelineConfig

        repo = self._runtime_repo(tmp_path)
        cfg = PipelineConfig(workspace_root=repo, test_command=["true"])
        runtime = DSHRuntime(repo, config=cfg, dry_run=True, test_mode=False)
        assert runtime.validation_pipeline.mutation_gate is not None
        assert callable(runtime.validation_pipeline.mutation_gate)

    def test_runtime_mutation_gate_off_in_test_mode(self, tmp_path):
        from core.runtime import DSHRuntime

        repo = self._runtime_repo(tmp_path)
        runtime = DSHRuntime(repo, test_mode=True)
        assert runtime.validation_pipeline.mutation_gate is None

    def test_runtime_wires_holdout_injector(self, tmp_path):
        from core.runtime import DSHRuntime
        from core.config import PipelineConfig
        repo = self._runtime_repo(tmp_path)
        cfg = PipelineConfig(workspace_root=repo, test_command=["true"])
        runtime = DSHRuntime(repo, config=cfg, dry_run=True, test_mode=False)
        assert runtime.validation_pipeline.holdout_injector is not None
        assert callable(runtime.validation_pipeline.holdout_injector)


class TestMutationGatePipelineIntegration:
    def _git_repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        def run(*args):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
        run("init", "-q")
        run("config", "user.email", "t@t")
        run("config", "user.name", "t")
        (repo / "MathUtil.cs").write_text(
            "public class MathUtil {\n    public static int Add(int a, int b) { throw new System.NotImplementedException(); }\n}\n",
            encoding="utf-8",
        )
        run("add", ".")
        run("commit", "-qm", "init")
        return repo

    def test_pipeline_reports_t2_5_failure_when_vacuous(self, tmp_path):
        from core.runtime import DSHRuntime
        from core.config import PipelineConfig
        from recovery.classifier import FailureType

        repo = self._git_repo(tmp_path)
        cfg = PipelineConfig(
            workspace_root=repo,
            build_command=["true"],
            test_command=["true"],
        )
        runtime = DSHRuntime(repo, config=cfg, dry_run=True, test_mode=False)

        task = TaskDefinition(
            task_id="T_PG_01",
            title="Vacuous suite gate",
            allowed_files=["MathUtil.cs"],
            target_symbols=["MathUtil.Add"],
        )
        proposal = PatchProposal(
            patches=[
                FilePatch(
                    file="MathUtil.cs",
                    hunks=[
                        PatchHunk(
                            old_text="public static int Add(int a, int b) { throw new System.NotImplementedException(); }",
                            new_text="public static int Add(int a, int b) { return a + b; }",
                        )
                    ],
                )
            ]
        )

        res = runtime.execute_transaction(task, proposal)
        assert res.success is False
        assert res.failure_type == FailureType.MUTATION_COVERAGE.value
        assert "T2.5" in res.error_message
