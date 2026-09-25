"""
fake_adb 桥的单元测试:用本地假 HTTP 服务验证命令转发逻辑。

覆盖:
- screen/epPNG 转换(JPEG -> PNG 字节流输出)
- click/swipe/text 命令 -> HTTP POST
- getprop/wm size 等伪命令 -> 伪文本
- 未识别命令静默成功

运行: .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import io
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from contextlib import redirect_stdout, redirect_stderr

# 让 fake_adb 可被 import 测试
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image


class FakeHttpHandler(BaseHTTPRequestHandler):
    """记录请求并在 /screencap 返回一张纯色 JPEG。"""

    requests = []  # (method, path, body)

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.__class__.requests.append(("GET", self.path, b""))
        if self.path == "/screencap":
            img = Image.new("RGB", (64, 64), (10, 20, 30))
            buf = io.BytesIO()
            img.save(buf, format="JPEG")
            data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        self.__class__.requests.append(("POST", self.path, body))
        self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()


class FakeAdbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = HTTPServer(("127.0.0.1", 0), FakeHttpHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        import fake_adb.fake_adb as fake_adb_mod
        cls.fake = fake_adb_mod
        cls.fake.BASE = cls.base
        cls.fake._GAME = cls.base

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=3)

    def setUp(self):
        FakeHttpHandler.requests = []

    def run_dispatch(self, argv, expect_exit=0):
        """执行 dispatch, 捕获 stdout 字节。"""
        out = io.BytesIO()
        err = io.BytesIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                self.fake.dispatch(argv[:])
                code = 0
            except SystemExit as e:
                code = e.code if e.code is not None else 0
        self.assertEqual(code, expect_exit, f"exit={code}, stderr={err.getvalue()!r}")
        return out.getvalue()

    def test_devices_lists_serial(self):
        out = self.run_dispatch(["devices"])
        self.assertIn(b"127.0.0.1:5555", out)
        self.assertIn(b"device", out)

    def test_connect_ok(self):
        out = self.run_dispatch(["connect", "127.0.0.1:5555"])
        self.assertIn(b"connected", out)

    def test_screencap_returns_png(self):
        out = self.run_dispatch(["-s", "127.0.0.1:5555", "exec-out", "screencap", "-p"])
        # PNG 魔数
        self.assertTrue(out.startswith(b"\x89PNG\r\n\x1a\n"), f"got {out[:8]!r}")

    def test_click_forwards(self):
        self.run_dispatch(["-s", "s", "shell", "input", "tap", "100", "200"])
        self.assertEqual(len(FakeHttpHandler.requests), 1)
        method, path, body = FakeHttpHandler.requests[0]
        self.assertEqual((method, path), ("POST", "/click"))
        self.assertEqual(json.loads(body), {"x": 100, "y": 200})

    def test_swipe_forwards(self):
        self.run_dispatch(["-s", "s", "shell", "input", "swipe", "1", "2", "3", "4", "500"])
        method, path, body = FakeHttpHandler.requests[0]
        self.assertEqual((method, path), ("POST", "/swipe"))
        self.assertEqual(json.loads(body),
                         {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "duration": 500})

    def test_text_forwards(self):
        self.run_dispatch(["-s", "s", "shell", "input", "text", "abc"])
        method, path, body = FakeHttpHandler.requests[0]
        self.assertEqual((method, path), ("POST", "/input"))
        self.assertEqual(json.loads(body), {"text": "abc"})

    def test_getprop_returns_release(self):
        out = self.run_dispatch(["-s", "s", "shell", "getprop", "ro.build.version.release"])
        self.assertIn(b"13", out)

    def test_wm_size_returns_resolution(self):
        out = self.run_dispatch(["-s", "s", "shell", "wm", "size"])
        self.assertIn(b"1280x720", out)

    def test_unknown_command_silent_success(self):
        self.run_dispatch(["some-unknown-cmd", "abc"])

    def test_keyevent_noop(self):
        self.run_dispatch(["-s", "s", "shell", "input", "keyevent", "HOME"])


if __name__ == "__main__":
    unittest.main(verbosity=2)