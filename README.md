# dsh-portable — stack local AI (OmniRoute gateway + 2 proxy + dsh web)

## 1. Máy mới — 4 lệnh

1. Cài node ≥18 qua nvm: `nvm install 26 && nvm use 26`
2. Clone repo (hoặc offline): `git clone <url> dsh-portable` — offline: máy cũ `git bundle create dsh-portable.bundle --all`, copy file, máy mới `git clone dsh-portable.bundle dsh-portable`
3. Copy `secrets/` qua kênh riêng (USB/1Password — KHÔNG git, KHÔNG chat), perms 600
4. `./bootstrap.sh`

## 2. Sau bootstrap (1 lần/máy)

- `opencode auth login` (auth sqlite không port được).
- Tunnel per-machine: `tailscale up` + funnel tới `127.0.0.1:3080`, hoặc ngrok (domain trong `secrets/ngrok.conf`).
- `export CHROME_EXECUTABLE=<path>` nếu Chrome khác `/usr/bin/google-chrome` (hardcode tại `modules/qwen-web2api/src/utils/browser-bridge.js:38`).

## 3. Kiểm tra sau bootstrap

- Models: `curl -s http://127.0.0.1:20128/v1/models -H "Authorization: Bearer $OMNIROUTE_API_KEY"` (key từ `~/.dsh/.credentials.yaml` refs `OMNIROUTE_API_KEY`) — kỳ vọng tổng >1170, `qwen-local/` ≥40, `oc-local/` ≥7.
- Nodes: `bin/register-omni.sh` chạy idempotent (PATCH baseUrl, không đẻ trùng).
- Wrapper: `echo "trả lời đúng 1 từ: OK" | bin/ask-omniroute auto/best-coding`

## 4. Renew cookies Qwen (khi WAF/expire)

- Dấu hiệu: response rỗng / `RGV587` → stop proxy (kill port 8200), chờ 1-2h.
- Lấy cookies Firefox: `python3 modules/qwen-web2api/scripts/extract-creds-firefox.py` (cookies `chat.qwen.ai`) → cập nhật `modules/qwen-web2api/data/data.json` theo mẫu `data/data.example.json` + `data/data_template.json`.
- Khởi động lại: `bin/start-qwen.sh`.

## 5. Phase 4 — Port máy mới (chi tiết)

- Máy cũ, bundle test trước khi rời: `omniroute sync bundle /tmp/bundle.json --include providers,keys` — OK → note dùng được; fail → re-login thủ công.
- Nếu keys portable: máy mới `omniroute sync import /tmp/bundle.json` (thay re-login). Bundle = SECRET, chuyển qua kênh riêng.
- Flow: nvm/node → clone (hoặc bundle) → secrets → `./bootstrap.sh` → `opencode auth login` (1 lần) → tunnel → `CHROME_EXECUTABLE`.

## 6. Vận hành (upgrade)

| Đổi gì | Làm gì |
|---|---|
| dsh | Re-run `./bootstrap.sh` |
| omniroute | Re-run `./bootstrap.sh` + `omniroute sync` |
| opencode CLI | `opencode auth login` 1 lần/máy |
| pipeline | `git pull` |
| Chrome | Env `CHROME_EXECUTABLE` |

## 7. Sự thật kiến trúc

- OmniRoute `:20128` = gateway trung tâm (1176+ models, prefix `agy/`, `codex/`, `oc/`, `oc-local/`, `qwen-local/`, `auto/`, ...).
- 2 proxy local: qwen `:8200` (browser-bridge) + oc `:8300` (opencode bridge), đăng ký thành provider nodes `qwen-local` + `oc-local`.
- `secrets/` tách kênh riêng (7 file, 600, gitignore).
- `dsh web` chạy `--trusted-host` tailscale funnel (`justargldude.tailccc7ac.ts.net` → `127.0.0.1:3080`).
- Hàng ngày: `./start.sh` (boot 2 proxy + omniroute nếu chưa + `dsh web` foreground).
