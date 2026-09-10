# PROMPT — Gửi cho agent ở session mới (copy toàn bộ file này làm 1 tin nhắn)

> Cách dùng: copy toàn bộ nội dung file này, paste làm prompt đầu tiên cho một session DSH mới.
> File self-contained: mọi fact cần kiểm tra đều ghi cách verify, mọi tranh chấp giữa 3 vòng review đã arbitrate và đóng cứng.

---

# NHIỆM VỤ: Triển khai "dsh-portable" — modular hóa toàn bộ custom DeepSeek Harness (kế hoạch v3 FINAL)

## 0. Sứ mệnh

Máy này có bộ custom AI stack tự phát triển quanh DeepSeek Harness (DSH): 2 proxy LLM (Qwen web2api :8200, OpenCode bridge :8300) nằm trong `~/dsh-web2api` **không có version control ở top-level**, 2 file patch tay trong npm tree của dsh (tắt trust fence), cấu hình thủ công rải rác trong `~/.dsh`, và `~/dsh-pipeline` phụ thuộc 9 symlink CLI wrapper của `~/dsh-subagent-cockpit`.

**Mục tiêu:** gom toàn bộ custom vào 1 git repo `~/dsh-portable` với `bootstrap.sh` idempotent, secrets tách kênh riêng, pipeline chuyển sang HTTP OmniRoute (127.0.0.1:20128), gỡ sạch patch npm + cockpit symlink. Sau này port máy mới / upgrade chỉ cần 1 lệnh.

Kế hoạch này đã qua 3 vòng review độc lập có verify live, mâu thuẫn đã arbitrate. **Nhiệm vụ của bạn: THỰC THI từng phase, KHÔNG re-review kiến trúc.** Nếu 1 fact không khớp máy (máy đã đổi) → verify live nhanh, adapt, ghi rõ trong report. Làm việc trực tiếp bằng tool file/bash, không delegate.

Đây là nhiệm vụ dài nhiều round → tạo goal (`create_goal`) để tự tiếp tục.

## 1. Sự thật máy đã verify (tin được, chỉ quick-check khi nghi)

**Stack đang chạy:**
- `dsh web` @ :3080. Tunnel thực tế = **Tailscale Funnel** `justargldude.tailccc7ac.ts.net` → 127.0.0.1:3080. **ngrok KHÔNG chạy** (binary có ở ~/.local/bin, NGROK_AUTHTOKEN/NGROK_DOMAIN trong ~/.dsh/ngrok.conf).
- Qwen proxy :8200 (Node, `~/dsh-web2api/qwen`, node_modules 110M), oc-proxy :8300 (`~/dsh-web2api/opencode-proxy/server.mjs`, 1 file), DeepSeek proxy :8100 **VẪN SỐNG** (python, `.venv/bin/python server.py`) — kế hoạch cũ tưởng nó chết, sai.
- OmniRoute @ :20128 (npm pkg `omniroute` CLI 3.8.50, server v16.3.1), storage `~/.omniroute/storage.sqlite`, `STORAGE_ENCRYPTION_KEY` trong `~/.omniroute/.env`. **1178 model**. 2 custom node: `qwen-local` → 127.0.0.1:8200/v1, `oc-local` → 127.0.0.1:8300/v1. 5 account agy + 2 codex + builtin. Auth API nội bộ: header `x-omniroute-cli-token` = HMAC-SHA256(machineIdSync(true), salt "omniroute-cli-auth-v1").
- Model map verify live (tất cả tồn tại): `agy/gemini-3.8-flash-high`, `codex/gpt-5.5` (+27 codex), `auto/glm`, `aug/glm-5.2`, `cfp/zai-org/glm-5.2`, `oc/muse-spark-1.2-contributor-free`, `qwen-local/qwen3.8-max` (prefix có 40), `oc/deepseek-v4-flash-free`, `auto/best-coding`.

