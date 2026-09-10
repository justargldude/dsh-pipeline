#!/usr/bin/env bash
# register-web2api-omni.sh — Đăng ký 2 proxy local (Qwen :8200, OpenCode :8300)
# vào OmniRoute dưới dạng 2 provider độc lập (Provider Nodes, chuẩn OpenAI-compatible).
#
# KHÔNG sửa code OmniRoute — chỉ dùng HTTP API có sẵn của Omni.
# Chạy lại nhiều lần vẫn an toàn: node đã tồn tại → update baseUrl, không tạo trùng.
#
# Provider tạo ra:
#   qwen-local/*  → http://127.0.0.1:8200/v1  (Qwen Web2API - browser bridge chống WAF)
#   oc-local/*    → http://127.0.0.1:8300/v1  (OpenCode CLI bridge - free models
#                   qua gateway opencode.ai/zen: muse-spark, nemotron, mimo, ...)
#
# Provider ds-local (DeepSeek Web2API :8100) đã bị XÓA — thay bằng oc-local.
#
# Yêu cầu: OmniRoute đang chạy ở port 20128 (OMNI_PORT), các proxy đã start
# (~/dsh-web2api/bin/start-qwen.sh, start-opencode.sh).
set -euo pipefail

OMNI_PORT="${OMNI_PORT:-20128}"
OMNI_BASE="http://127.0.0.1:$OMNI_PORT"

QWEN_PROXY_URL="${QWEN_PROXY_URL:-http://127.0.0.1:8200/v1}"
QWEN_PROXY_KEY="${QWEN_PROXY_KEY:-sk-justar-local-qwen}"
OC_PROXY_URL="${OC_PROXY_URL:-http://127.0.0.1:8300/v1}"
OC_PROXY_KEY="${OC_PROXY_KEY:-sk-justar-local-oc}"

# ── Lấy CLI token của Omni (HMAC theo machine-id, giống peerContext.ts) ──
TOKEN_FILE="${TMPDIR:-/tmp}/omni-cli-token.txt"
node -e '
const {createRequire} = require("node:module");
const {createHmac} = require("node:crypto");
const fs = require("fs");
let omnirouteRoot = null;
for (const base of [process.env.HOME + "/.local/node-latest/lib/node_modules", "/usr/local/lib/node_modules", "/usr/lib/node_modules", "/opt/homebrew/lib/node_modules", (process.env.APPDATA ? process.env.APPDATA + "\\npm\\node_modules" : null)].filter(Boolean)) {
  try { if (fs.existsSync(base + "/omniroute")) { omnirouteRoot = base + "/omniroute"; break; } } catch {}
}
if (!omnirouteRoot) { console.error("omniroute package not found"); process.exit(1); }
const req = createRequire(omnirouteRoot + "/dist/server.js");
const {machineIdSync} = req("node-machine-id");
const rawId = machineIdSync(true);
const salt = process.env.OMNIROUTE_CLI_SALT || "omniroute-cli-auth-v1";
console.log(createHmac("sha256", rawId).update(salt).digest("hex"));
' > "$TOKEN_FILE"
CLI_TOKEN="$(cat "$TOKEN_FILE")"

auth_ok() {
    curl -s -m 6 -o /dev/null -w "%{http_code}" "$OMNI_BASE/api/provider-nodes" \
        -H "x-omniroute-cli-token: $CLI_TOKEN" | grep -q 200
}
if ! auth_ok; then
    echo "LỖI: không xác thực được với OmniRoute ở $OMNI_BASE (server chạy chưa? đúng máy?)" >&2
    exit 1
fi

# ── Xóa 1 node + mọi connection thuộc node đó (theo prefix) ──────────────
# $1 prefix của node cần xóa. Idempotent: node không có → không làm gì.
remove_node() {
    local prefix="$1"
    local node_id connection_id

    node_id="$(curl -s -m 10 "$OMNI_BASE/api/provider-nodes" -H "x-omniroute-cli-token: $CLI_TOKEN" \
        | node -e "let d='';process.stdin.on('data',c=>d+=c).on('end',()=>{
            const j=JSON.parse(d);
            const n=(j.nodes||[]).find(x=>x.prefix===process.argv[1]);
            console.log(n?n.id:'');})" "$prefix")"
    [ -n "$node_id" ] || { echo "· Node '$prefix' không còn — không cần xóa"; return 0; }

    # Xóa mọi connection trỏ tới node này trước, rồi xóa node
    curl -s -m 10 "$OMNI_BASE/api/providers" -H "x-omniroute-cli-token: $CLI_TOKEN" \
        | node -e "let d='';process.stdin.on('data',c=>d+=c).on('end',()=>{
            const j=JSON.parse(d);
            for (const c of (j.connections||[])) {
                if (c.provider===process.argv[1]) console.log(c.id);
            }})" "$node_id" | while read -r connection_id; do
        [ -n "$connection_id" ] || continue
        curl -s -m 10 -X DELETE "$OMNI_BASE/api/providers/$connection_id" \
            -H "x-omniroute-cli-token: $CLI_TOKEN" >/dev/null
        echo "· Đã xóa connection $connection_id"
    done

    curl -s -m 10 -X DELETE "$OMNI_BASE/api/provider-nodes/$node_id" \
        -H "x-omniroute-cli-token: $CLI_TOKEN" >/dev/null
    echo "· Đã xóa node '$prefix' ($node_id)"
}

