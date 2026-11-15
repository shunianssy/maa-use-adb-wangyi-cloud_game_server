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
from maa_settings import MaaSettings
# 登录与签到统一走 netease_login(按线上接口抓包修正, 替代 sdk/signin.py 的探测式实现)
from netease_login import (
    login_with_captcha, login_with_password, netease_signin, mask_phone,
    normalize_phone, read_token, request_sms_code, save_token,
)
# 多账号: 保存多个账号 token, 支持按列表顺序轮流执行一键长草
from accounts import AccountStore
# 账号独立配置: 每个账号可保存一份一键长草运行配置(未配置则回落到全局设置)
from account_settings import AccountSettingsStore

# --- 配置 ---
# 服务器端部署时可通过环境变量覆盖以下配置(见 Docker/生产环境使用方式)
# NETEASE_TOKEN: 登录凭证,直接写入 TOKEN_FILE,实现无交互部署
GAME_CODE = os.environ.get("NETEASE_GAME_CODE", "mrfz")
# token 文件默认放在脚本所在目录(绝对路径): 无论从哪个工作目录启动服务都能读到"已登录"状态
TOKEN_FILE = os.environ.get(
    "NETEASE_TOKEN_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "token"))
# 多账号列表文件: 保存每个账号的 token 与轮流执行开关(已加入 .gitignore)
ACCOUNTS_FILE = os.environ.get(
    "NETEASE_ACCOUNTS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.json"))
