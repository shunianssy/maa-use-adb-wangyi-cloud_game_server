import asyncio
import json
import os
import time
import sys
import signal
import platform
import logging
import base64
import subprocess
import datetime
from typing import Optional, Coroutine
try:
    from websockets.legacy.client import WebSocketClientProtocol
except ImportError:  # websockets >=12 removed legacy package
    from websockets.client import WebSocketClientProtocol  # type: ignore[attr-defined]
from io import BytesIO

import requests
from aiohttp import web

from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaRelay
from sdk.wsconnect import (
    connect, object_from_string, encode_mess,
    pack_message, send_action, login, exit_game
)
from sdk.signin import netease_signin
from maa_settings import MaaSettings

# --- 配置 ---
# 服务器端部署时可通过环境变量覆盖以下配置(见 Docker/生产环境使用方式)
# NETEASE_TOKEN: 登录凭证,直接写入 TOKEN_FILE,实现无交互部署
GAME_CODE = os.environ.get("NETEASE_GAME_CODE", "mrfz")
TOKEN_FILE = os.environ.get("NETEASE_TOKEN_FILE", "token")
HOST = os.environ.get("NETEASE_HOST", "127.0.0.1")
PORT = int(os.environ.get("NETEASE_PORT", "22888"))
WIDTH = int(os.environ.get("NETEASE_WIDTH", "1280"))
HEIGHT = int(os.environ.get("NETEASE_HEIGHT", "720"))

# --- WebUI 配置 ---
# WEBUI_DIR: 前端静态资源目录(相对项目根), 可通过环境变量覆盖
WEBUI_DIR = os.environ.get("NETEASE_WEBUI_DIR", os.path.join(os.path.dirname(__file__), "webui"))

# --- MAA 每日任务配置 ---
# NETEASE_MAA_RESOURCE: MAA pipeline 资源包目录(可指向 maa-cli 资源目录或自带示例)
NETEASE_MAA_RESOURCE = os.environ.get("NETEASE_MAA_RESOURCE", os.path.join(os.path.dirname(__file__), "maa_pipeline"))
# NETEASE_MAA_SETTINGS: 一键长草设置文件路径(含任务开关/作战参数/每日定时等)
NETEASE_MAA_SETTINGS = os.environ.get("NETEASE_MAA_SETTINGS", os.path.join(os.path.dirname(__file__), "maa_settings.json"))

# 一键长草设置实例(启动即加载; 每日定时任务读取它做触发)
maa_settings = MaaSettings(NETEASE_MAA_SETTINGS)

# --- 颜色定义 ---
class Colors:
    RESET = '\033[0m'
    RED = '\033[31m'
    GREEN = '\033[32m'
    YELLOW = '\033[33m'
    BLUE = '\033[34m'
    CYAN = '\033[36m'
    BOLD = '\033[1m'

if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


# --- API 解码工具 ---
def decode_api_response(encoded_data: str) -> dict:
    """Decode base64 encoded API response from cg.163.com"""
    try:
        raw_bytes = base64.b64decode(encoded_data)
    except Exception:
        return {}

    # Find the decryption key by detecting JSON start pattern
    decode_key = None
    if len(raw_bytes) >= 2:
        first_byte = raw_bytes[0]
        second_byte = raw_bytes[1]
        for key_candidate in range(256):
            char1 = chr((first_byte - key_candidate) % 256)
            char2 = chr((second_byte - key_candidate) % 256)
            if char1 == "{" and char2 == "\"":
                decode_key = key_candidate
                break

    if decode_key is None:
        return {}

    decoded_str = "".join(chr((b - decode_key) % 256) for b in raw_bytes)
    try:
        return json.loads(decoded_str)
    except json.JSONDecodeError:
        return {}


def fetch_user_info(token: str) -> dict:
    """Fetch user info including remaining game time from cg.163.com API"""
    try:
        headers = {"Authorization": f"Bearer {token}"}
        response = requests.get(
            "https://n.cg.163.com/api/v2/users/@me",
            headers=headers,
            timeout=10
        )
        if response.status_code != 200:
            return {}
        return decode_api_response(response.text)
    except Exception as e:
        logging.warning(f"Failed to fetch user info: {e}")
        return {}


# --- 全局状态 ---
class AppState:
    def __init__(self):
        self.pc: Optional[RTCPeerConnection] = None
        self.sock: Optional[WebSocketClientProtocol] = None
        self.snapshotter: Optional['VideoSnapshotter'] = None
        self.is_ready = False
        self.width = WIDTH
        self.height = HEIGHT
        self.token: Optional[str] = None
        self.cloud_game_task: Optional[asyncio.Task] = None
        self.shutdown_event: Optional[asyncio.Event] = None

    def reset(self):
        self.pc = None
        self.sock = None
        self.snapshotter = None
        self.is_ready = False
        self.token = None
        self.cloud_game_task = None

app_state = AppState()

# --- MAA 每日任务协调器(后台线程运行) ---
from maa_coordinator import MaaCoordinator
maa_coord = None  # 惰性初始化, 在 run_server 中创建(需 HOST/PORT 确定后)
_daily_checker_task: Optional[asyncio.Task] = None  # 每日定时检查协程

# 活跃 WebUI WebSocket 客户端集合(用于状态主动广播)
_ws_clients: set = set()


