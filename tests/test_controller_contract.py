"""
控制器契约测试:使用本地假 HTTP 服务验证 NeteaseCloudGameController
的 connect / screencap / click / swipe / input_text 行为, 不依赖真实云游戏。

运行(标准库 unittest, 无需额外依赖):
    python -m unittest discover -s tests -v
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO

import numpy as np
from PIL import Image

from maa_bridge.netease_controller import NeteaseCloudGameController

WIDTH, HEIGHT = 1280, 720


class FakeCloudGameHandler(BaseHTTPRequestHandler):
    """模拟 server.py 的最小行为(仅覆盖桥接用到的接口)。

    info_ready / calls 为类级共享状态, 便于测试用例控制与断言。
    """

    calls = []       # 记录收到请求的 (method, path, [body])
    info_ready = True

    def log_message(self, *args):
        """静默访问日志。"""
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.__class__.calls.append(("GET", self.path))
        if self.path == "/info":
            if self.__class__.info_ready:
                self._json({"status": "ok", "width": WIDTH, "height": HEIGHT})
            else:
                self._json({"status": "connecting", "message": "..."})
        elif self.path == "/screencap":
            # 生成一张纯色 JPEG 返回(RGB=(10,20,30))
            img = Image.new("RGB", (WIDTH, HEIGHT), (10, 20, 30))
            buf = BytesIO()
            img.save(buf, format="JPEG")
            data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json({"status": "error", "message": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        body = json.loads(raw or b"{}")
        self.__class__.calls.append(("POST", self.path, body))
        if self.path in ("/start", "/click", "/swipe", "/input"):
            self._json({"status": "ok"})
        else:
            self._json({"status": "error", "message": "not found"}, 404)


class FakeServerMixin(unittest.TestCase):
    """启动/关闭线程化假服务并暴露 base_url。"""

    @classmethod
    def setUpClass(cls):
        cls.handler_cls = FakeCloudGameHandler
        cls.server = HTTPServer(("127.0.0.1", 0), cls.handler_cls)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=3)

    def setUp(self):
        # 每个用例重置共享状态
        self.handler_cls.calls = []
        self.handler_cls.info_ready = True

    def make_ctrl(self, **kwargs):
        kwargs.setdefault("base_url", self.base_url)
        kwargs.setdefault("connect_timeout", 5)
        return NeteaseCloudGameController(**kwargs)


class TestConnect(FakeServerMixin):
    def test_connect_waits_until_ready(self):
        """首次 /info 未就绪时, connect 应轮询直至 ok, 且分辨率被校正。"""
        self.handler_cls.info_ready = False

        def _flip():
            import time
            time.sleep(0.5)
            self.handler_cls.info_ready = True

        threading.Thread(target=_flip, daemon=True).start()

        ctrl = self.make_ctrl()
        self.assertTrue(ctrl.connect())
        self.assertEqual(ctrl._width, WIDTH)
        self.assertEqual(ctrl._height, HEIGHT)

    def test_connect_timeout_when_never_ready(self):
        """服务从未就绪时, connect 应在超时后返回 False。"""
        self.handler_cls.info_ready = False
        ctrl = NeteaseCloudGameController(self.base_url, connect_timeout=2)
        self.assertFalse(ctrl.connect())


class TestScreencap(FakeServerMixin):
    def test_screencap_returns_bgr_frame(self):
        """screencap 返回 (H, W, 3) uint8 的 BGR 数组(RGB(10,20,30) -> BGR(30,20,10))。"""
        ctrl = self.make_ctrl()
        self.assertTrue(ctrl.connect())
        frame = ctrl.screencap()
        self.assertIsInstance(frame, np.ndarray)
        self.assertEqual(frame.shape, (HEIGHT, WIDTH, 3))
        self.assertEqual(frame.dtype, np.uint8)
        self.assertEqual(tuple(frame[0, 0].tolist()), (30, 20, 10))


class TestInputActions(FakeServerMixin):
    def test_click_swipe_input_forward(self):
        """点击/滑动/输入正确转发为 POST JSON 并返回 True。"""
        ctrl = self.make_ctrl()
        self.assertTrue(ctrl.connect())

        self.assertTrue(ctrl.click(640, 360))
        self.assertTrue(ctrl.swipe(100, 200, 800, 200, 500))
        self.assertTrue(ctrl.input_text("arknights"))

        calls = self.handler_cls.calls
        self.assertIn(("POST", "/click", {"x": 640, "y": 360}), calls)
        self.assertIn(("POST", "/swipe", {"x1": 100, "y1": 200, "x2": 800, "y2": 200, "duration": 500}), calls)
        self.assertIn(("POST", "/input", {"text": "arknights"}), calls)


class TestSystemOps(FakeServerMixin):
    def test_system_ops_return_true(self):
        """云游戏不具备的系统操作(启动/按键/多点触控)占位返回 True。"""
        ctrl = self.make_ctrl()
        self.assertTrue(ctrl.start_app("x"))
        self.assertTrue(ctrl.stop_app("x"))
        self.assertTrue(ctrl.click_key(4))
        self.assertTrue(ctrl.key_down(4))
        self.assertTrue(ctrl.key_up(4))
        self.assertTrue(ctrl.touch_down(0, 1, 2, 3))
        self.assertTrue(ctrl.touch_move(0, 1, 2, 3))
        self.assertTrue(ctrl.touch_up(0))


if __name__ == "__main__":
    unittest.main(verbosity=2)