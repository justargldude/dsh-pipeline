import subprocess
from pathlib import Path
import pytest

from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk, RiskLevel
from core.runtime import DSHRuntime
from core.workspace import WorkspaceManager
from build.sandbox import MockBuildRunner, SubprocessBuildRunner
from validation.behavioral import MockBehavioralValidator
from validation.regression import MockRegressionValidator
from validation.risk import RiskValidator
from safety.patch_validator import PatchValidator, PatchValidationError
from safety.scope_guard import ScopeGuard, ScopeViolationError
from safety.ast_guard import ASTGuard
from recon.database import EvidenceDatabase, SymbolRecord
from context.builder import ContextBuilder
from context.budget import ContextComplexity, TokenBudgetManager
from context.ranking import ContextItem, ContextRanker, PriorityLevel
from model.schemas import ModelRequest, ModelType
from model.providers import MockModelProvider
from model.router import ModelRouter
from task.dag import TaskDAG
from task.scheduler import DAGScheduler
from recovery.classifier import FailureType


@pytest.fixture
def temp_git_repo(tmp_path: Path):
    """Creates an isolated clean Git repository for testing."""
    repo = tmp_path / "baseline_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Baseline Tester"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tester@baseline.local"], cwd=repo, check=True)

    test_file = repo / "Player.cs"
    test_file.write_text(
        "public class Player {\n"
        "    public void Update() {\n"
        "        int x = 1;\n"
        "        int y = 1;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)
    return repo


def test_baseline_runtime_default_validators(temp_git_repo: Path):
    """Verify that DSHRuntime defaults to None (fail closed) in prod, and mock runners in test_mode."""
    runtime_prod = DSHRuntime(temp_git_repo)
    assert runtime_prod.build_runner is None
    assert runtime_prod.validation_pipeline.behavioral_validator is None
    assert runtime_prod.validation_pipeline.regression_validator is None

    runtime_test = DSHRuntime(temp_git_repo, test_mode=True)
    assert isinstance(runtime_test.build_runner, MockBuildRunner)
    assert runtime_test.build_runner.should_succeed is True
    assert isinstance(runtime_test.validation_pipeline.behavioral_validator, MockBehavioralValidator)
    assert isinstance(runtime_test.validation_pipeline.regression_validator, MockRegressionValidator)


def test_baseline_short_old_text_ambiguity_behavior(temp_git_repo: Path):
    """Verify that old_text <= 20 chars with multiple matches IS rejected under Phase 3."""
    # File has two occurrences of '        int ' (length < 20)
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="        int ", new_text="        long ")],
            )
        ]
    )

    with pytest.raises(PatchValidationError) as exc:
        PatchValidator.validate_proposal(proposal, temp_git_repo)
    assert "Ambiguous hunk match" in str(exc.value)


def test_baseline_long_old_text_ambiguity_rejection(temp_git_repo: Path):
    """Verify that old_text > 20 chars appearing multiple times IS rejected."""
    # Write a file with two identical blocks > 20 chars
    long_line = "        // Long unique comment line with more than twenty chars\n"
    (temp_git_repo / "Player.cs").write_text(
        f"public class Player {{\n{long_line}{long_line}}}\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=temp_git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "Add duplicate long lines"], cwd=temp_git_repo, check=True)

    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text=long_line, new_text="        // Replaced\n")],
            )
        ]
    )

    with pytest.raises(PatchValidationError) as exc:
        PatchValidator.validate_proposal(proposal, temp_git_repo)
    assert "Ambiguous hunk match" in str(exc.value)


def test_baseline_empty_old_text_appends_to_file(temp_git_repo: Path):
    """Verify that old_text='' on an existing file is rejected under Phase 3."""
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="", new_text="// Appended at end\n")],
            )
        ]
    )
    with pytest.raises(PatchValidationError) as exc:
        PatchValidator.validate_proposal(proposal, temp_git_repo)
    assert "Empty old_text is not allowed on existing file" in str(exc.value)



def test_baseline_git_add_all_stages_unrelated_files(temp_git_repo: Path):
    """Verify that worktree isolation and exact staging do NOT commit unrelated untracked files."""
    # Create an unrelated untracked file
    unrelated_file = temp_git_repo / "unrelated_secret.txt"
    unrelated_file.write_text("unrelated data", encoding="utf-8")

    # If is_clean allows untracked paths:
    runtime = DSHRuntime(temp_git_repo, dry_run=False, allowed_untracked_paths=["unrelated_secret.txt"], test_mode=True)

    task = TaskDefinition(
        task_id="T_GIT_01",
        title="Commit with untracked file in workspace",
        allowed_files=["Player.cs"],
    )
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="        int x = 1;", new_text="        int x = 99;")],
            )
        ]
    )

    res = runtime.execute_transaction(task, proposal)
    assert res.success is True

    # Check git log - unrelated_secret.txt was NOT committed in this transaction!
    res_diff = subprocess.run(
        ["git", "show", "--name-only", "--pretty=", "HEAD"],
        cwd=temp_git_repo,
        capture_output=True,
        text=True,
    )
    committed_files = res_diff.stdout.strip().splitlines()
    assert "unrelated_secret.txt" not in committed_files
    assert "Player.cs" in committed_files
    # Unrelated file survives in main workspace untouched
    assert (temp_git_repo / "unrelated_secret.txt").read_text(encoding="utf-8") == "unrelated data"


