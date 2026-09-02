import os
import subprocess
from pathlib import Path
import pytest
from pydantic import ValidationError

from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk, RiskLevel
from safety.patch_engine import normalize_repo_path, PatchValidationError, apply_hunks, validate_and_simulate_proposal
from safety.scope_guard import ScopeGuard, ScopeViolationError
from safety.ast_guard import ASTGuard, ASTViolationError
from safety.policy import SafetyPolicy
from validation.risk import RiskValidator
from recon.database import EvidenceDatabase, SymbolRecord
from recon.indexer import SymbolIndexer
from core.runtime import DSHRuntime


@pytest.fixture
def test_repo(tmp_path: Path):
    repo = tmp_path / "safety_test_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Safety Tester"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tester@safety.local"], cwd=repo, check=True)

    test_file = repo / "Player.cs"
    test_file.write_text(
        "public class Player {\n"
        "    public void Update() {\n"
        "        Debug.Assert(this.Health > 0);\n"
        "        int x = 1;\n"
        "    }\n"
        "    public void Update(int delta) {\n"
        "        int y = delta;\n"
        "    }\n"
        "    public void Attack() {\n"
        "        Fire();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)
    return repo


# 1. .git/config rejected
def test_git_config_path_rejected():
    with pytest.raises(PatchValidationError) as exc:
        normalize_repo_path(".git/config")
    assert "Access to '.git' path component is forbidden" in str(exc.value)


# 2. nested .git/config rejected
def test_nested_git_config_path_rejected():
    with pytest.raises(PatchValidationError) as exc:
        normalize_repo_path("src/sub/.git/config")
    assert "Access to '.git' path component is forbidden" in str(exc.value)

    with pytest.raises(PatchValidationError) as exc2:
        normalize_repo_path("foo/.GIT/HEAD")
    assert "Access to '.git' path component is forbidden" in str(exc2.value)


# 3. traversal rejected
def test_traversal_path_rejected():
    with pytest.raises(PatchValidationError) as exc:
        normalize_repo_path("../../etc/passwd")
    assert "Path traversal detected" in str(exc.value)

    with pytest.raises(PatchValidationError) as exc2:
        normalize_repo_path("src/../../outside.cs")
    assert "Path traversal detected" in str(exc2.value)


# 4. absolute path rejected
def test_absolute_path_rejected():
    with pytest.raises(PatchValidationError) as exc:
        normalize_repo_path("/etc/passwd")
    assert "Absolute paths are forbidden" in str(exc.value)

    with pytest.raises(PatchValidationError) as exc2:
        normalize_repo_path("C:\\Windows\\System32\\file.cs")
    assert "forbidden" in str(exc2.value)


# 5. symlink escape rejected
def test_symlink_escape_rejected(test_repo: Path, tmp_path: Path):
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    outside_file = outside_dir / "secret.cs"
    outside_file.write_text("secret code", encoding="utf-8")

    # Create symlink inside repo pointing outside repo
    symlink_path = test_repo / "symlink_escape.cs"
    try:
        os.symlink(outside_file, symlink_path)
    except OSError:
        pytest.skip("Symlinks not supported in test environment")

    task = TaskDefinition(
        task_id="T_SYM_ESC",
        title="Symlink escape attempt",
        allowed_files=["symlink_escape.cs"],
    )
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="symlink_escape.cs",
                hunks=[PatchHunk(old_text="secret", new_text="exposed")],
            )
        ]
    )

    guard = ScopeGuard()
    with pytest.raises(ScopeViolationError) as exc:
        guard.validate(task, proposal, test_repo)
    assert "points outside repository root" in str(exc.value) or "escapes repository root" in str(exc.value)


# 6. path normalization consistency
def test_path_normalization_consistency():
    p1 = "src\\models\\..\\models\\Player.cs"
    p2 = "src/models/./Player.cs"
    p3 = "./src/models/Player.cs"
    assert normalize_repo_path(p1) == "src/models/Player.cs"
    assert normalize_repo_path(p2) == "src/models/Player.cs"
    assert normalize_repo_path(p3) == "src/models/Player.cs"
    assert normalize_repo_path(p1) == normalize_repo_path(p2) == normalize_repo_path(p3)


# 7. unauthorized file rejected
def test_unauthorized_file_rejected(test_repo: Path):
    task = TaskDefinition(
        task_id="T_UNAUTH",
        title="Unauthorized file edit",
        allowed_files=["Player.cs"],
    )
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Enemy.cs",
                hunks=[PatchHunk(old_text="a", new_text="b")],
            )
        ]
    )
    guard = ScopeGuard()
    with pytest.raises(ScopeViolationError) as exc:
        guard.validate(task, proposal, test_repo)
    assert "is not in allowed_files" in str(exc.value)


# 8. target symbol mismatch rejected
def test_target_symbol_mismatch_rejected(test_repo: Path):
    task = TaskDefinition(
        task_id="T_SYM_MISMATCH",
        title="Mismatch target symbol",
        allowed_files=["Player.cs"],
        target_symbols=["Player.Update"],
    )
    # Patch modifies Attack(), which is not in target_symbols
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="        Fire();", new_text="        FireSuper();")],
            )
        ]
    )
    guard = ScopeGuard()
    with pytest.raises(ScopeViolationError) as exc:
        guard.validate(task, proposal, test_repo)
    assert "Target symbol violation" in str(exc.value)
    assert "Player.Attack" in str(exc.value)