**Git topology `~/dsh-web2api` (QUAN TRỌNG):**
- Top-level KHÔNG phải git repo. Nhưng `qwen/.git` CÓ (8e806a9, main), `deepseek/.git` CÓ (tag v3.3.1).
- `.gitignore` đã cover: deepseek/.env, qwen/.env, qwen/data/data.json, creds-extracted.json, deepseek/data/, logs, node_modules. **CHƯA cover** `bundles/` và `opencode-proxy/logs/`.

**npm tree:** `@deepseek-ai/dsh@0.1.1-rc.2` (upstream tag dsh-v0.1.1-rc.2 = commit b150a551). 2 file patch tay: `.../dsh/node_modules/@deepseek-ai/dsh-client-connection/lib/client.js` + `lib/index.js`. `dsh web` CÓ flag chính thống `--trusted-host <authority...>` (repeatable) — thay thế hoàn toàn patch. Restore pristine: `npm pack @deepseek-ai/dsh-client-connection@0.1.1-rc.2` → đè 2 file, diff = identical.

**`~/.dsh`:**
- `.credentials.yaml`: `refs.TOKENROUTER_API_KEY`, `refs.DEEPSEEK_LOCAL_KEY`, `refs.QWEN_LOCAL_KEY`, `refs.XKIRO_API_KEY`, `refs.OMNIROUTE_API_KEY` — 5 key thật, resolve OMNIROUTE_API_KEY gọi /v1/models OK.
- `settings.yaml` (7.7KB, nhiều section — merge cẩn thận): `llm-pi-ai.providers` = tokenrouter, deepseek-local, qwen-local, omniroute. `subagent-library.entries` = `web2api-relay` (provider deepseek-local) + `omni-coder` (provider omniroute, auto/best-chat).
- `profiles/web/package.json`: có `"dsh-subagent-cockpit": "link:..."` trong dependencies VÀ trong `dsh.profile.bundles`.
- 9 symlink trong ~/.local/bin: ask, ask-agy, ask-claude, ask-codex, ask-ds, ask-qwen, ask-xkiro, cockpit, translate-plugins. (ask-glm, ask-muse là FILE THẬT — giữ.)
- Plugin syntax: `dsh plugin --profile web add <pkg>` (bắt buộc --profile).

**`~/dsh-pipeline` (git, branch base-0ed544c):** 4 file dirty chưa commit: `orchestrator/coordinator.py`, `safety/scope_guard.py`, `artifacts/codebase_survey.json`, `artifacts/raw_index.json` — commit checkpoint trước khi đụng code.
- `orchestrator/subagents.py`: `create_qa_client()` trả `SubagentClient` (CLI stdin→stdout, coordinator gọi `qa_client.query(prompt)`), `create_dev_provider()` có branch substring theo thứ tự: glm/tokenrouter → xkiro → muse (`_is_muse_name`) → qwen (8200 trực tiếp) → default deepseek (8100).
- `model/providers.py`: `resolve_tokenrouter_api_key()`/`resolve_xkiro_api_key()` pattern = env → credentials.yaml refs → **hardcoded fallback (sk-nb9k.../sk-xt-...) — KHÔNG tái tạo pattern hardcode key thật cho hàm omniroute mới**.
- `core/config.py` `get_model_family()`: substring-match, dùng cho anti-reward-hacking (coordinator.py:144-149 enforce QA≠Dev family).

**Công cụ:** python3 + yaml CÓ; `yq` KHÔNG; zstd CÓ; pnpm CÓ. Chrome hardcode `/usr/bin/google-chrome` tại `qwen/src/utils/browser-bridge.js:38` (cross-OS → env `CHROME_EXECUTABLE`).

**Bug trong `~/dsh-web2api/bin/register-web2api-omni.sh`:** (a) dòng 89–90 khai báo `local name="$1" prefix="$2" url="$3" key="$4"` TRÙNG 2 lần; (b) token-derive array (dòng ~34) chỉ có base Linux (`~/.local/node-latest/lib/node_modules`, `/usr/local/lib/node_modules`, `/usr/lib/node_modules`) — thiếu `/opt/homebrew/lib/node_modules` (macOS) + `%APPDATA%/npm/node_modules` (Windows).

## 2. Nguyên tắc an toàn (BẮT BUỘC)

