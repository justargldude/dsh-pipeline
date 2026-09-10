# VERSIONS — dsh-portable (pin đã verify live)

| Thành phần | Phiên bản pin | Cài đặt |
|---|---|---|
| @deepseek-ai/dsh (npm) | 0.1.1-rc.2 (upstream tag `dsh-v0.1.1-rc.2` = commit `b150a551`) | `npm i -g @deepseek-ai/dsh@0.1.1-rc.2` |
| omniroute (npm CLI) | 3.8.50; server v16.3.1 | `npm i -g omniroute@3.8.50` |
| node | ≥18 (máy hiện tại 26.8.1) | nvm / apt |
| dsh-pipeline | `a4e5c80` @ branch `base-0ed544c` (chỉ biết HTTP endpoint `127.0.0.1:20128` — không phụ thuộc symlink cockpit) | `git pull` |

## ⚠️ CẢNH BÁO ĐỎ: STORAGE_ENCRYPTION_KEY

- `STORAGE_ENCRYPTION_KEY` trong `~/.omniroute/.env` — MẤT = MẤT TOÀN BỘ providers trong `storage.sqlite` (không decrypt được).
- Backup 2 nơi: cold tarball (`$HOME/dsh-portable-cold-backup-<date>.tar.gz`) + cloud/1Password.
- `secrets/omniroute.env` là bản lưu — chuyển qua kênh riêng, KHÔNG git, KHÔNG chat.

## Vận hành / upgrade

| Đổi gì | Làm gì |
|---|---|
| dsh | Re-run `./bootstrap.sh` (npm tree sạch, overlay merge idempotent) |
| omniroute | Re-run `./bootstrap.sh` + `omniroute sync` (providers sống trong sqlite, `register-omni.sh` PATCH baseUrl idempotent) |
| opencode CLI | `opencode auth login` 1 lần/máy (auth sqlite không port được) |
| pipeline | `git pull` |
| Chrome | Env `CHROME_EXECUTABLE` (không đụng source; hardcode tại `modules/qwen-web2api/src/utils/browser-bridge.js:38`) |