# 9. target symbol correct change accepted
def test_target_symbol_correct_change_accepted(test_repo: Path):
    task = TaskDefinition(
        task_id="T_SYM_MATCH",
        title="Valid target symbol change",
        allowed_files=["Player.cs"],
        target_symbols=["Player.Update"],
    )
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="        int x = 1;", new_text="        int x = 99;")],
            )
        ]
    )
    guard = ScopeGuard()
    # Should pass without raising ScopeViolationError
    guard.validate(task, proposal, test_repo)


# 10. overloaded methods distinguished
def test_overloaded_methods_distinguished(test_repo: Path):
    # Only Update(int delta) is allowed to change, NOT Update()
    task = TaskDefinition(
        task_id="T_OVERLOAD",
        title="Target specific overload",
        allowed_files=["Player.cs"],
        target_symbols=["Player.Update(int)"],
    )

    # Patch modifying Update() (no params) must be rejected
    proposal_bad = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="        int x = 1;", new_text="        int x = 99;")],
            )
        ]
    )
    guard = ScopeGuard()
    with pytest.raises(ScopeViolationError) as exc:
        guard.validate(task, proposal_bad, test_repo)
    assert "Target symbol violation" in str(exc.value)

    # Patch modifying Update(int delta) must be accepted
    proposal_good = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="        int y = delta;", new_text="        int y = delta * 2;")],
            )
        ]
    )
    guard.validate(task, proposal_good, test_repo)


# 11. one-line legitimate return not falsely rejected
def test_one_line_legitimate_return_accepted():
    ast_guard = ASTGuard()
    old_code = """
    public class Validator {
        public bool IsValid() {
            return false;
        }
    }
    """
    new_code = """
    public class Validator {
        public bool IsValid() {
            return true;
        }
    }
    """
    # Changing return false to return true in a simple getter/validator is legitimate
    ast_guard.validate_csharp_transition(old_code, new_code, "Validator.cs")


# 12. generic method signature handled
def test_generic_method_signature_handled():
    indexer = SymbolIndexer(EvidenceDatabase())
    dump_text = """
    // Namespace: Game.Inventory
    public class Storage<T> {
        // RVA: 0x55AA11
        [CustomAttr("test")]
        public static async Task<Dictionary<string, List<T>>> QueryItemsAsync<U>(U filter, in int limit) { }
    }
    """
    indexer.parse_il2cpp_dump_snippet(dump_text, version="v1")
    sym = indexer.db.get_symbol("Storage.QueryItemsAsync", "v1")
    assert sym is not None
    assert sym.rva == "0x55AA11"
    assert "Task<Dictionary<string, List<T>>>" in sym.signature
    assert "<U>" in sym.signature


# 13. Assert(x) -> Assert(true) rejected
def test_assert_semantic_weakening_rejected():
    ast_guard = ASTGuard()
    old_code = """
    public class Player {
        public void Update() {
            Debug.Assert(this.Health > 0);
            Calculate();
        }
    }
    """
    new_code = """
    public class Player {
        public void Update() {
            Debug.Assert(true);
            Calculate();
        }
    }
    """
    with pytest.raises(ASTViolationError) as exc:
        ast_guard.validate_csharp_transition(old_code, new_code, "Player.cs")
    assert "Assertion semantic weakening detected" in str(exc.value)


# 14. actual introduced risky change detected
def test_actual_introduced_risk_detected(test_repo: Path):
    task = TaskDefinition(
        task_id="T_RISK_CHECK",
        title="Check risk intro",
        allowed_files=["Player.cs"],
        risk=RiskLevel.MEDIUM,
    )
    # Introducing [DllImport]
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="        int x = 1;", new_text="        [DllImport(\"kernel32\")] static extern void Hook();\n        int x = 1;")],
            )
        ]
    )
    res = RiskValidator.validate_risk(task, proposal, test_repo)
    assert res.success is False
    assert len(res.violations) > 0
    assert "DllImport" in res.violations[0]


# 15. HIGH risk does not bypass checks
def test_high_risk_does_not_bypass_checks(test_repo: Path):
    task_high = TaskDefinition(
        task_id="T_HIGH_RISK",
        title="High risk task",
        allowed_files=["Player.cs"],
        risk=RiskLevel.HIGH,
    )
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="        int x = 1;", new_text="        unsafe { int* p = null; }\n        int x = 1;")],
            )
        ]
    )
    # Under Phase 5, HIGH risk does NOT bypass
    res = RiskValidator.validate_risk(task_high, proposal, test_repo)
    assert res.success is False
    assert len(res.violations) > 0


# 16. invalid confidence rejected
def test_invalid_confidence_rejected():
    with pytest.raises(ValidationError):
        PatchProposal(
            patches=[FilePatch(file="Player.cs", hunks=[PatchHunk(old_text="a", new_text="b")])],
            confidence=1.5,  # > 1.0 invalid
        )
    with pytest.raises(ValidationError):
        PatchProposal(
            patches=[FilePatch(file="Player.cs", hunks=[PatchHunk(old_text="a", new_text="b")])],
            confidence=-0.1,  # < 0.0 invalid
        )


# 17. negative budget rejected
def test_negative_budget_rejected():
    with pytest.raises(ValidationError):
        TaskDefinition(
            task_id="T_NEG",
            title="Negative budget",
            allowed_files=["Player.cs"],
            max_lines_added=-5,
        )
    with pytest.raises(ValidationError):
        TaskDefinition(
            task_id="T_NEG2",
            title="Negative budget",
            allowed_files=["Player.cs"],
            max_lines_deleted=-1,
        )


# 18. empty/no-op hunk rejected
def test_empty_and_noop_hunk_rejected():
    with pytest.raises(ValidationError):
        PatchHunk(old_text="", new_text="")

    with pytest.raises(ValidationError):
        PatchHunk(old_text="int x = 1;", new_text="int x = 1;")
