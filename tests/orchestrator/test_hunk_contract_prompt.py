"""SYSTEM_PROMPT hunk contract tests — regression cho 2 hard-halt thật của
muse-spark qua OmniRoute (smoke 2026-09-11):

- Smoke 3: old_text không khớp byte — model tự gõ lại snippet từ nhớ, gộp
  newline (`def test_add(): assert add(2, 3) == 5` 1 dòng vs file thật 2 dòng)
  → T0_STRUCTURAL "old_text not found in target file".
- Smoke 4: old_text RỖNG trên file tồn tại (model tưởng append = empty
  anchor) → T0_STRUCTURAL "Empty old_text is not allowed on existing file".

Prompt phải dạy rõ contract trước khi Dev sinh patch — không thể dựa vào
model tự suy ra semantics.
"""
import re
from core.runtime import DSHRuntime


def _p():
    return DSHRuntime.SYSTEM_PROMPT


def test_prompt_has_hunk_contract_section():
    assert "HUNK CONTRACT" in _p(), "prompt thiếu mục HUNK CONTRACT"


def test_prompt_teaches_byte_exact_old_text():
    """old_text phải là bản copy chính xác từ file — không gõ lại từ nhớ."""
    low = _p().lower()
    assert "old_text" in low
    assert ("byte-for-byte" in low) or ("exactly as" in low) or ("verbatim" in low), (
        "phải nói rõ copy byte-for-byte/verbatim từ file content trong context"
    )
    # whitespace phải được nêu đích danh (newline/indentation)
    assert any(w in low for w in ("newline", "indentation", "whitespace")), (
        "phải nêu giữ nguyên newline/indentation"
    )


def test_prompt_forbids_empty_old_text_on_existing_file():
    low = _p().lower()
    assert "empty" in low, "phải dạy old_text rỗng bị cấm với file đã tồn tại"
    # phải phân biệt file mới (được phép rỗng) vs file tồn tại (cấm)
    assert ("new file" in low) or ("existing" in low), (
        "phải phân biệt new file vs existing file"
    )


def test_prompt_teaches_append_pattern():
    """Muốn append: dùng vài dòng cuối file làm anchor (copy chính xác),
    new_text = anchor + phần thêm. Model muse đã fail vì không biết cách này."""
    low = _p().lower()
    assert "append" in low, "phải dạy pattern append bằng anchor cuối file"


def test_prompt_directs_copy_from_context_not_memory():
    low = _p().lower()
    assert ("context" in low) and ("copy" in low), (
        "phải chỉ đạo copy snippet từ file content trong context, không từ trí nhớ"
    )