# 账号独立配置文件: 每个账号的一键长草运行配置(已加入 .gitignore)
ACCOUNT_SETTINGS_FILE = os.environ.get(
    "NETEASE_ACCOUNT_SETTINGS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "account_settings.json"))
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
# 每日定时流程结束后等待一键长草完成的最长时间(秒): 4 小时, 超时后不再等待
# (防止异常情况下后台看护协程永久挂起; 正常任务通常远早于此完成)
DAILY_FLOW_MAX_WAIT = int(os.environ.get("NETEASE_DAILY_FLOW_MAX_WAIT", str(4 * 3600)))

# 一键长草设置实例(启动即加载; 每日定时任务读取它做触发)
maa_settings = MaaSettings(NETEASE_MAA_SETTINGS)

# 多账号列表实例(启动即加载; 登录成功后自动登记, 轮流执行时按顺序切换)
accounts_store = AccountStore(ACCOUNTS_FILE)

# 账号独立配置实例(未配置的账号自动使用全局设置)
account_settings = AccountSettingsStore(ACCOUNT_SETTINGS_FILE)

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

# 多账号轮流执行状态: 后台流程任务 / 停止标志 / 进度(供 /maa/status 展示)
_multi_flow_task: Optional[asyncio.Task] = None
_multi_stop_requested = False
_multi_progress: dict = {"running": False, "index": 0, "total": 0, "phone": ""}

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

    协议要点(与点击命令 pack_message 的 mm/cm 一致):
    - 触摸事件命令格式: "事件码 X Y 触点ID"(press=1 / drag=2 / release=3);
    - 坐标必须为**原始像素值** —— 云游戏服务端按像素解析, 若传 0-65535 归一化
      坐标(超出屏幕范围)会导致滑动完全无效(画面无任何反应)。

    性能说明: 滑动被拆成若干触摸点, 逐点经 send_action 发送。曾因
    send_action 每条命令都等 PONG 导致高延迟网络下整条命令卡死;
    现在逐点使用 wait_pong=False(只发不等), 并限制点数上限。
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

    # 控制触摸点数量: 既保证轨迹平滑, 又避免命令过多堆积(上限 24 点)
    num_points = min(24, max(5, swipe_duration // 40))
    interval = swipe_duration / 1000.0 / num_points
    touch_id = 0

    def create_touch_cmd(evt_type: int, px: int, py: int, tid: int) -> dict:
        """构造触摸事件命令: 坐标为原始像素值(与点击一致, 见函数 docstring)。"""
        timestamp = str(int(time.time() * 1000))
        cmd_str = f"{evt_type} {px} {py} {tid}"
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


# 账号可独立覆盖的运行配置字段(其余字段如 daily 仍为全局设置)
_RUN_SETTING_KEYS = ("tasks", "fight", "annihilation", "infrast",
                     "signin", "inventory", "award")


def _settings_for_account(phone: str) -> dict:
    """返回账号的完整设置(全局设置 + 账号独立覆盖)。

    phone 为空或账号未单独配置时, 结果等同于全局设置(向后兼容)。
    """
    base = maa_settings.get()
    if not normalize_phone(phone):
        return base
    return account_settings.merged(phone, base)


def _run_options_from_settings(cfg: dict) -> tuple:
    """从完整设置中提取本次执行的任务列表与作战选项。

    Returns:
        (tasks, options): tasks 已含 annihilation(若启用), options 供协调器使用
    """
    tasks = [k for k, v in (cfg.get("tasks") or {}).items() if v]
    if (cfg.get("annihilation") or {}).get("enabled"):
        tasks.append("annihilation")
    opts = {
        "fight": cfg.get("fight") or {},
        "annihilation": cfg.get("annihilation") or {},
        "infrast": cfg.get("infrast") or {},
        "award": cfg.get("award") or {},
        "inventory": cfg.get("inventory") or {},
    }
    return tasks, opts


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

    请求体: {"tasks": [str...], "options": {运行选项(可选)}}
    - tasks 可含 "annihilation"(每周剿灭)
    - options.fight: {stage, times(正整数或 "auto"), medicine_mode, medicine}
    - options.annihilation: {auto, times}
    - options.infrast: 基建设置(mode/facility/drones/threshold/...)
    - options.award: 领取奖励细分项
    - options.inventory: 库存保持(保持项 enabled/count; 扫描仓库后按缺口规划理智作战)
    未提供的选项回落到已保存设置(maa_settings.json)。
    """
    try:
        data = await request.json()
        tasks = data.get('tasks', [])
        options = data.get('options') or {}
    except (json.JSONDecodeError, AttributeError):
        return web.json_response({"status": "error", "message": "Invalid request body"}, status=400)

    if not isinstance(tasks, list):
        tasks = []

    coord = _get_coord()
    # 多账号模式: 后台按账号列表顺序执行, 每个账号使用自己的独立配置
    if _multi_accounts_enabled():
        if coord.running or _multi_busy():
            return web.json_response(
                {"status": "error", "message": "已有一键长草任务在运行"}, status=400)
        global _multi_flow_task
        _multi_flow_task = _spawn_bg(_run_multi_account_flow("一键长草(多账号)"))
        coord.status.log("多账号轮流执行已开始: 各账号使用自己的独立配置")
        return web.json_response(
            {"status": "ok", "message": "多账号任务已开始(依次切换账号执行)"})

    if not tasks:
        return web.json_response({"status": "error", "message": "请至少启用一个任务"}, status=400)

    # 运行选项与已保存设置合并: 表单值优先, 缺失字段用设置默认
    saved = maa_settings.get()
    merged = {
        "fight": {**saved["fight"], **(options.get("fight") or {})},
        "annihilation": {**saved["annihilation"], **(options.get("annihilation") or {})},
        "infrast": {**saved["infrast"], **(options.get("infrast") or {})},
        "signin": {**saved["signin"], **(options.get("signin") or {})},
        "award": {**saved["award"], **(options.get("award") or {})},
        # 库存保持: 前端每次提交全量保持项(每项 enabled/count), 外层键级覆盖即可
        "inventory": {**saved.get("inventory", {}), **(options.get("inventory") or {})},
    }
    do_signin = bool(merged.get("signin", {}).get("enabled", False))

    # 「开始唤醒」包含启动云游戏: 勾选 awaken 且云游戏未就绪时, 先启动并等待画面就绪
    if "awaken" in tasks and not app_state.is_ready:
        if not (app_state.token or read_token(TOKEN_FILE)):
            return web.json_response(
                {"status": "error", "message": "尚未登录云游戏账号, 请先在「云游戏账号」卡片登录"},
                status=400)
        coord.status.log("「开始唤醒」已勾选: 自动启动云游戏...")
        ok, msg = await _handle_start_internal()
        if not ok:
            coord.status.log(f"启动云游戏失败: {msg}")
            return web.json_response({"status": "error", "message": msg}, status=400)
        if not await _wait_game_ready(90):
            coord.status.log("等待云游戏就绪超时, 一键长草未执行")
            return web.json_response(
                {"status": "error", "message": "云游戏启动超时(请检查网络/账号剩余时长)"},
                status=400)
        coord.status.log("云游戏已就绪, 开始执行一键长草")

    started = coord.start(tasks, options=merged, signin=do_signin,
                          token=app_state.token or read_token(TOKEN_FILE))
    if not started:
        return web.json_response({"status": "error", "message": coord.status.snapshot().get("error") or "已有一个任务在运行或未选择任务"}, status=400)

    return web.json_response({"status": "ok", "message": "任务已开始"})


async def handle_maa_settings_get(request: web.Request):
    """返回已保存的一键长草设置(前端据此恢复表单)。

    带 ?phone= 时返回该账号的独立配置(未单独配置则回落到全局设置),
    便于前端按账号切换编辑。
    """
    phone = normalize_phone(request.query.get("phone"))
    if phone and accounts_store.get(phone):
        return web.json_response({
            "status": "ok",
            "settings": _settings_for_account(phone),
            "account": mask_phone(phone),
        })
    return web.json_response({"status": "ok", "settings": maa_settings.get()})


async def handle_maa_settings_save(request: web.Request):
    """合并保存一键长草设置。

    - 不带 ?phone=: 保存全局设置(与旧版行为一致);
    - 带 ?phone=: 运行配置(tasks/fight/annihilation/infrast/signin/inventory/award)
      保存为该账号的独立配置; daily(每日定时)仍保存为全局设置。
    """
    phone = normalize_phone(request.query.get("phone"))
    try:
        patch = await request.json()
    except (json.JSONDecodeError, AttributeError):
        return web.json_response({"status": "error", "message": "Invalid request body"}, status=400)
    if not isinstance(patch, dict):
        return web.json_response({"status": "error", "message": "settings must be an object"}, status=400)
    if phone and not accounts_store.get(phone):
        return web.json_response({"status": "error", "message": "账号不存在"}, status=400)

    if phone:
        # 账号独立配置: 运行字段 + 每周执行日写入账号; daily 的 enabled/time 仍归全局
        run_patch = {key: patch[key] for key in _RUN_SETTING_KEYS if key in patch}
        daily_patch = patch.get("daily")
        if isinstance(daily_patch, dict):
            if "weekdays" in daily_patch:
                run_patch["weekdays"] = daily_patch["weekdays"]
            global_daily = {k: v for k, v in daily_patch.items() if k != "weekdays"}
            if global_daily:
                before_daily = maa_settings.get().get("daily") or {}
                updated = maa_settings.update({"daily": global_daily})
                after_daily = updated.get("daily") or {}
                if after_daily != before_daily:
                    _print_daily_status(after_daily)
        account_settings.update(phone, run_patch)
        return web.json_response(
            {"status": "ok", "settings": _settings_for_account(phone)})

    before_daily = maa_settings.get().get("daily") or {}
    updated = maa_settings.update(patch)
    # 「每日定时」有实际改动时在控制台打印最新状态(与启动横幅区分, 避免混淆)
    after_daily = updated.get("daily") or {}
    if after_daily != before_daily:
        _print_daily_status(after_daily)
    return web.json_response({"status": "ok", "settings": updated})


async def handle_maa_signin(request: web.Request):
    """执行网易云游戏每日签到(平台奖励), 结果写入一键长草日志并返回。

    token 优先取进程内会话, 缺失时回落到 token 文件 —— 只要已登录(有 token 文件)
    即可签到, 无需先启动云游戏。
    """
    token = app_state.token or read_token(TOKEN_FILE) or ""
    if not token:
        return web.json_response(
            {"status": "error", "message": "未登录云游戏账号, 请先在「云游戏账号」卡片登录"},
            status=400)
    app_state.token = app_state.token or token

    coord = _get_coord()
    coord.status.log("发起网易云游戏签到...")
    loop = asyncio.get_running_loop()
    try:
        # requests 为阻塞调用, 放入线程池避免阻塞事件循环(内部含 2 次请求)
        result = await asyncio.wait_for(
            loop.run_in_executor(None, netease_signin, token), timeout=25)
    except (asyncio.TimeoutError, Exception) as e:
        result = {"ok": False, "message": f"签到超时/异常: {e}"}

    coord.status.log(f"签到{'成功' if result.get('ok') else '失败'}: "
                     f"{result.get('message', '')}")
    return web.json_response({"status": "ok" if result.get("ok") else "error", "result": result})


async def _wait_game_ready(timeout: float = 90.0) -> bool:
    """轮询等待云游戏就绪(is_ready), 超时返回 False。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app_state.is_ready:
            return True
        await asyncio.sleep(1)
    return False


def _cloud_game_owned_by_flow() -> bool:
    """判断当前云游戏连接是否"空闲"(可被定时流程接管并在结束后断开)。

    返回 True 表示调用前没有进行中的云游戏连接(本次流程将自行启动它),
    结束后可安全断开以节省云游戏时长/流量; 返回 False 说明用户已手动开着
    云游戏, 此时不接管、也不在结束后断开, 避免误伤用户会话。
    """
    return not (app_state.cloud_game_task and not app_state.cloud_game_task.done())


async def _auto_exit_cloud_game(trigger: str, owned: bool, maa_log) -> None:
    """断开由定时流程自行启动的云游戏连接(节省云游戏时长与流量)。

    Args:
        trigger: 触发来源描述(仅用于日志)
        owned: 该连接是否由本次流程启动 —— False(用户手动开启)时不处理
        maa_log: 一键长草日志(TaskStatus)
    """
    if not owned:
        return
    # 连接已不存在(用户提前退出等)时无需处理
    if not (app_state.cloud_game_task and not app_state.cloud_game_task.done()):
        return
    maa_log.log(f"{trigger}: 任务已结束, 断开云游戏以节省时长/流量...")
    logging.info("%s: 自动断开云游戏连接", trigger)
    await _handle_exit_internal()
    maa_log.log(f"{trigger}: 云游戏已断开")


async def _watch_flow_and_exit_cloud_game(trigger: str, owned: bool, maa_log) -> None:
    """后台等待一键长草执行结束后断开云游戏(每日定时/测试执行的后置步骤)。

    说明: _run_daily_flow 在任务启动后立即返回, 因此这里轮询协调器线程状态,
    待其自然结束(或达到最长等待上限)后调用 _auto_exit_cloud_game 释放连接。

    Args:
        trigger: 触发来源描述
        owned: 云游戏连接是否由本次流程启动(False 时不做任何处理)
        maa_log: 一键长草日志(TaskStatus)
    """
    if not owned:
        return   # 用户手动开启的会话不接管
    coord = _get_coord()
    deadline = time.monotonic() + DAILY_FLOW_MAX_WAIT
    while coord.running and time.monotonic() < deadline:
        await asyncio.sleep(5)
    if coord.running:
        # 超时保护: 极端情况下不再等待, 避免后台任务永久挂起(日志留痕便于排查)
        maa_log.log(f"{trigger}: 等待一键长草结束超时({int(DAILY_FLOW_MAX_WAIT)} 秒), 跳过自动断开云游戏")
        logging.warning("%s: 等待一键长草结束超时, 跳过自动断开云游戏", trigger)
        return
    await _auto_exit_cloud_game(trigger, owned, maa_log)


def _multi_busy() -> bool:
    """多账号轮流执行流程是否正在进行。"""
    return _multi_flow_task is not None and not _multi_flow_task.done()


def _multi_accounts_enabled() -> bool:
    """是否启用多账号轮流执行(需开关开启且至少 2 个启用账号)。"""
    if not accounts_store.auto_switch:
        return False
    return len(accounts_store.enabled_accounts()) >= 2


async def _run_multi_account_flow(trigger: str,
                                  respect_weekdays: bool = False) -> None:
    """多账号轮流执行: 逐个账号「切换登录 → 启动云游戏 → 一键长草 → 断开」。

    说明:
    - 每个账号使用自己的独立配置(account_settings 覆盖全局设置), 互不影响;
    - respect_weekdays=True(每日定时)时, 跳过今天不在该账号「每周执行日」中的账号;
      手动执行/测试执行不传该参数, 所有启用账号都会执行;
    - 切换账号必须重建云游戏会话(旧会话属于上一个账号), 因此每个账号开始前
      先断开已有连接, 结束后也主动断开, 再进入下一个账号;
    - 单个账号启动失败/超时/未启用任务不阻断后续账号, 仅记录日志并继续;
    - 停止请求(handle_maa_stop)会终止当前账号并跳过剩余账号。
    """
    global _multi_stop_requested, _multi_progress

    coord = _get_coord()
    maa_log = coord.status
    accounts = accounts_store.enabled_accounts()
    total = len(accounts)
    today = datetime.datetime.now().isoweekday()
    _multi_stop_requested = False
    _multi_progress = {"running": True, "index": 0, "total": total, "phone": ""}
    maa_log.log(f"{trigger}: 多账号轮流执行开始(共 {total} 个账号)")

    try:
        for idx, acc in enumerate(accounts, 1):
            if _multi_stop_requested:
                maa_log.log(f"{trigger}: 收到停止请求, 终止剩余账号")
                break
            phone = acc["phone"]
            label = mask_phone(phone)
            _multi_progress = {"running": True, "index": idx,
                               "total": total, "phone": label}

            # 每日定时: 仅执行今天在其「每周执行日」中的账号
            if respect_weekdays and today not in _weekdays_for_account(phone):
                maa_log.log(f"{trigger}: [{idx}/{total}] 账号 {label} 今日"
                            f"({_WEEKDAY_NAMES[today - 1]})不在执行日, 跳过")
                continue

            # 0) 取该账号的独立配置(未单独配置时回落到全局设置)
            acc_cfg = _settings_for_account(phone)
            acc_tasks, acc_opts = _run_options_from_settings(acc_cfg)
            acc_signin = bool((acc_cfg.get("signin") or {}).get("enabled", False))
            if not acc_tasks:
                maa_log.log(f"{trigger}: [{idx}/{total}] 账号 {label} 未启用任何任务, 跳过")
                continue
            maa_log.log(f"{trigger}: [{idx}/{total}] 切换到账号 {label}, 启动云游戏...")

            # 1) 切换为当前账号: token 落盘 + 进程内更新(供启动云游戏复用)
            accounts_store.set_current(phone)
            if not save_token(acc["token"], TOKEN_FILE):
                maa_log.log(f"{trigger}: [{idx}/{total}] 账号 {label} token 落盘失败, 仍尝试启动")
            await _handle_exit_internal()
            app_state.token = acc["token"]

            # 2) 用该账号启动云游戏并等待画面就绪
            ok, msg = await _handle_start_internal()
            if not ok:
                maa_log.log(f"{trigger}: [{idx}/{total}] 账号 {label} 启动云游戏失败: {msg}")
                continue
            if not await _wait_game_ready(90):
                maa_log.log(f"{trigger}: [{idx}/{total}] 账号 {label} 等待云游戏就绪超时")
                await _handle_exit_internal()
                continue

            # 3) 执行一键长草(任务与选项均来自该账号的独立配置)
            started = coord.start(
                acc_tasks, options=acc_opts, signin=acc_signin, token=acc["token"],
                keep_log=True, title=f"账号 {label} ({idx}/{total})")
            if not started:
                maa_log.log(f"{trigger}: [{idx}/{total}] 账号 {label} 一键长草启动失败")
                await _handle_exit_internal()
                continue

            while coord.running:
                if _multi_stop_requested:
                    coord.stop()
                    break
                await asyncio.sleep(5)

            maa_log.log(f"{trigger}: [{idx}/{total}] 账号 {label} 执行结束")
            # 4) 断开本账号会话, 避免下一个账号复用旧会话
            await _handle_exit_internal()
            if _multi_stop_requested:
                maa_log.log(f"{trigger}: 收到停止请求, 终止剩余账号")
                break
            await asyncio.sleep(2)   # 留出会话释放时间, 避免紧接着连接被服务端拒绝
    except asyncio.CancelledError:
        maa_log.log(f"{trigger}: 多账号执行被取消")
        raise
    except Exception as e:
        logging.exception("多账号轮流执行异常")
        maa_log.log(f"{trigger}: 多账号执行异常: {e}")
    finally:
        try:
            await _handle_exit_internal()
        except (asyncio.CancelledError, Exception):
            pass   # 服务关闭/任务取消时尽力断开, 不阻断收尾
        _multi_progress = {"running": False, "index": 0, "total": 0, "phone": ""}
        maa_log.log(f"{trigger}: 多账号轮流执行结束")


async def _run_daily_flow(trigger: str, respect_weekdays: bool = False) -> tuple:
    """执行一次「启动云游戏 → 一键长草 → 结束后断开云游戏」流程。

    每日定时与「测试执行」共用。任务结束后自动断开云游戏以节省时长与流量;
    仅当云游戏连接由本次流程自行启动时才断开, 避免误断用户手动开启的会话。
    开启「多账号轮流执行」时改为后台跑多账号流程(每个账号各走一遍上述流程)。

    Args:
        trigger: 触发来源描述(写入一键长草日志)
        respect_weekdays: 是否按账号的每周执行日过滤(仅每日定时为 True;
                          「测试执行」应忽略执行日, 立即运行)

    Returns:
        (ok, message): ok 表示一键长草是否成功启动
    """
    maa_log = _get_coord().status
    if _multi_busy() or _get_coord().running:
        maa_log.log(f"{trigger}: 已有一键长草任务在运行, 跳过本次触发")
        return False, "已有一键长草任务在运行"

    # 多账号模式: 每个账号使用自己的独立配置, 后台串行执行
    if _multi_accounts_enabled():
        global _multi_flow_task
        _multi_flow_task = _spawn_bg(
            _run_multi_account_flow(trigger, respect_weekdays=respect_weekdays))
        maa_log.log(f"{trigger}: 多账号轮流执行已开始, 共 "
                    f"{len(accounts_store.enabled_accounts())} 个账号")
        return True, "多账号任务已启动"

    # 单账号: 使用当前账号的配置(未单独配置时回落到全局设置)
    cfg = _settings_for_account(accounts_store.current_phone())
    enabled, opts = _run_options_from_settings(cfg)
    do_signin = bool((cfg.get("signin") or {}).get("enabled", False))

    # 启动前先记录连接归属: owned=True 表示由本次流程负责启动并在结束后断开
    owned = _cloud_game_owned_by_flow()
    maa_log.log(f"{trigger}: 启动云游戏...")
    ok, msg = await _handle_start_internal()
    if not ok:
        maa_log.log(f"{trigger}: 启动云游戏失败: {msg}")
        return False, msg
    if not await _wait_game_ready(90):
        maa_log.log(f"{trigger}: 等待云游戏就绪超时")
        await _auto_exit_cloud_game(trigger, owned, maa_log)  # 就绪失败也要释放连接
        return False, "等待云游戏就绪超时"

    if not enabled:
        maa_log.log(f"{trigger}: 未启用任何任务, 已跳过一键长草")
        await _auto_exit_cloud_game(trigger, owned, maa_log)
        return False, "未启用任何任务"

    started = _get_coord().start(enabled, options=opts, signin=do_signin,
                                 token=app_state.token or read_token(TOKEN_FILE))
    if not started:
        maa_log.log(f"{trigger}: 一键长草启动失败(可能已有任务在运行)")
        await _auto_exit_cloud_game(trigger, owned, maa_log)
        return False, "一键长草启动失败(可能已有任务在运行)"
    maa_log.log(f"{trigger}: 自动执行一键长草 {', '.join(enabled)}")
    await broadcast_status()
    # 任务启动成功后, 后台等待其结束并断开云游戏(不阻塞当前调用方)
    _spawn_bg(_watch_flow_and_exit_cloud_game(trigger, owned, maa_log))
    return True, "任务已启动"


# 「测试执行」等后台任务的强引用集合(避免 asyncio 任务被垃圾回收)
_bg_tasks: set = set()


def _spawn_bg(coro) -> asyncio.Task:
    """把协程放到后台执行并保留引用, 异常写日志(不打断调用方)。"""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    task.add_done_callback(
        lambda t: logging.error("后台任务异常: %s", t.exception())
        if not t.cancelled() and t.exception() else None)
    return task


async def handle_maa_daily_test(request: web.Request):
    """「测试执行」: 立即执行一次每日流程(启动云游戏 → 一键长草)。

    说明: 不影响 last_daily_run 记录(当天定时仍会照常触发); 因等待云游戏就绪耗时较长,
    这里后台执行并立即返回, 进度请查看一键长草日志。
    """
    if _get_coord().running or _multi_busy():
        return web.json_response(
            {"status": "error", "message": "已有一键长草任务在运行"}, status=400)
    _spawn_bg(_run_daily_flow("测试执行"))
    return web.json_response(
        {"status": "ok", "message": "测试执行已开始(启动云游戏 → 一键长草), 请查看日志"})


# 星期中文名(ISO: 1=周一 ... 7=周日), 供每日定时展示与日志使用
_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _normalize_weekdays(raw) -> set:
    """把设置中的「每周执行日」规整为 ISO 星期集合 {1..7}。

    说明:
    - 非列表(旧配置/异常写入)按每天处理, 保持原有"每日执行"语义;
    - 空列表表示不执行任何一天(定时不会触发);
    - 非法值(越界/非数字)忽略。
    """
    if not isinstance(raw, list):
        return set(range(1, 8))
    days = set()
    for item in raw:
        try:
            day = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= day <= 7:
            days.add(day)
    return days


def _format_weekdays(raw) -> str:
    """把每周执行日格式化为可读文本: 每天 / 未选择执行日 / 周一/周三/周五。"""
    days = _normalize_weekdays(raw)
    if len(days) == 7:
        return "每天"
    if not days:
        return "未选择执行日"
    return "/".join(_WEEKDAY_NAMES[d - 1] for d in sorted(days))


def _weekdays_for_account(phone: str) -> set:
    """返回账号生效的每周执行日: 账号独立配置优先, 未配置回落到全局设置。"""
    if phone:
        stored = account_settings.get_weekdays(phone)
        if stored is not None:
            return set(stored)
    return _normalize_weekdays(
        (maa_settings.get().get("daily") or {}).get("weekdays"))


def _day_has_work(day: int) -> bool:
    """判断指定 ISO 星期是否有账号需要执行。

    - 多账号模式: 任一启用账号的执行日包含该天即触发;
    - 单账号模式: 当前账号的执行日包含该天即触发。
    """
    if _multi_accounts_enabled():
        return any(day in _weekdays_for_account(acc["phone"])
                   for acc in accounts_store.enabled_accounts())
    return day in _weekdays_for_account(accounts_store.current_phone())


def _print_daily_status(daily: dict) -> None:
    """打印每日定时任务的当前状态(启动横幅与设置变更时共用)。

    状态实时取自 maa_settings.json, 控制台以"最新一次打印"为准:
      - 启动时打印一次(启动瞬间的快照)
      - 之后每次通过网页控制台改动「每日定时」都会重新打印
    """
    time_str = str(daily.get("time") or "08:00")
    days_text = _format_weekdays(daily.get("weekdays"))
    custom_days = sum(1 for p in account_settings.phones()
                      if account_settings.get_weekdays(p) is not None)
    if custom_days:
        days_text += f", {custom_days} 个账号自定义执行日"
    if daily.get("enabled"):
        print(f"{Colors.GREEN}[OK] 每日定时任务: 已启用({days_text} {time_str} 自动执行)"
              f"{Colors.RESET}", flush=True)
    else:
        print(f"{Colors.YELLOW}[--] 每日定时任务: 未启用(可在网页控制台「每日定时执行」开启)"
              f"{Colors.RESET}", flush=True)


async def daily_checker():
    """每日定时任务: 到达设置时间且当天未执行过时, 自动启动云游戏并一键长草。

    触发时机: enable=true 且当前 HH:MM == daily.time 且 last_daily_run != 今天,
              且当天有账号需要执行(每周执行日按账号配置, 未配置回落全局)。
    """
    while True:
        try:
            cfg = maa_settings.get()
            daily = cfg.get("daily") or {}
            if daily.get("enabled"):
                now = datetime.datetime.now()
                hhmm = now.strftime("%H:%M")
                today = now.strftime("%Y-%m-%d")
                if (hhmm == str(daily.get("time", ""))
                        and cfg.get("last_daily_run") != today
                        and _day_has_work(now.isoweekday())):
                    logging.info("每日定时触发: %s, 开始自动执行(按账号执行日过滤)", hhmm)
                    maa_settings.update({"last_daily_run": today})
                    await _run_daily_flow(
                        f"每日定时任务触发({hhmm})", respect_weekdays=True)
            await asyncio.sleep(30)   # 每 30 秒检查一次, 粒度足够
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.exception("每日定时检查异常")
            await asyncio.sleep(30)


async def handle_maa_stop(request: web.Request):
    """停止当前运行的 MAA 任务(多账号模式下同时终止剩余账号)。"""
    global _multi_stop_requested
    _multi_stop_requested = True
    coord = _get_coord()
    stopped = coord.stop()
    return web.json_response(
        {"status": "ok", "stopped": stopped, "multi_running": _multi_busy()})


async def handle_maa_status(request: web.Request):
    """返回 MAA 协调器状态, 并附带云游戏连接状态(供前端轮询兜底)。"""
    coord = _get_coord()
    snap = coord.snapshot()
    # 附带轻量游戏状态(不请求外部剩余时长 API, 保持低延迟)
    snap["game"] = await status_payload(with_remaining=False)
    snap["ws_clients"] = sum(1 for w in _ws_clients if not w.closed)
    # 多账号状态: 供前端展示账号进度与账号列表(不含 token)
    snap["multi_account"] = {"enabled": accounts_store.auto_switch, **_multi_progress}
    snap["accounts"] = accounts_store.public_list()
    return web.json_response(snap)

# --- 网易云游戏账号登录接口(WebUI 账号卡片) ---

async def handle_login_sms(request: web.Request):
    """发送短信验证码: POST /api/login/sms  {"phone": "138..."}。

    requests 为阻塞调用, 放线程池执行, 避免阻塞事件循环。
    """
    try:
        data = await request.json()
    except (json.JSONDecodeError, AttributeError):
        return web.json_response(
            {"status": "error", "message": "请求体不是合法 JSON"}, status=400)

    phone = str((data or {}).get("phone", "")).strip()
    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, request_sms_code, phone), timeout=15)
    except (asyncio.TimeoutError, Exception) as e:
        logging.warning("发送短信验证码异常: %s", e)
        result = {"ok": False, "message": f"发送验证码超时/异常: {e}"}

    if not result.get("ok"):
        return web.json_response(
            {"status": "error", "message": result.get("message", "发送验证码失败")},
            status=400)
    return web.json_response(
        {"status": "ok", "message": result.get("message", "验证码已发送")})


def _apply_login_token(token: str, source: str, phone: str = "") -> str:
    """登录成功后统一处理: token 落盘 + 多账号登记 + 进程内状态更新。

    Args:
        token: 新登录 token
        source: 登录方式(日志用: sms/password)
        phone: 登录手机号(有效时自动登记/更新多账号列表并设为当前账号)

    Returns:
        面向用户的提示文案(云游戏会话进行中时说明"下次启动生效")
    """
    saved = save_token(token, TOKEN_FILE)
    is_new = False
    pnum = normalize_phone(phone)
    if pnum:
        is_new = accounts_store.upsert(pnum, token)
        accounts_store.set_current(pnum)
    # 云游戏会话进行中: token 变更只落盘, 避免影响当前会话的退出/签到使用的账号
    session_active = bool(app_state.cloud_game_task and not app_state.cloud_game_task.done())
    if session_active:
        message = "登录成功(当前云游戏会话仍使用原账号, 下次启动生效)"
    else:
        app_state.token = token
        message = "登录成功"
    if pnum:
        message += f"; 已{'添加' if is_new else '更新'}账号 {mask_phone(pnum)}"
    if not saved:
        message += "; 但 token 落盘失败, 重启后需重新登录"
    logging.info("云游戏账号登录成功(source=%s, token_saved=%s, session_active=%s, phone=%s)",
                 source, saved, session_active, mask_phone(pnum) if pnum else "-")
    return message


async def handle_login_verify(request: web.Request):
    """提交短信验证码登录: POST /api/login/verify  {"phone": "...", "code": "..."}。

    成功后 token 立即落盘(供下次启动云游戏复用); 若当前没有进行中的云游戏会话,
    同时写入进程内 app_state, 便于直接使用「云游戏签到」等功能。
    """
    try:
        data = await request.json()
    except (json.JSONDecodeError, AttributeError):
        return web.json_response(
            {"status": "error", "message": "请求体不是合法 JSON"}, status=400)

    data = data or {}
    phone = str(data.get("phone", "")).strip()
    code = str(data.get("code", "")).strip()
    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, login_with_captcha, phone, code), timeout=20)
    except (asyncio.TimeoutError, Exception) as e:
        logging.warning("云游戏登录异常: %s", e)
        result = {"ok": False, "message": f"登录超时/异常: {e}"}

    if not result.get("ok") or not result.get("token"):
        return web.json_response(
            {"status": "error", "message": result.get("message", "登录失败")}, status=400)

    return web.json_response(
        {"status": "ok", "message": _apply_login_token(result["token"], "sms", phone)})


