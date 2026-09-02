from pathlib import Path
from typing import Dict, List, Optional
import io
from unidiff import PatchSet
from task.schema import PatchProposal, FilePatch, PatchHunk
from safety.patch_engine import (
    PatchValidationError,
    apply_hunks,
    apply_file_patch,
    normalize_repo_path,
    validate_and_simulate_proposal,
)


class PatchValidator:
    @staticmethod
    def parse_unified_diff(diff_str: str) -> PatchProposal:
        """Parses a standard unified diff string into structured PatchProposal."""
        try:
            patch_set = PatchSet(io.StringIO(diff_str))
            file_patches: List[FilePatch] = []

            for patched_file in patch_set:
                target_file = patched_file.path
                hunks: List[PatchHunk] = []

                for h in patched_file:
                    old_lines = [line.value for line in h if line.is_removed]
                    new_lines = [line.value for line in h if line.is_added]
                    hunks.append(
                        PatchHunk(
                            old_text="".join(old_lines),
                            new_text="".join(new_lines),
                        )
                    )

                file_patches.append(FilePatch(file=target_file, hunks=hunks))

            return PatchProposal(patches=file_patches, reason="Parsed from Unified Diff")
        except Exception as e:
            raise PatchValidationError(f"Failed to parse unified diff: {str(e)}")

    @staticmethod
    def validate_proposal(proposal: PatchProposal, repo_path: Path) -> Dict[str, str]:
        """Validates the proposal against the repository state and returns the simulated file mapping."""
        return validate_and_simulate_proposal(proposal, repo_path)

