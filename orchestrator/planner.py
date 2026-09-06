import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from orchestrator.subagents import SubagentClient, AntigravityClient
from task.schema import RiskLevel

logger = logging.getLogger("dsh.orchestrator.planner")


class PlannedTask(BaseModel):
    task_id: str
    title: str
    description: str = ""
    allowed_files: List[str] = Field(default_factory=list)
    target_symbols: List[str] = Field(default_factory=list)
    test_file: Optional[str] = None
    test_code: Optional[str] = None
    # Anti-reward-hacking v2.3 Checkpoint 1 (Holdout Testing): QA emits TWO
    # exams. `test_code` is the visible red test the Dev agent may read;
    # `holdout_test_code` is the hidden exam, filtered out of Dev context and
    # injected into the worktree only immediately before T3 Regression.
    holdout_test_file: Optional[str] = None
    holdout_test_code: Optional[str] = None
    test_cmd: Optional[str] = None
    build_cmd: Optional[str] = None
    max_lines_added: int = 300
    max_lines_deleted: int = 150
    risk: str = "medium"


class AuditReport(BaseModel):
    summary: str
    detected_framework: str = "unknown"
    detected_build_cmd: Optional[str] = None
    detected_test_cmd: Optional[str] = None
    tasks: List[PlannedTask] = Field(default_factory=list)


