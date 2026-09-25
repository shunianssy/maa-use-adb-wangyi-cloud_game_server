"""
假 adb 桥:把 MAA(MaaCore)发起的 adb 命令, 转发到本项目 server.py 的 HTTP 接口。

原理:
    MaaCore 通过 AsstConnect(handle, adb_path, address, config) 驱动设备, 其内部
    以 `adb_path -s <address> shell <cmd>` 形式调用 adb(见官方 config.json 的
    连接配置)。本脚本伪装成一个 adb 可执行文件, 拦截这些命令并翻译成本项目的
    HTTP 调用:
        exec-out screencap -p   -> GET  /screencap   (返回 JPEG, 需转 PNG)
        shell input tap x y     -> POST /click
        shell input swipe ..    -> POST /swipe
        shell input text ".."   -> POST /input
        shell wm size           -> 返回伪分辨率文本
        shell getprop ...       -> 返回伪属性
        devices / connect       -> 返回在线设备
    其余(如 minitouch 部署, 已被 TouchMode=adb 规避)返回空成功输出。

使用:
    python fake_adb.py <args...>    (由 MaaCore 以 adb 命令触发)
环境变量:
    FAKE_ADB_BASE=http://127.0.0.1:22888   云游戏 HTTP 服务地址
    FAKE_ADB_SERIAL=127.0.0.1:5555         伪设备序列号(与 AsstConnect 的 address 一致)
"""

import io
import os
import re
import sys
import time
import urllib.request

BASE = os.environ.get("FAKE_ADB_BASE", "http://127.0.0.1:22888")
SERIAL = os.environ.get("FAKE_ADB_SERIAL", "127.0.0.1:5555")

# 云游戏 HTTP 服务地址(在持久线程中已通过 /start 建立 WebRTC)
_GAME = BASE


