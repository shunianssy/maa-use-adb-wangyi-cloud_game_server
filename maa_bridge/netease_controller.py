"""
网易云游戏 MAA 自定义控制器桥接。

将 MaaFramework 的 CustomController 原语(screencap / click / swipe / input_text)
映射到本项目 server.py 提供的 HTTP 接口:

    screencap  -> GET  /screencap              (返回 JPEG 二进制)
    click      -> POST /click                  ({"x": int, "y": int})
    swipe      -> POST /swipe                  ({"x1","y1","x2","y2","duration"})
    input_text -> POST /input                  ({"text": str})

注意事项:
- screencap 必须返回 BGR 顺序的 numpy.ndarray, shape=(H, W, 3), dtype=uint8;
  server.py 返回的是 JPEG,此处用 Pillow 解码并完成 RGB -> BGR 翻转。
- 云游戏连接需要先 POST /start,随后轮询 /info 直到 status == "ok"。
- visit 云游戏缺少 start_app/stop_app/touch_*/key_* 等系统能力,这些抽象方法
  以占位实现返回 True,不影响点击/滑动/输入等核心功能。
"""

import logging
import time
import uuid
from io import BytesIO
from typing import Optional

import numpy as np
import requests
from PIL import Image

from maa.controller import CustomController

logger = logging.getLogger(__name__)