async def handle_login_password(request: web.Request):
    """密码登录: POST /api/login/password  {"phone": "...", "password": "..."}。

    说明: 网易云游戏的密码登录需要先携带**当前有效 token** 获取加密参数
    (接口 /api/v2/user-pwd-info 要求登录态), 因此 token 已失效时请改用短信验证码登录。
    """
    try:
        data = await request.json()
    except (json.JSONDecodeError, AttributeError):
        return web.json_response(
            {"status": "error", "message": "请求体不是合法 JSON"}, status=400)

    data = data or {}
    phone = str(data.get("phone", "")).strip()
    password = str(data.get("password", ""))
    # 当前 token: 进程内优先, 缺失时回落到 token 文件
    current_token = app_state.token or read_token(TOKEN_FILE) or ""

    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, login_with_password, current_token, phone, password),
            timeout=25)
    except (asyncio.TimeoutError, Exception) as e:
        logging.warning("密码登录异常: %s", e)
        result = {"ok": False, "message": f"登录超时/异常: {e}"}

    if not result.get("ok") or not result.get("token"):
        return web.json_response(
            {"status": "error", "message": result.get("message", "登录失败")}, status=400)

    return web.json_response(
        {"status": "ok", "message": _apply_login_token(result["token"], "password", phone)})


