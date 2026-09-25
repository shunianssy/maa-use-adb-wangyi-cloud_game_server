"""
server.py 端口释放工具的单元测试: 验证 netstat 解析与 taskkill 调用逻辑。

覆盖:
- _pid_listening_on: 能从 netstat 输出正确解析监听 PID; 未占用/非监听返回 None
- _free_port_if_occupied: 占用时不杀当前进程; taskkill 失败只告警不抛出

运行: .venv\\Scripts\\python.exe -m unittest tests.test_server_port -v
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import server  # noqa: E402  (需 .venv 环境: 依赖 aiortc)


NETSTAT_OCCUPIED = """\
  TCP    127.0.0.1:22888    0.0.0.0:0    LISTENING    4321

  TCP    127.0.0.1:22889    0.0.0.0:0    LISTENING    9999
"""


class TestPidListeningOn(unittest.TestCase):
    def test_finds_listening_pid(self):
        with mock.patch("subprocess.check_output", return_value=NETSTAT_OCCUPIED):
            pid = server._pid_listening_on("127.0.0.1", 22888)
        self.assertEqual(pid, 4321)

    def test_returns_none_when_port_free(self):
        with mock.patch("subprocess.check_output", return_value=NETSTAT_OCCUPIED):
            pid = server._pid_listening_on("127.0.0.1", 22890)
        self.assertIsNone(pid)

    def test_ignores_other_protocol_states(self):
        # 仅有 TIME_WAIT/其他端口时, 不应误判
        out = "  TCP    127.0.0.1:22888    0.0.0.0:0    TIME_WAIT    4321\n"
        with mock.patch("subprocess.check_output", return_value=out):
            pid = server._pid_listening_on("127.0.0.1", 22888)
        self.assertIsNone(pid)

    def test_netstat_failure_returns_none(self):
        with mock.patch("subprocess.check_output", side_effect=OSError("no netstat")):
            pid = server._pid_listening_on("127.0.0.1", 22888)
        self.assertIsNone(pid)


class TestFreePortIfOccupied(unittest.TestCase):
    def test_kills_occupier(self):
        # 第一次探测到占用 4321, 杀掉后下一轮探测端口已释放 -> 结束循环
        with mock.patch.object(server, "_pid_listening_on", side_effect=[4321, None]), \
             mock.patch.object(server, "_pid_name", return_value="python.exe"), \
             mock.patch.object(server.os, "getpid", return_value=1111), \
             mock.patch("subprocess.run") as m_run:
            m_run.return_value = mock.Mock(returncode=0, stderr="")
            # 平台越权: 强制按 Windows 分支执行
            with mock.patch.object(server.platform, "system", return_value="Windows"):
                server._free_port_if_occupied("127.0.0.1", 22888)
        m_run.assert_called_once()
        # 校验调用了 taskkill /F /PID 4321
        self.assertIn("4321", m_run.call_args.args[0])

    def test_skips_current_process(self):
        with mock.patch.object(server, "_pid_listening_on", return_value=os.getpid()), \
             mock.patch.object(server.platform, "system", return_value="Windows"), \
             mock.patch("subprocess.run") as m_run:
            server._free_port_if_occupied("127.0.0.1", 22888)
        m_run.assert_not_called()  # 不应杀掉自身

    def test_kill_failure_only_warns(self):
        with mock.patch.object(server, "_pid_listening_on", return_value=4321), \
             mock.patch.object(server._pid_name, "__call__", return_value="python.exe"), \
             mock.patch.object(server.platform, "system", return_value="Windows"), \
             mock.patch("subprocess.run", side_effect=OSError("denied")):
            # 不应抛出异常
            server._free_port_if_occupied("127.0.0.1", 22888)

    def test_noop_when_platform_not_windows(self):
        with mock.patch.object(server, "_pid_listening_on") as m_listen, \
             mock.patch.object(server.platform, "system", return_value="Linux"):
            server._free_port_if_occupied("127.0.0.1", 22888)
        m_listen.assert_not_called()  # 非 Windows 不探测不杀进程


if __name__ == "__main__":
    unittest.main(verbosity=2)