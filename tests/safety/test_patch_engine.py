import subprocess
from pathlib import Path
import pytest

from task.schema import FilePatch, PatchHunk, PatchProposal, TaskDefinition
from safety.patch_engine import (
    PatchValidationError,
    apply_hunks,
    apply_file_patch,
    normalize_repo_path,
    validate_and_simulate_proposal,
)
from safety.patch_validator import PatchValidator
from safety.scope_guard import ScopeGuard
from core.runtime import DSHRuntime


@pytest.fixture
def temp_git_repo(tmp_path: Path):
    repo = tmp_path / "patch_engine_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Engine"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "engine@test.local"], cwd=repo, check=True)

    test_file = repo / "Player.cs"
    test_file.write_text(
        "public class Player {\n"
        "    public void Update() {\n"
        "        int x = 1;\n"
        "        int y = 2;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)
    return repo


# 1. old_text occurs exactly once -> success
def test_old_text_exact_single_match():
    base = "line1\nline2\nline3\n"
    hunks = [PatchHunk(old_text="line2", new_text="line_modified")]
    result = apply_hunks(base, hunks, is_new_file=False, file_path="test.txt")
    assert result == "line1\nline_modified\nline3\n"


# 2. old_text occurs zero times -> reject
def test_old_text_zero_occurrences_rejects():
    base = "line1\nline2\nline3\n"
    hunks = [PatchHunk(old_text="missing_line", new_text="replacement")]
    with pytest.raises(PatchValidationError) as exc:
        apply_hunks(base, hunks, is_new_file=False, file_path="test.txt")
    assert "old_text not found in target file" in str(exc.value)


# 3. old_text occurs multiple times -> reject
def test_old_text_multiple_occurrences_rejects():
    base = "duplicate\nmiddle\nduplicate\n"
    hunks = [PatchHunk(old_text="duplicate", new_text="replaced")]
    with pytest.raises(PatchValidationError) as exc:
        apply_hunks(base, hunks, is_new_file=False, file_path="test.txt")
    assert "Ambiguous hunk match: old_text appears 2 times" in str(exc.value)


# 4. short ambiguous old_text (<= 20 chars) -> reject
def test_short_ambiguous_old_text_rejects():
    base = "a = 1;\nb = 1;\n"
    # "1;" is 2 characters (< 20 chars), appears twice -> must be rejected
    hunks = [PatchHunk(old_text="1;", new_text="99;")]
    with pytest.raises(PatchValidationError) as exc:
        apply_hunks(base, hunks, is_new_file=False, file_path="test.txt")
    assert "Ambiguous hunk match" in str(exc.value)


# 5. long ambiguous old_text (> 20 chars) -> reject
def test_long_ambiguous_old_text_rejects():
    long_line = "// This is a very long comment line with more than twenty characters\n"
    base = f"{long_line}some_code();\n{long_line}"
    hunks = [PatchHunk(old_text=long_line, new_text="// Replaced\n")]
    with pytest.raises(PatchValidationError) as exc:
        apply_hunks(base, hunks, is_new_file=False, file_path="test.txt")
    assert "Ambiguous hunk match" in str(exc.value)


# 6. empty old_text on existing file -> reject
def test_empty_old_text_on_existing_file_rejects():
    base = "existing content\n"
    hunks = [PatchHunk(old_text="", new_text="appended content\n")]
    with pytest.raises(PatchValidationError) as exc:
        apply_hunks(base, hunks, is_new_file=False, file_path="test.txt")
    assert "Empty old_text is not allowed on existing file" in str(exc.value)


# 7. empty old_text on new file -> allowed under explicit creation semantics
def test_empty_old_text_on_new_file_creates_file():
    hunks = [PatchHunk(old_text="", new_text="created initial content\n")]
    result = apply_hunks(None, hunks, is_new_file=True, file_path="new_file.txt")
    assert result == "created initial content\n"


def test_non_empty_old_text_on_new_file_rejects():
    hunks = [PatchHunk(old_text="some old text", new_text="new text")]
    with pytest.raises(PatchValidationError) as exc:
        apply_hunks(None, hunks, is_new_file=True, file_path="new_file.txt")
    assert "Cannot match old_text on non-existent file" in str(exc.value)


def test_empty_creation_new_file_rejects_as_noop():
    from pydantic import ValidationError
    with pytest.raises((PatchValidationError, ValidationError)):
        apply_hunks(None, [PatchHunk(old_text="", new_text="")], is_new_file=True, file_path="new_file.txt")


# 8. old_text == new_text -> reject
def test_noop_hunk_identical_text_rejects():
    from pydantic import ValidationError
    base = "line1\nline2\n"
    with pytest.raises((PatchValidationError, ValidationError)):
        apply_hunks(base, [PatchHunk(old_text="line1", new_text="line1")], is_new_file=False, file_path="test.txt")


def test_noop_net_file_unchanged_rejects():
    base = "initial content"
    hunks = [
        PatchHunk(old_text="initial", new_text="temporary"),
        PatchHunk(old_text="temporary", new_text="initial"),
    ]
    with pytest.raises(PatchValidationError) as exc:
        apply_hunks(base, hunks, is_new_file=False, file_path="test.txt")
    assert "produces no changes (no-op patch)" in str(exc.value)


# 9. duplicate FilePatch same normalized path -> reject
def test_duplicate_filepatch_normalized_path_rejects(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Player.cs").write_text("content\n", encoding="utf-8")

    proposal = PatchProposal(
        patches=[
            FilePatch(file="Player.cs", hunks=[PatchHunk(old_text="content", new_text="content1")]),
            FilePatch(file="./Player.cs", hunks=[PatchHunk(old_text="content1", new_text="content2")]),
        ]
    )
    with pytest.raises(PatchValidationError) as exc:
        validate_and_simulate_proposal(proposal, repo)
    assert "Duplicate FilePatch entries for normalized path 'Player.cs'" in str(exc.value)


# 10. sequential hunks -> deterministic final content
def test_sequential_hunks_deterministic():
    base = "alpha -> beta -> gamma\n"
    hunks = [
        PatchHunk(old_text="alpha", new_text="1"),
        PatchHunk(old_text="beta", new_text="2"),
        PatchHunk(old_text="gamma", new_text="3"),
    ]
    result = apply_hunks(base, hunks, is_new_file=False, file_path="test.txt")
    assert result == "1 -> 2 -> 3\n"


# 11. second hunk becomes invalid after first hunk -> reject
def test_second_hunk_invalid_after_first_rejects():
    base = "start state"
    hunks = [
        PatchHunk(old_text="start state", new_text="middle state"),
        PatchHunk(old_text="start state", new_text="final state"),  # "start state" no longer exists
    ]
    with pytest.raises(PatchValidationError) as exc:
        apply_hunks(base, hunks, is_new_file=False, file_path="test.txt")
    assert "Hunk 2 old_text not found in target file" in str(exc.value)


# 12. simulation and actual application produce identical content
def test_simulation_and_runtime_parity(temp_git_repo: Path):
    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[
                    PatchHunk(
                        old_text="        int x = 1;",
                        new_text="        int x = 100;",
                    ),
                    PatchHunk(
                        old_text="        int y = 2;",
                        new_text="        int y = 200;",
                    ),
                ],
            )
        ]
    )

    # 1. Simulated state via authoritative engine
    simulated_map = validate_and_simulate_proposal(proposal, temp_git_repo)
    expected_content = simulated_map["Player.cs"]

    # 2. ScopeGuard simulation check
    task = TaskDefinition(task_id="T_PARITY_01", title="Parity Test", allowed_files=["Player.cs"])
    scope_guard = ScopeGuard()
    scope_guard.validate(task, proposal, temp_git_repo)

    # 3. Runtime execution
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)
    res = runtime.execute_transaction(task, proposal)
    assert res.success is True

    # 4. Actual disk content
    actual_content = (temp_git_repo / "Player.cs").read_text(encoding="utf-8")
    assert actual_content == expected_content
    assert "int x = 100;" in actual_content
    assert "int y = 200;" in actual_content