async def handle_login_status(request: web.Request):
    """查询登录状态: GET /api/login/status[?remaining=1]。

    remaining=1 时额外拉取云游戏剩余时长(较慢, 前端仅在页面加载与登录后调用)。
    """
    token = app_state.token or read_token(TOKEN_FILE) or ""
    current_acc = accounts_store.current()
    payload = {
        "status": "ok",
        "logged_in": bool(token or current_acc),
        # 当前账号(脱敏), 便于前端提示"当前使用哪个账号"
        "account": mask_phone(current_acc["phone"]) if current_acc else "",
        "accounts": accounts_store.public_list(),
        "auto_switch": accounts_store.auto_switch,
    }
    if token and request.query.get("remaining") == "1":
        loop = asyncio.get_running_loop()
        try:
            info = await asyncio.wait_for(
                loop.run_in_executor(None, fetch_user_info, token), timeout=8)
        except (asyncio.TimeoutError, Exception):
            info = {}
        if info:
            payload["remaining_time"] = info.get("free_time_left")
    return web.json_response(payload)


# --- 多账号管理接口(WebUI 账号列表) ---

def _accounts_payload() -> dict:
    """账号列表的统一下发负载(绝不包含 token)。"""
    return {
        "status": "ok",
        "auto_switch": accounts_store.auto_switch,
        "current": mask_phone(accounts_store.current_phone())
        if accounts_store.current_phone() else "",
        "accounts": accounts_store.public_list(),
    }


