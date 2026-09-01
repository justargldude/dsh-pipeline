import subprocess
from pathlib import Path
import pytest

from task.schema import TaskDefinition, RiskLevel, PatchProposal, FilePatch, PatchHunk
from recovery.classifier import FailureType
from model.schemas import ModelType, ModelRequest
from model.router import ModelRouter
from model.providers import MockModelProvider, BaseModelProvider
from context.builder import ContextBuilder
from core.runtime import DSHRuntime


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


def test_router_decisions():
    low_risk_task = TaskDefinition(task_id="T1", title="Simple fix", allowed_files=["A.cs"], risk=RiskLevel.LOW)
    high_risk_task = TaskDefinition(task_id="T2", title="Unsafe hook", allowed_files=["A.cs"], risk=RiskLevel.HIGH)

    # 1. Normal low-risk task -> FAST
    assert ModelRouter.route(low_risk_task) == ModelType.FAST

    # 2. High-risk task -> REASONING
    assert ModelRouter.route(high_risk_task) == ModelType.REASONING

    # 3. Syntax error failure -> FAST
    assert ModelRouter.route(low_risk_task, failure_type=FailureType.SYNTAX) == ModelType.FAST

    # 4. Behavioral regression failure -> REASONING
    assert ModelRouter.route(low_risk_task, failure_type=FailureType.BEHAVIORAL) == ModelType.REASONING

    # 5. Low evidence confidence (< 0.85) -> REASONING
    assert ModelRouter.route(low_risk_task, evidence_confidence=0.72) == ModelType.REASONING


def test_extract_patch_proposal_from_markdown():
    raw_markdown = """Here is the fix:
```json
{
  "patches": [
    {
      "file": "Player.cs",
      "hunks": [
        {
          "old_text": "Update() {}",
          "new_text": "Update() { /* hooked */ }"
        }
      ]
    }
  ],
  "reason": "Add hook",
  "confidence": 0.95
}
```
Hope this helps!"""

    proposal = BaseModelProvider.extract_patch_proposal(raw_markdown)
    assert proposal is not None
    assert len(proposal.patches) == 1
    assert proposal.patches[0].file == "Player.cs"
    assert proposal.confidence == 0.95


def test_execute_with_model_success(temp_git_repo: Path):
    runtime = DSHRuntime(temp_git_repo, dry_run=False)
    context_builder = ContextBuilder()

    task = TaskDefinition(
        task_id="T_MODEL_01",
        title="Hook Player via Model",
        allowed_files=["Player.cs"],
    )

    valid_proposal = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[
                    PatchHunk(
                        old_text="    public void Update() {}",
                        new_text="    public void Update() {\n        // AI Hooked\n    }",
                    )
                ],
            )
        ],
        reason="Model generated hook",
        confidence=0.98,
    )

    provider = MockModelProvider(predefined_proposal=valid_proposal)
    res = runtime.execute_with_model(task, provider=provider, context_builder=context_builder)

    assert res.success is True
    assert res.commit_hash is not None

    content = (temp_git_repo / "Player.cs").read_text(encoding="utf-8")
    assert "// AI Hooked" in content


def test_execute_with_model_malformed_output(temp_git_repo: Path):
    runtime = DSHRuntime(temp_git_repo, dry_run=False)
    context_builder = ContextBuilder()

    task = TaskDefinition(
        task_id="T_MODEL_02",
        title="Model hallucination test",
        allowed_files=["Player.cs"],
    )

    # Provider returning non-JSON garbage
    provider = MockModelProvider(raw_response="I cannot fulfill this request as JSON.")
    res = runtime.execute_with_model(task, provider=provider, context_builder=context_builder)

    assert res.success is False
    assert res.failure_type == FailureType.PATCH_INVALID.value


def test_resolve_deepseek_api_key_env(monkeypatch):
    from model.providers import resolve_deepseek_api_key
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-env-key-12345")
    assert resolve_deepseek_api_key() == "sk-test-env-key-12345"


def test_resolve_deepseek_api_key_from_dsh_settings(monkeypatch, tmp_path):
    from model.providers import resolve_deepseek_api_key
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    
    # Mock home directory with ~/.dsh/settings.yaml
    dsh_dir = tmp_path / ".dsh"
    dsh_dir.mkdir()
    settings_file = dsh_dir / "settings.yaml"
    settings_file.write_text("deepseek_api_key: sk-dsh-settings-key-9999\n", encoding="utf-8")
    
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert resolve_deepseek_api_key() == "sk-dsh-settings-key-9999"