def test_baseline_target_symbols_enforcement(temp_git_repo: Path):
    """Verify that target_symbols in TaskDefinition is strictly enforced by safety guards."""
    task = TaskDefinition(
        task_id="T_SYM_01",
        title="Task targeting SpecificMethod",
        allowed_files=["Player.cs"],
        target_symbols=["Player.SpecificMethod"],  # SpecificMethod doesn't exist; Update is in Player.cs
    )
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="        int x = 1;", new_text="        int x = 42;")],
            )
        ]
    )
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)
    res = runtime.execute_transaction(task, proposal)
    # Rejected because target_symbols is strictly enforced
    assert res.success is False
    assert "Target symbol violation" in res.error_message


def test_baseline_high_risk_does_not_bypass_checks(temp_git_repo: Path):
    """Verify that RiskLevel.HIGH does NOT bypass RiskValidator pattern checks."""
    task_high = TaskDefinition(
        task_id="T_RISK_01",
        title="High risk task",
        allowed_files=["Player.cs"],
        risk=RiskLevel.HIGH,
    )
    task_med = TaskDefinition(
        task_id="T_RISK_02",
        title="Medium risk task",
        allowed_files=["Player.cs"],
        risk=RiskLevel.MEDIUM,
    )

    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="    public void Update() {", new_text="    public void Update() {\n        [DllImport(\"kernel32\")] static extern void Dangerous();\n")],
            )
        ]
    )

    # HIGH risk fails RiskValidator (no bypass)
    high_result = RiskValidator.validate_risk(task_high, proposal, temp_git_repo)
    assert high_result.success is False
    assert len(high_result.violations) > 0

    # MEDIUM risk fails RiskValidator
    med_result = RiskValidator.validate_risk(task_med, proposal, temp_git_repo)
    assert med_result.success is False
    assert len(med_result.violations) > 0


def test_baseline_symbol_database_distinguishes_overloads():
    """Verify that EvidenceDatabase distinguishes overloaded methods without collision."""
    db = EvidenceDatabase()
    sym1 = SymbolRecord(
        symbol_name="Player.Update",
        class_name="Player",
        signature="void Update()",
        rva="0x1000",
        version="v1",
        parameters=[],
    )
    sym2 = SymbolRecord(
        symbol_name="Player.Update",
        class_name="Player",
        signature="void Update(int delta)",
        rva="0x2000",
        version="v1",
        parameters=["int delta"],
    )

    db.insert_symbol(sym1)
    db.insert_symbol(sym2)

    retrieved1 = db.get_symbol("Player.Update", "v1", signature="void Update()")
    retrieved2 = db.get_symbol("Player.Update", "v1", signature="void Update(int delta)")
    assert retrieved1 is not None
    assert retrieved1.rva == "0x1000"
    assert retrieved2 is not None
    assert retrieved2.rva == "0x2000"
    
    all_syms = db.get_symbols_by_name("Player.Update", "v1")
    assert len(all_syms) == 2
    db.close()


def test_baseline_context_builder_token_budget_priority_dropping():
    """Verify that with very tight token budget, ContextRanker drops Priority 2 (TARGET_SOURCE) while keeping Priority 1."""
    task = TaskDefinition(task_id="T_CTX_01", title="Test Context", allowed_files=["Player.cs"])

    evidence = {"symbol": "Player.Update", "confidence": 0.9}
    snippets = {"Player.cs": "public class Player {\n" + "    int a;\n" * 100 + "}"}

    budget = 50
    items = [
        ContextItem(priority=PriorityLevel.TARGET_SYMBOL_EVIDENCE, category="EVIDENCE", content=str(evidence)),
        ContextItem(priority=PriorityLevel.DIRECT_CALLERS_CALLEES, category="TARGET_SOURCE", content=snippets["Player.cs"]),
    ]
    selected = ContextRanker.rank_and_trim(items, token_budget=budget)

    # Only evidence fits; source file snippet is dropped
    assert len(selected) == 1
    assert selected[0].priority == PriorityLevel.TARGET_SYMBOL_EVIDENCE


