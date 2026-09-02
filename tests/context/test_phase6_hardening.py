import pytest
from pathlib import Path
from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk, RiskLevel
from context.budget import ContextComplexity, TokenBudgetManager, ContextBudgetExceededError
from context.ranking import ContextItem, ContextRanker, PriorityLevel
from context.builder import ContextBuilder, ContextManifest
from recon.database import EvidenceDatabase, SymbolRecord, CrossVersionMatchRecord, MatchState
from recon.diff import CrossVersionMatcher
from recon.evidence import EvidenceService
from model.schemas import ModelType, ModelRequest
from model.router import ModelRouter
from memory.episodic import EpisodicMemoryStore, EpisodeRecord, EpisodeStatus, EpisodeValidation
from memory.retrieval import EpisodeRetriever, StaleDetector
from recovery.history import RecoveryHistory
from recovery.classifier import FailureType


@pytest.fixture
def recon_db():
    db = EvidenceDatabase()
    yield db
    db.close()


# 1. Large target file still provides target source
def test_large_target_file_still_provides_target_source():
    large_code = "public class LargeManager {\n" + "\n".join(f"    public void Method{i}() {{ int x = {i}; }}" for i in range(150)) + "\n}"
    task = TaskDefinition(
        task_id="T_LARGE_01",
        title="Handle large target file",
        allowed_files=["LargeManager.cs"],
    )

    builder = ContextBuilder()
    context_str, manifest = builder.build_context_with_manifest(
        task=task,
        file_snippets={"LargeManager.cs": large_code},
        complexity=ContextComplexity.NORMAL,
    )

    assert "SOURCE FILE: LargeManager.cs" in context_str
    assert "public class LargeManager" in context_str
    assert any("TARGET_SOURCE:LargeManager.cs" in s for s in manifest.mandatory_sections)
    assert not any(om["category"] == "TARGET_SOURCE:LargeManager.cs" for om in manifest.omitted_sections)


# 2. History cannot crowd out target source
def test_history_cannot_crowd_out_target_source():
    target_code = "public class Target {\n    public void ImportantMethod() { /* vital logic */ }\n}\n"
    massive_history = "Attempt failure:\n" + ("Long diagnostic traceback error line details\n" * 200)

    task = TaskDefinition(
        task_id="T_HIST_01",
        title="Protect target source from history",
        allowed_files=["Target.cs"],
    )

    builder = ContextBuilder()
    context_str, manifest = builder.build_context_with_manifest(
        task=task,
        file_snippets={"Target.cs": target_code},
        previous_failure=massive_history,
        complexity=ContextComplexity.SIMPLE,
    )

    # Mandatory target source MUST be present
    assert "SOURCE FILE: Target.cs" in context_str
    assert "ImportantMethod" in context_str
    # Mandatory sections must contain Target.cs
    assert any("TARGET_SOURCE:Target.cs" in s for s in manifest.mandatory_sections)


# 3. Memory cannot crowd out task and source
def test_memory_cannot_crowd_out_task_and_source():
    target_code = "public class CoreEntity {\n    public void CoreAction() {}\n}\n"
    task = TaskDefinition(
        task_id="T_MEM_CROWD_01",
        title="Protect task and source from memory",
        allowed_files=["CoreEntity.cs"],
    )

    # Create many advisory episodes
    store = EpisodicMemoryStore()
    patch = PatchProposal(patches=[FilePatch(file="CoreEntity.cs", hunks=[PatchHunk(old_text="a", new_text="b")])])
    for i in range(10):
        ep = EpisodeRecord(
            task_id=f"T_HIST_{i}",
            symbol="CoreEntity.CoreAction",
            solution_patch=patch,
            version="v1",
            confidence=0.95,
            status=EpisodeStatus.VALIDATED,
        )
        store.store_episode(ep)

    retriever = EpisodeRetriever(store)
    episodes = retriever.retrieve_advisory_episodes("CoreEntity.CoreAction", current_version="v1", max_results=10)

    builder = ContextBuilder()
    context_str, manifest = builder.build_context_with_manifest(
        task=task,
        file_snippets={"CoreEntity.cs": target_code},
        advisory_episodes=episodes,
        complexity=ContextComplexity.SIMPLE,
    )

    # Task and source MUST be present
    assert "[T_MEM_CROWD_01]" in context_str
    assert "SOURCE FILE: CoreEntity.cs" in context_str


