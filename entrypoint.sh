#!/bin/sh
# 网易云游戏 MAA 容器启动脚本
#
# 两种模式:
#   1) 默认(MaaCore 模式, 推荐):
#        自动拉取 MAA(MaaCore + 官方 resource) -> 导出运行环境 -> 前台运行 server.py
#        网页控制台「一键长草」直接使用官方 MaaCore(与 Windows 本机一致)
#   2) 桥接模式(可选, 传入参数触发): 后台运行 server.py -> 等待就绪 -> 前台运行 maa_bridge
#        例: docker run <镜像> --resource /app/resource --task Main
#
# 相关环境变量:
#   MAA_AUTO_PULL=1|0|force  自动拉取开关(默认 1: 缺失时拉取; force: 强制更新到最新版)
#   MAA_DATA_DIR             MAA 目录(动态库与 resource 同目录), 默认 /app/maa_data
#   MAA_RESOURCE_MIRROR      下载镜像前缀(国内加速), 如 https://ghfast.top/
#   NETEASE_TOKEN            登录 token(可选, 自动写入 token 文件)
set -e

TOKEN_FILE="${NETEASE_TOKEN_FILE:-token}"
MAA_DIR="${MAA_DATA_DIR:-/app/maa_data}"

# 1) 可选: 通过环境变量注入登录 token(无交互部署), 不覆盖已存在的 token 文件
if [ -n "${NETEASE_TOKEN}" ] && [ ! -s "${TOKEN_FILE}" ]; then
  echo "[entrypoint] writing NETEASE_TOKEN to ${TOKEN_FILE}"
  printf '%s\n' "${NETEASE_TOKEN}" > "${TOKEN_FILE}"
fi

# 2) 自动拉取 MAA 核心与资源(幂等: 已安装则跳过; MAA_AUTO_PULL=0 关闭; force 强制更新)
auto_pull() {
  echo "[entrypoint] ensuring MAA core & resource at ${MAA_DIR} ..."
  case "$1" in
    force) python scripts/fetch_maa_resource.py "${MAA_DIR}" --force ;;
    *)     python scripts/fetch_maa_resource.py "${MAA_DIR}" ;;
  esac
}

case "${MAA_AUTO_PULL:-1}" in
  0|false|no|off)
    echo "[entrypoint] MAA auto pull disabled (MAA_AUTO_PULL=${MAA_AUTO_PULL})"
    ;;
  *)
    # 拉取失败不阻断启动: 网页控制台/手动操作仍可用, 仅一键长草不可用
    if auto_pull "${MAA_AUTO_PULL}"; then
      echo "[entrypoint] MAA ready"
    else
      echo "[entrypoint] WARN: MAA 拉取失败: 网页控制台仍可用, 但一键长草不可用;" >&2
      echo "[entrypoint]       国内网络可设置 MAA_RESOURCE_MIRROR(如 https://ghfast.top/) 后重启容器" >&2
    fi
    ;;
esac

# 3) MaaCore 运行环境(动态库与 resource 同目录; 用户目录必须已存在)
export MAA_LIB_DIR="${MAA_LIB_DIR:-${MAA_DIR}}"
export MAA_DATA_DIR="${MAA_DIR}"
export MAA_USER_DIR="${MAA_USER_DIR:-${MAA_DIR}/debug}"
export FAKE_ADB_PATH="${FAKE_ADB_PATH:-/app/fake_adb/adb.sh}"
# Linux 下 dlopen 依赖库(onnxruntime 等, 与主库同目录)需由 LD_LIBRARY_PATH 解析
export LD_LIBRARY_PATH="${MAA_LIB_DIR}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
mkdir -p "${MAA_USER_DIR}"
if [ -f "${FAKE_ADB_PATH}" ]; then
  chmod +x "${FAKE_ADB_PATH}" 2>/dev/null || true
fi

# 4) 启动
if [ "$#" -gt 0 ]; then
  # ---- 桥接模式(兼容旧用法): 后台 HTTP 服务 + 前台 maa_bridge ----
  echo "[entrypoint] bridge mode: starting server.py in background ..."
  python server.py &
  SERVER_PID=$!

  # 等待 HTTP 服务就绪(最多 60s)
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

  # 转发停止信号, 避免 server.py 成为孤儿进程
  cleanup() {
    echo "[entrypoint] shutting down ..."
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  }
  trap cleanup INT TERM

  echo "[entrypoint] starting maa_bridge with args: $*"
  python -m maa_bridge.main "$@"
  EXIT_CODE=$?

  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
  exit "$EXIT_CODE"
fi

# ---- 默认模式: 前台运行云游戏服务(内含 MaaCore + 假 adb 一键长草) ----
echo "[entrypoint] starting server.py with MaaCore (data: ${MAA_DIR}) ..."
exec python server.py