# 13. multiple files are applied independently
def test_multiple_files_applied_independently(temp_git_repo: Path):
    (temp_git_repo / "Enemy.cs").write_text("class Enemy {}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=temp_git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "Add Enemy.cs"], cwd=temp_git_repo, check=True)

    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="        int x = 1;", new_text="        int x = 50;")],
            ),
            FilePatch(
                file="Enemy.cs",
                hunks=[PatchHunk(old_text="class Enemy {}", new_text="class Enemy { int hp = 10; }")],
            ),
        ]
    )

    task = TaskDefinition(task_id="T_MULTI_01", title="Multi File", allowed_files=["Player.cs", "Enemy.cs"])
    runtime = DSHRuntime(temp_git_repo, dry_run=False, test_mode=True)
    res = runtime.execute_transaction(task, proposal)
    assert res.success is True

    assert "int x = 50;" in (temp_git_repo / "Player.cs").read_text(encoding="utf-8")
    assert "int hp = 10;" in (temp_git_repo / "Enemy.cs").read_text(encoding="utf-8")


# 14. Path normalization and traversal rejection
def test_path_normalization_and_traversal_rejection(temp_git_repo: Path):
    assert normalize_repo_path("src\\sub\\file.cs") == "src/sub/file.cs"
    assert normalize_repo_path("./src/./sub/../sub/file.cs") == "src/sub/file.cs"

    with pytest.raises(PatchValidationError) as exc_abs:
        normalize_repo_path("/src/sub/file.cs")
    assert "Absolute paths are forbidden" in str(exc_abs.value)

    with pytest.raises(PatchValidationError) as exc:
        normalize_repo_path("../../etc/passwd")
    assert "Path traversal detected" in str(exc.value)

    proposal = PatchProposal(
        patches=[
            FilePatch(
                file="../../etc/shadow",
                hunks=[PatchHunk(old_text="root", new_text="hacked")],
            )
        ]
    )
    with pytest.raises(PatchValidationError) as exc:
        validate_and_simulate_proposal(proposal, temp_git_repo)
    assert "Path traversal detected" in str(exc.value)
