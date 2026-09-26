#!/bin/sh
# 假 adb 入口(Linux 版): 把 MaaCore 发起的 adb 命令转发给 fake_adb.py
#
# MaaCore 通过 AsstConnect(adb_path=本文件) 调用, 之后以
#   "<adb_path> -s <serial> shell ..." 形式执行命令, 本脚本原样透传给 fake_adb.py。
# 可用 FAKE_ADB_PYTHON 指定解释器, 默认自动选择 python3 / python。
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="${FAKE_ADB_PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN=python3
  else
    PYTHON_BIN=python
  fi
fi

exec "$PYTHON_BIN" "$DIR/fake_adb.py" "$@"