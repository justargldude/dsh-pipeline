"""TDD red test: discovery smoke-check lifecycle ordering (QA F-03, round 3).

The smoke-check in run() must execute BEFORE the red test is written to the
main repo; otherwise the runner's discovered-test count is identical before
and after and every orchestration with a counting runner false-positives.
"""
import subprocess
from pathlib import Path

from orchestrator.coordinator import AutonomousCoordinator
from orchestrator.planner import PlannedTask
from unittest.mock import MagicMock


class _Q:
    name = "stub-qa"
    def query(self, prompt, timeout=None):
        return "ok"


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def test_smoke_check_runs_before_write_to_main(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / "tests").mkdir()
    (repo / "tests" / "existing.test.js").write_text("//x", encoding="utf-8")

    task = PlannedTask(
        task_id="T_F03", title="t", description="d", allowed_files=["README.md"],
        test_file="tests/test_new_01.test.js", test_code="// red",
        test_cmd='bash -c "find tests -name \'*.test.js\' | wc -l"',
    )
    coord = AutonomousCoordinator(
        target_repo=repo, qa_client=_Q(), dev_provider=MagicMock(), dry_run=True, test_mode=False,
    )

    order = []
    real_verify = coord._verify_red_test_discovered
    real_write = coord._write_red_test_to_main

    def spy_verify(task_arg, count_cmd=None):
        order.append("verify")
        return real_verify(task_arg, count_cmd=count_cmd)

    def spy_write(task_arg):
        order.append("write")
        return real_write(task_arg)

    monkeypatch.setattr(coord, "_verify_red_test_discovered", spy_verify)
    monkeypatch.setattr(coord, "_write_red_test_to_main", spy_write)

    # Drive the exact run() code path up to the smoke-check/write section.
    # Use the loop body logic via a minimal crafted audit report + patched planner.
    from orchestrator.planner import AuditReport
    import orchestrator.planner as planner_mod
    monkeypatch.setattr(
        planner_mod.AutonomousPlanner, "audit_and_plan",
        lambda self, g, max_tasks=3: AuditReport(
            summary="s", tasks=[task],
            detected_test_cmd="bash -c \"find tests -name '*.test.js' | wc -l\"",
        ),
    )
    from core.runtime import DSHRuntime
    from task.schema import TransactionResult
    monkeypatch.setattr(
        DSHRuntime, "execute_with_recovery",
        lambda self, task_arg, provider, context_builder: TransactionResult(
            task_id=task_arg.task_id, success=True, commit_hash="abc", dry_run=True,
        ),
    )
    monkeypatch.setattr(coord, "_review_diff_with_qa", lambda t, d: "APPROVED")

    coord.run(user_goal="g", max_tasks=1)

    assert order and order[0] == "verify", (
        "run() must call _verify_red_test_discovered BEFORE _write_red_test_to_main; "
        f"observed order: {order}"
    )