class AutonomousPlanner:
    """Uses the designated QA subagent (e.g. Antigravity, Claude, Codex) as Lead Architect & Scout
    to scan target repository, identify issues, and synthesize TDD tasks.
    """

    def __init__(self, target_repo: Path, qa_client: SubagentClient):
        self.target_repo = target_repo.resolve()
        self.qa_client = qa_client
        self.antigravity = qa_client  # backwards compatibility alias


    def inspect_repo_profile(self) -> Dict[str, Any]:
        """Collects lightweight repo signals without dumping full source code."""
        profile: Dict[str, Any] = {
            "root": str(self.target_repo),
            "files": [],
            "manifests": {},
            "framework": "unknown",
            "default_test_cmd": None,
            "default_build_cmd": None,
        }

        # 1. Git tracked files
        try:
            res = subprocess.run(
                ["git", "ls-files"],
                cwd=self.target_repo,
                capture_output=True,
                text=True,
                check=True,
            )
            files = [f for f in res.stdout.splitlines() if f.strip()][:150]
            profile["files"] = files
        except Exception:
            # Fallback for non-git directories
            profile["files"] = [
                str(p.relative_to(self.target_repo))
                for p in self.target_repo.glob("**/*")
                if p.is_file() and not any(part.startswith(".") for part in p.parts)
            ][:100]

        # 2. Identify key manifests & project types
        pkg_json = self.target_repo / "package.json"
        if pkg_json.exists():
            data = None
            try:
                data = json.loads(pkg_json.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"[PLANNER] Malformed package.json ({e}); using Node.js fallbacks.")
            if data is not None:
                profile["framework"] = "nodejs"
                scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
                profile["manifests"]["package.json"] = {
                    "name": data.get("name"),
                    "scripts": scripts,
                    "main": data.get("main"),
                }
                if "test" in scripts:
                    profile["default_test_cmd"] = "npm test"
                else:
                    profile["default_test_cmd"] = "node --test tests/"
                # Node repos have no compiler: the build gate is either the
                # declared build script or, failing that, the test command
                # itself. Leaving default_build_cmd None hard-fails the
                # runtime with BUILD_CONFIGURATION_MISSING.
                if "build" in scripts:
                    profile["default_build_cmd"] = "npm run build"
                else:
                    profile["default_build_cmd"] = profile["default_test_cmd"]
            else:
                # F-05: malformed manifest must still yield usable commands,
                # never None (which hard-crashes with CONFIG_MISSING).
                profile["framework"] = "nodejs"
                profile["default_test_cmd"] = "node --test tests/"
                profile["default_build_cmd"] = "node --test tests/"

        # Python projects
        if (self.target_repo / "pytest.ini").exists() or (self.target_repo / "requirements.txt").exists():
            profile["framework"] = "python"
            profile["default_test_cmd"] = "pytest"
            if not profile["default_build_cmd"]:
                profile["default_build_cmd"] = "pytest"

        # .NET / C# projects
        csproj_files = list(self.target_repo.glob("*.csproj"))
        if csproj_files:
            profile["framework"] = "csharp"
            profile["default_build_cmd"] = "dotnet build"
            profile["default_test_cmd"] = "dotnet test"

        # Read top README if exists
        readme = self.target_repo / "README.md"
        if readme.exists():
            profile["manifests"]["README_SAMPLE"] = readme.read_text(encoding="utf-8")[:1200]

        return profile

    def audit_and_plan(self, user_goal: str, max_tasks: int = 3) -> AuditReport:
        """Prompts Antigravity to perform recon and produce structured TDD tasks."""
        profile = self.inspect_repo_profile()

        prompt = f"""You are the Lead Architect and QA Engineer (Role: {self.qa_client.name}).
The user requested an autonomous evaluation, review, and patch pipeline for this repository:
Goal: {user_goal}

### Target Repository Profile:
- Framework: {profile['framework']}
- Default Build Command: {profile['default_build_cmd']}
- Default Test Command: {profile['default_test_cmd']}
- Key Files Tracked:
{json.dumps(profile['files'][:80], indent=2)}

- Manifests/README Sample:
{json.dumps(profile['manifests'], indent=2)}

### Instructions:
1. Break down the user's goal into at most {max_tasks} concrete, manageable TDD tasks.
2. For each task, define:
   - task_id: unique identifier (e.g. "TASK_001")
   - title: concise task title
   - description: what needs to be evaluated/fixed
   - allowed_files: list of target files that need modifications (MUST be exact relative paths from the file list above)
   - target_symbols: list of functions, classes, or symbols to modify
   - test_file: path to the VISIBLE test file the Dev agent may read (e.g. "tests/test_fix1.js")
   - test_code: full runnable VISIBLE test code that initially FAILS on current code and PASSES after the fix.
   - holdout_test_file: path to a SEPARATE HIDDEN holdout test file (e.g. "tests/holdout_task_001.js"). The Dev agent will NEVER see this file or its content; it is injected only right before final regression validation.
   - holdout_test_code: full runnable HOLDOUT test code — an independent second exam that must also fail before the fix and pass after it. It MUST test the same contracts from a DIFFERENT angle (different concrete inputs, edge cases like negative numbers, zero, overflow, empty/null inputs) — not a copy of the visible test with renamed variables. The FIRST LINE of holdout_test_code MUST be the marker comment: DSH_HOLDOUT (this marker lets the pipeline scrub any accidental quotes of this content from Dev-facing contexts; it is not part of the test logic).
   - test_cmd: exact shell command to run ALL tests of this task (e.g. "{profile['default_test_cmd'] or 'npm test'}")
3. Test-writing requirements (BOTH visible and holdout tests):
   - PRIORITY: if the target repo's dependency graph already contains a property-based-testing library (CsCheck, FsCheck, Hypothesis, fast-check...), use it. Otherwise use a NATIVE GENERATIVE LOOP: a plain for-loop of 100 iterations generating pseudo-random inputs from a seeded generator (e.g. `new Random(42)` / `Random(seed)` with a FIXED literal seed, or a small deterministic LCG) and asserting the mathematical invariant inside the loop. Zero external dependencies — the build must not break on repos without PBT libraries.
   - Each task must include at least one property-based invariant test among its visible or holdout tests: a Roundtrip property (f(g(x)) == x where applicable), an Idempotence property (f(f(x)) == f(x) where applicable), or a Non-negative/monotonic invariant appropriate to the domain.
   - Cover contract violations explicitly: negative numbers, null/empty inputs, and overflow must appear as concrete test cases (Design by Contract: precondition guard clauses must exist).
4. CRITICAL anti-cheat constraints:
   - The Dev agent cannot see the holdout test. Never reference holdout content inside test_code, description, or allowed_files.
   - Never write a tautological assertion (comparing an expression to itself, Assert(true), x == x).
   - Never weaken or delete an existing test to make a task pass.

Respond ONLY with valid JSON matching this schema:
{{
  "summary": "Executive summary of repo audit and planned fixes",
  "detected_framework": "{profile['framework']}",
  "detected_build_cmd": "{profile['default_build_cmd'] or ''}",
  "detected_test_cmd": "{profile['default_test_cmd'] or ''}",
  "tasks": [
    {{
      "task_id": "TASK_001",
      "title": "...",
      "description": "...",
      "allowed_files": ["lib/index.js"],
      "target_symbols": ["myFunction"],
      "test_file": "tests/test_task_001.js",
      "test_code": "// Runnable VISIBLE test verifying fix",
      "holdout_test_file": "tests/holdout_task_001.js",
      "holdout_test_code": "// Runnable HIDDEN holdout test (independent second exam)",
      "test_cmd": "node --test tests/test_task_001.js",
      "max_lines_added": 300,
      "max_lines_deleted": 150,
      "risk": "medium"
    }}
  ]
}}
"""
        logger.info(f"[PLANNER] Prompting {self.qa_client.name} for repository audit and task breakdown...")
        raw_report = self.qa_client.query_json(prompt)
        return AuditReport(**raw_report)