1. Không paste giá trị secret vào output/log/git — chỉ thao tác theo path, chmod 600.
2. Secrets KHÔNG BAO GIỜ vào git. Verify bằng `git check-ignore` + `git ls-files` trước commit đầu.
3. Sau mỗi phase: executive summary 5–10 dòng + chờ confirm user trước phase tiếp (trừ khi user nói "làm hết").
4. **Phase 3 (cutover) chỉ chạy SAU khi smoke test Phase 2 thật sự xanh.** Trước đó không kill 8100, không uninstall cockpit, không đụng npm tree.
5. Test discipline: `pytest ... > /tmp/test.log 2>&1 || tail -n 25 /tmp/test.log`. Không cat log dài.
6. Mọi lệnh orchestrate pass tường minh `--qa` + `--dev`.
7. Backup trước khi phá: tarball Phase 0 = restore point toàn cục.

## 3. Phase 0 — Bảo vệ nguồn (~30p, an toàn tuyệt đối)

1. Sửa `~/dsh-web2api/.gitignore`: THÊM `bundles/`, `qwen/`, `deepseek/`, `opencode-proxy/logs/`.
2. `git init` top-level + commit (chỉ: bin/, opencode-proxy/, scripts/, .gitignore, README*, HUONG-DAN.md, dsh-subagent-entry.yaml). **Không `git add -A`** — qwen/deepseek có .git riêng sẽ thành gitlink hỏng history.
3. Tạo `~/dsh-portable/secrets/` + `.gitignore` (nội dung: `secrets/`): copy + chmod 600: `qwen/data/data.json` → `qwen-data.json`; `qwen/.env` → `qwen.env`; `~/.omniroute/.env` → `omniroute.env`; `~/.dsh/.credentials.yaml` → `dsh-credentials.yaml`; `creds-extracted.json`; `deepseek/.env` → `deepseek.env`; `~/.dsh/ngrok.conf` → `ngrok.conf`.
4. Cold tarball: source (bỏ node_modules) + secrets/ → `~/dsh-portable-cold-backup-<date>.tar.gz`.

**Acceptance:** web2api có commit, status sạch; secrets/ 600; `git ls-files` không chứa secret.

## 4. Phase 1 — Scaffold dsh-portable (~2h)

```
~/dsh-portable/
├── bootstrap.sh        # idempotent 6 bước, set -euo pipefail, check deps TRƯỚC (node≥18, npm, curl, ss, fuser, python3+yaml) — thiếu package → báo rõ, không fail giữa chừng
├── modules/qwen-web2api/   # copy ~/dsh-web2api/qwen: src, public, package.json, package-lock.json, scripts, docs, data_template.json, data.example.json — KHÔNG node_modules/caches/logs/data.json/.env
├── modules/oc-proxy/       # server.mjs
├── bin/start-qwen.sh       # từ start-qwen.sh, sửa DIR → $SCRIPT_DIR/../modules/qwen-web2api
├── bin/start-oc.sh         # từ start-opencode.sh, sửa DIR tương ứng
├── bin/register-omni.sh     # từ register-web2api-omni.sh + 3 fix: xóa dòng local trùng; token-derive thêm 2 base macOS/Windows; giữ nguyên logic idempotent (node tồn tại → PATCH baseUrl)
├── bin/ask-omniroute        # wrapper QA (xem Phase 2.3)
├── overlays/settings.providers.yaml  # llm-pi-ai.providers: tokenrouter + omniroute
├── overlays/settings.subagents.yaml  # web2api-relay: provider → omniroute, model auto/best-chat
├── overlays/credentials.template.yaml # 5 key names (refs.*), giá trị placeholder
├── overlays/AGENTS.md       # ~/.dsh/AGENTS.md mới: bỏ ask-* cockpit, thêm ask-omniroute + prefix model OmniRoute
├── secrets/                 # từ Phase 0
├── start.sh                 # boot toàn stack: 2 proxy + omniroute (nếu chưa chạy) + dsh web --trusted-host "$TUNNEL_HOSTS"
├── VERSIONS.md              # pin: dsh npm=0.1.1-rc.2 (b150a551), omniroute npm=3.8.50/server v16.3.1, node≥18 (đang 26.8.1), pipeline=<sha>. CẢNH BÁO ĐỎ: mất STORAGE_ENCRYPTION_KEY = mất toàn bộ providers trong sqlite.
└── README.md                # flow máy mới 4 lệnh + quy trình renew cookies Qwen (extract Firefox)
```

