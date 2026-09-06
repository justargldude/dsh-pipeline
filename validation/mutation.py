"""T2.5 Mutation Gate (anti-reward-hacking v2.3 Stage 2 Phase A).

Defeats vacuous test suites: mutates ONLY the diff/target region the Dev
agent just modified (never the whole codebase), runs the test suite against
each mutant, and rejects the submission when fewer than 70% of the mutants
are killed (mutation_score = killed / total).

Mutation operators (arithmetic / boolean inversions, per the v2.3 spec):
    == <-> !=      > <-> <=      < <-> >=      + <-> -      true <-> false

The gate is deterministic (no wall-clock or unseeded randomness anywhere),
so the same submission always yields the same verdict.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from validation.pipeline import MutationGateResult


class MutationOperator(Enum):
    EQ_TO_NE = ("==", "!=")
    NE_TO_EQ = ("!=", "==")
    GT_TO_LE = (">", "<=")
    LE_TO_GT = ("<=", ">")
    LT_TO_GE = ("<", ">=")
    GE_TO_LT = (">=", "<")
    PLUS_TO_MINUS = ("+", "-")
    MINUS_TO_PLUS = ("-", "+")
    TRUE_TO_FALSE = ("true", "false")
    FALSE_TO_TRUE = ("false", "true")


@dataclass
class Mutant:
    mutant_id: str
    file_path: str
    line_no: int                     # 1-based line of the ORIGINAL text
    original_text: str
    mutated_text: str
    operator: MutationOperator
    killed: bool = False
    kill_output: str = ""

    @property
    def is_surviving(self) -> bool:
        return not self.killed


@dataclass
class DiffRegion:
    """A contiguous mutated region of one file (line numbers are 1-based,
    inclusive, in the POST-patch file)."""
    file_path: str
    start_line: int
    end_line: int


def extract_diff_regions(old_code: str, new_code: str, file_path: str) -> List[DiffRegion]:
    """Computes the line ranges of `new_code` that changed vs `old_code`.

    Pure function (difflib is deterministic) — used to restrict mutation to
    the exact region the Dev agent touched (v2.3: "only mutate the diff /
    target_symbols region, never the whole codebase").
    """
    old_lines = old_code.splitlines()
    new_lines = new_code.splitlines()
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)

    regions: List[DiffRegion] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        regions.append(DiffRegion(file_path=file_path, start_line=j1 + 1, end_line=j2))
    # Merge adjacent/overlapping regions (e.g. replace+insert opcodes touching).
    merged: List[DiffRegion] = []
    for r in sorted(regions, key=lambda x: (x.start_line, x.end_line)):
        if merged and r.start_line <= merged[-1].end_line + 1:
            merged[-1].end_line = max(merged[-1].end_line, r.end_line)
        else:
            merged.append(r)
    return merged


def _iter_operator_tokens(line: str) -> List[Tuple[MutationOperator, int, int]]:
    """Finds mutation operator token sites on a single line of code.

    Returns (operator, token_start_col, token_end_col) triples. String and
    char literals are skipped so we never mutate inside quoted text.
    Comment segments are skipped as well (mutating a comment changes
    nothing semantically and would create fake surviving mutants).
    """
    sites: List[Tuple[MutationOperator, int, int]] = []
    n = len(line)
    i = 0
    while i < n:
        ch = line[i]
        # Skip string literals "..." and verbatim strings @"..."
        if ch == '"':
            i = _skip_string(line, i)
            continue
        # Skip char literals 'a' or '\n'
        if ch == "'":
            i = _skip_char(line, i)
            continue
        # Skip comments (line comments to end of line)
        if ch == "/" and i + 1 < n and line[i + 1] == "/":
            break
        # Block comments are rare on one line; treat /* ... */ simply:
        if ch == "/" and i + 1 < n and line[i + 1] == "*":
            end = line.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        # Operator matching. Longest-first ordering is critical: at position
        # i the two-char tokens (==, !=, <=, >=) are tested before their
        # one-char prefixes (=, !, <, >), so 'a <= b' yields LE_TO_GT (not a
        # bogus LT_TO_GE on the '<'). Lambdas '=>' are excluded explicitly.
        matched_at_i = False
        for op in (
            MutationOperator.EQ_TO_NE,
            MutationOperator.NE_TO_EQ,
            MutationOperator.LE_TO_GT,
            MutationOperator.GE_TO_LT,
            MutationOperator.GT_TO_LE,
            MutationOperator.LT_TO_GE,
            MutationOperator.PLUS_TO_MINUS,
            MutationOperator.MINUS_TO_PLUS,
            MutationOperator.TRUE_TO_FALSE,
            MutationOperator.FALSE_TO_TRUE,
        ):
            old_tok, new_tok = op.value
            # Word tokens (true/false) require identifier boundaries.
            if old_tok in ("true", "false"):
                s = i
                e = i + len(old_tok)
                if line.startswith(old_tok, s) and (s == 0 or not (line[s-1].isalnum() or line[s-1] == "_")) and (e >= n or not (line[e].isalnum() or line[e] == "_")):
                    sites.append((op, s, e))
                    i = e
                    matched_at_i = True
                    break
                continue
            if line.startswith(old_tok, i):
                # '=>' is a lambda arrow, not a comparison — never mutate it.
                if old_tok == ">" and i > 0 and line[i - 1] == "=":
                    continue
                # '++'/'--' are increment/decrement operators: mutating one
                # char yields 'i-+' / 'i+-' which cannot compile — such a
                # mutant would be "killed" by the compiler, not by the tests,
                # inflating the score without proving anything.
                if old_tok == "+" and ((i + 1 < n and line[i + 1] == "+") or (i > 0 and line[i - 1] == "+")):
                    continue
                if old_tok == "-" and ((i + 1 < n and line[i + 1] == "-") or (i > 0 and line[i - 1] == "-")):
                    continue
                sites.append((op, i, i + len(old_tok)))
                i += len(old_tok)
                matched_at_i = True
                break
        if not matched_at_i:
            i += 1
    return sites


def _skip_string(line: str, start: int) -> int:
    """Returns the index just past a string literal starting at `start`."""
    i = start
    verbatim = i > 0 and line[i-1] == "@"
    n = len(line)
    i += 1  # consume the opening quote
    while i < n:
        ch = line[i]
        if ch == "\\" and not verbatim:
            i += 2
            continue
        if ch == '"':
            if verbatim and i + 1 < n and line[i+1] == '"':
                i += 2  # escaped quote in verbatim string
                continue
            return i + 1
        i += 1
    return n


def _skip_char(line: str, start: int) -> int:
    i = start + 1
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "'":
            return i + 1
        i += 1
    return n


class ASTMutationGate:
    """T2.5 Mutation Gate: mutates only the Dev diff region and requires the
    test suite to kill at least `threshold` (default 70%) of the mutants."""

    def __init__(
        self,
        test_runner: Callable[[Path], Tuple[int, str]],
        threshold: float = 0.70,
        max_mutants: int = 40,
        seed: int = 20260907,
        lazy: bool = False,
    ):
        self.test_runner = test_runner
        self.threshold = threshold
        self.max_mutants = max_mutants
        self.seed = seed
        if threshold < 0 or threshold > 1:
            raise ValueError(f"threshold must be in [0,1], got {threshold}")
        # lazy=True: constructor performs zero I/O; used by tests/diagnostic.
        self.lazy = lazy

    # ------------------------------------------------------------------
    # Mutant generation (pure)
    # ------------------------------------------------------------------
    def generate_mutants(
        self,
        old_code: str,
        new_code: str,
        file_path: str,
    ) -> List[Mutant]:
        """Generates deterministic mutants restricted to the diff region."""
        regions = extract_diff_regions(old_code, new_code, file_path)
        if not regions:
            return []

        new_lines = new_code.splitlines()
        mutants: List[Mutant] = []
        seen_sites: set = set()

        for region in regions:
            for ln in range(region.start_line, region.end_line + 1):
                if ln < 1 or ln > len(new_lines):
                    continue
                line = new_lines[ln - 1]
                for op, s, e in _iter_operator_tokens(line):
                    key = (file_path, ln, s, op)
                    if key in seen_sites:
                        continue
                    seen_sites.add(key)
                    old_tok, new_tok = op.value
                    mutated_line = line[:s] + new_tok + line[e:]
                    mutants.append(
                        Mutant(
                            mutant_id=f"m{len(mutants)+1:03d}",
                            file_path=file_path,
                            line_no=ln,
                            original_text=line,
                            mutated_text=mutated_line,
                            operator=op,
                        )
                    )
        return mutants

    # ------------------------------------------------------------------
    # Gate execution
    # ------------------------------------------------------------------
    def run_gate(
        self,
        repo_path: Path,
        old_code: str,
        new_code: str,
        file_path: str,
        extra_files: Optional[Dict[str, Tuple[str, str]]] = None,
    ) -> MutationGateResult:
        """Executes the gate for one file transition.

        Writes the mutant into the worktree, runs the test runner, restores
        the original post-patch content, and aggregates the verdict.
        `extra_files` optionally maps additional mutated files to
        (old, new) content so multi-file diffs can be gated together.
        """
        mutants = self.generate_mutants(old_code, new_code, file_path)
        if extra_files:
            for ef, (e_old, e_new) in extra_files.items():
                mutants.extend(self.generate_mutants(e_old, e_new, ef))

        if not mutants:
            # No mutable tokens in the diff: vacuity cannot be measured here;
            # report success (nothing to kill) with score normalized to 1.0
            # and zero total mutants, so the gate never spuriously blocks
            # docs/whitespace-only diffs.
            return MutationGateResult(
                success=True,
                mutation_score=1.0,
                threshold=self.threshold,
                killed=0,
                total=0,
                surviving_mutants=[],
                details={"note": "no mutable tokens in diff region"},
            )

        target = Path(repo_path) / file_path
        original_disk = target.read_text(encoding="utf-8")
        try:
            killed = 0
            survivors: List[str] = []
            for mut in mutants[: self.max_mutants]:
                disk_lines = original_disk.splitlines(keepends=True)
                disk_lines[mut.line_no - 1] = mut.mutated_text + ("\n" if disk_lines and disk_lines[mut.line_no - 1].endswith("\n") else "")
                target.write_text("".join(disk_lines), encoding="utf-8")
                rc, out = self.test_runner(Path(repo_path))
                # KILLED = the suite FAILS (non-zero rc) under the mutant:
                # the tests actually detect the behavioral change. A mutant
                # that still passes (rc == 0) SURVIVES — evidence of a
                # vacuous test suite.
                mut.killed = (rc != 0)
                mut.kill_output = out
                if mut.killed:
                    killed += 1
                else:
                    survivors.append(f"{mut.file_path}:{mut.line_no} {mut.operator.name}: '{mut.original_text.strip()}' -> '{mut.mutated_text.strip()}'")
        finally:
            # Deterministic restore: always write back the exact post-patch
            # content, regardless of how the runner behaves.
            target.write_text(original_disk, encoding="utf-8")
        score = killed / len(mutants[: self.max_mutants])
        success = score >= self.threshold
        return MutationGateResult(
            success=success,
            mutation_score=score,
            threshold=self.threshold,
            killed=killed,
            total=len(mutants[: self.max_mutants]),
            surviving_mutants=survivors,
            details={
                "file_path": file_path,
                "generated": len(mutants),
                "executed": len(mutants[: self.max_mutants]),
            },
        )


    def run_gate_for_files(
        self,
        repo_path: Path,
        old_code_by_file: Dict[str, str],
        new_code_by_file: Dict[str, str],
    ) -> MutationGateResult:
        """Gates every changed file of a task's diff and aggregates the
        result (overall score = total killed / total executed across files).

        Used by the runtime wiring: mutates ONLY the files the Dev patch
        touched, restoring each file exactly after the run.
        """
        total_killed = 0
        total_exec = 0
        all_survivors: List[str] = []
        for f, new_code in new_code_by_file.items():
            old_code = old_code_by_file.get(f, "")
            res = self.run_gate(repo_path, old_code, new_code, f)
            total_killed += res.killed
            total_exec += res.total
            all_survivors.extend(res.surviving_mutants)
            if not res.success:
                return MutationGateResult(
                    success=False,
                    mutation_score=res.mutation_score,
                    threshold=self.threshold,
                    killed=res.killed,
                    total=res.total,
                    surviving_mutants=all_survivors,
                    details={"failed_file": f, **res.details},
                )
        score = (total_killed / total_exec) if total_exec else 1.0
        return MutationGateResult(
            success=score >= self.threshold,
            mutation_score=score,
            threshold=self.threshold,
            killed=total_killed,
            total=total_exec,
            surviving_mutants=all_survivors,
            details={"files": list(new_code_by_file.keys())},
        )


def run_gate_for_task(
    repo_path: Path,
    task,
    old_code_by_file: Dict[str, str],
    new_code_by_file: Dict[str, str],
    test_runner: Callable[[Path], Tuple[int, str]],
    threshold: float = 0.70,
    max_mutants: int = 40,
) -> MutationGateResult:
    """Convenience wrapper used by the runtime wiring: gates every changed
        .cs file of a task's diff and aggregates the result (overall score =
        total killed / total executed across all files)."""
    gate = ASTMutationGate(test_runner=test_runner, threshold=threshold, max_mutants=max_mutants)
    total_killed = 0
    total_exec = 0
    all_survivors: List[str] = []
    for f, new_code in new_code_by_file.items():
        old_code = old_code_by_file.get(f, "")
        res = gate.run_gate(repo_path, old_code, new_code, f)
        total_killed += res.killed
        total_exec += res.total
        all_survivors.extend(res.surviving_mutants)
        if not res.success:
            return MutationGateResult(
                success=False,
                mutation_score=res.mutation_score,
                threshold=threshold,
                killed=res.killed,
                total=res.total,
                surviving_mutants=all_survivors,
                details={"failed_file": f, **res.details},
            )
    score = (total_killed / total_exec) if total_exec else 1.0
    return MutationGateResult(
        success=score >= threshold,
        mutation_score=score,
        threshold=threshold,
        killed=total_killed,
        total=total_exec,
        surviving_mutants=all_survivors,
        details={"files": list(new_code_by_file.keys())},
    )
