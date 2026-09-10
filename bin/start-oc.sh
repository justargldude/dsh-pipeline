#!/usr/bin/env bash
# Khởi động OpenCode bridge proxy (opencode serve → OpenAI API)
# Endpoint: http://127.0.0.1:8300/v1  |  Key: sk-justar-local-oc
# Model free qua gateway opencode.ai (zen): muse-spark, nemotron, mimo, ling, big-pickle.
# LƯU Ý: gateway free chậm — 1 lần gọi chat có thể mất 60-180s (bình thường, không phải lỗi).
set -e
DIR="$(cd "$(dirname "$0")/../modules/oc-proxy" && pwd)"
cd "$DIR"
KEY="sk-justar-local-oc"
PORT=8300

# -f: HTTP 4xx/5xx coi là fail (không chỉ connection error)
alive() { curl -s -f -m 5 -o /dev/null "http://127.0.0.1:$PORT/v1/models" -H "Authorization: Bearer $KEY"; }

# Đã chạy và trả lời → thoát nhẹ
if alive; then
    echo "OpenCode proxy already running on http://127.0.0.1:$PORT/v1"
    exit 0
fi

# Cổng bị chiếm bởi process (treo hoặc đang xử lý request) → dọn từng PID
if ss -tln 2>/dev/null | grep -q "127.0.0.1:$PORT "; then
    for PID in $(fuser "$PORT/tcp" 2>/dev/null); do
        echo "Port $PORT held by PID $PID — SIGTERM first"
        kill "$PID" 2>/dev/null || true
    done
    sleep 3
    for PID in $(fuser "$PORT/tcp" 2>/dev/null); do
        echo "PID $PID still alive — SIGKILL"
        kill -9 "$PID" 2>/dev/null || true
    done
    sleep 2
fi

# Nếu opencode serve (upstream 8124) chưa chạy, server.mjs sẽ tự spawn nó.
mkdir -p logs
setsid nohup node server.mjs >> logs/oc-proxy-start.log 2>&1 < /dev/null &
CHILD_PID=$!

# Chờ sẵn sàng tối đa 40s (spawn opencode serve có thể mất thêm ~20s)
for i in $(seq 1 40); do
    sleep 1
    if alive; then
        echo "OpenCode proxy STARTED on http://127.0.0.1:$PORT/v1"
        echo "Free models: muse-spark-1.3-contributor-free, nemotron-3-ultra-free, mimo-v2.5-free, ..."
        exit 0
    fi
    if ! kill -0 "$CHILD_PID" 2>/dev/null; then
        echo "FAILED — process died during startup. Tail log:"
        tail -8 logs/oc-proxy-start.log
        exit 1
    fi
done
echo "FAILED — không ready sau 40s. Tail log:"
tail -8 logs/oc-proxy-start.log
exit 1
