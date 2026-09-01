import subprocess
from pathlib import Path
from typing import List, Optional
import pytest

from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk
from model.schemas import ModelRequest, ModelResponse, ModelType
from model.providers import BaseModelProvider
from context.builder import ContextBuilder
from core.runtime import DSHRuntime


class SequenceMockProvider(BaseModelProvider):
    """Mock provider that returns a sequence of proposals across multiple calls."""
    def __init__(self, responses: List[Optional[PatchProposal]]):
        self.responses = responses
        self.call_count = 0
        self.models_called: List[ModelType] = []

    def generate(self, req: ModelRequest) -> ModelResponse:
        self.models_called.append(req.model_type)
        if self.call_count < len(self.responses):
            prop = self.responses[self.call_count]
            self.call_count += 1
            if prop is None:
                return ModelResponse(raw_content="malformed json", patch_proposal=None, tokens_used=50)
            return ModelResponse(raw_content=prop.model_dump_json(), patch_proposal=prop, tokens_used=100)
        return ModelResponse(raw_content="{}", patch_proposal=None, tokens_used=10)


@pytest.fixture
def temp_git_repo(tmp_path: Path):
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "agent@test.local"], cwd=repo, check=True)

    test_file = repo / "Player.cs"
    test_file.write_text("public class Player {\n    public void Update() {}\n}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)
    return repo


def test_recovery_success_attempt_1(temp_git_repo: Path):
    runtime = DSHRuntime(temp_git_repo, dry_run=False)
    context_builder = ContextBuilder()

    task = TaskDefinition(
        task_id="T_REC_01",
        title="Recover on attempt 1",
        allowed_files=["Player.cs"],
    )

    valid_patch = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() {\n        // Fixed Attempt 1\n    }")]
            )
        ],
        reason="Fixed on retry",
    )

    # Attempt 0 returns malformed (None), Attempt 1 returns valid_patch
    provider = SequenceMockProvider(responses=[None, valid_patch])

    res = runtime.execute_with_recovery(task, provider=provider, context_builder=context_builder)
    assert res.success is True
    assert provider.call_count == 2
    assert "// Fixed Attempt 1" in (temp_git_repo / "Player.cs").read_text()


def test_recovery_success_attempt_2(temp_git_repo: Path):
    runtime = DSHRuntime(temp_git_repo, dry_run=False)
    context_builder = ContextBuilder()

    task = TaskDefinition(
        task_id="T_REC_02",
        title="Recover on attempt 2 (Reasoning)",
        allowed_files=["Player.cs"],
    )

    # Attempt 0: invalid hunk
    bad_patch_0 = PatchProposal(
        patches=[FilePatch(file="Player.cs", hunks=[PatchHunk(old_text="non_existent()", new_text="")])]
    )
    # Attempt 1: unallowed file
    bad_patch_1 = PatchProposal(
        patches=[FilePatch(file="Secret.cs", hunks=[PatchHunk(old_text="", new_text="// bad")])]
    )
    # Attempt 2: valid patch
    good_patch_2 = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="    public void Update() {}", new_text="    public void Update() {\n        // Fixed Attempt 2\n    }")]
            )
        ]
    )

    provider = SequenceMockProvider(responses=[bad_patch_0, bad_patch_1, good_patch_2])

    res = runtime.execute_with_recovery(task, provider=provider, context_builder=context_builder)
    assert res.success is True
    assert provider.call_count == 3
    # Attempt 2 should have been routed to REASONING
    assert provider.models_called[-1] == ModelType.REASONING
    assert "// Fixed Attempt 2" in (temp_git_repo / "Player.cs").read_text()


def test_recovery_hard_halt(temp_git_repo: Path):
    runtime = DSHRuntime(temp_git_repo, dry_run=False)
    context_builder = ContextBuilder()

    task = TaskDefinition(
        task_id="T_REC_03",
        title="Total failure hard halt",
        allowed_files=["Player.cs"],
    )

    # All attempts fail
    provider = SequenceMockProvider(responses=[None, None, None])

    res = runtime.execute_with_recovery(task, provider=provider, context_builder=context_builder)
    assert res.success is False
    assert res.failure_type == "HARD_HALT"
    assert "Hard halt" in res.error_message
    assert provider.call_count == 3

    # Workspace is clean
    assert runtime.ws.is_clean() is True
