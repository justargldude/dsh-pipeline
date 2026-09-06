"""Phase D (v2.3) — D1 Security Prover + D2 Metamorphic Check.

RED tests first. Contracts:
D1 (Security Prover):
  - Coordinator accepts a prover_client (third reviewer) whose model family
    MUST differ from the Dev family (enforced via get_model_family).
  - Prover reviews the diff with an exploit-focused prompt and returns a
    validated {verdict, flagged_risks, summary} JSON.
  - A REJECTED verdict from QA or the prover marks the task FAILED
    (verdicts are enforced, not just logged).
D2 (Metamorphic Check):
  - validation/metamorphic.py exposes deterministic semantic-preserving
    transforms: reseed_random_literals, rename_test_locals, reverse_test_order.
  - MetamorphicGate: original passes but a variant fails => reject
    (anti-hardcode / anti-flaky / anti-order-dependence).
  - Identity variants (nothing to transform) are skipped, not failed.
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from orchestrator.coordinator import AutonomousCoordinator


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    def run(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (repo / "README.md").write_text("repo", encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "init")
    return repo


class _StubClient:
    """Minimal subagent-like client: name + query returning fixed JSON."""
    def __init__(self, name, payload):
        self.name = name
        self._payload = payload

    def query(self, prompt, timeout=None):
        return json.dumps(self._payload)


# ==============================================================================
# D1 — Security Prover
# ==============================================================================

class TestD1SecurityProver:
    def test_coordinator_accepts_prover_client(self, tmp_path):
        repo = _git_repo(tmp_path)
        prover = _StubClient("security-prover", {"verdict": "APPROVED", "flagged_risks": [], "summary": "clean"})
        coord = AutonomousCoordinator(
            target_repo=repo,
            qa_client=_StubClient("stub-qa", {}),
            dev_provider=MagicMock(),
            prover_client=prover,
            dry_run=True,
        )
        assert coord.prover_client is prover

    def test_prover_family_must_differ_from_dev(self, tmp_path):
        """Cross-family enforcement extends to the prover: same family as
        Dev is rejected (unless mock/unknown)."""
        from core.config import get_model_family

        repo = _git_repo(tmp_path)
        # deepseek (dev) and ds-pro-x (prover) both resolve to family 'deepseek'
        assert get_model_family("deepseek") == get_model_family("ds-pro-x") == "deepseek"
        dev = MagicMock()
        dev.model_name = "deepseek"
        with pytest.raises(ValueError, match="(?i)prover|family"):
            AutonomousCoordinator(
                target_repo=repo,
                qa_client=_StubClient("stub-qa", {}),
                dev_provider=dev,
                prover_client=_StubClient("ds-pro-x", {}),
                prover_name="ds-pro-x",
                dry_run=True,
            )

    def test_prover_review_returns_validated_verdict(self, tmp_path):
        repo = _git_repo(tmp_path)
        prover = _StubClient(
            "security-prover",
            {"verdict": "REJECTED", "flagged_risks": ["cmd injection in Build()"], "summary": "unsafe"},
        )
        coord = AutonomousCoordinator(
            target_repo=repo,
            qa_client=_StubClient("stub-qa", {}),
            dev_provider=MagicMock(),
            prover_client=prover,
            dry_run=True,
        )
        from orchestrator.planner import PlannedTask
        task = PlannedTask(task_id="T1", title="t", description="d")
        verdict = coord._review_diff_with_prover(task, "diff --git a/x b/x")
        assert verdict["verdict"] == "REJECTED"
        assert "cmd injection" in verdict["flagged_risks"][0]

    def test_prover_review_prompt_is_exploit_focused(self, tmp_path):
        captured = {}

        class _CapturingProver:
            name = "security-prover"
            def query(self, prompt, timeout=None):
                captured["prompt"] = prompt
                return json.dumps({"verdict": "APPROVED", "flagged_risks": [], "summary": "ok"})

        repo = _git_repo(tmp_path)
        coord = AutonomousCoordinator(
            target_repo=repo,
            qa_client=_StubClient("stub-qa", {}),
            dev_provider=MagicMock(),
            prover_client=_CapturingProver(),
            dry_run=True,
        )
        from orchestrator.planner import PlannedTask
        coord._review_diff_with_prover(PlannedTask(task_id="T", title="t"), "+ added code")
        p = captured["prompt"].lower()
        assert "exploit" in p or "khai thác" in p, "prover prompt must ask how the diff could be exploited"
        assert "inject" in p or "secur" in p

    def test_prover_invalid_json_rejected(self, tmp_path):
        class _BadProver:
            name = "security-prover"
            def query(self, prompt, timeout=None):
                return "not json at all"

        repo = _git_repo(tmp_path)
        coord = AutonomousCoordinator(
            target_repo=repo,
            qa_client=_StubClient("stub-qa", {}),
            dev_provider=MagicMock(),
            prover_client=_BadProver(),
            dry_run=True,
        )
        from orchestrator.planner import PlannedTask
        with pytest.raises(ValueError):
            coord._review_diff_with_prover(PlannedTask(task_id="T", title="t"), "diff")

    def test_rejected_verdict_enforced_as_task_failure(self, tmp_path):
        """A REJECTED verdict (QA or prover) must FAIL the task — verdicts
        are gates, not decorations."""
        repo = _git_repo(tmp_path)
        coord = AutonomousCoordinator(
            target_repo=repo,
            qa_client=_StubClient("stub-qa", {}),
            dev_provider=MagicMock(),
            dry_run=True,
        )
        assert coord._verdicts_reject([
            {"verdict": "APPROVED", "flagged_risks": [], "summary": "ok"},
        ]) is False
        assert coord._verdicts_reject([
            {"verdict": "APPROVED", "flagged_risks": [], "summary": "ok"},
            {"verdict": "REJECTED", "flagged_risks": ["x"], "summary": "bad"},
        ]) is True


# ==============================================================================
# D2 — Metamorphic Check
# ==============================================================================

class TestD2Transforms:
    def test_module_exists(self):
        import validation.metamorphic as MM
        for fn in ("reseed_random_literals", "rename_test_locals", "reverse_test_order"):
            assert hasattr(MM, fn), f"missing {fn}"

    def test_reseed_random_literal(self):
        from validation.metamorphic import reseed_random_literals
        assert reseed_random_literals("var r = new Random(42);") == "var r = new Random(43);"
        assert reseed_random_literals("random.seed(2024)") == "random.seed(2025)"

    def test_reseed_ignores_unseeded_and_nonliteral(self):
        from validation.metamorphic import reseed_random_literals
        assert reseed_random_literals("new Random()") == "new Random()"
        assert reseed_random_literals("new Random(seed)") == "new Random(seed)"

    def test_rename_locals(self):
        from validation.metamorphic import rename_test_locals
        code = "int input = 1;\nint result = Add(input);\nAssert.AreEqual(2, result);"
        out = rename_test_locals(code)
        assert "input" not in out or "input_" in out  # renamed with suffix
        # Semantics preserved: the same number of references remain.
        assert out.count("input") == out.count("input_") + 0 or "input" not in out

    def test_rename_noop_on_unmatched(self):
        from validation.metamorphic import rename_test_locals
        code = "int alpha = 1; int beta = 2;"
        assert rename_test_locals(code) == code

    def test_reverse_test_order_python(self):
        from validation.metamorphic import reverse_test_order
        code = "def test_a():\n    assert 1\n\n\ndef test_b():\n    assert 2\n"
        out = reverse_test_order(code)
        assert out.index("test_b") < out.index("test_a")

    def test_reverse_test_order_single_test_identity(self):
        from validation.metamorphic import reverse_test_order
        code = "def test_only():\n    assert 1\n"
        assert reverse_test_order(code) == code

    def test_reverse_test_order_csharp_methods(self):
        from validation.metamorphic import reverse_test_order
        code = (
            "[Test]\npublic void First() { Assert.True(1 == 1); }\n\n"
            "[Test]\npublic void Second() { Assert.True(2 == 2); }\n"
        )
        out = reverse_test_order(code)
        assert out.index("Second") < out.index("First")


class TestD2MetamorphicGate:
    def _repo_with_test(self, tmp_path, content):
        repo = tmp_path / "repo"
        repo.mkdir(exist_ok=True)
        (repo / "tests").mkdir(exist_ok=True)
        (repo / "tests" / "test_x.py").write_text(content, encoding="utf-8")
        return repo

    def test_original_pass_variant_fail_rejects(self, tmp_path):
        """Original suite passes, but the reseeded variant fails => the patch
        (or test) is hardcoded/flaky => REJECT."""
        from validation.metamorphic import MetamorphicGate, MetamorphicResult

        repo = self._repo_with_test(tmp_path, "def test_x():\n    r = random.Random(7)\n    assert f(r) == 1\n")
        seen_seeds = []

        def runner(repo_path):
            # Passes only with the ORIGINAL seed 7 (hardcoded behavior).
            code = (repo_path / "tests" / "test_x.py").read_text()
            import re as _re
            m = _re.search(r"Random\((\d+)\)", code)
            seen_seeds.append(m.group(1) if m else None)
            rc = 0 if (m and m.group(1) == "7") else 1
            return (rc, "out")

        gate = MetamorphicGate(test_runner=runner)
        res = gate.run_gate(repo, "tests/test_x.py")
        assert res.success is False
        assert res.failed_variant is not None
        assert "7" in seen_seeds and "8" in seen_seeds, "both original and reseeded variant must have run"
        # Test file restored exactly afterwards.
        assert (repo / "tests" / "test_x.py").read_text(encoding="utf-8") == "def test_x():\n    r = random.Random(7)\n    assert f(r) == 1\n"

    def test_all_variants_pass_accepts(self, tmp_path):
        from validation.metamorphic import MetamorphicGate

        repo = self._repo_with_test(
            tmp_path,
            "def test_a():\n    assert 1\n\n\ndef test_b():\n    assert 2\n",
        )
        gate = MetamorphicGate(test_runner=lambda p: (0, "ok"))
        res = gate.run_gate(repo, "tests/test_x.py")
        assert res.success is True
        assert res.failed_variant is None
        assert (repo / "tests" / "test_x.py").read_text(encoding="utf-8") == "def test_a():\n    assert 1\n\n\ndef test_b():\n    assert 2\n"

    def test_original_fails_skips_variants(self, tmp_path):
        """If the ORIGINAL suite fails, metamorphic checking is meaningless
        (red state) — report skipped, not rejected."""
        from validation.metamorphic import MetamorphicGate

        repo = self._repo_with_test(tmp_path, "def test_a():\n    assert 1\n")
        calls = []
        def runner(p):
            calls.append(1)
            return (1, "fail")
        gate = MetamorphicGate(test_runner=runner)
        res = gate.run_gate(repo, "tests/test_x.py")
        # Only the original run happened.
        assert len(calls) == 1
        assert res.success is True and res.skipped is True

    def test_identity_variants_skipped_not_failed(self, tmp_path):
        """A test with nothing to reseed/rename/reorder yields identity
        variants — they must be skipped, not counted as failures."""
        from validation.metamorphic import MetamorphicGate

        repo = self._repo_with_test(tmp_path, "alpha = 1\nassert alpha\n")
        gate = MetamorphicGate(test_runner=lambda p: (0, "ok"))
        res = gate.run_gate(repo, "tests/test_x.py")
        assert res.success is True
        assert res.skipped_variants >= 1

    def test_result_model_fields(self):
        from validation.metamorphic import MetamorphicResult
        r = MetamorphicResult(success=True, skipped=False, skipped_variants=0, failed_variant=None, details={})
        assert r.success is True