# 4. Target symbols influence source extraction
def test_target_symbols_influence_source_extraction():
    full_source = """using System;
using System.Collections.Generic;

namespace Game.Core {
    public class PlayerController {
        public void UnrelatedInit() {
            int a = 1;
        }

        public void TargetUpdate(int delta) {
            int speed = 100 * delta;
        }

        public void UnrelatedRender() {
            int b = 2;
        }
    }
}
"""
    task = TaskDefinition(
        task_id="T_SYM_EXTRACT",
        title="Extract TargetUpdate only",
        allowed_files=["PlayerController.cs"],
        target_symbols=["PlayerController.TargetUpdate"],
    )

    builder = ContextBuilder()
    context_str, manifest = builder.build_context_with_manifest(
        task=task,
        file_snippets={"PlayerController.cs": full_source},
        complexity=ContextComplexity.NORMAL,
    )

    assert "TargetUpdate" in context_str
    assert manifest.extraction_method in ("tree_sitter_symbol", "regex_fallback_symbol")


# 5. Mandatory context is always present
def test_mandatory_context_is_always_present():
    task = TaskDefinition(
        task_id="T_MANDATORY_01",
        title="Check mandatory presence",
        allowed_files=["Main.cs"],
        max_lines_added=25,
        max_lines_deleted=10,
    )
    snippet = "public class Main { public static void Entry() {} }"

    builder = ContextBuilder()
    context_str, manifest = builder.build_context_with_manifest(
        task=task,
        file_snippets={"Main.cs": snippet},
        complexity=ContextComplexity.NORMAL,
    )

    assert "# TASK OBJECTIVE: [T_MANDATORY_01]" in context_str
    assert "Allowed Files: Main.cs" in context_str
    assert "Line Budget: +25 / -10" in context_str
    assert "### [SOURCE FILE: Main.cs]" in context_str
    assert len(manifest.mandatory_sections) >= 2


# 6. Budget exhaustion causes explicit behavior
def test_budget_exhaustion_causes_explicit_error(monkeypatch):
    task = TaskDefinition(
        task_id="T_BUDGET_FAIL",
        title="Force budget exhaustion",
        allowed_files=["Big.cs"],
    )
    large_snippet = "public class Big {\n" + ("    int x = 1;\n" * 1000) + "}"

    builder = ContextBuilder()
    # Mock compute_available_budget to an impossibly small number (e.g., 20 tokens)
    monkeypatch.setattr(TokenBudgetManager, "compute_available_budget", lambda complexity: 20)

    with pytest.raises(ContextBudgetExceededError) as exc_info:
        builder.build_context_with_manifest(
            task=task,
            file_snippets={"Big.cs": large_snippet},
            complexity=ContextComplexity.SIMPLE,
        )
    assert "Context budget exceeded" in str(exc_info.value)


# 7. Missing evidence confidence is not 1.0
def test_missing_evidence_confidence_is_not_one(recon_db):
    evidence_svc = EvidenceService(recon_db)
    ev = evidence_svc.get_symbol_evidence("NonExistent.Method", "v1", "v2")

    # Missing evidence must have confidence 0.0, is_fact False, match_state UNMAPPED
    assert ev["confidence"] == 0.0
    assert ev["is_fact"] is False
    assert ev["match_state"] == MatchState.UNMAPPED.value

    # ModelRouter with missing confidence (None) routes to REASONING
    task = TaskDefinition(task_id="T_ROUTE_01", title="Test router", allowed_files=["A.cs"], risk=RiskLevel.LOW)
    model_type = ModelRouter.route(task, evidence_confidence=None)
    assert model_type == ModelType.REASONING


