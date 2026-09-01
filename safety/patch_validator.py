from pathlib import Path
from task.schema import PatchProposal


class PatchValidationError(Exception):
    pass


class PatchValidator:
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
                        temp_content = temp_content.replace(hunk.old_text, hunk.new_text, 1)
            else:
                for hunk in file_patch.hunks:
                    if hunk.old_text:
                        raise PatchValidationError(
                            f"Cannot match old_text on non-existent file: {file_patch.file}"
                        )
