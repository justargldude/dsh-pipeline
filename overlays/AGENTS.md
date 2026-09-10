# dsh subagent guide (OmniRoute stack)

- Inspect status: `subagent_status` (hoac `ask --status`) — khong search file.
- Subagent delegation: `subagent_ask(prompt, cli?, model?, effort?)` hoac shell `echo "<prompt>" | ask-omniroute "<model>"` (doc prompt tu stdin).
- Model OmniRoute dang prefix/model — cac prefix: agy/, codex/, oc/, oc-local/, qwen-local/, auto/, aug/, cfp/, cx/, cxa/, tllm/, dva/, gh/, github/, openrouter/, opencode/, opencode-zen/, deepseek-web/, ds-web/, qwen-web/, no-think/.
- Named roster: `delegate(library_id: "omni-coder", ...)` — brain qua OmniRoute.
- Account/Quota: dung cockpit-tools (app doc lap), khong goi truc tiep tu subagent.
- Context discipline: redirect heavy output to /tmp/ (`pytest > /tmp/test.log 2>&1 || tail -25 /tmp/test.log`).
- Guardrails: 429/5xx retry 1 lan sau 60s; auth error → AUTH_REQUIRED hint re-login, khong blind-retry; gateway free cham 60-180s/turn la binh thuong — timeout >=300s.
- Khong in key/cookie ra output.
