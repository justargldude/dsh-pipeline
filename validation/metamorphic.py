"""Metamorphic Testing (v2.3 Stage 2 Phase D2).

Anti-reward-hacking gate that runs the SAME patch against semantic-preserving
variants of the test suite:
- Reseed: Random(42) -> Random(43), random.seed(2024) -> 2025. A suite that
  only passes with one specific seed is either flaky or hardcoded against
  the generator's exact sequence.
- Rename: rename test-local identifiers (input -> input_v2). A patch that
  only passes with specific variable names is content-sniffing.
- Reorder: reverse the order of test functions/methods. A patch that only
  passes in one order is order-dependent (shared state leaks).

Rule: if the ORIGINAL suite passes but any variant FAILS, the task is
rejected. Original fails => skipped (meaningless on a red suite).
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


@dataclass
class MetamorphicResult:
    success: bool
    skipped: bool = False
    skipped_variants: int = 0
    failed_variant: Optional[str] = None
    details: Dict = field(default_factory=dict)


# ==============================================================================
# Deterministic semantic-preserving transforms (each returns new code; the
# gate detects "changed" by comparing with the original text)
# ==============================================================================

# Single non-overlapping pattern covering all seeded-generator call sites:
# C# new Random(42), Python random.Random(42) / random.seed(42) / rng.seed(42).
_SEEDED_RANDOM_RE = re.compile(
    r"(?P<head>(?:new\s+Random)|(?:random\.Random)|(?:random\.seed)|(?:\.seed))"
    r"(?P<ws1>\s*\(\s*)(?P<seed>-?\d+)(?P<ws2>\s*\))"
)


def reseed_random_literals(code: str) -> str:
    """Reseeds integer literals passed to random generators: +1 on every
    literal seed. Unseeded and non-literal (variable) seeds are left alone."""
    def _sub(m):
        return m.group("head") + m.group("ws1") + str(int(m.group("seed")) + 1) + m.group("ws2")
    return _SEEDED_RANDOM_RE.sub(_sub, code)


_PY_FUNC_SPLIT_RE = re.compile(
    r"(?m)^def\s+(test_\w+)\s*\((?:[^)\n]*)\)(?:\s*->\s*[^:\n]+)?:"
)


def reverse_test_order(code: str) -> str:
    """Reverses the order of test functions/methods in a test file."""
    # Python: def test_*(): ... blocks (prefix preserved, blocks reversed)
    matches = list(_PY_FUNC_SPLIT_RE.finditer(code))
    if len(matches) >= 2:
        prefix = code[: matches[0].start()]
        blocks = []
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(code)
            blocks.append(code[m.start() : end])
        return prefix + "".join(reversed(blocks))
    # C#: [Test] / [TestMethod] / [Fact] marked methods
    test_attr = re.compile(r"(?m)^\s*\[(?:Test|TestMethod|Fact)\]")
    markers = list(test_attr.finditer(code))
    if len(markers) >= 2:
        prefix = code[: markers[0].start()]
        blocks = []
        for i, m in enumerate(markers):
            end = markers[i + 1].start() if i + 1 < len(markers) else len(code)
            blocks.append(code[m.start() : end])
        return prefix + "".join(reversed(blocks))
    return code


def rename_test_locals(code: str) -> str:
    """Renames common test-local identifier names with a deterministic
    suffix (_v2), updating every reference in the file.

    Target vocabulary is the conventional test locals: input, output,
    expected, actual, result, value (whole-word matches only).
    """
    out = code
    for nm in ("input", "output", "expected", "actual", "result", "value"):
        pat = re.compile(rf"\b{nm}\b(?!_v2\b)(?!\w)")
        out = pat.sub(f"{nm}_v2", out)
    return out


# ==============================================================================
# Gate
# ==============================================================================

class MetamorphicGate:
    """Runs the original suite, then each semantic-preserving variant.

    A variant byte-identical to the original (nothing to transform) is
    SKIPPED — identity variants prove nothing and must not fail the gate.
    """

    VARIANTS = (
        ("reseed", reseed_random_literals),
        ("rename", rename_test_locals),
        ("reorder", reverse_test_order),
    )

    def __init__(self, test_runner: Callable[[Path], Tuple[int, str]]):
        self.test_runner = test_runner

    def run_gate(self, repo_path: Path, test_file: str) -> MetamorphicResult:
        target = Path(repo_path) / test_file
        original = target.read_text(encoding="utf-8")

        rc, out = self.test_runner(Path(repo_path))
        if rc != 0:
            return MetamorphicResult(
                success=True,
                skipped=True,
                details={"note": "original suite failing (red state); variants not run"},
            )

        skipped_variants = 0
        try:
            for name, fn in self.VARIANTS:
                variant_code = fn(original)
                if variant_code == original:
                    skipped_variants += 1
                    continue
                target.write_text(variant_code, encoding="utf-8")
                vrc, vout = self.test_runner(Path(repo_path))
                if vrc != 0:
                    return MetamorphicResult(
                        success=False,
                        skipped=False,
                        skipped_variants=skipped_variants,
                        failed_variant=name,
                        details={
                            "test_file": test_file,
                            "variant_output": vout[:500],
                        },
                    )
        finally:
            # Deterministic restore of the original test file.
            target.write_text(original, encoding="utf-8")

        return MetamorphicResult(
            success=True,
            skipped=False,
            skipped_variants=skipped_variants,
            failed_variant=None,
            details={"test_file": test_file},
        )
