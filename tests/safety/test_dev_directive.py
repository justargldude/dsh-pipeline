import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.runtime import DSHRuntime


def test_dev_system_prompt_has_critical_reasoning_directive():
    prompt = DSHRuntime.SYSTEM_PROMPT
    assert "CRITICAL REASONING DIRECTIVE" in prompt, (
        "Dev system prompt must include a 'CRITICAL REASONING DIRECTIVE' section header"
    )
    prompt_lower = prompt.lower()
    assert "past observation" in prompt_lower or "historical" in prompt_lower, (
        "Directive must state that historical notes/logs are past observations, not rules"
    )
    assert "never assume" in prompt_lower or "endpoint" in prompt_lower, (
        "Directive must instruct never to assume endpoints or components are broken from old reports"
    )
    assert "verify" in prompt_lower and ("real-time execution" in prompt_lower or "liveness" in prompt_lower), (
        "Directive must require verifying liveness with real-time execution"
    )
