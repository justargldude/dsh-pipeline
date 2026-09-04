# AI Subagent & Delegation Guide

- **Inspect Status**: Do NOT search files. Call tool `subagent_status` (or shell `ask --status`) to inspect active models, effort levels, and available CLIs on-demand.
- **Subagent Delegation**:
  - Smart Router: Tool `subagent_ask(cli, prompt, model?, effort?)` or shell `ask "<prompt>"`.
  - Direct CLIs: `ask-agy` (Gemini 3), `ask-codex` (GPT-5.6), `ask-claude` (Sonnet 3.7), `ask-ds` (DeepSeek Web2API).
  - Named Roster: Tool `delegate(library_id: "web2api-relay", prompt: "...")`.
- **Context Discipline**: Always redirect heavy test/build output to `/tmp/` to prevent context explosion:
  `<command> > /tmp/test.log 2>&1 || tail -25 /tmp/test.log`
- **Deterministic Code Pipeline (Multi-Agent TDD)**:
  - Do NOT search files for pipeline code. Call global CLI:
    `dsh-pipeline orchestrate "<goal>" --target-repo "<path>" --qa <qa_model> --dev <dev_model>`
  - **CRITICAL - Dynamic QA / Dev Model Assignment (No Hardcoding)**:
    1. The Main Model MUST NOT assume or hardcode which model acts as QA and which acts as Dev.
    2. If the user's prompt **does NOT** explicitly specify which model is for QA and which is for Dev:
       The Main Model **MUST STOP AND ASK THE USER** to confirm model roles. Run `ask --status` to present available CLIs/models (e.g., Antigravity/agy, Claude, Codex, DeepSeek) to guide the user.
    3. If the user's prompt **specifies** the models (e.g., "QA is Antigravity, Dev is DeepSeek"):
       Extract them and pass explicitly: `--qa <qa_model> --dev <dev_model>` (e.g., `--qa agy --dev deepseek`).
- **Guardrails**: Auto-resume automatically catches 429 rate-limits and token exhaustion up to 3 times.


