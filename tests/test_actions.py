"""
server.py 动作逻辑单元测试:使用假 sock / snapshotter 验证
do_click / do_swipe / do_input 与 status_payload, 不依赖真实云游戏连接。

运行: python -m unittest discover -s tests -v
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# 先 stub sdk.wsconnect, 避免测试触发真实网络 import 开销
import sys
import types

_fake_ws = types.ModuleType("sdk.wsconnect")
_fake_ws.connect = MagicMock()
_fake_ws.object_from_string = MagicMock()
_fake_ws.encode_mess = MagicMock()
_fake_ws.pack_message = lambda op, data: {"op": op, "data": data}
_fake_ws.send_action = AsyncMock()
_fake_ws.login = MagicMock()
_fake_ws.exit_game = MagicMock()
sys.modules["sdk.wsconnect"] = _fake_ws

# stub sdk 包本身
_sdk_pkg = types.ModuleType("sdk")
_sdk_pkg.wsconnect = _fake_ws
sys.modules["sdk"] = _sdk_pkg

# stub sdk.signin(server.py 顶部会导入它, 避免测试触达真实签到网络代码)
_fake_signin = types.ModuleType("sdk.signin")
_fake_signin.netease_signin = MagicMock(return_value={"ok": True, "endpoint": "https://stub"})
_sdk_pkg.signin = _fake_signin
sys.modules["sdk.signin"] = _fake_signin

import server  # noqa: E402


class TestDailyStatusLog(unittest.IsolatedAsyncioTestCase):
    """每日定时状态输出: 必须明确区分「已启用 / 未启用」, 不得出现自相矛盾的文案。"""

    def test_enabled_message(self):
        with patch("builtins.print") as printer:
            server._print_daily_status({"enabled": True, "time": "08:00"})
        text = " ".join(str(call.args[0]) for call in printer.call_args_list)
        self.assertIn("已启用", text)
        self.assertIn("每天 08:00", text)
        self.assertNotIn("未启用", text)

    def test_disabled_message(self):
        with patch("builtins.print") as printer:
            server._print_daily_status({})
        text = " ".join(str(call.args[0]) for call in printer.call_args_list)
        self.assertIn("未启用", text)
        self.assertNotIn("已启用", text)

    def test_disabled_message_guides_user(self):
        # 未启用时给出明确指引(避免用户不知道去哪里开)
        with patch("builtins.print") as printer:
            server._print_daily_status({"enabled": False, "time": ""})
        text = " ".join(str(call.args[0]) for call in printer.call_args_list)
        self.assertIn("网页控制台", text)


class AsyncTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 构造一个"已就绪"的假 AppState
        self.app_state = server.app_state
        self.app_state.reset()
        self.app_state.is_ready = True
        self.app_state.width = 1280
        self.app_state.height = 720
        self.app_state.sock = MagicMock()

    async def test_click_fails_when_not_ready(self):
        self.app_state.is_ready = False
        ok, err = await server.do_click(100, 200)
        self.assertFalse(ok)
        self.assertIn("not ready", err)

    async def test_click_ok(self):
        _fake_ws.send_action.reset_mock()
        ok, err = await server.do_click(640, 360)
        self.assertTrue(ok)
        self.assertIsNone(err)
        # 应发送两次动作:先 mm(移动) 后 cm(点击)
        self.assertEqual(_fake_ws.send_action.call_count, 2)

    async def test_swipe_ok(self):
        _fake_ws.send_action.reset_mock()
        ok, err = await server.do_swipe(100, 100, 300, 300, 500)
        self.assertTrue(ok)
        self.assertIsNone(err)
        # 触摸事件坐标必须为原始像素(与点击命令一致):
        # 若传 0-65535 归一化坐标, 云游戏端因坐标越界会忽略滑动(画面无任何反应)
        cmds = [c.args[1]["data"]["cmd"] for c in _fake_ws.send_action.call_args_list]
        self.assertTrue(cmds[0].startswith("1 100 100"), cmds[0])    # press 起点
        self.assertTrue(cmds[-1].startswith("3 300 300"), cmds[-1])  # release 终点
        for cmd in cmds[1:-1]:
            self.assertTrue(cmd.startswith("2 "), cmd)               # drag 中间点
        # 中间点坐标必须落在屏幕像素范围内(归一化后会出现数千的大数值)
        mid_coords = [int(v) for cmd in cmds[1:-1] for v in cmd.split()[1:3]]
        self.assertTrue(all(0 <= v <= 1280 for v in mid_coords), mid_coords)

    async def test_swipe_rejects_negative_duration(self):
        ok, err = await server.do_swipe(0, 0, 10, 10, -1)
        self.assertFalse(ok)

    async def test_input_ok(self):
        ok, err = await server.do_input("abc")
        self.assertTrue(ok)

    async def test_status_ready_payload(self):
        payload = await server.status_payload()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["width"], 1280)

    async def test_status_disconnected_when_not_ready(self):
        self.app_state.is_ready = False
        self.app_state.cloud_game_task = None
        payload = await server.status_payload()
        self.assertEqual(payload["status"], "disconnected")


class _FakeCloudGameTask:
    """最小化的云游戏任务桩: 仅需支持 done() 判断(用于连接归属判定)。"""

    def __init__(self, done: bool = False):
        self._done = done

    def done(self) -> bool:
        return self._done


class DailyFlowAutoCloseTest(unittest.IsolatedAsyncioTestCase):
    """每日定时流程: 任务结束后自动断开云游戏; 用户手动开启的会话不接管。

    背景: 定时任务若跑完不退出云游戏, 会持续消耗云游戏时长与流量,
    因此流程结束后需要主动断开; 但用户手动开启的会话不能被误断。
    """

    def setUp(self):
        self.app_state = server.app_state
        self.app_state.reset()
        self.log = MagicMock()

    def _fake_coord(self, start_result: bool = True):
        """构造协调器桩: running 表示线程是否存活, start 返回是否启动成功。"""
        coord = MagicMock()
        coord.running = False
        coord.start.return_value = start_result
        coord.status = self.log
        return coord

    def test_owned_when_no_active_connection(self):
        self.app_state.cloud_game_task = None
        self.assertTrue(server._cloud_game_owned_by_flow())

    def test_not_owned_when_connection_active(self):
        self.app_state.cloud_game_task = _FakeCloudGameTask(done=False)
        self.assertFalse(server._cloud_game_owned_by_flow())

    def test_owned_when_previous_task_finished(self):
        # 上次会话已结束(done=True)视为空闲, 可被本次流程接管
        self.app_state.cloud_game_task = _FakeCloudGameTask(done=True)
        self.assertTrue(server._cloud_game_owned_by_flow())

    async def test_auto_exit_skips_user_owned_session(self):
        # owned=False(用户手动开启)时必须保持连接不动
        self.app_state.cloud_game_task = _FakeCloudGameTask(done=False)
        with patch.object(server, "_handle_exit_internal", new=AsyncMock()) as m_exit:
            await server._auto_exit_cloud_game("每日定时", False, self.log)
        m_exit.assert_not_awaited()

    async def test_auto_exit_when_owned(self):
        self.app_state.cloud_game_task = _FakeCloudGameTask(done=False)
        with patch.object(server, "_handle_exit_internal", new=AsyncMock()) as m_exit:
            await server._auto_exit_cloud_game("每日定时", True, self.log)
        m_exit.assert_awaited_once()

    async def test_auto_exit_skips_when_connection_already_gone(self):
        self.app_state.cloud_game_task = None
        with patch.object(server, "_handle_exit_internal", new=AsyncMock()) as m_exit:
            await server._auto_exit_cloud_game("每日定时", True, self.log)
        m_exit.assert_not_awaited()

    async def test_watch_closes_after_coordinator_finishes(self):
        self.app_state.cloud_game_task = _FakeCloudGameTask(done=False)
        with patch.object(server, "_get_coord", return_value=self._fake_coord()), \
             patch.object(server, "_handle_exit_internal", new=AsyncMock()) as m_exit:
            await server._watch_flow_and_exit_cloud_game("每日定时", True, self.log)
        m_exit.assert_awaited_once()

    async def test_watch_skips_when_not_owned(self):
        coord = self._fake_coord()
        coord.running = True   # 即使任务还在跑, 不接管就不应做任何事
        with patch.object(server, "_get_coord", return_value=coord), \
             patch.object(server, "_handle_exit_internal", new=AsyncMock()) as m_exit:
            await server._watch_flow_and_exit_cloud_game("每日定时", False, self.log)
        m_exit.assert_not_awaited()

    async def test_run_daily_flow_releases_connection_when_no_tasks(self):
        # 云游戏由流程自行启动但没有任何可执行任务 -> 立即释放连接(节省时长/流量)
        self.app_state.cloud_game_task = None
        with patch.object(server, "_get_coord", return_value=self._fake_coord()), \
             patch.object(server, "_handle_start_internal",
                          new=AsyncMock(return_value=(True, "ok"))), \
             patch.object(server, "_wait_game_ready", new=AsyncMock(return_value=True)), \
             patch.object(server.maa_settings, "get",
                          return_value={"tasks": {}, "annihilation": {}}), \
             patch.object(server, "_auto_exit_cloud_game", new=AsyncMock()) as m_exit:
            ok, msg = await server._run_daily_flow("每日定时")
        self.assertFalse(ok)
        m_exit.assert_awaited_once()

    async def test_run_daily_flow_schedules_auto_close_after_start(self):
        # 任务启动成功后应挂后台看护协程, 由它等待结束再断开云游戏
        self.app_state.cloud_game_task = None

        def _consume_coro(coro):
            """替代 _spawn_bg: 关闭协程对象, 避免"never awaited"告警。"""
            coro.close()

        with patch.object(server, "_get_coord", return_value=self._fake_coord()), \
             patch.object(server, "_handle_start_internal",
                          new=AsyncMock(return_value=(True, "ok"))), \
             patch.object(server, "_wait_game_ready", new=AsyncMock(return_value=True)), \
             patch.object(server.maa_settings, "get", return_value={
                 "tasks": {"awaken": True}, "annihilation": {},
                 "fight": {}, "infrast": {}, "award": {}, "inventory": {}, "signin": {}}), \
             patch.object(server, "_spawn_bg", side_effect=_consume_coro) as m_spawn:
            ok, msg = await server._run_daily_flow("每日定时")
        self.assertTrue(ok)
        m_spawn.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)