def _activate_account(phone: str) -> tuple:
    """把指定账号设为当前账号: token 落盘, 无会话时同步到进程内。

    Returns:
        (ok, message): ok 表示切换成功(账号存在)
    """
    acc = accounts_store.get(phone)
    if not acc:
        return False, "账号不存在"
    accounts_store.set_current(acc["phone"])
    saved = save_token(acc["token"], TOKEN_FILE)
    # 云游戏会话进行中: 只落盘, 当前会话继续使用原账号(退出/签到不受影响)
    session_active = bool(app_state.cloud_game_task and not app_state.cloud_game_task.done())
    if not session_active:
        app_state.token = acc["token"]
    message = f"已切换到账号 {mask_phone(acc['phone'])}"
    if session_active:
        message += "(当前云游戏会话仍使用原账号, 下次启动生效)"
    if not saved:
        message += "; 但 token 落盘失败, 重启后需重新登录"
    return True, message


async def _read_json_obj(request: web.Request):
    """读取 JSON 对象请求体; 失败返回 (None, 错误响应)。"""
    try:
        data = await request.json()
    except (json.JSONDecodeError, AttributeError):
        return None, web.json_response(
            {"status": "error", "message": "请求体不是合法 JSON"}, status=400)
    if not isinstance(data, dict):
        return None, web.json_response(
            {"status": "error", "message": "请求体必须是 JSON 对象"}, status=400)
    return data, None


