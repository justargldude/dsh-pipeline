#!/usr/bin/env bash
# bootstrap.sh — dsh-portable: boot toàn stack local AI (idempotent)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# === BƯỚC 1 === deps check TRƯỚC
fail=0
check_cmd() {
  _cmd="$1"; _hint="$2"
  if ! command -v "$_cmd" >/dev/null 2>&1; then
    echo "THIẾU: $_cmd — cài qua $_hint"
    fail=1
  fi
}
check_cmd node "nodejs >=18 (apt/nvm)"
check_cmd npm "npm (kèm nodejs)"
check_cmd curl "apt install curl"
check_cmd ss "apt install iproute2"
check_cmd fuser "apt install psmisc"
check_cmd python3 "apt install python3"
check_cmd tar "apt install tar"
if command -v node >/dev/null 2>&1; then
  _ver="$(node --version | sed 's/^v//; s/\..*//')"
  if [ "${_ver:-0}" -lt 18 ]; then
    echo "THIẾU: node>=18 (đang có $(node --version)) — nâng cấp nodejs"
    fail=1
  fi
fi
if command -v python3 >/dev/null 2>&1; then
  if ! python3 -c "import yaml" 2>/dev/null; then
    echo "THIẾU: python3-yaml — cài qua apt install python3-yaml / pip install pyyaml"
    fail=1
  fi
fi
if [ "$fail" -ne 0 ]; then
  echo "LỖI: thiếu dependency — cài đủ rồi chạy lại" >&2
  exit 1
fi
command -v omniroute >/dev/null 2>&1 || npm i -g omniroute@3.8.50
command -v dsh >/dev/null 2>&1 || npm i -g @deepseek-ai/dsh@0.1.1-rc.2

# === BƯỚC 2 === OmniRoute serve
omni_alive() {
  # 401 cũng là "alive": server sống nhưng /v1/models yêu cầu Bearer key
  _code="$(curl -s -m 5 -o /dev/null -w "%{http_code}" http://127.0.0.1:20128/v1/models 2>/dev/null || echo 000)"
  [ "$_code" = "200" ] || [ "$_code" = "401" ]
}
if [ ! -f "$HOME/.omniroute/.env" ]; then
  mkdir -p "$HOME/.omniroute"
  cp secrets/omniroute.env "$HOME/.omniroute/.env"
  chmod 600 "$HOME/.omniroute/.env"
fi
if ! omni_alive; then
  setsid nohup omniroute serve --port 20128 --no-open >/dev/null 2>&1 &
  for i in $(seq 1 30); do
    if omni_alive; then break; fi
    sleep 1
    if [ "$i" -eq 30 ]; then
      echo "LỖI: OmniRoute không đáp sau 30s" >&2
      exit 1
    fi
  done
fi

# === BƯỚC 3 === seed modules + start 2 proxy
if [ ! -f modules/qwen-web2api/data/data.json ]; then
  cp secrets/qwen-data.json modules/qwen-web2api/data/data.json
fi
if [ ! -f modules/qwen-web2api/.env ]; then
  cp secrets/qwen.env modules/qwen-web2api/.env
fi
python3 -c "import json;json.load(open('modules/qwen-web2api/data/data.json'))" 2>/dev/null || echo "CẢNH BÁO: data.json không hợp lệ (sau seed) — xem README renew cookies"
if [ ! -d modules/qwen-web2api/node_modules ]; then
  (cd modules/qwen-web2api && npm ci --no-audit --no-fund)
fi
"$SCRIPT_DIR/bin/start-qwen.sh"
"$SCRIPT_DIR/bin/start-oc.sh"

# === BƯỚC 4 === register OmniRoute nodes
"$SCRIPT_DIR/bin/register-omni.sh"

# === BƯỚC 5 === overlays + symlinks
mkdir -p "$HOME/.dsh" "$HOME/.local/bin"
if [ -f "$HOME/.dsh/settings.yaml" ]; then
  cp "$HOME/.dsh/settings.yaml" "$HOME/.dsh/settings.yaml.bak-$(date +%Y%m%d-%H%M%S)"
fi
python3 <<'PY'
import os, yaml
base_p = os.path.expanduser("~/.dsh/settings.yaml")
prov_p = "overlays/settings.providers.yaml"
sub_p = "overlays/settings.subagents.yaml"
base = {}
if os.path.exists(base_p):
    with open(base_p) as f:
        base = yaml.safe_load(f) or {}
with open(prov_p) as f:
    prov = yaml.safe_load(f) or {}
with open(sub_p) as f:
    sub = yaml.safe_load(f) or {}
def deep_merge(b, o):
    for k, v in o.items():
        if k in b and isinstance(b[k], dict) and isinstance(v, dict):
            deep_merge(b[k], v)
        else:
            b[k] = v