class NeteaseCloudGameController(CustomController):
    """基于 HTTP 的网易云游戏控制器。

    通过 MaaFramework 自定义控制器回调,把 MAA 的原语操作转发到云游戏 HTTP 服务。
    """

    def __init__(
        self,
        base_url: str,
        width: int = 1280,
        height: int = 720,
        connect_timeout: int = 180,
        http_timeout: int = 10,
    ) -> None:
        """初始化控制器。

        Args:
            base_url: 云游戏 HTTP 服务地址, 如 http://127.0.0.1:22888
            width: 期望的云游戏分辨率宽(用于失败兜底图像), 默认 1280
            height: 期望的云游戏分辨率高(用于失败兜底图像), 默认 720
            connect_timeout: 等待云游戏连接就绪的超时秒数, 默认 180
            http_timeout: 单次 HTTP 请求超时秒数, 默认 10
        """
        super().__init__()
        self._base = base_url.rstrip("/")
        self._width = int(width)
        self._height = int(height)
        self._connect_timeout = max(5, int(connect_timeout))
        self._http_timeout = max(1, int(http_timeout))
        # 复用 Session 以启用 keep-alive, 降低高频截图/点击的握手开销
        self._session = requests.Session()
        self._uuid = f"netease-cloud-game-{uuid.uuid4().hex[:12]}"

    # ------------------------------------------------------------------ #
    # 内部工具                                                             #
    # ------------------------------------------------------------------ #
    def _request(self, method: str, path: str, **kwargs) -> Optional[requests.Response]:
        """统一 HTTP 请求封装:自动补全 base URL、附加超时与错误日志。

        Returns:
            requests.Response 或 None(网络异常时)
        """
        try:
            return self._session.request(method, f"{self._base}{path}", timeout=self._http_timeout, **kwargs)
        except requests.RequestException as e:
            logger.error("[bridge] HTTP %s %s failed: %s", method, path, e)
            return None

    def _post_json(self, path: str, payload: dict) -> bool:
        """POST JSON 并判定是否成功(2xx 视为成功)。"""
        resp = self._request("POST", path, json=payload)
        if resp is None:
            return False
        if 200 <= resp.status_code < 300:
            return True
        logger.warning("[bridge] POST %s -> HTTP %s: %s", path, resp.status_code, resp.text[:200])
        return False

    def _blank_frame(self) -> np.ndarray:
        """生成一张纯黑兜底帧,避免截图失败时框架拿到错误 shape 崩溃。"""
        return np.zeros((self._height, self._width, 3), dtype=np.uint8)

    # ------------------------------------------------------------------ #
    # MaaFramework 抽象方法(必实现)                                       #
    # ------------------------------------------------------------------ #
    def connect(self) -> bool:
        """发起云游戏连接并轮询直至就绪。

        流程:POST /start 拉起连接 -> 轮询 /info 直到 status == "ok",
        同时用 /info 返回的分辨率校正本对象宽度/高度。
        """
        if self._request("POST", "/start") is None:
            logger.error("[bridge] Failed to send /start request")
            return False

        deadline = time.monotonic() + self._connect_timeout
        while time.monotonic() < deadline:
            resp = self._request("GET", "/info")
            if resp is not None and resp.status_code == 200:
                try:
                    info = resp.json()
                except ValueError:
                    info = {}
                if info.get("status") == "ok":
                    # 以服务端实际分辨率校正坐标空间
                    self._width = int(info.get("width", self._width))
                    self._height = int(info.get("height", self._height))
                    logger.info("[bridge] Cloud game connected, resolution %dx%d", self._width, self._height)
                    return True
            # 未就绪:短暂休眠后重试
            time.sleep(2)

        logger.error("[bridge] Cloud game connect timeout after %ss", self._connect_timeout)
        return False

    def request_uuid(self) -> str:
        """返回本控制器实例的唯一标识。"""
        return self._uuid

    def screencap(self) -> np.ndarray:
        """获取当前画面截图, 返回 BGR 顺序的 (H, W, 3) uint8 数组。"""
        resp = self._request("GET", "/screencap")
        if resp is None or resp.status_code != 200:
            logger.warning("[bridge] screencap failed (HTTP %s), returning blank frame",
                           resp.status_code if resp else "no-response")
            return self._blank_frame()
        try:
            img = Image.open(BytesIO(resp.content)).convert("RGB")
            # RGB -> BGR:仅翻转通道顺序,不复制整幅图的内存(视图操作)
            return np.asarray(img)[:, :, ::-1]
        except Exception as e:
            logger.error("[bridge] screencap decode failed: %s", e)
            return self._blank_frame()

    def click(self, x: int, y: int) -> bool:
        """在 (x, y) 处执行点击。"""
        return self._post_json("/click", {"x": int(x), "y": int(y)})

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int) -> bool:
        """从 (x1, y1) 滑动到 (x2, y2), 耗时 duration 毫秒。"""
        return self._post_json("/swipe", {
            "x1": int(x1), "y1": int(y1),
            "x2": int(x2), "y2": int(y2),
            "duration": int(duration),
        })

    def input_text(self, text: str) -> bool:
        """输入一段文本(云游戏侧逐字输入)。"""
        return self._post_json("/input", {"text": str(text)})

    # ------------------------------------------------------------------ #
    # 云游戏不具备的系统能力:占位实现返回 True(不影响核心操作)            #
    # ------------------------------------------------------------------ #
    def start_app(self, intent: str) -> bool:
        """云游戏不支持独立启动应用, 恒返回 True 跳过。"""
        logger.debug("[bridge] start_app(%r) ignored (cloud game)", intent)
        return True

    def stop_app(self, intent: str) -> bool:
        """云游戏不支持独立关闭应用, 恒返回 True 跳过。"""
        logger.debug("[bridge] stop_app(%r) ignored (cloud game)", intent)
        return True

    def touch_down(self, contact: int, x: int, y: int, pressure: int) -> bool:
        """多点触控按压:云游戏协议不暴露, 占位跳过。"""
        logger.debug("[bridge] touch_down(contact=%s) ignored", contact)
        return True

    def touch_move(self, contact: int, x: int, y: int, pressure: int) -> bool:
        """多点触控移动:云游戏协议不暴露, 占位跳过。"""
        logger.debug("[bridge] touch_move(contact=%s) ignored", contact)
        return True

    def touch_up(self, contact: int) -> bool:
        """多点触控抬起:云游戏协议不暴露, 占位跳过。"""
        logger.debug("[bridge] touch_up(contact=%s) ignored", contact)
        return True

    def click_key(self, keycode: int) -> bool:
        """物理按键点击:云游戏不暴露, 占位跳过。"""
        logger.debug("[bridge] click_key(%s) ignored", keycode)
        return True

    def key_down(self, keycode: int) -> bool:
        """物理按键按下:云游戏不暴露, 占位跳过。"""
        logger.debug("[bridge] key_down(%s) ignored", keycode)
        return True

    def key_up(self, keycode: int) -> bool:
        """物理按键抬起:云游戏不暴露, 占位跳过。"""
        logger.debug("[bridge] key_up(%s) ignored", keycode)
        return True