#!/bin/sh
# 网易云游戏 MAA 桥接容器启动脚本
# 流程: 注入 token(可选) -> 后台启动 server.py -> 等待 HTTP 就绪 -> 前台运行 maa_bridge
set -e

TOKEN_FILE="${NETEASE_TOKEN_FILE:-token}"

# 1) 可选: 通过环境变量注入登录 token(无交互部署), 不覆盖已存在的 token 文件
if [ -n "${NETEASE_TOKEN}" ] && [ ! -s "${TOKEN_FILE}" ]; then
  echo "[entrypoint] writing NETEASE_TOKEN to ${TOKEN_FILE}"
  printf '%s\n' "${NETEASE_TOKEN}" > "${TOKEN_FILE}"
fi

# 2) 后台启动云游戏 HTTP 服务
echo "[entrypoint] starting server.py ..."
python server.py &
SERVER_PID=$!

# 3) 等待 HTTP 服务就绪(最多 60s)
ready=0
i=0
while [ "$i" -lt 60 ]; do
  if python - <<'PY'
import sys, urllib.request
try:
    urllib.request.urlopen("http://127.0.0.1:22888/info", timeout=2)
    sys.exit(0)
except Exception:
    sys.exit(1)
PY
  then
    ready=1
    break
  fi
  sleep 1
  i=$((i + 1))
done

if [ "$ready" -ne 1 ]; then
  echo "[entrypoint] server.py did not become ready within 60s" >&2
  kill "$SERVER_PID" 2>/dev/null || true
  exit 1
fi

# 4) 转发停止信号, 避免 server.py 成为孤儿进程
cleanup() {
  echo "[entrypoint] shutting down ..."
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
}
trap cleanup INT TERM

# 5) 前台运行桥接(透传 docker run 附加参数, 如 --resource /app/resource --task Main)
echo "[entrypoint] starting maa_bridge with args: $*"
python -m maa_bridge.main "$@"
EXIT_CODE=$?

# 6) 桥接退出后关闭 HTTP 服务, 保持退出码一致
kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true
exit "$EXIT_CODE"