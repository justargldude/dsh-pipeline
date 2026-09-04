import logging
import os
import re
import uuid
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Sequence, Set, Tuple

from task.schema import FilePatch, PatchHunk, PatchProposal

logger = logging.getLogger("dsh.safety.patch")


class PatchValidationError(Exception):
    """Raised when patch structure, match semantics, or simulation fails."""
    pass


DEFAULT_MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
DEFAULT_MAX_PATCH_SIZE_BYTES = 1 * 1024 * 1024  # 1 MB
DEFAULT_MAX_FILES_IN_PROPOSAL = 50


def detect_newline_style(content: str) -> str:
    """Detects if content predominantly uses Windows CRLF (\r\n) or Unix LF (\n)."""
    if "\r\n" in content:
        return "\r\n"
    return "\n"


def atomic_write_file(
    file_path: Path,
    content: str,
    encoding: str = "utf-8",
    preserve_newline: bool = True,
) -> None:
    """Atomically writes content to a file with newline preservation and fsync durability."""
    target = file_path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    newline_style = "\n"
    if preserve_newline and target.exists():
        try:
            raw_bytes = target.read_bytes()
            # Detect CRLF in original raw bytes
            if b"\r\n" in raw_bytes:
                newline_style = "\r\n"
        except Exception:
            pass

    # Normalize newlines to match detected style
    if newline_style == "\r\n":
        normalized_content = content.replace("\r\n", "\n").replace("\n", "\r\n")
    else:
        normalized_content = content.replace("\r\n", "\n")

    temp_path = target.parent / f".{target.name}.tmp.{uuid.uuid4().hex}"
    try:
        with open(temp_path, "wb") as f:
            f.write(normalized_content.encode(encoding))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, target)
    except Exception as e:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
        raise PatchValidationError(f"Failed to atomically write file {target}: {e}")


def normalize_repo_path(file_path: str) -> str:
    """Normalizes a relative repository file path.
    
    Enforces security invariants:
    1. Rejects control characters (\x00..\x1f, \x7f).
    2. Rejects absolute paths (leading '/', '\\', Windows drive letters, UNC paths).
    3. Normalizes directory separators ('\\' -> '/') and collapses redundant slashes.
    4. Resolves relative '.' and '..' components, strictly rejecting root traversal.
    5. Rejects any path where any component is '.git' (case-insensitive).
    """
    if not file_path or not file_path.strip():
        return ""

    raw = file_path.strip()

    # 1. Control character rejection
    for ch in raw:
        if ord(ch) < 32 or ord(ch) == 127:
            raise PatchValidationError(f"Invalid control character in path: {file_path!r}")

    # 2. Absolute path rejection
    if raw.startswith(("/", "\\")):
        raise PatchValidationError(f"Absolute paths are forbidden: '{file_path}'")
    if re.match(r"^[a-zA-Z]:", raw):
        raise PatchValidationError(f"Windows drive absolute paths are forbidden: '{file_path}'")

    # 3. Separator normalization & duplicate slash collapsing
    cleaned = raw.replace("\\", "/")
    cleaned = re.sub(r"/+", "/", cleaned).strip("/")

    # 4. Resolve components and enforce traversal & .git protection
    parts = []
    for part in cleaned.split("/"):
        if part in ("", "."):
            continue
        if part.lower() == ".git":
            raise PatchValidationError(f"Access to '.git' path component is forbidden: '{file_path}'")
        if part == "..":
            if not parts:
                raise PatchValidationError(f"Path traversal detected: '{file_path}' escapes repository root.")
            parts.pop()
        else:
            parts.append(part)

    if not parts:
        return ""
    return "/".join(parts)


def _locate_unique_match(content: str, old_text: str, file_path: str, hunk_idx: int) -> int:
    """Locates the unique non-overlapping occurrence of old_text in content.

    Single-pass replacement of the former count()+replace() double scan:
    - `find` for the first occurrence; a second `find` starting at
      pos + len(old_text) detects any further NON-overlapping occurrence
      (using pos+1 would incorrectly count self-overlapping patterns like
      "aa" inside "aaa").
    - Raises PatchValidationError with the SAME message format as before:
      "Hunk {idx} old_text not found..." (with 100-char snippet) when absent,
      "Ambiguous hunk match: old_text appears {N} times..." when multiple.
      The total count is computed via str.count() only on the error path,
      to keep the exact legacy message.

    An empty old_text is always rejected as ambiguous (matches legacy
    str.count("") == len+1 behavior).
    """
    pos = content.find(old_text)
    if pos == -1:
        snippet = old_text[:100] + ("..." if len(old_text) > 100 else "")
        raise PatchValidationError(
            f"Hunk {hunk_idx} old_text not found in target file: {file_path}\n"
            f"Snippet searched: {snippet}"
        )

    # Detect a second NON-overlapping occurrence only.
    if not old_text or content.find(old_text, pos + len(old_text)) != -1:
        count = content.count(old_text)
        raise PatchValidationError(
            f"Ambiguous hunk match: old_text appears {count} times in {file_path} (hunk {hunk_idx})"
        )

    return pos