# ── Tạo/cập nhật 1 node + connection ─────────────────────────────────────
# $1 tên hiển thị, $2 prefix, $3 baseUrl, $4 proxy key
register() {
    local name="$1" prefix="$2" url="$3" key="$4"
    local node_id connection_id

    # Tìm node theo prefix (idempotent)
    node_id="$(curl -s -m 10 "$OMNI_BASE/api/provider-nodes" -H "x-omniroute-cli-token: $CLI_TOKEN" \
        | node -e "let d='';process.stdin.on('data',c=>d+=c).on('end',()=>{
            const j=JSON.parse(d);
            const n=(j.nodes||[]).find(x=>x.prefix===process.argv[1]);
            console.log(n?n.id:'');})" "$prefix")"

    if [ -n "$node_id" ]; then
        echo "· Node '$name' đã có ($node_id) — update baseUrl"
        curl -s -m 10 -X PATCH "$OMNI_BASE/api/provider-nodes/$node_id" \
            -H "x-omniroute-cli-token: $CLI_TOKEN" -H "Content-Type: application/json" \
            -d "{\"name\":\"$name\",\"prefix\":\"$prefix\",\"apiType\":\"chat\",\"baseUrl\":\"$url\"}" >/dev/null
    else
        node_id="$(curl -s -m 10 -X POST "$OMNI_BASE/api/provider-nodes" \
            -H "x-omniroute-cli-token: $CLI_TOKEN" -H "Content-Type: application/json" \
            -d "{\"name\":\"$name\",\"prefix\":\"$prefix\",\"apiType\":\"chat\",\"baseUrl\":\"$url\"}" \
            | node -e "let d='';process.stdin.on('data',c=>d+=c).on('end',()=>{
                const j=JSON.parse(d); console.log(j.node?j.node.id:'');})")"
        [ -n "$node_id" ] || { echo "LỖI: không tạo được node $name" >&2; return 1; }
        echo "· Đã tạo node '$name' ($node_id)"
    fi

    # Tìm connection thuộc node này (provider = node id)
    connection_id="$(curl -s -m 10 "$OMNI_BASE/api/providers" -H "x-omniroute-cli-token: $CLI_TOKEN" \
        | node -e "let d='';process.stdin.on('data',c=>d+=c).on('end',()=>{
            const j=JSON.parse(d);
            const c=(j.connections||[]).find(x=>x.provider===process.argv[1]);
            console.log(c?c.id:'');})" "$node_id")"

    if [ -n "$connection_id" ]; then
        echo "· Connection đã có ($connection_id) — update key"
        curl -s -m 10 -X PATCH "$OMNI_BASE/api/providers/$connection_id" \
            -H "x-omniroute-cli-token: $CLI_TOKEN" -H "Content-Type: application/json" \
            -d "{\"apiKey\":\"$key\"}" >/dev/null
    else
        connection_id="$(curl -s -m 10 -X POST "$OMNI_BASE/api/providers" \
            -H "x-omniroute-cli-token: $CLI_TOKEN" -H "Content-Type: application/json" \
            -d "{\"provider\":\"$node_id\",\"name\":\"main\",\"apiKey\":\"$key\"}" \
            | node -e "let d='';process.stdin.on('data',c=>d+=c).on('end',()=>{
                const j=JSON.parse(d); console.log(j.connection?j.connection.id:'');})")"
        [ -n "$connection_id" ] || { echo "LỖI: không tạo được connection cho $name" >&2; return 1; }
        echo "· Đã tạo connection ($connection_id)"
    fi

    # Test kết nối
    local test
    test="$(curl -s -m 60 -X POST "$OMNI_BASE/api/providers/$connection_id/test" \
        -H "x-omniroute-cli-token: $CLI_TOKEN" -H "Content-Type: application/json" -d '{}')"
    if echo "$test" | grep -q '"valid":true'; then
        echo "✓ $name: kết nối OK → $url"
    else
        echo "✗ $name: test thất bại — proxy đang chạy không? ($test)" >&2
    fi
}

# Xóa DeepSeek Web2API local — đã được thay bằng OpenCode CLI bridge
remove_node "ds-local"

register "Qwen Web2API (local)"    "qwen-local" "$QWEN_PROXY_URL" "$QWEN_PROXY_KEY"
register "OpenCode CLI (local)"    "oc-local"   "$OC_PROXY_URL"   "$OC_PROXY_KEY"

echo
echo "Xong. Model dùng dạng: qwen-local/<model>  |  oc-local/<model>"
echo "oc-local free models: muse-spark-1.3-contributor-free, nemotron-3-ultra-free,"
echo "  mimo-v2.5-free, ling-3.0-flash-fin-free, big-pickle, ..."
echo "Danh sách: curl $OMNI_BASE/v1/models (key sk-...) hoặc xem Dashboard → Providers."
