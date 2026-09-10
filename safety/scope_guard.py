import difflib
import fnmatch
import re
from pathlib import Path
from typing import Optional
from task.schema import TaskDefinition, PatchProposal
from safety.policy import SafetyPolicy, SessionBudgetTracker
from safety.ast_guard import ASTGuard, ASTViolationError
from safety.patch_engine import normalize_repo_path, apply_hunks


class ScopeViolationError(Exception):
    pass


# Anti-reward-hacking v2.3 Checkpoint 5: patterns identifying TEST files that
# a Dev-agent task must never write or delete. Only QA-authored tasks
# (role='qa', e.g. red-test authoring) may touch these.
#
# The authoritative list lives on SafetyPolicy.dev_forbidden_test_file_patterns
# (policy layer, configurable); these module constants are the DEFAULTS the
# policy ships with and are kept for backwards compatibility with code that
# imported them directly.
DEV_FORBIDDEN_TEST_FILE_PATTERNS = [
    "*Test.cs",
    "*Tests.cs",
    "*Test.java",
    "*Tests.java",
    "*Test.ts",
    "*Tests.ts",
    "*Test.js",
    "*Tests.js",
    "test_*.py",
    "*_test.py",
    "*_test.go",
    "*_test.dart",
    "*.test.js",
    "*.spec.js",
    "*.test.ts",
    "*.spec.ts",
]

# Directory patterns: anything INSIDE such a directory is a test file.
DEV_FORBIDDEN_TEST_DIR_PATTERNS = [
    "*Tests/*",
    "*Test/*",
    "tests/*",
    "test/*",
    "__tests__/*",
]


def is_test_file(path: str) -> bool:
    """Return True if `path` identifies a test file (case-insensitive).

    Contract:
    - Python: basename matches fnmatch "test_*.py" or "*_test.py",
      or basename == "conftest.py".
    - C#/Java/Kotlin/PHP: basename matches "*test.<ext>", "*tests.<ext>",
      "*spec.<ext>", "*specs.<ext>" for ext in .cs/.java/.kt/.php,
      OR basename starts with "test".
    - JS/TS/Go: basename contains ".test." or ".spec.",
      or ends with "_test.go".
    - Directories: any path component equal to "tests", "test",
      "__tests__", "spec", "specs" (case-insensitive) -> test file.

    Total and deterministic: any string -> bool, never raises.
    Documented EXCEPTION: basename "test_runner.py" (case-insensitive) is
    explicitly NOT a test file — it is a production helper — even though it
    lexically matches fnmatch "test_*.py". Implementations special-case
    exactly "test_runner.py" as non-test per the authoritative QA contract.
    """
    try:
        if path is None:
            return False
        s = str(path)
    except Exception:
        return False
    if not s or not s.strip():
        return False
    try:
        normalized = s.replace("\\", "/")
        lowered = normalized.lower()
        parts = [p for p in lowered.split("/") if p != ""]
        if not parts:
            return False
        basename = parts[-1]
        # Documented exception: production helper, never a test file.
        if basename == "test_runner.py":
            return False
        # Directories: any path component equal to "tests", "test", "__tests__", "spec", "specs"
        test_dirs = {"tests", "test", "__tests__", "spec", "specs"}
        for comp in parts:
            if comp in test_dirs:
                return True
        # Python
        if (
            fnmatch.fnmatch(basename, "test_*.py")
            or fnmatch.fnmatch(basename, "*_test.py")
            or basename == "conftest.py"
        ):
            return True
        # C#/Java/Kotlin/PHP
        for ext in (".cs", ".java", ".kt", ".php"):
            if basename.endswith(ext):
                if (
                    fnmatch.fnmatch(basename, "*test" + ext)
                    or fnmatch.fnmatch(basename, "*tests" + ext)
                    or fnmatch.fnmatch(basename, "*spec" + ext)
                    or fnmatch.fnmatch(basename, "*specs" + ext)
                ):
                    return True
                if basename.startswith("test"):
                    return True
                break
        # JS/TS/Go
        if ".test." in basename or ".spec." in basename:
            return True
        if basename.endswith("_test.go"):
            return True
        return False
    except Exception:
        return False