# 8. Model cannot game confidence into FAST path
def test_model_cannot_game_confidence_into_fast_path():
    task = TaskDefinition(
        task_id="T_GAME_01",
        title="Attempt confidence gaming",
        allowed_files=["Player.cs"],
        risk=RiskLevel.LOW,
    )

    # Even if model's proposal had confidence=1.0, router uses verified evidence confidence (or None -> REASONING)
    model_type_unverified = ModelRouter.route(task, evidence_confidence=None)
    assert model_type_unverified == ModelType.REASONING

    model_type_low_conf = ModelRouter.route(task, evidence_confidence=0.80)
    assert model_type_low_conf == ModelType.REASONING

    # Only verified high confidence allows FAST path
    model_type_verified = ModelRouter.route(task, evidence_confidence=0.95)
    assert model_type_verified == ModelType.FAST


# 9. Overloaded symbols are distinguished
def test_overloaded_symbols_are_distinguished(recon_db):
    old_sym = SymbolRecord(
        symbol_name="Player.Attack",
        class_name="Player",
        signature="void Attack(int power, float radius)",
        rva="0x1000",
        version="v1",
        parameters=["int power", "float radius"],
    )
    recon_db.insert_symbol(old_sym)

    # Candidate 1: Same method name, different parameter types: Attack(string spell)
    cand_overload_1 = SymbolRecord(
        symbol_name="Player.Attack",
        class_name="Player",
        signature="void Attack(string spell)",
        rva="0x2000",
        version="v2",
        parameters=["string spell"],
    )
    # Candidate 2: Same method name, exact matching parameter types: Attack(int power, float radius)
    cand_overload_2 = SymbolRecord(
        symbol_name="Player.Attack",
        class_name="Player",
        signature="void Attack(int power, float radius)",
        rva="0x2100",
        version="v2",
        parameters=["int power", "float radius"],
    )

    recon_db.insert_symbol(cand_overload_1)
    recon_db.insert_symbol(cand_overload_2)

    matcher = CrossVersionMatcher(recon_db)
    match_res = matcher.match_symbol(
        old_symbol=old_sym,
        candidate_symbols=[cand_overload_1, cand_overload_2],
        old_version="v1",
        new_version="v2",
    )

    # Cand 2 has matching parameter types and signature -> higher confidence
    assert match_res.new_symbol == "Player.Attack"
    assert match_res.signature_similarity == 1.0
    assert match_res.confidence >= 0.85
    assert match_res.match_state == MatchState.MATCHED


# 10. Ambiguous cross-version match is rejected
def test_ambiguous_cross_version_match_rejected(recon_db):
    old_sym = SymbolRecord(
        symbol_name="Manager.Process",
        class_name="Manager",
        signature="void Process()",
        rva="0x1000",
        version="v1",
        parameters=[],
    )
    recon_db.insert_symbol(old_sym)

    # Two nearly identical candidates with close scores
    cand1 = SymbolRecord(
        symbol_name="Manager.ProcessA",
        class_name="Manager",
        signature="void ProcessA()",
        rva="0x2000",
        version="v2",
        parameters=[],
    )
    cand2 = SymbolRecord(
        symbol_name="Manager.ProcessB",
        class_name="Manager",
        signature="void ProcessB()",
        rva="0x2010",
        version="v2",
        parameters=[],
    )
    recon_db.insert_symbol(cand1)
    recon_db.insert_symbol(cand2)

    matcher = CrossVersionMatcher(recon_db, min_confidence=0.80, margin_threshold=0.10)
    match_res = matcher.match_symbol(
        old_symbol=old_sym,
        candidate_symbols=[cand1, cand2],
        old_version="v1",
        new_version="v2",
    )

    # Difference between ProcessA and ProcessB is 0.0 -> margin < 0.10 -> AMBIGUOUS
    assert match_res.match_state == MatchState.AMBIGUOUS
    assert match_res.verified is False
    assert match_res.margin < 0.10


