import json
import tempfile
import subprocess
from pathlib import Path
import pytest
from typer.testing import CliRunner

from orchestrator.subagents import AntigravityClient, DeepSeekClient
from orchestrator.planner import AutonomousPlanner, PlannedTask, AuditReport
from orchestrator.coordinator import AutonomousCoordinator, OrchestrationResult
from model.providers import MockModelProvider
from task.schema import PatchProposal, FilePatch, PatchHunk
from cli import app

runner = CliRunner()


def test_antigravity_client_json_extraction():
    # 1. Plain json
    raw1 = '{"status": "ok", "count": 42}'
    assert AntigravityClient._extract_json(raw1)["status"] == "ok"

    # 2. Markdown fence
    raw2 = 'Here is the analysis:\n```json\n{"verdict": "APPROVED"}\n```\nDone.'
    assert AntigravityClient._extract_json(raw2)["verdict"] == "APPROVED"

    # 3. Outer text with braces
    raw3 = 'Some lead text {"nested": {"key": 123}} trailing note.'
    assert AntigravityClient._extract_json(raw3)["nested"]["key"] == 123


def test_planner_inspect_repo_profile():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        (repo / "package.json").write_text(
            json.dumps({"name": "test-pkg", "scripts": {"test": "node --test"}}),
            encoding="utf-8",
        )
        (repo / "index.js").write_text("console.log('hello');", encoding="utf-8")

        client = AntigravityClient(test_mode=True)
        planner = AutonomousPlanner(repo, client)
        profile = planner.inspect_repo_profile()

        assert profile["framework"] == "nodejs"
        assert profile["default_test_cmd"] == "npm test"
        assert "package.json" in profile["manifests"]


def test_planner_audit_and_plan():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        (repo / "index.js").write_text("function add(a, b) { return a + b; }", encoding="utf-8")

        client = AntigravityClient(test_mode=True)
        mock_audit = {
            "summary": "Mock audit found 1 bug in add function.",
            "detected_framework": "nodejs",
            "detected_build_cmd": "",
            "detected_test_cmd": "node --test",
            "tasks": [
                {
                    "task_id": "TASK_001",
                    "title": "Fix add overflow bug",
                    "description": "Add typecheck to add function",
                    "allowed_files": ["index.js"],
                    "target_symbols": ["add"],
                    "test_file": "test_add.js",
                    "test_code": "assert(add(1, 2) === 3);",
                    "test_cmd": "node test_add.js",
                    "max_lines_added": 20,
                    "max_lines_deleted": 5,
                    "risk": "low",
                }
            ],
        }
        client.set_mock_response("Lead Architect", mock_audit)

        planner = AutonomousPlanner(repo, client)
        report = planner.audit_and_plan("Audit index.js", max_tasks=1)

        assert len(report.tasks) == 1
        assert report.tasks[0].task_id == "TASK_001"
        assert report.tasks[0].allowed_files == ["index.js"]


def test_autonomous_coordinator_end_to_end():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        # Initialize minimal git repo
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@dsh.local"], cwd=repo, check=True)

        target_file = repo / "Calculator.cs"
        target_file.write_text("public class Calculator {\n    public int Add(int a, int b) => a + b;\n}\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)

        # Mock Antigravity
        antigravity = AntigravityClient(test_mode=True)
        mock_plan = {
            "summary": "Audit: Calculator needs logging.",
            "detected_framework": "csharp",
            "detected_build_cmd": "dotnet build",
            "detected_test_cmd": "dotnet test",
            "tasks": [
                {
                    "task_id": "T_CALC_01",
                    "title": "Add logging to Add",
                    "description": "Log input values",
                    "allowed_files": ["Calculator.cs"],
                    "target_symbols": ["Add"],
                    "test_file": None,
                    "test_code": None,
                    "test_cmd": None,
                    "max_lines_added": 10,
                    "max_lines_deleted": 2,
                    "risk": "low",
                }
            ],
        }
        antigravity.set_mock_response("Lead Architect", mock_plan)
        antigravity.set_mock_response("Lead Gatekeeper", "APPROVED: Clean hook and no regression.")

        # Mock DeepSeek provider
        deepseek = DeepSeekClient(test_mode=True)
        mock_provider = MockModelProvider(
            predefined_proposal=PatchProposal(
                patches=[
                    FilePatch(
                        file="Calculator.cs",
                        hunks=[
                            PatchHunk(
                                old_text="    public int Add(int a, int b) => a + b;",
                                new_text="    public int Add(int a, int b) {\n        // [LOG]\n        return a + b;\n    }",
                            )
                        ],
                    )
                ],
                reason="Added logging",
                confidence=1.0,
            )
        )
        deepseek.set_mock_provider(mock_provider)

        coordinator = AutonomousCoordinator(
            target_repo=repo,
            antigravity=antigravity,
            deepseek=deepseek,
            dry_run=True,
            test_mode=True,
        )

        res: OrchestrationResult = coordinator.run("Review and log Calculator", max_tasks=1)
        assert res.success is True
        assert len(res.tasks) == 1
        assert res.tasks[0].task_id == "T_CALC_01"
        assert res.tasks[0].success is True
        assert "APPROVED" in res.tasks[0].review_verdict
        assert "BÁO CÁO ĐIỀU PHỐI TỰ ĐỘNG" in res.final_report


def test_cli_orchestrate_command_test_mode():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@dsh.local"], cwd=repo, check=True)

        sample_file = repo / "Main.cs"
        sample_file.write_text("public class Main { public static void Run() {} }\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "Init"], cwd=repo, check=True)

        # Test with explicit --qa and --dev flags
        result = runner.invoke(
            app,
            [
                "orchestrate",
                "Audit and patch Main.cs",
                "--target-repo",
                str(repo),
                "--qa",
                "claude",
                "--dev",
                "deepseek",
                "--test-mode",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0
        assert "QA (Lead/Reviewer): claude | Dev (Coder): deepseek" in result.stdout
        assert "Starting Autonomous Orchestration on" in result.stdout


def test_cli_orchestrate_missing_models_error():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        result = runner.invoke(
            app,
            [
                "orchestrate",
                "Audit and patch Main.cs",
                "--target-repo",
                str(repo),
            ],
            env={},  # empty env to ensure no DSH_QA_MODEL / DSH_DEV_MODEL
        )
        assert result.exit_code == 1
        assert "Không được hardcode mặc định model QA và Dev" in result.stdout


def test_subagent_factories():
    from orchestrator.subagents import create_qa_client, create_dev_provider, SubagentClient
    qa = create_qa_client("agy", test_mode=True)
    assert isinstance(qa, SubagentClient)
    assert qa.name == "agy"

    dev = create_dev_provider("deepseek", test_mode=True)
    assert dev is not None


def test_cli_prompt_inference_error_when_no_match():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@dsh.local"], cwd=repo, check=True)

        result = runner.invoke(
            app,
            [
                "prompt",
                "Fix random bug",
                "--repo",
                str(repo),
            ],
        )
        assert result.exit_code == 1
        assert "Could not automatically infer target file(s)" in result.stdout