def _is_dev_forbidden_test_file(rel_file: str, patterns: Optional[list] = None) -> Optional[str]:
    """Returns the matched pattern if `rel_file` is a test file, else None.

    Pure path-pattern matching (fnmatch over the basename, the full relative
    path, and every path component), independent of filesystem state.

    `patterns` defaults to the module-level DEV_FORBIDDEN_* constants; the
    ScopeGuard passes its policy's configured list so policy is the single
    source of truth.
    """
    file_pats, dir_pats = DEV_FORBIDDEN_TEST_FILE_PATTERNS, DEV_FORBIDDEN_TEST_DIR_PATTERNS
    if patterns is not None:
        # Caller-supplied unified list: dir patterns are those ending in "/*".
        file_pats = [p for p in patterns if not p.endswith("/*")]
        dir_pats = [p for p in patterns if p.endswith("/*")]

    lowered = rel_file.lower()
    components = lowered.split("/")
    basename = components[-1] if components else lowered

    # Documented exception (mirrors is_test_file): the production helper
    # "test_runner.py" is never a test file, even though it lexically
    # matches "test_*.py".
    if basename == "test_runner.py":
        return None

    for pattern in file_pats:
        if (
            fnmatch.fnmatch(lowered, pattern.lower())
            or fnmatch.fnmatch(basename, pattern.lower())
        ):
            return pattern
    for pattern in dir_pats:
        if fnmatch.fnmatch(lowered, pattern.lower()):
            return pattern
        # A file inside e.g. "MyTests/" matches component-wise too:
        for comp in components[:-1]:
            if fnmatch.fnmatch(comp, pattern.lower().rstrip("/*")):
                return pattern
    return None


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

    def validate(
        self,
        task: TaskDefinition,
        proposal: PatchProposal,
        repo_path: Optional[Path] = None,
        reservation_id: Optional[str] = None,
        repo_root: Optional[Path] = None,
    ):
        root = repo_root if repo_root is not None else repo_path
        if root is None:
            raise ValueError("repo_path or repo_root must be provided")
        repo_root = root.resolve()

        total_added = 0
        total_deleted = 0

        # Pre-normalize allowed files
        normalized_allowed = [normalize_repo_path(f) for f in task.allowed_files]

        for file_patch in proposal.patches:
            try:
                rel_file = normalize_repo_path(file_patch.file)
            except Exception as e:
                raise ScopeViolationError(f"Invalid path in patch: {str(e)}")

            if not rel_file:
                raise ScopeViolationError("Empty file path in patch proposal.")

            unresolved_path = repo_root / rel_file

            # 1. Symlink and Path Traversal inspection on unresolved chain
            curr = repo_root
            for part in rel_file.split("/"):
                curr = curr / part
                if curr.is_symlink():
                    try:
                        resolved_part = curr.resolve()
                        resolved_part.relative_to(repo_root)
                    except ValueError:
                        raise ScopeViolationError(
                            f"Symlink '{part}' in '{rel_file}' points outside repository root."
                        )

            # 2. Resolved path root escape check
            target_path = unresolved_path.resolve()
            try:
                target_path.relative_to(repo_root)
            except ValueError:
                raise ScopeViolationError(
                    f"Path traversal / symlink escape detected: '{file_patch.file}' escapes repository root."
                )

            # 3. Forbidden files pattern check (checks full relative path, components, and target name)
            for pattern in self.policy.forbidden_file_patterns:
                if (
                    fnmatch.fnmatch(rel_file, pattern)
                    or fnmatch.fnmatch(target_path.name, pattern)
                    or any(fnmatch.fnmatch(p, pattern) for p in rel_file.split("/"))
                ):
                    raise ScopeViolationError(
                        f"Forbidden file access detected: '{rel_file}' matches restricted pattern '{pattern}'"
                    )

            # 3.5 Anti-reward-hacking v2.3 Checkpoint 5: a Dev-agent task must
            # never write/delete a test file (only QA tasks may, e.g. red-test
            # authoring). This blocks the Dev agent from weakening the tests
            # 3.5 Anti-reward-hacking: ban Dev from writing test files.
            # MUST run BEFORE the allowed_files check (step 4) so even allowed test
            # files are rejected. New test file creation is also blocked.
            # QA-authored tasks (role='qa') are exempt for red-test authoring.
            if getattr(task, "role", "dev") != "qa":
                custom_patterns = getattr(self.policy, "dev_forbidden_test_file_patterns", None)
                default_patterns = getattr(SafetyPolicy(), "dev_forbidden_test_file_patterns", None)
                if custom_patterns is not None and custom_patterns != default_patterns:
                    matched = _is_dev_forbidden_test_file(
                        rel_file,
                        patterns=custom_patterns,
                    )
                    if matched:
                        raise ScopeViolationError(
                            f"Forbidden test file access for Dev-agent task: '{rel_file}' "
                            f"matches test file pattern '{matched}'. Test files may only "
                            f"be written by QA-authored tasks (role='qa')."
                        )
                elif (
                    _is_dev_forbidden_test_file(
                        rel_file,
                        patterns=getattr(self.policy, "dev_forbidden_test_file_patterns", None),
                    )
                    or is_test_file(rel_file)
                ):
                    raise ScopeViolationError(
                        f"Forbidden test file access for Dev-agent task: '{rel_file}' is a test file. "
                        "Test files may only be written by QA-authored tasks (role='qa')."
                    )

            # 4. Allowed files check
            if rel_file not in normalized_allowed:
                raise ScopeViolationError(
                    f"File '{rel_file}' is not in allowed_files: {task.allowed_files}"
                )

            # If the path is a symlink, the target file must also be authorized
            if unresolved_path.is_symlink():
                resolved_rel = normalize_repo_path(str(target_path.relative_to(repo_root)))
                if resolved_rel not in normalized_allowed:
                    raise ScopeViolationError(
                        f"Symlink '{rel_file}' points to unauthorized file '{resolved_rel}' not in allowed_files: {task.allowed_files}"
                    )

            # 5. Hunks count limit
            if len(file_patch.hunks) > self.policy.max_hunks_per_file:
                raise ScopeViolationError(
                    f"File '{rel_file}' has too many hunks ({len(file_patch.hunks)} > {self.policy.max_hunks_per_file})"
                )

            # 6. Diff calculation & Anti-bypass text scanning
            is_new = not target_path.exists()
            orig_content = target_path.read_text(encoding="utf-8") if not is_new else ""
            base_content = orig_content if not is_new else None

            for hunk in file_patch.hunks:
                # Scan new_text for anti-bypass patterns
                for pattern, msg in self.SUSPICIOUS_PATTERNS:
                    if pattern.search(hunk.new_text):
                        raise ScopeViolationError(f"Anti-bypass violation in '{rel_file}': {msg}")

            # Authoritative simulation for downstream analysis (e.g. AST Guard)
            try:
                simulated_content = apply_hunks(
                    base_content=base_content,
                    hunks=file_patch.hunks,
                    is_new_file=is_new,
                    file_path=rel_file,
                )
            except Exception:
                simulated_content = orig_content
                for hunk in file_patch.hunks:
                    if hunk.old_text and hunk.old_text in simulated_content:
                        simulated_content = simulated_content.replace(hunk.old_text, hunk.new_text, 1)
                    else:
                        simulated_content += hunk.new_text

            # Bug B: Count actual lines added and deleted from unified diff
            orig_lines = orig_content.splitlines()
            sim_lines = simulated_content.splitlines()
            diff_lines = list(difflib.unified_diff(orig_lines, sim_lines, lineterm=""))
            for line in diff_lines[2:]:
                if line.startswith("-"):
                    total_deleted += 1
                elif line.startswith("+"):
                    total_added += 1

            # 7. AST Guard for C# files (with target_symbols boundary enforcement)
            if rel_file.endswith(".cs"):
                try:
                    self.ast_guard.validate_csharp_transition(
                        orig_content,
                        simulated_content,
                        file_path=rel_file,
                        target_symbols=task.target_symbols,
                    )
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
                if reservation_id:
                    self.session_tracker.reserve(reservation_id, total_added, total_deleted)
                else:
                    self.session_tracker.check_and_add(total_added, total_deleted)
            except ValueError as e:
                raise ScopeViolationError(str(e))

    validate_pre_apply = validate