def test_baseline_ast_guard_assert_counting():
    """Verify how ASTGuard counts assertion invocations."""
    ast_guard = ASTGuard()
    old_code = """
    public class Player {
        public void Test() {
            Debug.Assert(x > 0);
            CustomAssertHelper.AssertSomething();
        }
    }
    """
    new_code = """
    public class Player {
        public void Test() {
            Debug.Assert(x > 0);
        }
    }
    """
    # Count dropped from 2 to 1 because both had 'assert' in invocation text
    with pytest.raises(Exception) as exc:
        ast_guard.validate_csharp_transition(old_code, new_code, "Player.cs")
    assert "Assertion weakening/removal detected" in str(exc.value)


def test_baseline_duplicate_filepatch_behavior(temp_git_repo: Path):
    """Verify that PatchProposal having multiple FilePatch objects for the same file is rejected under Phase 3."""
    # Proposal with 2 separate FilePatch objects for Player.cs
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="        int x = 1;", new_text="        int x = 10;")],
            ),
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="        int y = 1;", new_text="        int y = 20;")],
            ),
        ]
    )
    with pytest.raises(PatchValidationError) as exc:
        PatchValidator.validate_proposal(proposal, temp_git_repo)
    assert "Duplicate FilePatch entries" in str(exc.value)



def test_baseline_network_error_classified_as_network_error(temp_git_repo: Path):
    """Verify that when OpenAICompatibleProvider encounters a network error, it is classified as NETWORK_ERROR by runtime."""
    from model.providers import OpenAICompatibleProvider

    # Point provider to unreachable port with 0 retries for fast test execution
    provider = OpenAICompatibleProvider(api_key="fake-key", base_url="http://127.0.0.1:59999/v1", timeout_seconds=1, max_retries=0)
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)
    context_builder = ContextBuilder()

    task = TaskDefinition(
        task_id="T_NET_01",
        title="Network failure classification",
        allowed_files=["Player.cs"],
    )

    res = runtime.execute_with_model(task, provider=provider, context_builder=context_builder)
    assert res.success is False
    assert res.failure_type == FailureType.NETWORK_ERROR.value



def test_baseline_evidence_none_defaults_to_fast_model(temp_git_repo: Path):
    """Verify that when evidence is None in execute_with_model, confidence defaults to 1.0 (FAST model)."""
    task = TaskDefinition(
        task_id="T_CONF_01",
        title="Default confidence test",
        allowed_files=["Player.cs"],
        risk=RiskLevel.MEDIUM,
    )
    # When evidence is None, evidence_confidence is 1.0 -> Route returns FAST
    model_type = ModelRouter.route(task, evidence_confidence=1.0)
    assert model_type == ModelType.FAST


def test_baseline_dag_earlier_commits_remain_on_failure(temp_git_repo: Path):
    """Verify that when a DAG task fails, earlier committed tasks in the DAG are NOT rolled back."""
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)

    dag = TaskDAG()
    t1 = TaskDefinition(task_id="T1", title="Task 1 Success", allowed_files=["Player.cs"])
    t2 = TaskDefinition(task_id="T2", title="Task 2 Fail", allowed_files=["Player.cs"], dependencies=["T1"])
    dag.add_task(t1)
    dag.add_task(t2)

    patches = {
        "T1": PatchProposal(
            patches=[FilePatch(file="Player.cs", hunks=[PatchHunk(old_text="        int x = 1;", new_text="        int x = 555;")])]
        ),
        "T2": PatchProposal(
            patches=[FilePatch(file="Player.cs", hunks=[PatchHunk(old_text="non_existent_text()", new_text="")])]
        ),
    }

    scheduler = DAGScheduler(dag, runtime)
    summary = scheduler.run_sequential(patches)

    assert summary.success is False
    assert summary.completed_tasks == ["T1"]
    assert summary.failed_tasks == ["T2"]

    # T1's commit is still in git log and working tree!
    content = (temp_git_repo / "Player.cs").read_text(encoding="utf-8")
    assert "int x = 555;" in content


def test_baseline_stale_memory_ignores_code_changes():
    """Verify that StaleDetector only checks version string equality, not actual symbol content."""
    from memory.episodic import EpisodeRecord, EpisodeStatus, EpisodeValidation
    from memory.retrieval import StaleDetector

    proposal = PatchProposal(patches=[FilePatch(file="Player.cs", hunks=[])])
    episode = EpisodeRecord(
        task_id="T_MEM_01",
        symbol="Player.Update",
        solution_patch=proposal,
        version="v1.0",
        environment="linux",
    )

    # If version matches, even if target file code is completely different, it's VALIDATED
    status = StaleDetector.check_staleness(episode, current_version="v1.0", current_env="linux")
    assert status == EpisodeStatus.VALIDATED
