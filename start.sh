#!/usr/bin/env bash
# start.sh — boot toàn stack: 2 proxy + omniroute (nếu chưa) + dsh web
# TUNNEL_HOSTS có thể chứa nhiều host cách nhau bởi space (repeatable flag), ví dụ tailscale + ngrok.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TUNNEL_HOSTS="${TUNNEL_HOSTS:-justargldude.tailccc7ac.ts.net}"
omni_alive() {
  code="$(curl -s -m 5 -o /dev/null -w "%{http_code}" http://127.0.0.1:20128/v1/models 2>/dev/null || true)"
  [ "${code:-000}" = "200" ] || [ "${code:-000}" = "401" ]
}
"$SCRIPT_DIR/bin/start-qwen.sh"
"$SCRIPT_DIR/bin/start-oc.sh"
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
echo "Tunnel host(s): $TUNNEL_HOSTS"
ARGS=()
for h in $TUNNEL_HOSTS; do ARGS+=(--trusted-host "$h"); done
exec dsh web --host 127.0.0.1 --port 3080 --no-open "${ARGS[@]}"
