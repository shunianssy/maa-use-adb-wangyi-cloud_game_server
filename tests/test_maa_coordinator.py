"""
maa_coordinator 单元测试:验证状态机、任务参数构建、日志落盘与线程生命周期。
不依赖真实 MaaCore/云游戏。

覆盖:
- TaskStatus 状态机(空闲/运行/错误)与快照
- build_task_params 按官方集成文档生成合法任务参数(Fight 注入关卡)
- MaaCoordinator.start 的过滤/校验与 fight_stage 透传
- 日志自动落盘(logs/maa_*.log 的内容与路径)

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
        # 自定义关卡/次数/代理/理智药必须进入 Fight 参数
        opts = {
            "fight": {
                "stage": "1-7", "times": 8, "series": 2,
                "medicine_mode": "num", "medicine": 3,
            },
            "annihilation": {"auto": True},
        }
        parsed = json.loads(mc.build_task_params("combat", "Fight", opts))
        self.assertEqual(parsed["stage"], "1-7")
        self.assertEqual(parsed["times"], 8)
        self.assertEqual(parsed["series"], 2)
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

    def test_annihilation_params(self):
        # 每周剿灭: 复用 Fight 并指定当期剿灭关卡; AUTO=大次数刷满即止
        opts = {"fight": {"series": 0}, "annihilation": {"auto": True, "times": 4}}
        parsed = json.loads(mc.build_task_params("annihilation", "Fight", opts))
        self.assertEqual(parsed["stage"], "Annihilation")
        self.assertEqual(parsed["times"], 999)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)