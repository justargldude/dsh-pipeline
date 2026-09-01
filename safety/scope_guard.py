import fnmatch
import re
from pathlib import Path
from typing import Optional
from task.schema import TaskDefinition, PatchProposal
from safety.policy import SafetyPolicy, SessionBudgetTracker
from safety.ast_guard import ASTGuard, ASTViolationError


class ScopeViolationError(Exception):
    pass


class ScopeGuard:
    SUSPICIOUS_PATTERNS = [
        (re.compile(r"#if\s+false", re.IGNORECASE), "Detected '#if false' code exclusion"),
        (re.compile(r"throw\s+new\s+NotImplementedException", re.IGNORECASE), "Detected 'throw new NotImplementedException' placeholder"),
        (re.compile(r"//\s*TODO.*stub", re.IGNORECASE), "Detected dummy stub marker"),
    ]

    def __init__(
        self,
        policy: Optional[SafetyPolicy] = None,
        session_tracker: Optional[SessionBudgetTracker] = None,
    ):
        self.policy = policy or SafetyPolicy()
        self.session_tracker = session_tracker
        self.ast_guard = ASTGuard()

    def validate(self, task: TaskDefinition, proposal: PatchProposal, repo_path: Path):
        repo_root = repo_path.resolve()
        total_added = 0
        total_deleted = 0

        for file_patch in proposal.patches:
            rel_file = file_patch.file.replace("\\", "/").lstrip("/")
            target_path = (repo_root / rel_file).resolve()

            # 1. Path traversal & root escape check
            try:
                target_path.relative_to(repo_root)
            except ValueError:
                raise ScopeViolationError(f"Path traversal detected: '{file_patch.file}' escapes repository root.")

            # 2. Forbidden files pattern check
            for pattern in self.policy.forbidden_file_patterns:
                if fnmatch.fnmatch(rel_file, pattern) or fnmatch.fnmatch(target_path.name, pattern):
                    raise ScopeViolationError(
                        f"Forbidden file access detected: '{rel_file}' matches restricted pattern '{pattern}'"
                    )

            # 3. Allowed files check
            normalized_allowed = [f.replace("\\", "/").lstrip("/") for f in task.allowed_files]
            if rel_file not in normalized_allowed:
                raise ScopeViolationError(
                    f"File '{rel_file}' is not in allowed_files: {task.allowed_files}"
                )

            # 4. Symlink security
            if target_path.is_symlink():
                real_target = target_path.resolve()
                try:
                    real_target.relative_to(repo_root)
                except ValueError:
                    raise ScopeViolationError(f"Symlink '{rel_file}' points outside repository root.")

            # 5. Hunks count limit
            if len(file_patch.hunks) > self.policy.max_hunks_per_file:
                raise ScopeViolationError(
                    f"File '{rel_file}' has too many hunks ({len(file_patch.hunks)} > {self.policy.max_hunks_per_file})"
                )

            # 6. Diff calculation & Anti-bypass text scanning
            orig_content = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
            simulated_content = orig_content

            for hunk in file_patch.hunks:
                old_lines = hunk.old_text.splitlines() if hunk.old_text else []
                new_lines = hunk.new_text.splitlines() if hunk.new_text else []

                total_deleted += len(old_lines)
                total_added += len(new_lines)

                # Scan new_text for anti-bypass patterns
                for pattern, msg in self.SUSPICIOUS_PATTERNS:
                    if pattern.search(hunk.new_text):
                        raise ScopeViolationError(f"Anti-bypass violation in '{rel_file}': {msg}")

                if hunk.old_text:
                    simulated_content = simulated_content.replace(hunk.old_text, hunk.new_text, 1)
                else:
                    simulated_content += hunk.new_text

            # 7. AST Guard for C# files
            if rel_file.endswith(".cs"):
                try:
                    self.ast_guard.validate_csharp_transition(orig_content, simulated_content, file_path=rel_file)
                except ASTViolationError as e:
                    raise ScopeViolationError(f"AST Guard rejection: {str(e)}")

        # 8. Task-level diff budget
        if total_added > task.max_lines_added:
            raise ScopeViolationError(
                f"Lines added ({total_added}) exceeded task budget ({task.max_lines_added})"
            )
        if total_deleted > task.max_lines_deleted:
            raise ScopeViolationError(
                f"Lines deleted ({total_deleted}) exceeded task budget ({task.max_lines_deleted})"
            )

        # 9. Session / DAG cumulative budget check
        if self.session_tracker:
            try:
                self.session_tracker.check_and_add(total_added, total_deleted)
            except ValueError as e:
                raise ScopeViolationError(str(e))