async def broadcast_status(with_remaining=False):
    """向所有活跃 WebUI 客户端广播当前云游戏状态。

    当云游戏就绪/断开时调用, 使前端无需依赖连接时间点的旧快照。
    """
    if not _ws_clients:
        return
    payload = {"type": "status", **await status_payload(with_remaining=with_remaining)}
    for ws in list(_ws_clients):
        if ws.closed:
            _ws_clients.discard(ws)
            continue
        try:
            await ws.send_str(json.dumps(payload))
        except (ConnectionResetError, RuntimeError):
            _ws_clients.discard(ws)

# --- 快照工具 (从 ark-demo.py 移植并修改) ---
class VideoSnapshotter:
    def __init__(self, video_track):
        self._track = video_track
        self._task: Optional[asyncio.Task] = None
        self._last_frame = None
        self._got_first = asyncio.Event()
        self._running = False
        self._last_recv_ts: float = 0.0

    def start(self):
        if self._task:
            return
        self._running = True
        self._task = asyncio.create_task(self._pump())

    async def _pump(self):
        try:
            while self._running:
                frame = await self._track.recv()
                self._last_frame = frame
                self._last_recv_ts = time.time()
                if not self._got_first.is_set():
                    self._got_first.set()
        except asyncio.CancelledError:
            pass # 任务被取消是正常的
        except Exception:
            pass

    async def wait_ready(self, timeout=20.0):
        try:
            await asyncio.wait_for(self._got_first.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def snapshot_bytes(self, fmt="jpeg") -> Optional[bytes]:
        if not self._got_first.is_set():
            return None
        if self._last_recv_ts and time.time() - self._last_recv_ts > 10:
            self._last_frame = None
            if app_state.is_ready:
                app_state.is_ready = False
                logging.warning("Video stream stale for over 10s; marking service as not ready")
            return None
        frame = self._last_frame
        if frame is None:
            return None
        try:
            from PIL import Image
            img = frame.to_image()
            with BytesIO() as bio:
                img.save(bio, format=fmt.upper())
                return bio.getvalue()
        except Exception:
            return None

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

# --- API 处理函数 ---
async def status_payload(with_remaining: bool = False) -> dict:
    """组装统一的状态负载, HTTP /info 与 WebSocket 共用。

    Args:
        with_remaining: 是否额外拉取剩余游戏时长(较慢, 仅 /info 与需要时开启)
    """
    if not app_state.is_ready:
        return {
            "status": "connecting" if app_state.cloud_game_task and not app_state.cloud_game_task.done() else "disconnected",
            "message": "Cloud gaming service not ready or not connected.",
        }

    payload = {"status": "ok", "width": app_state.width, "height": app_state.height}

    if with_remaining and app_state.token:
        loop = asyncio.get_running_loop()
        user_info = loop.run_in_executor(None, fetch_user_info, app_state.token)
        info = None
        try:
            # 限制外部 API 拉取耗时, 避免阻塞 WebSocket 帧推送
            info = await asyncio.wait_for(user_info, timeout=3)
        except (asyncio.TimeoutError, Exception):
            pass
        if info:
            payload["remaining_time"] = info.get("free_time_left")

    return payload


async def handle_info(request: web.Request):
    return web.json_response(await status_payload(with_remaining=True))


async def do_click(x: int, y: int):
    """执行一次点击; 返回 (ok, err_msg)。HTTP 与 WebSocket 共用。"""
    if not app_state.is_ready or not app_state.sock:
        return False, "Service not ready"

    try:
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        return False, "Invalid coordinates"

    logging.warning(f"Action: Click at ({x}, {y})")
    move_action = pack_message("mm", {"x": x, "y": y})
    await send_action(app_state.sock, move_action)
    await asyncio.sleep(0.05)
    click_action = pack_message("cm", {"x": x, "y": y})
    await send_action(app_state.sock, click_action)
    return True, None


async def handle_click(request: web.Request):
    if not app_state.is_ready or not app_state.sock:
        return web.json_response({"status": "error", "message": "Service not ready"}, status=503)

    try:
        data = await request.json()
        x, y = int(data['x']), int(data['y'])
    except (json.JSONDecodeError, KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Invalid request body"}, status=400)

    ok, err = await do_click(x, y)
    if not ok:
        return web.json_response({"status": "error", "message": err or "Click failed"}, status=503)
    return web.json_response({"status": "ok"})

async def handle_screencap(request: web.Request):
    if not app_state.is_ready or not app_state.snapshotter:
        return web.json_response({"status": "error", "message": "Service not ready"}, status=503)

    image_bytes = await app_state.snapshotter.snapshot_bytes(fmt="jpeg")
    if not image_bytes:
        return web.json_response({"status": "error", "message": "Failed to capture screen"}, status=500)

    return web.Response(body=image_bytes, content_type="image/jpeg")

async def handle_click(request: web.Request):
    if not app_state.is_ready or not app_state.sock:
        return web.json_response({"status": "error", "message": "Service not ready"}, status=503)
    
    try:
        data = await request.json()
        x, y = int(data['x']), int(data['y'])
    except (json.JSONDecodeError, KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Invalid request body"}, status=400)

    logging.warning(f"Action: Click at ({x}, {y})")
    
    # 1. 先移动鼠标到目标位置
    move_action = pack_message("mm", {"x": x, "y": y})
    await send_action(app_state.sock, move_action)
    await asyncio.sleep(0.05) # 短暂等待，模拟真实操作

    # 2. 再执行点击操作
    click_action = pack_message("cm", {"x": x, "y": y})
    await send_action(app_state.sock, click_action)
    
    return web.json_response({"status": "ok"})

async def do_swipe(start_x, start_y, end_x, end_y, swipe_duration):
    """执行一次滑动; 返回 (ok, err_msg)。HTTP 与 WebSocket 共用。

    性能说明: 滑动被拆成若干触摸点, 逐点经 send_action 发送。曾因
    send_action 每条命令都等 PONG 导致高延迟网络下整条命令卡死;
    现在逐点使用 wait_pong=False(只发不等), 并限制点数上限, 且越界
    长时间未收到 PONG 也不会阻塞。
    """
    if not app_state.is_ready or not app_state.sock:
        return False, "Service not ready"

    try:
        start_x, start_y = int(start_x), int(start_y)
        end_x, end_y = int(end_x), int(end_y)
        swipe_duration = int(swipe_duration)
    except (TypeError, ValueError):
        return False, "Invalid swipe parameters"

    if swipe_duration <= 0:
        return False, "Invalid duration"

    logging.warning(f"Action: Swipe from ({start_x}, {start_y}) to ({end_x}, {end_y}) in {swipe_duration}ms")

    def normalize_coord(pixel_val: int, dimension: int) -> int:
        """Normalize pixel coordinate to 0-65535 range"""
        clamped = max(0, min(pixel_val, dimension - 1))
        return int((65535 * clamped) // dimension)

    screen_width = app_state.width
    screen_height = app_state.height

    # 控制触摸点数量: 既保证轨迹平滑, 又避免命令过多堆积(上限 24 点)
    num_points = min(24, max(5, swipe_duration // 40))
    interval = swipe_duration / 1000.0 / num_points
    touch_id = 0

    def create_touch_cmd(evt_type: int, px: int, py: int, tid: int) -> dict:
        """Create touch input command with normalized coordinates"""
        norm_x = normalize_coord(px, screen_width)
        norm_y = normalize_coord(py, screen_height)
        timestamp = str(int(time.time() * 1000))
        cmd_str = f"{evt_type} {norm_x} {norm_y} {tid}"
        return {"id": timestamp, "op": "input", "data": {"cmd": cmd_str}}

    try:
        down_cmd = create_touch_cmd(1, start_x, start_y, touch_id)
        await send_action(app_state.sock, down_cmd, wait_pong=False)
        await asyncio.sleep(0.02)

        delta_x = end_x - start_x
        delta_y = end_y - start_y
        for step in range(1, num_points + 1):
            progress = step / num_points
            current_x = int(start_x + delta_x * progress)
            current_y = int(start_y + delta_y * progress)
            move_cmd = create_touch_cmd(2, current_x, current_y, touch_id)
            await send_action(app_state.sock, move_cmd, wait_pong=False)
            await asyncio.sleep(interval)

        up_cmd = create_touch_cmd(3, end_x, end_y, touch_id)
        await send_action(app_state.sock, up_cmd, wait_pong=False)
    except Exception as e:
        # 中途发送失败不无限等待, 立即返回, 让 MAA 感知并可重试
        logging.error("Swipe send failed: %s", e)
        return False, f"Swipe send failed: {e}"
    return True, None


async def handle_swipe(request: web.Request):
    if not app_state.is_ready or not app_state.sock:
        return web.json_response({"status": "error", "message": "Service not ready"}, status=503)

    try:
        data = await request.json()
        start_x, start_y = int(data['x1']), int(data['y1'])
        end_x, end_y = int(data['x2']), int(data['y2'])
        swipe_duration = int(data['duration'])
    except (json.JSONDecodeError, KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Invalid request body"}, status=400)

    ok, err = await do_swipe(start_x, start_y, end_x, end_y, swipe_duration)
    if not ok:
        return web.json_response({"status": "error", "message": err or "Swipe failed"}, status=503)
    return web.json_response({"status": "ok"})

async def do_input(text: str):
    """逐字输入文本; 返回 (ok, err_msg)。HTTP 与 WebSocket 共用。"""
    if not app_state.is_ready or not app_state.sock:
        return False, "Service not ready"

    try:
        text = str(text)
    except Exception:
        return False, "Invalid text"

    for char in text:
        action = pack_message("ip", {"word": char})
        await send_action(app_state.sock, action)
        await asyncio.sleep(0.05)

    return True, None


async def handle_input(request: web.Request):
    if not app_state.is_ready or not app_state.sock:
        return web.json_response({"status": "error", "message": "Service not ready"}, status=503)

    try:
        data = await request.json()
        text = data['text']
    except (json.JSONDecodeError, KeyError, TypeError):
        return web.json_response({"status": "error", "message": "Invalid request body, 'text' field is required"}, status=400)

    ok, err = await do_input(text)
    if not ok:
        return web.json_response({"status": "error", "message": err or "Input failed"}, status=503)
    return web.json_response({"status": "ok"})

async def handle_start(request: web.Request):
    ok, message = await _handle_start_internal()
    return web.json_response({"status": "ok" if ok else "error", "message": message})

async def handle_exit(request: web.Request):
    ok, message = await _handle_exit_internal()
    return web.json_response({"status": "ok" if ok else "error", "message": message})

# --- WebUI / WebSocket 处理 ---

async def handle_ui(request: web.Request):
    """返回 WebUI 首页(index.html)。"""
    index_path = os.path.join(WEBUI_DIR, "index.html")
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        logging.error("WebUI index not found at %s", index_path)
        raise web.HTTPNotFound(text="WebUI index not found")
    return web.Response(text=content, content_type="text/html", charset="utf-8")


async def handle_ws(request: web.Request):
    """WebSocket 端点:负责 WebUI 控制台的实时推帧与命令转发。

    协议:
      服务器 -> 客户端:
        {"type": "status", ...状态负载}
        {"type": "frame", "image": "<base64 JPEG>"}
        {"type": "ack", "command": "...", "ok": bool, "message": "..."}
      客户端 -> 服务器:
        {"type": "click",  "x": int, "y": int}
        {"type": "swipe",  "x1","y1","x2","y2","duration"}
        {"type": "text",   "text": str}
        {"type": "start"}
        {"type": "exit"}
        {"type": "resizeRequest"}  # 触发一次较慢的实时信息(剩余时长)
    """
    ws = web.WebSocketResponse(heartbeat=15, max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)
    logging.info("WebUI client connected: %s", request.remote)
    _ws_clients.add(ws)  # 加入活跃客户端集合, 便于状态广播
    connected = True

    async def push_status(with_remaining=False):
        """推送一次状态负载到客户端。"""
        if not ws.closed:
            await ws.send_str(json.dumps(
                {"type": "status", **await status_payload(with_remaining=with_remaining)}))

    async def stream_frames():
        """实时推帧协程:有可用的快照器时持续返回 JPEG 帧。"""
        while connected and not ws.closed:
            snap = app_state.snapshotter
            if app_state.is_ready and snap:
                data = await snap.snapshot_bytes(fmt="jpeg")
                if data:
                    try:
                        b64 = base64.b64encode(data).decode("ascii")
                        await ws.send_str(json.dumps({"type": "frame", "image": b64}))
                    except Exception:  # 客户端中途断开等情况, 结束推帧避免刷屏报错
                        break
            # 节流:约 8 FPS, 兼顾流畅与 CPU 占用
            await asyncio.sleep(0.12)

    stream_task = asyncio.create_task(stream_frames())

    try:
        # 连接后立即推送一次初始状态
        await push_status(with_remaining=True)

        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    cmd = json.loads(msg.data)
                except json.JSONDecodeError:
                    logging.warning("Invalid WS message from %s", request.remote)
                    continue

                ctype = cmd.get("type", "")
                ok, err = True, None
                try:
                    if ctype == "click":
                        ok, err = await do_click(cmd.get("x"), cmd.get("y"))
                    elif ctype == "swipe":
                        ok, err = await do_swipe(
                            cmd.get("x1"), cmd.get("y1"),
                            cmd.get("x2"), cmd.get("y2"),
                            cmd.get("duration"),
                        )
                    elif ctype == "text":
                        ok, err = await do_input(cmd.get("text", ""))
                    elif ctype == "start":
                        ok, err = await _handle_start_internal()
                    elif ctype == "exit":
                        ok, err = await _handle_exit_internal()
                    elif ctype == "resizeRequest":
                        ok, err = True, None
                    else:
                        logging.warning("Unknown WS command %r from %s", ctype, request.remote)
                        continue

                    if not ws.closed:
                        await ws.send_str(json.dumps({"type": "ack", "command": ctype, "ok": bool(ok), "message": err or ""}))
                except Exception as e:  # 单条命令失败不拖垮连接
                    logging.error("WS command %s failed: %s", ctype, e)
                    if not ws.closed:
                        await ws.send_str(json.dumps({"type": "ack", "command": ctype, "ok": False, "message": str(e)}))
            elif msg.type == web.WSMsgType.CLOSE:
                break
    finally:
        _ws_clients.discard(ws)  # 连接关闭时移出活跃集合
        connected = False
        stream_task.cancel()
        try:
            await stream_task
        except asyncio.CancelledError:
            pass
        logging.info("WebUI client disconnected: %s", request.remote)
        if not ws.closed:
            await ws.close()

    return ws


async def _handle_start_internal():
    """启动云游戏连接的内部逻辑(HTTP /start 与 WebSocket 复用)。"""
    if app_state.cloud_game_task and not app_state.cloud_game_task.done():
        return True, "Cloud game connection already active."
    if app_state.cloud_game_task and app_state.cloud_game_task.done():
        app_state.cloud_game_task = None
    app_state.cloud_game_task = asyncio.create_task(run_cloud_game())
    return True, "Cloud game connection initiated."


async def _handle_exit_internal():
    """断开云游戏连接的内部逻辑(HTTP /exit 与 WebSocket 复用)。"""
    if not app_state.cloud_game_task or app_state.cloud_game_task.done():
        return True, "No active cloud game connection."
    print(f"{Colors.CYAN}[*] Received exit request. Disconnecting from cloud game...{Colors.RESET}")
    app_state.cloud_game_task.cancel()
    try:
        await app_state.cloud_game_task
    except asyncio.CancelledError:
        pass  # Expected
    return True, "Cloud game connection terminated."


async def handle_root(request: web.Request):
    """根路径:直接进入 WebUI 控制台。"""
    return await handle_ui(request)


# --- MAA 每日任务接口(WebUI 一键长草) ---

def _get_coord() -> Optional['MaaCoordinator']:
    """返回已初始化的协调器, 未初始化则创建。"""
    global maa_coord
    if maa_coord is None:
        maa_coord = MaaCoordinator()
    return maa_coord


# 假 adb 命令日志节流表: key -> (最后一次记录时间, 期间同类命令计数)
# MAA 识别阶段截图频率很高(每帧一条 exec-out screencap), 若不节流会刷爆日志。
_adb_log_throttle: dict = {}


def _adb_cmd_key(command: str) -> str:
    """把 adb 命令粗略归为几类, 作为节流键(避免 `-s <serial>` 干扰归类)。"""
    lowered = command.lower()
    if "screencap" in lowered:
        return "screencap"
    if "input tap" in lowered:
        return "input tap"
    if "input swipe" in lowered:
        return "input swipe"
    if "input text" in lowered:
        return "input text"
    return "other"


async def handle_maa_adb_log(request: web.Request):
    """接收 fake_adb 上报的 adb 命令, 节流后写入「一键长草日志」。

    请求体: {"command": str, "ok": bool, "note": str}
    """
    try:
        data = await request.json()
    except (json.JSONDecodeError, AttributeError):
        return web.json_response({"status": "error", "message": "invalid body"}, status=400)

    command = data.get("command", "")
    if not command:
        return web.json_response({"status": "error", "message": "empty command"}, status=400)
    ok = bool(data.get("ok", True))
    note = data.get("note", "") or ""

    # 节流: 同类命令 5 秒内只记一条, 其余累计计数待下次记录时补充说明
    key = _adb_cmd_key(command)
    now = time.monotonic()
    last, cnt = _adb_log_throttle.get(key, (0.0, 0))
    if now - last < 5.0:
        _adb_log_throttle[key] = (last, cnt + 1)
        return web.json_response({"status": "ok", "throttled": True})
    prev = f" (近5秒内同类命令取消记录 x{cnt})" if cnt else ""
    _adb_log_throttle[key] = (now, 0)

    _get_coord().status.log_adb(command, ok, note + prev)
    return web.json_response({"status": "ok"})


async def handle_maa_start(request: web.Request):
    """启动「一键长草」: 接收启用的任务 key 列表并执行。

    请求体: {"tasks": [str...], "options": {作战选项(可选)}}
    - tasks 可含 "annihilation"(每周剿灭)
    - options.fight: {stage, times, series, medicine_enabled, medicine}
    - options.annihilation: {times}
    未提供的作战项回落到已保存设置(maa_settings.json)。
    """
    try:
        data = await request.json()
        tasks = data.get('tasks', [])
        options = data.get('options') or {}
    except (json.JSONDecodeError, AttributeError):
        return web.json_response({"status": "error", "message": "Invalid request body"}, status=400)

    if not isinstance(tasks, list) or not tasks:
        return web.json_response({"status": "error", "message": "请至少启用一个任务"}, status=400)

    # 作战选项与已保存设置合并: 表单值优先, 缺失字段用设置默认
    saved = maa_settings.get()
    merged = {
        "fight": {**saved["fight"], **(options.get("fight") or {})},
        "annihilation": {**saved["annihilation"], **(options.get("annihilation") or {})},
        "signin": {**saved["signin"], **(options.get("signin") or {})},
        "award": {**saved["award"], **(options.get("award") or {})},
    }

    coord = _get_coord()
    do_signin = bool(merged.get("signin", {}).get("enabled", False))
    started = coord.start(tasks, options=merged,
                          signin=do_signin, token=app_state.token)
    if not started:
        return web.json_response({"status": "error", "message": coord.status.snapshot().get("error") or "已有一个任务在运行或未选择任务"}, status=400)

    return web.json_response({"status": "ok", "message": "任务已开始"})


async def handle_maa_settings_get(request: web.Request):
    """返回已保存的一键长草设置(前端据此恢复表单)。"""
    return web.json_response({"status": "ok", "settings": maa_settings.get()})


async def handle_maa_settings_save(request: web.Request):
    """合并保存一键长草设置(前端表单改动时调用)。"""
    try:
        patch = await request.json()
    except (json.JSONDecodeError, AttributeError):
        return web.json_response({"status": "error", "message": "Invalid request body"}, status=400)
    if not isinstance(patch, dict):
        return web.json_response({"status": "error", "message": "settings must be an object"}, status=400)
    updated = maa_settings.update(patch)
    return web.json_response({"status": "ok", "settings": updated})


async def handle_maa_signin(request: web.Request):
    """执行网易云游戏每日签到(平台奖励), 结果写入一键长草日志并返回。"""
    token = app_state.token
    if not token:
        return web.json_response(
            {"status": "error", "message": "未登录云游戏账号(token 缺失), 请先启动云游戏"},
            status=400)

    coord = _get_coord()
    coord.status.log("发起网易云游戏签到...")
    loop = asyncio.get_running_loop()
    try:
        # requests 为阻塞调用, 放入线程池避免阻塞事件循环
        result = await asyncio.wait_for(
            loop.run_in_executor(None, netease_signin, token), timeout=15)
    except (asyncio.TimeoutError, Exception) as e:
        result = {"ok": False, "message": f"签到超时/异常: {e}"}

    coord.status.log(f"签到{'成功' if result.get('ok') else '失败'}: "
                     f"{result.get('endpoint') or result.get('message')}")
    return web.json_response({"status": "ok" if result.get("ok") else "error", "result": result})


async def _wait_game_ready(timeout: float = 90.0) -> bool:
    """轮询等待云游戏就绪(is_ready), 超时返回 False。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app_state.is_ready:
            return True
        await asyncio.sleep(1)
    return False


async def daily_checker():
    """每日定时任务: 到达设置时间且当天未执行过时, 自动启动云游戏并一键长草。

    触发时机: 设置 enable=true 且当前 HH:MM == daily.time 且 last_daily_run != 今天。
    触发后: 1) 启动云游戏 2) 等待就绪 3) 按已保存设置启动一键长草。
    """
    while True:
        try:
            cfg = maa_settings.get()
            daily = cfg.get("daily") or {}
            if daily.get("enabled"):
                now = datetime.datetime.now()
                hhmm = now.strftime("%H:%M")
                today = now.strftime("%Y-%m-%d")
                if hhmm == str(daily.get("time", "")) and cfg.get("last_daily_run") != today:
                    logging.info("每日定时触发: %s, 开始自动执行", hhmm)
                    maa_settings.update({"last_daily_run": today})
                    maa_log = _get_coord().status
                    maa_log.log(f"每日定时任务触发({hhmm}), 启动云游戏...")
                    await _handle_start_internal()
                    if not await _wait_game_ready(90):
                        maa_log.log("每日定时: 等待云游戏就绪超时")
                        continue
                    # 组装任务: 已启用开关 + 可选剿灭
                    enabled = [k for k, v in (cfg.get("tasks") or {}).items() if v]
                    if (cfg.get("annihilation") or {}).get("enabled"):
                        enabled.append("annihilation")
                    if enabled:
                        opts = {"fight": cfg["fight"], "annihilation": cfg["annihilation"],
                                "award": cfg["award"]}
                        do_signin = bool((cfg.get("signin") or {}).get("enabled", False))
                        _get_coord().start(enabled, options=opts,
                                           signin=do_signin, token=app_state.token)
                        maa_log.log(f"每日定时: 自动执行一键长草 {', '.join(enabled)}")
                        await broadcast_status()
            await asyncio.sleep(30)   # 每 30 秒检查一次, 粒度足够
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.exception("每日定时检查异常")
            await asyncio.sleep(30)


async def handle_maa_stop(request: web.Request):
    """停止当前运行的 MAA 任务。"""
    coord = _get_coord()
    stopped = coord.stop()
    return web.json_response({"status": "ok", "stopped": stopped})


async def handle_maa_status(request: web.Request):
    """返回 MAA 协调器状态, 并附带云游戏连接状态(供前端轮询兜底)。"""
    coord = _get_coord()
    snap = coord.snapshot()
    # 附带轻量游戏状态(不请求外部剩余时长 API, 保持低延迟)
    snap["game"] = await status_payload(with_remaining=False)
    snap["ws_clients"] = sum(1 for w in _ws_clients if not w.closed)
    return web.json_response(snap)

# --- 云游戏连接逻辑 ---
async def run_cloud_game():
    try:
        try:
            token = open(TOKEN_FILE).read().strip()
        except FileNotFoundError:
            token = ""
        if not token:
            # 服务器端(无交互环境)优先使用 NETEASE_TOKEN 环境变量
            env_token = os.environ.get("NETEASE_TOKEN", "").strip()
            if env_token:
                token = env_token
                try:
                    with open(TOKEN_FILE, "w") as f:
                        f.write(token)
                    print(f"{Colors.GREEN}[✓] Token loaded from NETEASE_TOKEN env.{Colors.RESET}")
                except OSError as e:
                    print(f"{Colors.YELLOW}[!] Failed to persist token file: {e}{Colors.RESET}", file=sys.stderr)
            elif sys.stdin.isatty():
                # 仅在有交互终端时走手机号登录,避免无头环境永久阻塞
                pnum = input(f"{Colors.YELLOW}Input your phone number (will not be stored): {Colors.RESET}").strip()
                login("86-" + pnum)
                token = open(TOKEN_FILE).read().strip()
                if not token:
                    print(f"{Colors.RED}Login failed, exiting task.{Colors.RESET}", file=sys.stderr)
                    return
            else:
                # 无 TTY 且无环境变量:明确报错而非阻塞
                msg = ("Login required but running in non-interactive environment. "
                       "Set NETEASE_TOKEN env var or mount a token file.")
                print(f"{Colors.RED}[!] {msg}{Colors.RESET}", file=sys.stderr)
                logging.error(msg)
                return

        app_state.token = token
        print(f"{Colors.CYAN}[*] Connecting to cloud gaming service...{Colors.RESET}")
        res, sock = await connect(token, GAME_CODE, w=app_state.width, h=app_state.height)
        remote = object_from_string(res)
        if not isinstance(remote, RTCSessionDescription):
            raise RuntimeError(f"Unexpected offer type from signaling: {type(remote)!r}")
        
        pc = RTCPeerConnection()
        relay = MediaRelay()

        @pc.on("track")
        def on_track(track):
            if track.kind == "video":
                print(f"{Colors.BLUE}[*] Video track received.{Colors.RESET}")
                app_state.snapshotter = VideoSnapshotter(relay.subscribe(track))
                app_state.snapshotter.start()

        await pc.setRemoteDescription(remote)
        answer = await pc.createAnswer()
        if answer is None:
            raise RuntimeError("Failed to create SDP answer")
        patched = RTCSessionDescription(
            sdp=answer.sdp.replace("a=setup:active", "a=setup:passive"),
            type=answer.type,
        )
        await pc.setLocalDescription(patched)
        msg = {"id": str(int(time.time() * 1000)), "op": "answer", "data": {"sdp": patched.sdp}}
        await sock.send(encode_mess(json.dumps(msg)))

        app_state.pc = pc
        app_state.sock = sock

        print(f"{Colors.YELLOW}[*] Waiting for video stream...{Colors.RESET}")
        if not app_state.snapshotter:
            await asyncio.sleep(3)
        
        if app_state.snapshotter and await app_state.snapshotter.wait_ready():
            app_state.is_ready = True
            print(f"{Colors.GREEN}{Colors.BOLD}[✓] Cloud game ready. API is active.{Colors.RESET}")
            await broadcast_status()  # 通知 WebUI 前端已就绪
        else:
            app_state.is_ready = False
            print(f"{Colors.RED}[!] Failed to start video stream. API will not be functional.{Colors.RESET}", file=sys.stderr)
            await broadcast_status()  # 通知前端连接失败

        # Use a cancellable wait instead of Event().wait()
        # This allows the task to be properly cancelled by signals
        while True:
            await asyncio.sleep(1)

    except asyncio.CancelledError:
        print(f"\n{Colors.CYAN}[*] Cloud game task cancelled.{Colors.RESET}")
    except Exception as e:
        print(f"\n{Colors.RED}[!] An error occurred in cloud game task: {e}{Colors.RESET}", file=sys.stderr)
        logging.exception("Cloud game task error")
    finally:
        print(f"{Colors.CYAN}[*] Cleaning up cloud game resources...{Colors.RESET}")
        
        token_to_use = app_state.token
        pc_to_close = app_state.pc
        sock_to_close = app_state.sock
        snapshotter_to_stop = app_state.snapshotter

        # 重置状态
        app_state.reset()

        if token_to_use:
            print(f"{Colors.CYAN}[*] Sending exit command to cloud game server...{Colors.RESET}")
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, exit_game, token_to_use, GAME_CODE)
        
        async def close_with_timeout(awaitable: Coroutine, timeout=5.0):
            try:
                await asyncio.wait_for(awaitable, timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception:
                pass

        if snapshotter_to_stop:
            await close_with_timeout(snapshotter_to_stop.stop())
        if pc_to_close and pc_to_close.connectionState != "closed":
            await close_with_timeout(pc_to_close.close())
        if sock_to_close and sock_to_close.close_code is None:
            await close_with_timeout(sock_to_close.close())
        
        print(f"{Colors.GREEN}[✓] Cloud game resources cleaned up.{Colors.RESET}")
        try:
            await broadcast_status()  # 通知前端云游戏已断开
        except Exception:
            logging.exception("Failed to broadcast final status")


async def cleanup_background_tasks(app: web.Application):
    global _daily_checker_task
    if _daily_checker_task:
        _daily_checker_task.cancel()
        try:
            await _daily_checker_task
        except asyncio.CancelledError:
            pass
        _daily_checker_task = None
    if app_state.cloud_game_task and not app_state.cloud_game_task.done():
        print(f"\n{Colors.CYAN}Server shutting down. Cleaning up active cloud game connection...{Colors.RESET}")
        app_state.cloud_game_task.cancel()
        try:
            await app_state.cloud_game_task
        except asyncio.CancelledError:
            pass

# --- 端口释放工具(Windows): 启动前自动结束占用端口的残留进程 ---

def _pid_listening_on(host: str, port: int) -> Optional[int]:
    """通过 netstat 查找监听 (host, port) 的进程 PID; 未占用返回 None。

    匹配规则: 协议为 TCP、状态为 LISTENING、本地地址以 :port 结尾。
    本地 IP 不做严格匹配(容忍 0.0.0.0/[::] 通配绑定, 它们同样会与目标端口冲突)。
    """
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"], text=True,
            errors="ignore", timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        logging.warning("netstat 不可用, 跳过端口检查: %s", e)
        return None
    suffix = ":" + str(port)
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        # netstat 列(Windows): Proto LocalAddr ForeignAddr State PID
        if len(parts) < 5:
            continue
        if parts[1].endswith(suffix) and parts[-1].isdigit():
            return int(parts[-1])
    return None


def _pid_name(pid: int) -> str:
    """取进程名(tasklist), 供日志展示; 失败返回空串。"""
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            text=True, errors="ignore", timeout=10)
        first = (out.strip().splitlines() or [""])[0]
        # CSV 形如: "python.exe","1234","Console","1"," 10,000 K"
        parts = first.split('","')
        return parts[0].strip('"') if parts else ""
    except Exception:
        return ""


def _free_port_if_occupied(host: str, port: int, max_tries: int = 3) -> None:
    """若 (host, port) 已被占用, 结束占用进程, 避免“地址已被占用”启动失败。

    适用于修复上次异常退出后残留的 server 实例。
    说明: taskkill 成功后端口可能因 Windows 句柄释放存在短暂延迟, 因此
    结束进程后会轮询等待端口真正释放, 期间若再次被新进程占用则重试结束。
    安全性: 排除当前进程; 始终无法释放时仅告警不抛出, 由稍后
    site.start() 的 OSError 兜底提示。
    """
    if platform.system() != "Windows":
        return  # 非 Windows 直接交给 aiohttp 报错, 不擅自杀进程
    deadline = time.monotonic() + 6.0   # 至多等待 6 秒确认端口释放
    tries = 0
    while time.monotonic() < deadline:
        pid = _pid_listening_on(host, port)
        if pid is None:
            return                       # 端口已空闲, 可以绑定
        if pid == os.getpid():
            logging.warning("端口 %s:%d 由当前进程监听, 无需释放", host, port)
            return
        if tries >= max_tries:
            logging.warning("端口 %s:%d 持续被 PID %d 占用, 放弃自动释放; 请手动结束该进程",
                            host, port, pid)
            return
        tries += 1
        name = _pid_name(pid) or "未知进程"
        logging.warning("端口 %s:%d 被 PID %d(%s) 占用, 正在结束该进程...",
                        host, port, pid, name)
        try:
            proc = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, text=True, timeout=10)
        except Exception as e:
            logging.warning("结束进程失败: %s", e)
            return
        if proc.returncode == 0:
            logging.info("已结束占用进程 PID %d(%s), 等待端口释放...", pid, name)
        else:
            logging.warning(
                "无法结束 PID %d(%s): %s; 若仍绑定失败, 请以管理员身份运行或手动结束",
                pid, name, proc.stderr.strip())
            return
        time.sleep(0.3)                  # 留出句柄释放时间再探测


# --- 主函数 ---
async def run_server():
    """Run the aiohttp server with proper signal handling."""
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(levelname)s - %(message)s')
    access_log = logging.getLogger("aiohttp.access")

    app = web.Application()
    # WebUI 静态资源(style.css / app.js 等)
    if os.path.isdir(os.path.join(WEBUI_DIR, "static")):
        app.router.add_static("/static/", os.path.join(WEBUI_DIR, "static"), name="webui_static")
    app.add_routes([
        web.get('/', handle_root),
        web.get('/ui', handle_ui),
        web.get('/ws', handle_ws),
        web.get('/info', handle_info),
        web.get('/screencap', handle_screencap),
        web.post('/click', handle_click),
        web.post('/swipe', handle_swipe),
        web.post('/input', handle_input),
        web.post('/start', handle_start),
        web.post('/exit', handle_exit),
        web.post('/maa/start', handle_maa_start),
        web.post('/maa/stop', handle_maa_stop),
        web.post('/maa/adb-log', handle_maa_adb_log),
        web.get('/maa/status', handle_maa_status),
        web.get('/maa/settings', handle_maa_settings_get),
        web.post('/maa/settings', handle_maa_settings_save),
        web.post('/maa/signin', handle_maa_signin),
    ])

    # 打印 WebUI 访问入口提示
    ui_addr = f"http://{HOST}:{PORT}/"
    print(f"{Colors.GREEN}[✓] WebUI console available at {ui_addr}{Colors.RESET}")
    
    app.on_cleanup.append(cleanup_background_tasks)
    
    runner = web.AppRunner(app, access_log=access_log)
    await runner.setup()

    # 启动前释放端口: 若被上次残留的实例占用, 先结束占用进程再绑定
    _free_port_if_occupied(HOST, PORT)

    site = web.TCPSite(runner, HOST, PORT)
    await site.start()

    # 启动每日定时检查任务(到期自动执行云游戏 + 一键长草)
    global _daily_checker_task
    _daily_checker_task = asyncio.create_task(daily_checker())
    print(f"{Colors.GREEN}[✓] 每日定时任务已启用(当前设置: "
          f"{'开启 ' + maa_settings.get()['daily'].get('time', '') if maa_settings.get()['daily'].get('enabled') else '关闭'}){Colors.RESET}")
    
    print(f"{Colors.GREEN}{Colors.BOLD}[✓] API server is running at http://{HOST}:{PORT}{Colors.RESET}")
    print(f"{Colors.YELLOW}Send POST to /start to connect to the cloud game.{Colors.RESET}")
    print(f"{Colors.YELLOW}(Press CTRL+C to quit){Colors.RESET}")
    
    # Create shutdown event
    shutdown_event = asyncio.Event()
    
    # Setup signal handlers
    loop = asyncio.get_running_loop()
    
    def signal_handler():
        print(f"\n{Colors.CYAN}[*] Shutdown signal received...{Colors.RESET}")
        shutdown_event.set()
    
    # Register signal handlers
    # Note: loop.add_signal_handler() is not supported on Windows event loops,
    # so the KeyboardInterrupt fallback in main() is needed for Windows support
    if platform.system() != "Windows":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, signal_handler)
    
    try:
        # Wait for shutdown signal
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        print(f"{Colors.CYAN}[*] Shutting down server...{Colors.RESET}")
        # Cleanup cloud game task if running
        if app_state.cloud_game_task and not app_state.cloud_game_task.done():
            app_state.cloud_game_task.cancel()
            try:
                await app_state.cloud_game_task
            except asyncio.CancelledError:
                pass
        await runner.cleanup()
        print(f"{Colors.GREEN}[✓] Server stopped.{Colors.RESET}")

def main():
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        # This handles Ctrl+C on Windows
        print(f"\n{Colors.GREEN}[✓] Server stopped by user.{Colors.RESET}")

if __name__ == "__main__":
    main()