async def handle_accounts_get(request: web.Request):
    """查询多账号列表: GET /api/accounts。"""
    return web.json_response(_accounts_payload())


async def handle_accounts_switch(request: web.Request):
    """切换当前账号: POST /api/accounts/switch {"phone": "..."}。"""
    if _multi_busy():
        return web.json_response(
            {"status": "error", "message": "多账号任务执行中, 请先停止再切换账号"}, status=400)
    data, err = await _read_json_obj(request)
    if err:
        return err
    ok, message = _activate_account(str(data.get("phone", "")))
    if not ok:
        return web.json_response({"status": "error", "message": message}, status=400)
    logging.info("已切换当前账号: %s", mask_phone(data.get("phone")))
    return web.json_response({"status": "ok", "message": message, **_accounts_payload()})


async def handle_accounts_toggle(request: web.Request):
    """启用/停用账号参与轮流执行: POST /api/accounts/toggle {"phone","enabled"}。"""
    if _multi_busy():
        return web.json_response(
            {"status": "error", "message": "多账号任务执行中, 请先停止再修改账号"}, status=400)
    data, err = await _read_json_obj(request)
    if err:
        return err
    phone = str(data.get("phone", ""))
    enabled = bool(data.get("enabled", True))
    if not accounts_store.set_enabled(phone, enabled):
        return web.json_response(
            {"status": "error", "message": "账号不存在"}, status=400)
    return web.json_response({
        "status": "ok",
        "message": f"账号 {mask_phone(phone)} 已{'启用' if enabled else '停用'}轮流执行",
        **_accounts_payload(),
    })