# 11. Top1/Top2 margin is enforced
def test_top1_top2_margin_enforced(recon_db):
    old_sym = SymbolRecord(
        symbol_name="Player.Move",
        class_name="Player",
        signature="void Move(int x, int y)",
        rva="0x1000",
        version="v1",
        parameters=["int x", "int y"],
    )
    recon_db.insert_symbol(old_sym)

    # Strong candidate (high score)
    cand_strong = SymbolRecord(
        symbol_name="Player.Move",
        class_name="Player",
        signature="void Move(int x, int y)",
        rva="0x2000",
        version="v2",
        parameters=["int x", "int y"],
    )
    # Weak candidate
    cand_weak = SymbolRecord(
        symbol_name="Enemy.Walk",
        class_name="Enemy",
        signature="void Walk()",
        rva="0x3000",
        version="v2",
        parameters=[],
    )

    recon_db.insert_symbol(cand_strong)
    recon_db.insert_symbol(cand_weak)

    matcher = CrossVersionMatcher(recon_db, min_confidence=0.85, margin_threshold=0.10)
    match_res = matcher.match_symbol(
        old_symbol=old_sym,
        candidate_symbols=[cand_strong, cand_weak],
        old_version="v1",
        new_version="v2",
    )

    assert match_res.match_state == MatchState.MATCHED
    assert match_res.verified is True
    assert match_res.margin >= 0.10


# 12. Empty caller sets do not create false perfect similarity
def test_empty_caller_sets_return_zero_similarity():
    # Calling compute_jaccard on empty sets must return 0.0, not 1.0
    sim = CrossVersionMatcher.compute_jaccard(set(), set())
    assert sim == 0.0

    # One empty set also returns 0.0
    assert CrossVersionMatcher.compute_jaccard({"A"}, set()) == 0.0
    assert CrossVersionMatcher.compute_jaccard(set(), {"B"}) == 0.0

    # Non-empty overlapping sets return actual Jaccard index
    assert CrossVersionMatcher.compute_jaccard({"A", "B"}, {"B", "C"}) == 1.0 / 3.0


# 13. Stale memory is excluded by default
def test_stale_memory_excluded_by_default():
    store = EpisodicMemoryStore()
    proposal = PatchProposal(patches=[FilePatch(file="Player.cs", hunks=[PatchHunk(old_text="a", new_text="b")])])
    ep = EpisodeRecord(
        task_id="T_OLD_01",
        symbol="Player.Update",
        solution_patch=proposal,
        version="v1.0",
        environment="linux",
        confidence=0.95,
        status=EpisodeStatus.VALIDATED,
    )
    store.store_episode(ep)

    retriever = EpisodeRetriever(store)
    # Target version is v2.0 (diverged from v1.0)
    retrieved_default = retriever.retrieve_advisory_episodes("Player.Update", current_version="v2.0")
    # Stale episode is excluded by default
    assert len(retrieved_default) == 0

    # Explicit request can retrieve stale episodes for inspection
    retrieved_stale = retriever.retrieve_advisory_episodes("Player.Update", current_version="v2.0", include_stale=True)
    assert len(retrieved_stale) == 1
    assert retrieved_stale[0].status == EpisodeStatus.STALE


# 14. Recovery history does not inject unnecessarily large full patches
def test_recovery_history_uses_concise_summaries():
    history = RecoveryHistory(task_id="T_REC_SUMMARY")
    large_patch = PatchProposal(
        patches=[
            FilePatch(
                file="BigPlayer.cs",
                hunks=[PatchHunk(old_text="old_code_line_" + str(i), new_text="new_code_line_" + str(i)) for i in range(50)],
            )
        ],
        reason="Huge patch attempt",
    )

    history.record_attempt(
        attempt_index=0,
        model_type_used="fast",
        failure_type=FailureType.SYNTAX,
        error_message="CS1002: ; expected at line 42 with lots of compiler diagnostic output",
        patch_attempted=large_patch,
    )

    formatted = history.format_history_for_prompt()

    # Must contain concise summaries
    assert "Attempt 0 (Model: fast)" in formatted
    assert "What Failed: [SYNTAX]" in formatted
    assert "Attempted Action: Modified 1 file(s) (BigPlayer.cs) across 50 hunk(s)" in formatted
    assert "Key Constraint & Lesson:" in formatted
    # Must NOT contain the raw JSON dump of the 50 hunks
    assert "old_code_line_49" not in formatted
    assert '"patches":' not in formatted