**bootstrap.sh 6 bước:** ① deps + install-if-missing `omniroute`, `@deepseek-ai/dsh` (npm i -g) → ② copy `secrets/omniroute.env` → `~/.omniroute/.env` (nếu chưa có), start `omniroute serve --port 20128 --no-open` nếu /v1/models chưa đáp → ③ seed secrets vào modules (qwen-data.json → data/data.json, qwen.env → .env), `npm ci` trong qwen-web2api, start 2 proxy → ④ `bin/register-omni.sh` → ⑤ merge overlays vào `~/.dsh/settings.yaml` bằng **python3 deep-merge key-by-key CHỈ 2 section** `llm-pi-ai.providers` + `subagent-library.entries` (không ghi đè nguyên file — settings.yaml có nhiều section khác); copy credentials + AGENTS.md; symlink bin/* → ~/.local/bin (trừ khi đã có) → ⑥ verify: curl /v1/models (kỳ vọng ≥1178, qwen-local ≥40, oc-local ≥7) + nodes list + report.

**Test bổ sung:** `omniroute sync bundle /tmp/omni-bundle-test.json --include providers,keys` → nếu bundle chứa keys/account: note vào README Phase 4 (dùng `omniroute sync import` thay re-login; bundle = secret, kênh riêng).

**Acceptance:** chạy bootstrap 2 lần liên tiếp → 0 node/connection duplicate (so ID `/api/provider-nodes` + `/api/providers` trước/sau = stable). `git ls-files | grep -i secret` chỉ ra template.

## 5. Phase 2 — Pipeline chuyển OmniRoute (~2h, repo ~/dsh-pipeline)

0. `git add -A && git commit` checkpoint 4 file dirty.
1. `model/providers.py`: thêm `resolve_omniroute_api_key()` — env `OMNIROUTE_API_KEY` → credentials.yaml refs → **RuntimeError rõ ràng** (không hardcode fallback).
2. `orchestrator/subagents.py` — **branch OmniRoute đứng ĐẦU TIÊN** trong cả `create_dev_provider()` VÀ `create_qa_client()`, match `name.startswith()` theo prefix thật của OmniRoute: `agy/`, `codex/`, `oc/`, `oc-local/`, `qwen-local/`, `auto/`, `aug/`, `cfp/`, `cx/`, `cxa/`, `tllm/`, `dva/`, `gh/`, `github/`, `openrouter/`, `opencode/`, `opencode-zen/`, `deepseek-web/`, `ds-web/`, `qwen-web/`, `no-think/`. **KHÔNG dùng bare `"/" in name`** — sẽ bắt nhầm `z-ai/glm-5.3-free` (tokenrouter) và `xkiro/...`.
   - Dev: `OpenAICompatibleProvider(api_key=resolve_omniroute_api_key(), base_url=env OMNIROUTE_BASE_URL or "http://127.0.0.1:20128/v1", fast_model=name, reasoning_model=name, timeout 300s — gateway free chậm 60–180s/turn)`.
   - QA: `SubagentClient(name=name, cli_command=[shutil.which("ask-omniroute")])` — thiếu wrapper → RuntimeError (pattern như ask-muse).
   - GIỮ nguyên mọi branch cũ làm fallback.
3. `ask-omniroute` (bash ~20 dòng, dsh-portable/bin/): đọc prompt từ stdin; model = `$1` (default `auto/best-coding`); key = env `OMNIROUTE_API_KEY` hoặc python3 parse `~/.dsh/.credentials.yaml` refs; POST `http://127.0.0.1:20128/v1/chat/completions` (timeout 300, non-stream); in `choices[0].message.content` ra stdout; exit ≠ 0 khi HTTP error. Test: `echo "trả lời đúng 1 từ: OK" | ask-omniroute auto/best-coding`.
4. `core/config.py` `get_model_family()`: strip prefix OmniRoute trước khi substring-match — `qwen-local/qwen3.8-max` → alibaba, `agy/gemini-*` → google, `codex/*` → openai, `oc/muse-*` → muse, `aug/glm-5.2` → zhipu, `auto/best-coding` → `omni_auto` (family riêng, để QA/Dev khác prefix không bị coi cùng family). Mục đích: anti-reward-hacking QA≠Dev vẫn đúng khi cả 2 qua OmniRoute.
5. Tests mới (pattern test_glm_tokenrouter.py, mock HTTP, không đụng network thật): `tests/orchestrator/test_omniroute_provider.py` (mock POST /v1/chat/completions, assert resolve key đúng thứ tự env→credentials→error) + `tests/orchestrator/test_omniroute_prefix_precedence.py` (assert `qwen-local/qwen3.8-max` KHÔNG rơi branch qwen 8200; `oc/muse-spark-1.2-contributor-free` KHÔNG rơi branch muse/ask-muse; `codex/gpt-5.5` KHÔNG rơi branch codex CLI) + update `test_muse_spark.py` cho đường HTTP. Chạy: `pytest tests/orchestrator tests/safety -q > /tmp/test.log 2>&1 || tail -n 25 /tmp/test.log` — phải xanh toàn bộ.

**Acceptance Phase 2:** pytest xanh + smoke thật (1 vòng ngắn): `dsh-pipeline orchestrate "<goal nhỏ như thêm 1 hàm util + test>" --target-repo /tmp/smoke-repo --qa auto/best-coding --dev oc/muse-spark-1.2-contributor-free`. QA và Dev đều qua OmniRoute HTTP, log cho thấy 2 request tới :20128 (check call_logs omniroute hoặc ps). Smoke xong → commit "feat(omniroute): pipeline routes via HTTP OmniRoute".

## 6. Phase 3 — Cutover máy này (~1h, chỉ chạy SAU smoke Phase 2 xanh)

Thứ tự đúng — mỗi bước verify trước khi sang bước tiếp:

1. **Kill DeepSeek proxy 8100** (vẫn đang sống, pid python): kill PID đang listen ss -tlnp, bỏ `bash start-deepseek.sh` khỏi start chain (start.sh mới không gọi nó).
2. **Uninstall cockpit**: `~/dsh-subagent-cockpit/install.sh --uninstall` → edit `~/.dsh/profiles/web/package.json`: xóa `"dsh-subagent-cockpit"` khỏi dependencies VÀ bundles list → `rm -rf ~/.dsh/profiles/web/node_modules/dsh-subagent-cockpit` (nếu pnpm install không tự prune) → `pnpm install` → xóa 9 symlink còn sót trong ~/.local/bin (ask, ask-agy, ask-claude, ask-codex, ask-ds, ask-qwen, ask-xkiro, cockpit, translate-plugins). **GIỮ ask-glm + ask-muse (file thật, pipeline còn dùng đến khi verify Phase 2 xong — xóa sau nếu muốn).**
3. **Restore npm pristine**: `cd /tmp && npm pack @deepseek-ai/dsh-client-connection@0.1.1-rc.2` → giải nén, copy 2 file `lib/client.js` + `lib/index.js` đè lên bản patch → diff 2 file với tarball = identical → restart dsh web.
4. **start.sh mới** (`~/dsh-portable/start.sh`): `TUNNEL_HOSTS` mặc định = `justargldude.tailccc7ac.ts.net` (tailscale funnel ĐANG là tunnel thật) + ngrok domain nếu user bật ngrok; chạy `dsh web --host 127.0.0.1 --port 3080 --no-open --trusted-host $TUNNEL_HOSTS`. Verify: curl GUI qua `https://justargldude.tailccc7ac.ts.net` trả 200 và API trust fence không chặn. **KHÔNG hardcode 1 domain — thiếu tailscale hostname = mất GUI ngay lập tức.**
5. **Dọn dẹp**: xóa `~/dsh-pipeline.bak_1788733936`, `~/dsh-pipeline.zip`, `~/dsh-full-backup.tar.gz` (chỉ SAU khi cold tarball Phase 0 xác nhận nguyên vẹn — test tar -tzf), `~/.dsh/install_joi_theme.sh` (path đúng là ~/.dsh/ không phải ~/), `~/.dsh/.legacy_plugins_backup.tar.gz`, `~/dsh-cloud-dashboard`. Giữ nguyên: cockpit-tools AppImage (độc lập), repo ~/dsh-subagent-cockpit (chỉ uninstall khỏi hệ thống, không xóa source).

**Acceptance Phase 3:** dsh web boot sạch qua tailscale URL; `ps aux | grep cockpit` trống (trừ cockpit-tools); `ss -tlnp` không còn 8100; npm tree diff pristine = identical; orchestrate smoke vẫn chạy OK sau cutover (re-run 1 lần).

## 7. Phase 4 — Port máy mới (~30p, chỉ khi user yêu cầu — máy này chưa cần)

Flow: nvm/node≥18 → `git clone <private repo>` (hoặc git bundle offline) → copy `secrets/` qua kênh riêng (USB/1Password) → `./bootstrap.sh` → `opencode auth login` 1 lần (auth sqlite không port được) → tunnel per-machine (tailscale up hoặc ngrok config) → `CHROME_EXECUTABLE` env nếu Chrome khác path. Test `omniroute sync bundle` trước khi rời máy cũ — nếu keys portable thì import thay re-login.

**Acceptance:** /v1/models >1200; orchestrate smoke xanh; GUI mở qua tunnel.

## 8. Phase 5 — Vận hành (viết vào README, không phải việc làm ngay)

| Upgrade | Việc | Vì sao an toàn |
|---|---|---|
| dsh | re-run bootstrap | npm tree sạch 100%, overlay merge idempotent |
| OmniRoute | re-run bootstrap + `omniroute sync` | providers sống trong sqlite, register script PATCH baseUrl |
| opencode CLI | `opencode auth login` 1 lần | server.mjs tự spawn + probe |
| pipeline | `git pull` | chỉ biết HTTP endpoint 20128 |
| Chrome | env override | không đụng source |

## 9. Rủi ro đã biết + giảm thiểu (xử lý khi gặp, không re-plan)

- **Cookies Qwen hết hạn** (WAF/expire) → có `data.example.json` template + `scripts/extract-creds-firefox.py`; thêm mục "renew cookies" README.
- **STORAGE_ENCRYPTION_KEY mất = mất toàn bộ providers sqlite** → backup 2 nơi (cold tarball + cloud), cảnh báo đỏ VERSIONS.md.
- **OmniRoute đổi HTTP API** → register script `set -e` + fail rõ; fallback CLI flag `omniroute nodes add --provider --base-url --name` đã verify tồn tại.
- **Bootstrap OS lạ thiếu ss/fuser/setsid** → check deps bước ①, báo thiếu package thay vì fail giữa chừng.
- **Rollback từng phase**: P0 tarball; P1 repo mới không phá gì cũ; P2 git revert trong dsh-pipeline; P3 = reinstall cockpit (installer + tarball pristine npm).

## 10. Quy trình làm việc mỗi phase

1. Bắt đầu phase → todo_write các bước.
2. Làm xong phase → executive summary (đã làm gì, evidence, acceptance pass/fail từng tiêu chí).
3. Dừng, chờ user confirm "tiếp" trước phase sau — trừ khi user đã nói trước "chạy hết Phase 0→3 không cần hỏi".
4. Nếu gặp fact không khớp (máy đã đổi từ lúc plan): verify live, adapt, ghi rõ "DEVIATION + lý do" vào summary. KHÔNG dừng chờ hỏi nếu cách xử lý hiển nhiên và an toàn (reversible).
5. Tuyệt đối không: git commit secrets; paste giá trị key vào output; kill 8100 / uninstall cockpit / đụng npm tree trước khi Phase 2 smoke xanh.

**Bắt đầu từ Phase 0 ngay sau khi đọc xong.**
