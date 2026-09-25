"""
maa_coordinator 单元测试:验证状态机、任务参数构建、日志落盘与线程生命周期。
不依赖真实 MaaCore/云游戏。

覆盖:
- TaskStatus 状态机(空闲/运行/错误)与快照
- build_task_params 按官方集成文档生成合法任务参数(Fight 注入关卡/次数 auto)
- MaaCoordinator.start 的过滤/校验与 fight_stage 透传
- 日志自动落盘(logs/maa_*.log 的内容与路径)
- 子任务出错达到上限后跳过该任务, 并继续执行后续任务

运行: python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

# 使项目根可被 import(maa_coordinator 顶层模块)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import maa_coordinator as mc


class TestBuildTaskParams(unittest.TestCase):
    """任务参数必须是对齐官方集成文档的合法 JSON。"""

    def test_params_are_valid_json(self):
        for key, task_type in mc.DEFAULT_TASK_MAP.items():
            raw = mc.build_task_params(key, task_type)
            parsed = json.loads(raw)   # 非法 JSON 会直接抛异常
            self.assertIsInstance(parsed, dict, f"{task_type} 参数非对象")

    def test_startup_disables_game_start(self):
        # 云游戏已在游戏内: 必须禁用客户端自动启动, 避免 am start 空转
        parsed = json.loads(mc.build_task_params("awaken", "StartUp"))
        self.assertEqual(parsed["start_game_enabled"], False)
        self.assertEqual(parsed["client_type"], "Official")

    def test_fight_params_from_options(self):
        # 自定义关卡/次数/理智药必须进入 Fight 参数; 代理倍率恒为 AUTO(0)
        opts = {
            "fight": {
                "stage": "1-7", "times": 8,
                "medicine_mode": "num", "medicine": 3,
            },
            "annihilation": {"auto": True},
        }
        parsed = json.loads(mc.build_task_params("combat", "Fight", opts))
        self.assertEqual(parsed["stage"], "1-7")
        self.assertEqual(parsed["times"], 8)
        self.assertEqual(parsed["series"], 0)   # 代理倍率恒为 AUTO, 前端已移除该配置
        self.assertEqual(parsed["medicine"], 3)
        # 理智药 AUTO: 视为全部使用(999), MAA 无药时自动停止用药
        opts_auto = {"fight": {"medicine_mode": "auto", "medicine": 0}}
        parsed_auto = json.loads(mc.build_task_params("combat", "Fight", opts_auto))
        self.assertEqual(parsed_auto["medicine"], 999)
        # 理智药关闭
        opts_off = {"fight": {"medicine_mode": "off", "medicine": 3}}
        parsed_off = json.loads(mc.build_task_params("combat", "Fight", opts_off))
        self.assertEqual(parsed_off["medicine"], 0)
        # 留空表示识别当前/上次关卡
        parsed_empty = json.loads(mc.build_task_params("combat", "Fight"))
        self.assertEqual(parsed_empty["stage"], "")

    def test_fight_times_auto_and_sanitized(self):
        # 次数 auto(不分大小写): 传大次数, 由 MAA 刷完当前理智自动停
        for raw in ("auto", "AUTO", " Auto "):
            parsed = json.loads(mc.build_task_params("combat", "Fight", {"fight": {"times": raw}}))
            self.assertEqual(parsed["times"], mc.FIGHT_TIMES_AUTO, raw)
        # 数字越界/非法值必须被钳制或回退默认, 不向 MAA 传脏数据
        self.assertEqual(
            json.loads(mc.build_task_params("combat", "Fight", {"fight": {"times": 0}}))["times"], 1)
        self.assertEqual(
            json.loads(mc.build_task_params(
                "combat", "Fight", {"fight": {"times": 100000}}))["times"], mc.FIGHT_TIMES_AUTO)
        self.assertEqual(
            json.loads(mc.build_task_params(
                "combat", "Fight", {"fight": {"times": "abc"}}))["times"], mc.DEFAULT_FIGHT_TIMES)

    def test_annihilation_params(self):
        # 每周剿灭: 复用 Fight 并指定当期剿灭关卡; AUTO=大次数刷满即止
        opts = {"fight": {}, "annihilation": {"auto": True, "times": 4}}
        parsed = json.loads(mc.build_task_params("annihilation", "Fight", opts))
        self.assertEqual(parsed["stage"], "Annihilation")
        self.assertEqual(parsed["times"], 999)
        self.assertEqual(parsed["series"], 0)
        self.assertEqual(parsed["medicine"], 0)
        # 指定场次(非 AUTO)
        opts_fixed = {"fight": {}, "annihilation": {"auto": False, "times": 3}}
        parsed_fixed = json.loads(mc.build_task_params("annihilation", "Fight", opts_fixed))
        self.assertEqual(parsed_fixed["times"], 3)

    def test_award_params_from_settings(self):
        # 领取奖励细分项全部来自设置(award 六项映射到 Award 参数)
        opts = {"award": {"award": True, "mail": True, "recruit": True,
                          "orundum": True, "mining": False, "specialaccess": True}}
        parsed = json.loads(mc.build_task_params("reward", "Award", opts))
        self.assertEqual(parsed["award"], True)
        self.assertEqual(parsed["mail"], True)
        self.assertEqual(parsed["recruit"], True)
        self.assertEqual(parsed["orundum"], True)
        self.assertEqual(parsed["mining"], False)
        self.assertEqual(parsed["specialaccess"], True)
        # 未提供领奖设置时默认全开(与 WebUI 抓图配置一致)
        default_parsed = json.loads(mc.build_task_params("reward", "Award"))
        self.assertTrue(default_parsed["mail"])
        self.assertTrue(default_parsed["specialaccess"])

    def test_depot_no_required_params(self):
        parsed = json.loads(mc.build_task_params("inventory", "Depot"))
        self.assertIsInstance(parsed, dict)

    def test_infrast_default_params(self):
        # 未提供基建设置时: 复用默认值(常规模式 + 全设施 + 贸易站-龙门币)
        parsed = json.loads(mc.build_task_params("infrast", "Infrast"))
        self.assertEqual(parsed["mode"], 0)
        self.assertEqual(parsed["facility"], list(mc.INFRAST_FACILITIES))
        self.assertEqual(parsed["drones"], "Money")
        self.assertEqual(parsed["threshold"], 0.3)
        self.assertTrue(parsed["replenish"])
        self.assertTrue(parsed["dorm_trust_enabled"])
        self.assertTrue(parsed["reception_clue_exchange"])
        # 常规模式不带自定义排班配置字段
        self.assertNotIn("filename", parsed)

    def test_infrast_params_from_settings(self):
        # 设置面板的基建设置必须完整落到 Infrast 参数
        opts = {"infrast": {
            "mode": 0,
            "facility": ["Mfg", "Trade", "Dorm"],
            "drones": "PureGold",
            "threshold": 0.45,
            "replenish": False,
            "dorm_notstationed_enabled": True,
            "dorm_trust_enabled": False,
            "reception_message_board": False,
            "reception_clue_exchange": False,
            "reception_send_clue": False,
        }}
        parsed = json.loads(mc.build_task_params("infrast", "Infrast", opts))
        self.assertEqual(parsed["mode"], 0)
        self.assertEqual(parsed["facility"], ["Mfg", "Trade", "Dorm"])
        self.assertEqual(parsed["drones"], "PureGold")
        self.assertEqual(parsed["threshold"], 0.45)
        self.assertFalse(parsed["replenish"])
        self.assertTrue(parsed["dorm_notstationed_enabled"])
        self.assertFalse(parsed["dorm_trust_enabled"])
        self.assertFalse(parsed["reception_message_board"])
        self.assertFalse(parsed["reception_clue_exchange"])
        self.assertFalse(parsed["reception_send_clue"])

    def test_infrast_rotation_mode(self):
        # 队列轮换(mode=20000): 不携带自定义排班字段
        opts = {"infrast": {"mode": 20000, "facility": ["Mfg", "Trade"]}}
        parsed = json.loads(mc.build_task_params("infrast", "Infrast", opts))
        self.assertEqual(parsed["mode"], 20000)
        self.assertNotIn("filename", parsed)
        self.assertNotIn("plan_index", parsed)

    def test_infrast_custom_mode_requires_filename(self):
        # 自定义基建模式: 有配置路径时写入 filename/plan_index
        opts = {"infrast": {"mode": 10000,
                            "filename": " resource/custom_infrast/x.json ",
                            "plan_index": 2}}
        parsed = json.loads(mc.build_task_params("infrast", "Infrast", opts))
        self.assertEqual(parsed["mode"], 10000)
        self.assertEqual(parsed["filename"], "resource/custom_infrast/x.json")
        self.assertEqual(parsed["plan_index"], 2)
        # 缺配置路径时回退常规模式, 避免 AppendTask 因参数非法整体失败
        fallback = json.loads(mc.build_task_params(
            "infrast", "Infrast", {"infrast": {"mode": 10000, "filename": "  "}}))
        self.assertEqual(fallback["mode"], 0)
        self.assertNotIn("filename", fallback)

    def test_infrast_invalid_values_sanitized(self):
        # 非法模式/设施/无人机/阈值都必须被规整, 不向 MAA 传脏数据
        opts = {"infrast": {
            "mode": 123,
            "facility": ["Mfg", "Bogus", "Mfg", "Trade"],
            "drones": "Nope",
            "threshold": 5,
        }}
        parsed = json.loads(mc.build_task_params("infrast", "Infrast", opts))
        self.assertEqual(parsed["mode"], 0)
        self.assertEqual(parsed["facility"], ["Mfg", "Trade"])   # 保序去重并剔除非法项
        self.assertEqual(parsed["drones"], "Money")
        self.assertEqual(parsed["threshold"], 1.0)               # 钳制到 [0, 1.0]
        # 设施全为非法项时回退默认全选(而非空数组导致 MAA 报错)
        empty = json.loads(mc.build_task_params(
            "infrast", "Infrast", {"infrast": {"facility": []}}))
        self.assertEqual(empty["facility"], list(mc.INFRAST_FACILITIES))

    def test_default_task_map_no_hog(self):
        # "库存保持"协议层映射为官方支持的 Depot(不存在 Hog 类型)
        self.assertEqual(mc.DEFAULT_TASK_MAP["inventory"], "Depot")
        # 每周剿灭映射为 Fight
        self.assertEqual(mc.DEFAULT_TASK_MAP["annihilation"], "Fight")


class TestTaskStatus(unittest.TestCase):
    def test_initial_idle(self):
        st = mc.TaskStatus()
        self.assertEqual(st.state, "idle")
        snap = st.snapshot()
        self.assertEqual(snap["state"], "idle")
        self.assertEqual(snap["finished"], [])

    def test_run_lifecycle(self):
        st = mc.TaskStatus()
        st.start_run(["awaken", "reward"])
        self.assertEqual(st.state, "running")
        st.set_current("StartUp")
        self.assertEqual(st.snapshot()["current"], "StartUp")
        st.mark_done("awaken", True)
        st.set_final("idle", "完成")
        snap = st.snapshot()
        self.assertEqual(snap["state"], "idle")
        self.assertIn("awaken", snap["finished"])

    def test_failure_records_error(self):
        st = mc.TaskStatus()
        st.start_run(["combat"])
        st.mark_done("combat", False, "boom")
        st.set_final("error", "boom")
        snap = st.snapshot()
        self.assertEqual(snap["state"], "error")
        self.assertIn("boom", snap["error"])

    def test_log_ring_buffer_limited(self):
        st = mc.TaskStatus()
        st.start_run(["awaken"])
        self.addCleanup(st._close_log_file)   # 避免测试后遗留打开的文件句柄
        for i in range(150):
            st.log(f"line-{i}")
        self.assertLessEqual(len(st.snapshot()["log"]), 100)

    def test_log_autosaved_to_disk(self):
        # 日志必须自动落盘: start_run 创建文件, log 同步写入, 结束关闭句柄
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(mc, "MAA_LOG_DIR", tmp):
                st = mc.TaskStatus()
                st.start_run(["awaken", "combat"])
                st.log("hello maa")
                st.set_final("idle", "完成")
                snap = st.snapshot()
                self.assertTrue(snap["log_path"], "log_path 不应为空")
                self.assertTrue(os.path.isfile(snap["log_path"]), "日志文件应存在")
                with open(snap["log_path"], encoding="utf-8") as f:
                    content = f.read()
                self.assertIn("hello maa", content)
                self.assertIn("完成", content)


class TestMaaCoordinator(unittest.TestCase):
    def test_start_without_tasks_rejected(self):
        coord = mc.MaaCoordinator()
        self.assertFalse(coord.start([]))
        self.assertEqual(coord.status.state, "idle")

    def test_start_with_unknown_tasks_rejected(self):
        # 只传未知任务: TASK_EXEC_ORDER 过滤后为空 -> 拒绝
        coord = mc.MaaCoordinator()
        self.assertFalse(coord.start(["unknown-task"]))

    def test_exec_order_filters_and_validates(self):
        # 验证 TASK_EXEC_ORDER 包含所有已知任务 key
        known = {"awaken", "recruit", "infrast", "combat", "annihilation",
                 "inventory", "credit", "reward"}
        self.assertEqual(set(mc.TASK_EXEC_ORDER), known)
        # 剿灭必须排在理智作战之前(先清本周剿灭再刷普通关)
        self.assertLess(mc.TASK_EXEC_ORDER.index("annihilation"),
                        mc.TASK_EXEC_ORDER.index("combat"))

    def test_start_runs_thread_and_passes_options(self):
        # 不连真实 MaaCore: 替换 _run 为记录参数的桩, 验证 start 透传 options
        coord = mc.MaaCoordinator()
        seen = {}

        def fake_run(tasks):
            seen["tasks"] = list(tasks)
            seen["options"] = dict(coord._options)
            seen["signin"] = coord._signin
            seen["token"] = coord._token
            coord.status.set_final("idle", "完成")

        coord._run = fake_run
        opts = {"fight": {"stage": "CE-6"}, "annihilation": {"times": 2}}
        self.assertTrue(coord.start(["awaken", "combat", "annihilation"],
                                    options=opts, signin=True, token="abc"))
        coord._thread.join(timeout=5)
        # 按 TASK_EXEC_ORDER 顺序过滤后的结果
        self.assertEqual(seen["tasks"], ["awaken", "annihilation", "combat"])
        self.assertEqual(seen["options"]["fight"]["stage"], "CE-6")
        self.assertTrue(seen["signin"])
        self.assertEqual(seen["token"], "abc")

    def test_running_flag(self):
        # 不真正启动线程(避免连 MaaCore/云游戏), 仅验证初始 not running
        coord = mc.MaaCoordinator()
        self.assertFalse(coord.running)
        snap = coord.snapshot()
        self.assertFalse(snap["running"])
        self.assertEqual(snap["state"], "idle")


class _ScriptedAssistant:
    """脚本化假 MaaCore: 用于验证「逐个任务执行 + 出错超限跳过」流程。

    - error_indexes 中指定序号的任务在运行期间持续上报子任务错误
      (每次 running() 上报一次), 直至协调器请求 stop;
    - 其余任务在 start() 时直接上报 TaskChainCompleted 并立即结束。
    """

    def __init__(self, error_indexes=(0,)):
        self.error_indexes = set(error_indexes)
        self.appended: list = []
        self.started = 0
        self.stopped = 0
        self._callback = None
        self._running = False
        self._index = -1

    @property
    def version(self) -> str:
        return "scripted"

    def initialize(self, callback=None):
        self._callback = callback

    def connect(self) -> bool:
        return True

    def append_task(self, task_type, params) -> int:
        self.appended.append(task_type)
        return len(self.appended)

    def start(self) -> None:
        self.started += 1
        self._index += 1
        self._running = True
        if self._index not in self.error_indexes and self._callback:
            # 正常任务: 立即上报完成并结束
            self._callback(10002, json.dumps({"taskchain": self.appended[self._index]}))
            self._running = False

    def running(self) -> bool:
        if not self._running:
            return False
        if self._callback:
            self._callback(20000, json.dumps(
                {"taskchain": self.appended[self._index], "why": "scripted error"}))
        return True

    def stop(self) -> None:
        self.stopped += 1
        self._running = False


class TestSubTaskErrorSkip(unittest.TestCase):
    """子任务出错达到上限后必须跳过该任务, 并继续执行后续任务。"""

    def test_skip_flag_trips_at_limit(self):
        coord = mc.MaaCoordinator()
        coord.status.start_run(["combat"])
        self.addCleanup(coord.status._close_log_file)
        # 未达上限: 不请求跳过
        for _ in range(mc.MAX_SUB_TASK_ERRORS - 1):
            coord._on_maa_msg(20000, json.dumps({"taskchain": "Fight", "why": "x"}))
        self.assertFalse(coord._skip_flag.is_set())
        # 达到上限: 请求跳过当前任务
        coord._on_maa_msg(20000, json.dumps({"taskchain": "Fight", "why": "x"}))
        self.assertTrue(coord._skip_flag.is_set())
        self.assertIn("子任务出错", "\n".join(coord.status.snapshot()["log"]))

    def test_reset_error_tracking(self):
        coord = mc.MaaCoordinator()
        coord.status.start_run(["combat"])
        self.addCleanup(coord.status._close_log_file)
        coord._on_maa_msg(20000, json.dumps({"taskchain": "Fight", "why": "x"}))
        coord._reset_error_tracking()
        self.assertEqual(coord._sub_error_count, 0)
        self.assertFalse(coord._skip_flag.is_set())
        # 任务链状态记录同样复位(每次任务单独判定成败)
        self.assertEqual(coord._chain_outcome.get("state"), "")

    def test_chain_error_marks_outcome(self):
        # 任务链出错必须记录结果, 供 _wait_task 判定任务失败
        coord = mc.MaaCoordinator()
        coord.status.start_run(["combat"])
        self.addCleanup(coord.status._close_log_file)
        coord._on_maa_msg(10000, json.dumps({"taskchain": "Fight", "why": "boom"}))
        self.assertEqual(coord._chain_outcome.get("state"), "error")
        self.assertEqual(coord._chain_outcome.get("why"), "boom")

    def test_run_skips_failed_task_and_continues(self):
        # _run 接收的是 start() 过滤排序后的任务序列(awaken 先于 combat);
        # 这里让第二个任务(combat)持续上报子任务错误
        scripted = _ScriptedAssistant(error_indexes=(1,))
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(mc, "MAA_LOG_DIR", tmp), \
                 mock.patch("maa_core_wrapper.MaaCoreAssistant", lambda *a, **k: scripted), \
                 mock.patch.object(mc.time, "sleep", lambda _s: None):
                coord = mc.MaaCoordinator()
                coord._run(["awaken", "combat"])

        # 两个任务都被追加并启动; 出错任务被 stop 一次(即跳过)
        self.assertEqual(scripted.appended, ["StartUp", "Fight"])
        self.assertEqual(scripted.started, 2)
        self.assertEqual(scripted.stopped, 1)
        snap = coord.status.snapshot()
        self.assertIn("awaken", snap["finished"])
        self.assertIn("combat", snap["finished"])
        self.assertIn("跳过 1 个: combat", snap["message"])
        self.assertIn("子任务出错达到上限", "\n".join(snap["log"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)