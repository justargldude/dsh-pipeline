#!/usr/bin/env bash
# Khởi động proxy Qwen (chat.qwen.ai → OpenAI API)
# Endpoint: http://127.0.0.1:8200/v1  |  Key: sk-justar-local-qwen
# LƯU Ý: Aliyun WAF có thể punish IP tạm thời nếu gọi dồn dập.
# Nếu response rỗng/RGV587: DỪNG server, đợi 1-2 giờ rồi thử lại.
set -e
DIR="$(cd "$(dirname "$0")/../modules/qwen-web2api" && pwd)"
cd "$DIR"
KEY="sk-justar-local-qwen"
PORT=8200

# -f: HTTP 4xx/5xx coi là fail
alive() { curl -s -f -m 5 -o /dev/null "http://127.0.0.1:$PORT/v1/models" -H "Authorization: Bearer $KEY"; }

# Đã chạy và trả lời → thoát nhẹ
if alive; then
    echo "Qwen proxy already running on http://127.0.0.1:$PORT/v1"
    exit 0
fi

# Cổng bị chiếm → dọn từng PID (fuser có thể trả nhiều PID), SIGTERM trước SIGKILL
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
    # npm start sinh cụm process (npm wrapper → node) — dọn sạch bằng pattern hẹp,
    # chỉ match node chạy start.js của proxy này, không đụng process khác
    pkill -9 -f "node.*$DIR.*start\.js" 2>/dev/null || true
    pkill -9 -f "node.*start\.js" -D 2>/dev/null || true
    sleep 2
fi

mkdir -p logs
setsid nohup npm start >> logs/server.log 2>&1 < /dev/null &
CHILD_PID=$!

# Chờ sẵn sàng tối đa 40s (npm start + account init chậm hơn) — fail sớm nếu chết
for i in $(seq 1 40); do
    sleep 1
    if ! kill -0 "$CHILD_PID" 2>/dev/null; then
        # npm wrapper có thể exit sớm sau khi spawn node con — chỉ coi là chết
        # nếu CẢ port không lên trong 3s nữa
        sleep 3
        if alive; then
            echo "Qwen proxy STARTED on http://127.0.0.1:$PORT/v1"
            exit 0
        fi
        echo "FAILED — process died during startup. Tail log:"
        tail -8 logs/server.log
        exit 1
    fi
    if alive; then
        echo "Qwen proxy STARTED on http://127.0.0.1:$PORT/v1"
        exit 0
    fi
done
echo "FAILED — không ready sau 40s. Tail log:"
tail -8 logs/server.log
exit 1