# chỉ merge 2 section
p_providers = ((prov.get("llm-pi-ai") or {}).get("providers") or {})
if p_providers:
    base.setdefault("llm-pi-ai", {}).setdefault("providers", {})
    deep_merge(base["llm-pi-ai"]["providers"], p_providers)
s_entries = ((sub.get("subagent-library") or {}).get("entries") or {})
if s_entries:
    base.setdefault("subagent-library", {}).setdefault("entries", {})
    deep_merge(base["subagent-library"]["entries"], s_entries)
tmp_p = base_p + ".tmp"
with open(tmp_p, "w") as f:
    yaml.safe_dump(base, f, sort_keys=False, allow_unicode=True)
os.replace(tmp_p, base_p)
PY
cp overlays/AGENTS.md "$HOME/.dsh/AGENTS.md"
if [ ! -f "$HOME/.dsh/.credentials.yaml" ]; then
  cp secrets/dsh-credentials.yaml "$HOME/.dsh/.credentials.yaml"
  chmod 600 "$HOME/.dsh/.credentials.yaml"
fi
for f in ask-omniroute start-qwen.sh start-oc.sh register-omni.sh; do
  if [ -e "$HOME/.local/bin/$f" ] && [ ! -L "$HOME/.local/bin/$f" ]; then
    echo "CẢNH BÁO: $HOME/.local/bin/$f là file thật — skip symlink"
  else
    ln -sfn "$SCRIPT_DIR/bin/$f" "$HOME/.local/bin/$f"
  fi
done

# === BƯỚC 6 === verify + report
python3 <<'PY'
import yaml, json, urllib.request, os
creds = yaml.safe_load(open(os.path.expanduser("~/.dsh/.credentials.yaml")))
key = (creds.get("refs") or {}).get("OMNIROUTE_API_KEY", "")
req = urllib.request.Request("http://127.0.0.1:20128/v1/models", headers={"Authorization": "Bearer " + key})
data = json.load(urllib.request.urlopen(req, timeout=15))
models = data.get("data", data if isinstance(data, list) else [])
total = len(models)
qwen = sum(1 for m in models if str(m.get("id", "")).startswith("qwen-local/"))
oc = sum(1 for m in models if str(m.get("id", "")).startswith("oc-local/"))
print(f"models: tổng={total} qwen-local/={qwen} oc-local/={oc}")
PY
node <<'JS'
const http = require("http"), fs = require("fs"), crypto = require("crypto"), os = require("os");
const { createRequire } = require("node:module");
// Tìm omniroute package giống register-omni.sh, rồi require node-machine-id
// qua createRequire(dist/server.js) — cách bản gốc đã verify chạy được.
let omnirouteRoot = null;
for (const base of [os.homedir() + "/.local/node-latest/lib/node_modules", "/usr/local/lib/node_modules", "/usr/lib/node_modules", "/opt/homebrew/lib/node_modules", (process.env.APPDATA ? process.env.APPDATA + "\\npm\\node_modules" : null)].filter(Boolean)) {
  try { if (fs.existsSync(base + "/omniroute")) { omnirouteRoot = base + "/omniroute"; break; } } catch {}
}
if (!omnirouteRoot) { console.error("FAIL: không tìm thấy omniroute package"); process.exit(1); }
const req = createRequire(omnirouteRoot + "/dist/server.js");
const { machineIdSync } = req("node-machine-id");
const rawId = machineIdSync(true);
const salt = process.env.OMNIROUTE_CLI_SALT || "omniroute-cli-auth-v1";
const token = crypto.createHmac("sha256", rawId).update(salt).digest("hex");
http.get({ host: "127.0.0.1", port: 20128, path: "/api/provider-nodes", headers: { "x-omniroute-cli-token": token } }, (res) => {
  let b = "";
  res.on("data", (c) => b += c);
  res.on("end", () => {
    const q = b.includes("qwen-local"), o = b.includes("oc-local");
    console.log((q ? "OK" : "FAIL") + ": provider-node qwen-local");
    console.log((o ? "OK" : "FAIL") + ": provider-node oc-local");
    if (!q || !o) process.exit(1);
  });
}).on("error", (e) => { console.error("FAIL: provider-nodes " + e.message); process.exit(1); });
JS
echo "REPORT: bootstrap 6 bước xong (deps/omniroute/seed/register/overlay/verify)"
set +e
omniroute sync bundle /tmp/omni-bundle-test.json --include providers,keys 2>&1 | head -3 || true
if [ -f /tmp/omni-bundle-test.json ]; then
  echo "NOTE README Phase 4: bundle chứa keys -> sync import thay re-login; bundle là secret"
else
  echo "NOTE: sync bundle test fail — xem report, không exit 1"
fi
set -e
