from pathlib import Path
from typing import List, Optional
import io
from unidiff import PatchSet
from task.schema import PatchProposal, FilePatch, PatchHunk


class PatchValidationError(Exception):
    pass


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
    def validate_proposal(proposal: PatchProposal, repo_path: Path):
        if not proposal.patches:
            raise PatchValidationError("Patch proposal contains no file patches.")

        for file_patch in proposal.patches:
            if not file_patch.file:
                raise PatchValidationError("File path in patch cannot be empty.")

            target_path = (repo_path / file_patch.file.lstrip("/")).resolve()

            if not file_patch.hunks:
                raise PatchValidationError(f"File patch for '{file_patch.file}' has no hunks.")

            if target_path.exists():
                file_content = target_path.read_text(encoding="utf-8")
                temp_content = file_content
                for hunk in file_patch.hunks:
                    if hunk.old_text:
                        if hunk.old_text not in temp_content:
                            raise PatchValidationError(
                                f"Hunk old_text not found in target file: {file_patch.file}\n"
                                f"Snippet searched: {hunk.old_text[:100]}..."
                            )
                        # Check unambiguous match if old_text is short
                        if len(hunk.old_text.strip()) > 20 and temp_content.count(hunk.old_text) > 1:
                            raise PatchValidationError(
                                f"Ambiguous hunk match: old_text appears multiple times in {file_patch.file}"
                            )
                        temp_content = temp_content.replace(hunk.old_text, hunk.new_text, 1)
            else:
                for hunk in file_patch.hunks:
                    if hunk.old_text:
                        raise PatchValidationError(
                            f"Cannot match old_text on non-existent file: {file_patch.file}"
                        )