async def handle_accounts_remove(request: web.Request):
    """从轮流列表移除账号: POST /api/accounts/remove {"phone": "..."}。

    说明: 仅从多账号列表移除, 不注销当前登录/不影响 token 文件。
    """
    if _multi_busy():
        return web.json_response(
            {"status": "error", "message": "多账号任务执行中, 请先停止再修改账号"}, status=400)
    data, err = await _read_json_obj(request)
    if err:
        return err
    phone = str(data.get("phone", ""))
    was_current = accounts_store.current_phone() == normalize_phone(phone)
    if not accounts_store.remove(phone):
        return web.json_response(
            {"status": "error", "message": "账号不存在"}, status=400)
    # 同步删除该账号的独立配置(账号已移出列表, 避免残留配置影响后续同名账号)
    removed_cfg = account_settings.remove(phone)
    message = f"已从列表移除账号 {mask_phone(phone)}"
    if removed_cfg:
        message += "(其独立配置已删除)"
    # 移除的是当前账号: 顺延到剩余账号(保证 token 文件与当前账号一致)
    if was_current and accounts_store.current_phone():
        ok, switch_msg = _activate_account(accounts_store.current_phone())
        if ok:
            message += f"; {switch_msg}"
    return web.json_response({"status": "ok", "message": message, **_accounts_payload()})