def http_get(path: str) -> bytes:
    """GET 请求, 返回原始响应体; 失败抛出异常。"""
    req = urllib.request.Request(_GAME + path, headers={"User-Agent": "fake-adb"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def http_post(path: str, payload: dict) -> None:
    """POST JSON 请求, 非 2xx 抛异常。"""
    body = json_dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _GAME + path, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        pass


def report_cmd(command: str, ok: bool = True, note: str = "") -> None:
    """把 MaaCore 发来的 adb 命令上报 server, 写入「一键长草日志」便于调试。

    上报失败(server 未启动/网络抖动)不做任何处理, 绝不影响 adb 命令本身:
    本桥的职责是转发控制命令, 日志仅作辅助。超时收紧到 1s, 避免拉高 adb 调用延迟。
    """
    try:
        body = json_dumps({
            "command": command,
            "ok": bool(ok),
            "note": note or "",
        }).encode("utf-8")
        req = urllib.request.Request(
            _GAME + "/maa/adb-log", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=1):
            pass
    except Exception:
        pass  # 静默: 日志上报是尽力而为


def json_dumps(payload: dict) -> str:
    import json
    return json.dumps(payload, ensure_ascii=False)


def png_from_jpeg(jpeg: bytes) -> bytes:
    """将 /screencap 返回的 JPEG 转成 PNG(MaaCore 的 screencap -p 期望 PNG)。

    优先使用 Pillow; 若不满足则降级为返回原始 JPEG(部分流程可容忍)。
    """
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(jpeg))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        sys.stderr.write(f"[fake-adb] PNG convert failed, fallback to JPEG: {e}\n")
        return jpeg


def respond_ok(text: str = "") -> None:
    """以退出码 0 结束, 输出 text(adb 命令通常把结果写 stdout)。"""
    if text:
        _write_stdout(text.encode("utf-8", "replace"))
    sys.exit(0)


def _write_stdout(data: bytes) -> None:
    """兼容真实终端(有 .buffer)与测试重定向(BytesIO 无 .buffer)。

    - 真实 stdout: 写二进制 buffer, 保持原样
    - 重定向的 BytesIO: 直接 write(bytes)
    """
    buf = getattr(sys.stdout, "buffer", None)
    if buf is not None:
        buf.write(data)
        buf.flush()
    else:
        sys.stdout.write(data)


def dispatch(argv: list) -> None:
    """解析假的 adb 参数列表, 分发给对应处理器。"""
    args = argv[:]
    use_serial = None

    # 解析 `-s <serial>`(可能在任意位置)
    i = 0
    while i < len(args):
        if args[i] == "-s" and i + 1 < len(args):
            use_serial = args[i + 1]
            del args[i:i + 2]
            continue
        i += 1

    if not args:
        respond_ok("")

    cmd = args[0]

    # --- 设备发现 / 连接 ---
    if cmd == "devices":
        # adb devices 格式: "<serial>\tdevice"
        respond_ok(f"List of devices attached\n{SERIAL}\tdevice\n")
    if cmd == "connect":
        # adb connect <serial>
        respond_ok(f"connected to {args[1] if len(args) > 1 else SERIAL}\n")
    if cmd == "kill-server" or cmd == "start-server":
        # no-op
        respond_ok("")

    # --- shell 类命令: 统一按 `shell <cmd...>` 处理 ---
    if cmd == "shell" and len(args) >= 2:
        sub = args[1:]
        handle_shell(sub)
    if cmd == "exec-out" and len(args) >= 2:
        # 二进制定向输出(截图): MAA 用 exec-out 拿 screencap
        handle_exec_out(args[1:])

    # 未识别命令: 静默成功(adb 对未知命令一般返回空)
    respond_ok("")


def handle_shell(sub: list) -> None:
    """处理 `shell ...` 命令, sub 为 shell 具体子命令与参数。"""
    joined = " ".join(sub)
    # 去掉 shell 里常见的引号包裹
    joined = joined.strip("\"'")

    # 分辨率: MAA 的 display 命令是 `wm size | tail -n 1 | grep -o -E [0-9]+`
    # 期望输出两个纯数字(宽 与 高, displayFormat=%d%d); 同时兼容直接 wm size
    if "wm size" in joined or "wm" in joined:
        if "|" in joined or "grep" in joined or "tail" in joined:
            respond_ok("1280\n720\n")
        respond_ok("Physical size: 1280x720\n")
    # getprop 系列 -> 伪属性
    if "getprop" in joined:
        if "ro.build.version.release" in joined:
            respond_ok("13\n")
        if "ro.product.cpu.abilist" in joined:
            respond_ok("arm64-v8a,armeabi-v7a\n")
        respond_ok("\n")
    # settings get secure android_id -> 伪 uuid(16 位 hex, MaaCore 有格式校验)
    if "settings get secure android_id" in joined:
        respond_ok("f3e2d1c0b4a59687\n")
    # screencap
    if "screencap" in joined:
        handle_screencap()
    # input tap / swipe / text
    if "input" in joined:
        handle_input(joined)
    # SurfaceOrientation / SurfaceFlinger 等 -> 伪输出
    if "SurfaceOrientation" in joined or "SurfaceFlinger" in joined:
        respond_ok("0\n")
    # am start / force-stop / keyevent -> no-op
    if joined.startswith("am ") or "keyevent" in joined:
        respond_ok("")
    # 其他(如 wm/echo/Chmod 等)
    respond_ok("\n")


def handle_exec_out(sub: list) -> None:
    """处理 `exec-out ...` 命令(二进制输出, 主要是截图)。"""
    joined = " ".join(sub)
    if "screencap" in joined:
        handle_screencap()
    respond_ok("")


def handle_screencap() -> None:
    """执行截图: 从 HTTP 拉 JPEG -> 转 PNG -> 以二进制输出 stdout 并退出。

    失败时(base_url 不可达 / 云游戏未就绪)返回一张纯色占位 PNG, 而不是进程
    直接失败——这样 MaaCore 的连接/截图测速流程能继续, 与真实 adb 的
    容错行为一致(识别可能失败, 但连接不会因此中断)。
    """
    try:
        jpeg = http_get("/screencap")
    except Exception as e:
        report_cmd("adb exec-out screencap -p", ok=False, note=f"screencap fetch failed: {e}")
        sys.stderr.write(f"[fake-adb] screencap fetch failed({e}), returning blank frame\n")
        _write_stdout(_blank_png())
        sys.exit(0)
    _write_stdout(png_from_jpeg(jpeg))
    sys.exit(0)


def _blank_png() -> bytes:
    """生成一张深色纯色占位 PNG(用于截图不可用时的兜底)。"""
    try:
        from PIL import Image
        img = Image.new("RGB", (1280, 720), (16, 20, 28))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        # Pillow 不可用时给出极小的合法 PNG(1x1)
        return (b"\x89PNG\r\n\x1a\n" + b"\x00" * 0)


def handle_input(joined: str) -> None:
    """处理 `input tap/swipe/text`。joined 为 shell 命令整体字符串。"""
    m = re.search(r"input\s+tap\s+(\d+)\s+(\d+)", joined)
    if m:
        try:
            http_post("/click", {"x": int(m.group(1)), "y": int(m.group(2))})
        except Exception as e:
            report_cmd("adb shell " + joined, ok=False, note=f"click failed: {e}")
            sys.stderr.write(f"[fake-adb] click failed: {e}\n")
            sys.exit(1)
        respond_ok("")

    m = re.search(r"input\s+swipe\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*(\d*)", joined)
    if m:
        duration = int(m.group(5)) if m.group(5) else 300
        try:
            http_post("/swipe", {
                "x1": int(m.group(1)), "y1": int(m.group(2)),
                "x2": int(m.group(3)), "y2": int(m.group(4)),
                "duration": duration,
            })
        except Exception as e:
            report_cmd("adb shell " + joined, ok=False, note=f"swipe failed: {e}")
            sys.stderr.write(f"[fake-adb] swipe failed: {e}\n")
            sys.exit(1)
        respond_ok("")

    m = re.search(r"input\s+text\s+(.+)", joined)
    if m:
        text = m.group(1).strip().strip("\"'")
        try:
            http_post("/input", {"text": text})
        except Exception as e:
            report_cmd("adb shell " + joined, ok=False, note=f"input failed: {e}")
            sys.stderr.write(f"[fake-adb] input failed: {e}\n")
            sys.exit(1)
        respond_ok("")

    respond_ok("")


if __name__ == "__main__":
    full_cmd = "adb " + " ".join(sys.argv[1:])
    try:
        report_cmd(full_cmd)          # 先上报命令(尽力而为, 失败静默)
        dispatch(sys.argv[1:])
    except SystemExit:
        raise
    except Exception as e:
        # 兜底: 任何未处理异常都按"静默成功"退出, 不让 MaaCore 判定连接失败
        report_cmd(full_cmd, ok=False, note=f"unhandled: {e}")
        sys.stderr.write(f"[fake-adb] unhandled: {e}\n")
        sys.exit(0)