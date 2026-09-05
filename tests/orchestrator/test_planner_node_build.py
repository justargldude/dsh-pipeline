"""TDD red tests: Node.js build command inference in AutonomousPlanner (bug #4).

Pipeline round-2 TASK_003: pipeline #1 of the previous operational session died
with BUILD_CONFIGURATION_MISSING on the cockpit repo even though the planner
HAD detected `npm test` — because default_build_cmd is only ever populated for
C# projects. For Node.js, a build step must be inferred (npm run build when
defined, else reuse the test command) so the coordinator can populate
config.build_command and the runtime does not hard-fail at startup.
"""
import json
from pathlib import Path

from orchestrator.planner import AutonomousPlanner
from orchestrator.subagents import SubagentClient


class _StubQA(SubagentClient):
    """Never invoked: inspect_repo_profile() must be pure-local."""

    def query(self, prompt, timeout=None):  # pragma: no cover
        raise AssertionError("inspect_repo_profile must not call the QA client")


def test_node_repo_with_build_script_infers_npm_run_build(tmp_path: Path):
    pkg = {"name": "x", "scripts": {"test": "node --test tests/", "build": "vite build"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "a.js").write_text("export {};", encoding="utf-8")
    subprocess_git = tmp_path / ".git"
    subprocess_git.mkdir()  # pretend-git: glob fallback path

    planner = AutonomousPlanner(tmp_path, _StubQA(name="stub", test_mode=True))
    profile = planner.inspect_repo_profile()

    assert profile["framework"] == "nodejs"
    assert profile["default_build_cmd"] == "npm run build", (
        "Node repo with a build script must have default_build_cmd inferred; "
        "leaving it None caused BUILD_CONFIGURATION_MISSING in production."
    )
    assert profile["default_test_cmd"] == "npm test"


def test_node_repo_without_build_script_falls_back_to_test_cmd(tmp_path: Path):
    pkg = {"name": "x", "scripts": {"test": "node --test tests/"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")

    planner = AutonomousPlanner(tmp_path, _StubQA(name="stub", test_mode=True))
    profile = planner.inspect_repo_profile()

    assert profile["framework"] == "nodejs"
    assert profile["default_build_cmd"] == "npm test", (
        "Without a build script the test command is the safest build gate; "
        "it must be used instead of leaving build_cmd None."
    )


def test_node_repo_without_any_scripts_uses_node_test(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"name": "x"}', encoding="utf-8")

    planner = AutonomousPlanner(tmp_path, _StubQA(name="stub", test_mode=True))
    profile = planner.inspect_repo_profile()

    assert profile["default_test_cmd"] == "node --test tests/"
    assert profile["default_build_cmd"] == "node --test tests/"