async def handle_accounts_auto(request: web.Request):
    """启用/关闭多账号轮流执行: POST /api/accounts/auto {"enabled": bool}。"""
    if _multi_busy():
        return web.json_response(
            {"status": "error", "message": "多账号任务执行中, 请先停止再修改设置"}, status=400)
    data, err = await _read_json_obj(request)
    if err:
        return err
    enabled = bool(data.get("enabled", False))
    accounts_store.set_auto_switch(enabled)
    count = len(accounts_store.enabled_accounts())
    message = "多账号轮流执行已开启"
    if enabled and count < 2:
        message += f"(当前仅 {count} 个启用账号, 至少需要 2 个才会轮流执行)"
    elif not enabled:
        message = "多账号轮流执行已关闭"
    return web.json_response(
        {"status": "ok", "message": message, **_accounts_payload()})


# --- 云游戏连接逻辑 ---
async def run_cloud_game():
    try:
        # token 来源优先级: token 文件(含 WebUI 登录写入) -> NETEASE_TOKEN 环境变量 -> 终端交互登录
        token = read_token(TOKEN_FILE) or ""
        if not token:
            # 服务器端(无交互环境)优先使用 NETEASE_TOKEN 环境变量
            env_token = os.environ.get("NETEASE_TOKEN", "").strip()
            if env_token:
                token = env_token
                if save_token(token, TOKEN_FILE):
                    print(f"{Colors.GREEN}[OK] Token loaded from NETEASE_TOKEN env.{Colors.RESET}")
                else:
                    print(f"{Colors.YELLOW}[!] Failed to persist token file.{Colors.RESET}", file=sys.stderr)
            elif sys.stdin.isatty():
                # 仅在有交互终端时走手机号登录,避免无头环境永久阻塞
                pnum = input(f"{Colors.YELLOW}Input your phone number (will not be stored): {Colors.RESET}").strip()
                login("86-" + pnum)
                token = read_token(TOKEN_FILE) or ""
                if not token:
                    print(f"{Colors.RED}Login failed, exiting task.{Colors.RESET}", file=sys.stderr)
                    return
            else:
                # 无 TTY 且无环境变量:明确报错而非阻塞(可先在 WebUI 账号卡片中登录)
                msg = ("Login required but running in non-interactive environment. "
                       "Login from the WebUI account panel, or set NETEASE_TOKEN env var "
                       "or mount a token file.")
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
            print(f"{Colors.GREEN}{Colors.BOLD}[OK] Cloud game ready. API is active.{Colors.RESET}")
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
        
        print(f"{Colors.GREEN}[OK] Cloud game resources cleaned up.{Colors.RESET}")
        try:
            await broadcast_status()  # 通知前端云游戏已断开
        except Exception:
            logging.exception("Failed to broadcast final status")


async def cleanup_background_tasks(app: web.Application):
    global _daily_checker_task, _multi_stop_requested, _multi_flow_task
    if _daily_checker_task:
        _daily_checker_task.cancel()
        try:
            await _daily_checker_task
        except asyncio.CancelledError:
            pass
        _daily_checker_task = None
    # 多账号流程: 先请求停止再取消, 避免继续切换/启动下一个账号
    _multi_stop_requested = True
    if _multi_flow_task and not _multi_flow_task.done():
        _multi_flow_task.cancel()
        try:
            await _multi_flow_task
        except (asyncio.CancelledError, Exception):
            pass
        _multi_flow_task = None
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
    # 启动时预加载 token 文件: 存在 token 即视为「已登录」(签到/密码登录等无需先启动云游戏)
    app_state.token = app_state.token or read_token(TOKEN_FILE) or ""
    # 多账号: token 文件缺失时用当前账号的 token 恢复(避免文件被删后无法自动启动)
    if not app_state.token:
        current_acc = accounts_store.current()
        if current_acc and save_token(current_acc["token"], TOKEN_FILE):
            app_state.token = current_acc["token"]
            logging.info("已从多账号列表恢复当前账号 token")
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
        # 网易云游戏账号登录(WebUI 账号卡片)
        web.post('/api/login/sms', handle_login_sms),
        web.post('/api/login/verify', handle_login_verify),
        web.post('/api/login/password', handle_login_password),
        web.get('/api/login/status', handle_login_status),
        # 多账号管理(账号列表/切换当前账号/启停/移除/轮流执行开关)
        web.get('/api/accounts', handle_accounts_get),
        web.post('/api/accounts/switch', handle_accounts_switch),
        web.post('/api/accounts/toggle', handle_accounts_toggle),
        web.post('/api/accounts/remove', handle_accounts_remove),
        web.post('/api/accounts/auto', handle_accounts_auto),
        # 每日定时「测试执行」: 立即跑一次 启动云游戏 → 一键长草
        web.post('/maa/daily/test', handle_maa_daily_test),
    ])

    # 打印 WebUI 访问入口提示
    ui_addr = f"http://{HOST}:{PORT}/"
    print(f"{Colors.GREEN}[OK] WebUI console available at {ui_addr}{Colors.RESET}")
    
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
    _print_daily_status(maa_settings.get().get("daily") or {})
    
    print(f"{Colors.GREEN}{Colors.BOLD}[OK] API server is running at http://{HOST}:{PORT}{Colors.RESET}")
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
        print(f"{Colors.GREEN}[OK] Server stopped.{Colors.RESET}")

def main():
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        # This handles Ctrl+C on Windows
        print(f"\n{Colors.GREEN}[OK] Server stopped by user.{Colors.RESET}")

if __name__ == "__main__":
    main()
