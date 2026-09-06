"""Phase C (v2.3) — PACT in Dev SYSTEM_PROMPT + Native PBT Loop in QA planner prompt.

Contracts:
1. Dev SYSTEM_PROMPT (core/runtime.py) must mandate PACT guard clauses:
   precondition validation on every public method, purity in Core/Domain,
   no hardcoding to visible test assertions, no-visibility warning about
   hidden tests.
2. QA planner prompt (orchestrator/planner.py) must mandate the Native PBT
   Generative Loop: 100 iterations, seeded generator, zero dependencies,
   roundtrip/idempotence invariants.
"""
from pathlib import Path

import core.runtime as RT
import orchestrator.planner as PL


class TestDevPromptPACT:
    def test_system_prompt_mentions_pact(self):
        src = RT.DSHRuntime.SYSTEM_PROMPT
        assert "PACT" in src, "Dev prompt must name the PACT contract discipline"

    def test_system_prompt_requires_guard_clauses(self):
        src = RT.DSHRuntime.SYSTEM_PROMPT
        low = src.lower()
        assert "guard clause" in low, "must demand precondition guard clauses"
        assert "precondition" in low

    def test_system_prompt_requires_purity_in_core(self):
        src = RT.DSHRuntime.SYSTEM_PROMPT.lower()
        assert "pure" in src
        assert "datetime.now" in src
        assert "random" in src

    def test_system_prompt_bans_hardcoding(self):
        src = RT.DSHRuntime.SYSTEM_PROMPT.lower()
        assert "hardcode" in src, "must ban hardcoding values asserted by tests"

    def test_system_prompt_warns_hidden_tests(self):
        src = RT.DSHRuntime.SYSTEM_PROMPT
        assert "cannot see" in src.lower(), "must warn Dev that hidden tests exist"


class TestQAPromptPBT:
    def _planner_source(self) -> str:
        return Path(PL.__file__).read_text(encoding="utf-8")

    def test_qa_prompt_requires_100_iterations(self):
        src = self._planner_source()
        assert "100 iterations" in src, "Native PBT loop must be 100 iterations"

    def test_qa_prompt_requires_seeded_generator(self):
        src = self._planner_source()
        low = src.lower()
        assert "seed" in low, "PBT generator must be seeded (deterministic)"

    def test_qa_prompt_requires_zero_dependencies(self):
        src = self._planner_source()
        low = src.lower()
        assert "zero external dependencies" in low or "zero dependencies" in low

    def test_qa_prompt_names_invariant_kinds(self):
        src = self._planner_source().lower()
        assert "roundtrip" in src
        assert "idempotence" in src

    def test_qa_prompt_uses_native_generative_loop_name(self):
        src = self._planner_source()
        assert "NATIVE GENERATIVE LOOP" in src.upper()