def apply_hunks(
    base_content: Optional[str],
    hunks: Sequence[PatchHunk],
    is_new_file: bool = False,
    file_path: str = "",
) -> str:
    """The single authoritative function for applying FilePatch hunks to file content.
    
    Enforces the following invariants:
    1. Empty hunks list is rejected.
    2. If is_new_file (or base_content is None):
       - First hunk MUST have old_text == "" (file creation).
       - First hunk MUST have non-empty new_text (cannot create empty no-op file).
       - Initial content becomes hunks[0].new_text.
       - Subsequent hunks must have non-empty old_text matching intermediate content uniquely.
    3. If existing file (not is_new_file and base_content is not None):
       - Any hunk with old_text == "" is REJECTED (no silent append on existing files).
       - For each hunk:
         - old_text == new_text is REJECTED (no-op hunk).
         - old_text must appear EXACTLY ONCE in current content.
           - 0 occurrences -> REJECT (old_text not found).
           - >1 occurrences -> REJECT (ambiguous match, regardless of length).
         - Replaces that unique occurrence with new_text.
       - If final content == base_content, REJECT (net no-op file patch).
    
    Returns the resulting file content as a string.
    """
    if not hunks:
        raise PatchValidationError(f"File patch for '{file_path}' has no hunks.")

    if is_new_file or base_content is None:
        # File creation mode
        first_hunk = hunks[0]
        if first_hunk.old_text != "":
            raise PatchValidationError(
                f"Cannot match old_text on non-existent file: {file_path}"
            )
        if not first_hunk.new_text:
            raise PatchValidationError(
                f"File creation for '{file_path}' produces empty content (no-op patch)."
            )

        current_content = first_hunk.new_text

        # Subsequent hunks operate sequentially on the created content
        for idx, hunk in enumerate(hunks[1:], start=2):
            if hunk.old_text == "":
                raise PatchValidationError(
                    f"Empty old_text is not allowed on existing file content in hunk {idx} for '{file_path}'"
                )
            if hunk.old_text == hunk.new_text:
                raise PatchValidationError(
                    f"Hunk {idx} is a no-op (old_text == new_text) for '{file_path}'"
                )

            pos = _locate_unique_match(current_content, hunk.old_text, file_path, idx)
            current_content = (
                current_content[:pos] + hunk.new_text + current_content[pos + len(hunk.old_text):]
            )

        return current_content

    else:
        # Existing file modification mode
        current_content = base_content

        for idx, hunk in enumerate(hunks, start=1):
            if hunk.old_text == "":
                raise PatchValidationError(
                    f"Empty old_text is not allowed on existing file: {file_path}"
                )
            if hunk.old_text == hunk.new_text:
                raise PatchValidationError(
                    f"Hunk {idx} is a no-op (old_text == new_text) for '{file_path}'"
                )

            pos = _locate_unique_match(current_content, hunk.old_text, file_path, idx)
            current_content = (
                current_content[:pos] + hunk.new_text + current_content[pos + len(hunk.old_text):]
            )

        if current_content == base_content:
            raise PatchValidationError(
                f"File patch for '{file_path}' produces no changes (no-op patch)."
            )

        return current_content


def apply_file_patch(
    base_content: Optional[str],
    file_patch: FilePatch,
    is_new_file: bool = False,
) -> str:
    """Convenience wrapper around apply_hunks for a FilePatch object."""
    return apply_hunks(
        base_content=base_content,
        hunks=file_patch.hunks,
        is_new_file=is_new_file,
        file_path=file_patch.file,
    )


def validate_and_simulate_proposal(
    proposal: PatchProposal,
    repo_path: Path,
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
    max_patch_size_bytes: int = DEFAULT_MAX_PATCH_SIZE_BYTES,
    max_files: int = DEFAULT_MAX_FILES_IN_PROPOSAL,
) -> Dict[str, str]:
    """Validates an entire PatchProposal and returns a dictionary mapping normalized
    relative file paths to their simulated final content.
    
    Enforces:
    1. Non-empty proposal.patches and file count limits.
    2. Path normalization & traversal rejection (cannot escape repo root).
    3. Exactly ONE FilePatch per normalized file path (duplicate entries rejected).
    4. Exact unique match, sequential hunk application, and no-op rejection via apply_hunks.
    5. File size and patch size limits.
    """
    if not proposal.patches:
        raise PatchValidationError("Patch proposal contains no file patches.")

    if len(proposal.patches) > max_files:
        raise PatchValidationError(
            f"Patch proposal contains too many files ({len(proposal.patches)} > {max_files})."
        )

    # Check total patch text size
    total_patch_size = sum(
        sum(len(h.old_text) + len(h.new_text) for h in fp.hunks)
        for fp in proposal.patches
    )
    if total_patch_size > max_patch_size_bytes:
        raise PatchValidationError(
            f"Patch proposal total size ({total_patch_size} bytes) exceeds limit ({max_patch_size_bytes} bytes)."
        )

    repo_root = repo_path.resolve()
    seen_paths: Set[str] = set()
    simulated_files: Dict[str, str] = {}

    for file_patch in proposal.patches:
        if not file_patch.file or not file_patch.file.strip():
            raise PatchValidationError("File path in patch cannot be empty.")

        norm_path = normalize_repo_path(file_patch.file)
        if not norm_path:
            raise PatchValidationError(f"Invalid file path in patch: '{file_patch.file}'")

        if norm_path in seen_paths:
            raise PatchValidationError(
                f"Duplicate FilePatch entries for normalized path '{norm_path}' in proposal."
            )
        seen_paths.add(norm_path)

        target_path = (repo_root / norm_path).resolve()
        try:
            target_path.relative_to(repo_root)
        except ValueError:
            raise PatchValidationError(
                f"Path traversal detected: '{file_patch.file}' escapes repository root."
            )

        is_new = not target_path.exists()
        base_content = None
        if not is_new:
            # Check file size limit
            file_size = target_path.stat().st_size
            if file_size > max_file_size_bytes:
                raise PatchValidationError(
                    f"File '{norm_path}' size ({file_size} bytes) exceeds maximum limit ({max_file_size_bytes} bytes)."
                )
            base_content = target_path.read_text(encoding="utf-8")

        final_content = apply_hunks(
            base_content=base_content,
            hunks=file_patch.hunks,
            is_new_file=is_new,
            file_path=norm_path,
        )
        simulated_files[norm_path] = final_content

    return simulated_files
