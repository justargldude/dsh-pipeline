import re
from pathlib import Path
from task.schema import TaskDefinition, PatchProposal


class ScopeViolationError(Exception):
    pass


class ScopeGuard:
    SUSPICIOUS_PATTERNS = [
        (re.compile(r"#if\s+false", re.IGNORECASE), "Detected '#if false' code exclusion"),
        (re.compile(r"throw\s+new\s+NotImplementedException", re.IGNORECASE), "Detected 'throw new NotImplementedException' placeholder"),
        (re.compile(r"//\s*TODO.*stub", re.IGNORECASE), "Detected dummy stub marker"),
    ]

    @classmethod
    def validate(cls, task: TaskDefinition, proposal: PatchProposal, repo_path: Path):
        repo_root = repo_path.resolve()
        total_added = 0
        total_deleted = 0

        for file_patch in proposal.patches:
            rel_file = file_patch.file.replace("\\", "/").lstrip("/")
            target_path = (repo_root / rel_file).resolve()

            try:
                target_path.relative_to(repo_root)
            except ValueError:
                raise ScopeViolationError(f"Path traversal detected: '{file_patch.file}' escapes repository root.")

            normalized_allowed = [f.replace("\\", "/").lstrip("/") for f in task.allowed_files]
            if rel_file not in normalized_allowed:
                raise ScopeViolationError(
                    f"File '{rel_file}' is not in allowed_files: {task.allowed_files}"
                )

            if target_path.is_symlink():
                real_target = target_path.resolve()
                try:
                    real_target.relative_to(repo_root)
                except ValueError:
                    raise ScopeViolationError(f"Symlink '{rel_file}' points outside repository root.")

            for hunk in file_patch.hunks:
                old_lines = hunk.old_text.splitlines() if hunk.old_text else []
                new_lines = hunk.new_text.splitlines() if hunk.new_text else []

                total_deleted += len(old_lines)
                total_added += len(new_lines)

                for pattern, msg in cls.SUSPICIOUS_PATTERNS:
                    if pattern.search(hunk.new_text):
                        raise ScopeViolationError(f"Anti-bypass violation in '{rel_file}': {msg}")

        if total_added > task.max_lines_added:
            raise ScopeViolationError(
                f"Lines added ({total_added}) exceeded task limit ({task.max_lines_added})"
            )
        if total_deleted > task.max_lines_deleted:
            raise ScopeViolationError(
                f"Lines deleted ({total_deleted}) exceeded task limit ({task.max_lines_deleted})"
            )
