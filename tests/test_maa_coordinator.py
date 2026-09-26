"""
maa_coordinator 单元测试:验证状态机、任务参数构建、日志落盘与线程生命周期。
不依赖真实 MaaCore/云游戏。

覆盖:
- TaskStatus 状态机(空闲/运行/错误)与快照
- build_task_params 按官方集成文档生成合法任务参数(Fight 注入关卡/次数 auto)
- MaaCoordinator.start 的过滤/校验与 fight_stage 透传
- 日志自动落盘(logs/maa_*.log 的内容与路径)
- 子任务出错达到上限后跳过该任务, 并继续执行后续任务
- 库存保持: 缺口计算与规划、DepotInfo 回调解析、仓库识别->理智作战补缺口流程

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
        # 库存保持优先级: 剿灭 > 库存保持 > 理智作战配置
        self.assertLess(mc.TASK_EXEC_ORDER.index("annihilation"),
                        mc.TASK_EXEC_ORDER.index("inventory"))
        self.assertLess(mc.TASK_EXEC_ORDER.index("inventory"),
                        mc.TASK_EXEC_ORDER.index("combat"))
        # 「领取奖励」需在库存保持之前(先领完奖励再扫描仓库, 邮件/任务材料计入库存)
        self.assertLess(mc.TASK_EXEC_ORDER.index("reward"),
                        mc.TASK_EXEC_ORDER.index("inventory"))

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
    - 其余任务在 start() 时直接上报 TaskChainCompleted 并立即结束;
    - depot_data 非 None 时, Depot 任务启动时先上报 DepotInfo(仓库识别结果),
      供库存保持流程测试(否则模拟识别无数据返回)。
    """

    def __init__(self, error_indexes=(0,), depot_data=None):
        self.error_indexes = set(error_indexes)
        self.depot_data = depot_data
        self.appended: list = []
        self.params: list = []
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
        self.params.append(params)
        return len(self.appended)

    def start(self) -> None:
        self.started += 1
        self._index += 1
        self._running = True
        task = self.appended[self._index]
        if self.depot_data is not None and task == "Depot" and self._callback:
            # 模拟仓库识别: 一次性上报 done=true 的库存数据
            self._callback(20003, json.dumps({
                "taskchain": "Depot", "what": "DepotInfo",
                "details": {"done": True, "data": json.dumps(self.depot_data)},
            }))
        if self._index not in self.error_indexes and self._callback:
            # 正常任务: 立即上报完成并结束
            self._callback(10002, json.dumps({"taskchain": task}))
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


# 库存保持测试用: 所有低级芯片均达标(8 职业各 20)的仓库快照
_FULL_CHIP_DEPOT = {
    "3231": 20, "3261": 20, "3241": 20, "3251": 20,
    "3211": 20, "3271": 20, "3221": 20, "3281": 20,
}


class TestInventoryPlan(unittest.TestCase):
    """库存保持: 缺口计算与规划(纯函数, 不依赖 MaaCore)。

    说明: 资源关卡有开放日轮换(芯片本/AP-5/CA-5), 各用例固定传 weekday,
    避免依赖测试运行当天的星期。
    """

    MONDAY = 1      # PR-A-1 / PR-B-1 / AP-5 开放
    TUESDAY = 2     # CA-5 开放
    SATURDAY = 6    # PR-A-1 / CA-5 关闭

    @staticmethod
    def _make_opt(**items):
        """构造库存保持设置: 传 key=目标数量 表示该项已勾选, 其余未勾选。"""
        opt = {k: {"enabled": False, "count": 20} for k in mc.INVENTORY_ORDER}
        for key, count in items.items():
            opt[key] = {"enabled": True, "count": count}
        return opt

    def test_missing_item_treated_as_zero(self):
        # 仓库无该物品数据(识别缺失) -> 缺口 = 目标数量, 关卡为对应芯片本
        plan = mc.plan_inventory({}, self._make_opt(chip_low=20), weekday=self.MONDAY)
        self.assertEqual(plan["stage"], "PR-A-1")
        self.assertEqual(plan["have"], 0)
        self.assertEqual(plan["target"], 20)
        self.assertEqual(plan["gap"], 20)

    def test_gap_and_item_mapping(self):
        # PR-A-1 掉落重装(3231)/医疗(3261): 医疗达标时规划重装, 缺口 5
        depot = {**_FULL_CHIP_DEPOT, "3231": 15}
        plan = mc.plan_inventory(depot, self._make_opt(chip_low=20), weekday=self.MONDAY)
        self.assertEqual(plan["stage"], "PR-A-1")
        self.assertEqual(plan["item_id"], "3231")
        self.assertEqual(plan["item_name"], "重装芯片")
        self.assertEqual(plan["have"], 15)
        self.assertEqual(plan["gap"], 5)

    def test_same_stage_picks_largest_gap(self):
        # 同关卡两种物品都缺: 取缺口更大的(3241 缺 8 > 3251 缺 3)
        depot = {**_FULL_CHIP_DEPOT, "3241": 12, "3251": 17}
        plans = mc.inventory_gaps(depot, self._make_opt(chip_low=20), weekday=self.MONDAY)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["stage"], "PR-B-1")
        self.assertEqual(plans[0]["item_id"], "3241")
        self.assertEqual(plans[0]["gap"], 8)

    def test_order_prefers_chip_low(self):
        # 低级芯片与采购凭证同时缺口: 规划顺序 chip_low 优先(下拉顺序即优先级)
        depot = {"4006": 0}
        opt = self._make_opt(chip_low=20, certificate=20)
        plan = mc.plan_inventory(depot, opt, weekday=self.MONDAY)
        self.assertEqual(plan["key"], "chip_low")
        gaps = mc.inventory_gaps(depot, opt, weekday=self.MONDAY)
        self.assertGreater(len(gaps), 1)
        self.assertEqual(gaps[0]["key"], "chip_low")
        self.assertIn("AP-5", [g["stage"] for g in gaps])

    def test_all_met_returns_none(self):
        # 全部保持项达标 -> 无规划, 不产生任何理智作战
        self.assertIsNone(mc.plan_inventory(
            _FULL_CHIP_DEPOT, self._make_opt(chip_low=20), weekday=self.MONDAY))
        self.assertEqual(mc.inventory_gaps(
            _FULL_CHIP_DEPOT, self._make_opt(chip_low=20), weekday=self.MONDAY), [])

    def test_disabled_items_ignored(self):
        # 全部未勾选 -> 无规划
        opt = {k: {"enabled": False, "count": 20} for k in mc.INVENTORY_ORDER}
        self.assertIsNone(mc.plan_inventory({}, opt, weekday=self.MONDAY))

    def test_invalid_count_falls_back(self):
        # count 非法 -> 回退该保持项默认值(技巧概要默认 200); 周二 CA-5 开放
        plan = mc.plan_inventory(
            {}, {"skill_summary": {"enabled": True, "count": "abc"}}, weekday=self.TUESDAY)
        self.assertEqual(plan["stage"], "CA-5")
        self.assertEqual(plan["target"], 200)
        self.assertEqual(plan["gap"], 200)

    def test_stage_mapping_for_resource_stages(self):
        # 采购凭证 -> AP-5(4006); 技巧概要·卷3 -> CA-5(3303): 预设映射防误改
        plan = mc.plan_inventory({}, self._make_opt(certificate=20), weekday=self.MONDAY)
        self.assertEqual((plan["stage"], plan["item_id"]), ("AP-5", "4006"))
        plan = mc.plan_inventory({}, self._make_opt(skill_summary=200), weekday=self.TUESDAY)
        self.assertEqual((plan["stage"], plan["item_id"]), ("CA-5", "3303"))

    # ---- 开放日(资源关按星期轮换, 非开放日必须跳过, 否则 MAA 导航卡住) ----

    def test_open_days_from_preset(self):
        # 开放日数据固定核对(PRTS 关卡一览/资源收集), 防止误改导致再次选到关闭关卡
        def days(key, stage):
            for item in mc.INVENTORY_PRESETS[key]["stages"]:
                if item["stage"] == stage:
                    return item["open_days"]
            raise AssertionError(f"未找到关卡 {stage}")

        self.assertEqual(days("chip_low", "PR-A-1"), [1, 4, 5, 7])     # 固若金汤
        self.assertEqual(days("chip_low", "PR-B-1"), [1, 2, 5, 6])     # 摧枯拉朽
        self.assertEqual(days("chip_low", "PR-C-1"), [3, 4, 6, 7])     # 势不可挡
        self.assertEqual(days("chip_low", "PR-D-1"), [2, 3, 6, 7])     # 身先士卒
        self.assertEqual(days("chip_high", "PR-A-2"), [1, 4, 5, 7])    # 与 PR-A-1 同日
        self.assertEqual(days("certificate", "AP-5"), [1, 4, 6, 7])    # 粉碎防御
        self.assertEqual(days("skill_summary", "CA-5"), [2, 3, 5, 7])  # 空中威胁

    def test_closed_stage_skipped(self):
        # 周六(6): PR-A-1(周一四五日)不开放 -> 跳过, 改规划今天开放的下一个缺口关卡
        plan = mc.plan_inventory({}, self._make_opt(chip_low=20), weekday=self.SATURDAY)
        self.assertEqual(plan["stage"], "PR-B-1")   # 周六开放且顺序最靠前
        self.assertNotEqual(plan["stage"], "PR-A-1")

    def test_all_closed_returns_none_but_visible(self):
        # 只勾选技巧概要(CA-5 周二三五日): 周六无计划; include_closed 仍可见缺口(供提示)
        opt = self._make_opt(skill_summary=200)
        self.assertIsNone(mc.plan_inventory({}, opt, weekday=self.SATURDAY))
        closed = mc.inventory_gaps({}, opt, weekday=self.SATURDAY, include_closed=True)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["stage"], "CA-5")

    def test_next_open_weekday(self):
        # "下次开放"计算: 周六(6) 之后 PR-A-1(1,4,5,7) 最近开放 -> 周日(7)
        self.assertEqual(mc.next_open_weekday([1, 4, 5, 7], 6), 7)
        # 周日(7) 之后 -> 周一(1)(跨周)
        self.assertEqual(mc.next_open_weekday([1, 4, 5, 7], 7), 1)
        # 空列表 -> 无可用项
        self.assertIsNone(mc.next_open_weekday([], 6))


class TestInventoryFightParams(unittest.TestCase):
    """库存保持的理智作战参数: 指定关卡 + drops 停止条件 + 次数 auto。"""

    def test_params_from_plan(self):
        plan = {"stage": "PR-A-1", "item_id": "3231", "item_name": "重装芯片",
                "have": 15, "target": 20, "gap": 5}
        parsed = json.loads(mc.build_inventory_fight_params(plan, {"medicine_mode": "auto"}))
        self.assertEqual(parsed["stage"], "PR-A-1")
        self.assertEqual(parsed["times"], mc.FIGHT_TIMES_AUTO)   # 次数 auto
        self.assertEqual(parsed["drops"], {"3231": 5})           # 刷够缺口即停
        self.assertEqual(parsed["series"], 0)                    # 代理倍率恒为 AUTO
        self.assertEqual(parsed["stone"], 0)                     # 库存保持不碎石
        self.assertEqual(parsed["client_type"], "Official")
        self.assertEqual(parsed["medicine"], 999)                # 沿用作战设置的 AUTO

    def test_medicine_follows_fight_settings(self):
        plan = {"stage": "CA-5", "item_id": "3303", "gap": 100}
        off = json.loads(mc.build_inventory_fight_params(plan, {"medicine_mode": "off"}))
        self.assertEqual(off["medicine"], 0)
        num = json.loads(mc.build_inventory_fight_params(
            plan, {"medicine_mode": "num", "medicine": 2}))
        self.assertEqual(num["medicine"], 2)

    def test_gap_sanitized_to_positive(self):
        # 缺口非法(0/负数/非数字)时钳制为 1, 避免 drops 条件无效导致无限刷
        for raw in (0, -3, "x"):
            parsed = json.loads(mc.build_inventory_fight_params(
                {"stage": "PR-B-2", "item_id": "3242", "gap": raw}))
            self.assertEqual(parsed["drops"], {"3242": 1}, raw)


class TestDepotInfoCallback(unittest.TestCase):
    """DepotInfo 回调解析: 渐进数据累计 + 完成标记 + 脏数据容错。"""

    def _coord(self):
        """构造协调器并临时改写日志目录(避免测试污染真实 logs/)。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(mc, "MAA_LOG_DIR", tmp.name):
            coord = mc.MaaCoordinator()
            coord.status.start_run(["inventory"])
        self.addCleanup(coord.status._close_log_file)
        return coord

    @staticmethod
    def _depot_msg(done, data):
        return json.dumps({
            "taskchain": "Depot", "what": "DepotInfo",
            "details": {"done": done, "data": data},
        })

    def test_progressive_data_accumulates(self):
        # 识别中(done=false)的渐进数据也要累计, 供超时/中断时降级使用
        coord = self._coord()
        coord._on_maa_msg(20003, self._depot_msg(False, json.dumps({"3231": 5})))
        self.assertEqual(coord._depot_data, {"3231": 5})
        self.assertFalse(coord._depot_done.is_set())

    def test_done_marks_complete_and_merges(self):
        coord = self._coord()
        coord._on_maa_msg(20003, self._depot_msg(False, json.dumps({"3231": 5})))
        coord._on_maa_msg(20003, self._depot_msg(True, json.dumps({"3231": 8, "4006": 120})))
        self.assertTrue(coord._depot_done.is_set())
        self.assertEqual(coord._depot_data, {"3231": 8, "4006": 120})
        self.assertIn("仓库识别完成: 共 2 种物品", "\n".join(coord.status.snapshot()["log"]))

    def test_taskchain_extrainfo_compatible(self):
        # 兼容 TaskChainExtraInfo(10003) 携带的 DepotInfo(版本差异兜底)
        coord = self._coord()
        coord._on_maa_msg(10003, self._depot_msg(True, json.dumps({"4006": 3})))
        self.assertEqual(coord._depot_data, {"4006": 3})
        self.assertTrue(coord._depot_done.is_set())

    def test_invalid_payload_tolerated(self):
        # data 非法 JSON / 非数字条目: 不抛异常, 忽略脏数据
        coord = self._coord()
        coord._on_maa_msg(20003, self._depot_msg(True, "not-json"))
        self.assertEqual(coord._depot_data, {})
        coord._on_maa_msg(20003, self._depot_msg(True, json.dumps({"3231": "x", "4006": 3})))
        self.assertEqual(coord._depot_data, {"4006": 3})


class TestInventoryRun(unittest.TestCase):
    """库存保持完整流程: 仓库识别 -> 缺口规划 -> 追加理智作战补缺口。"""

    INV_OPT = {
        "chip_low": {"enabled": True, "count": 20},
        "chip_high": {"enabled": False, "count": 20},
        "certificate": {"enabled": False, "count": 20},
        "skill_summary": {"enabled": False, "count": 200},
    }

    def _run_inventory(self, scripted, inv_opt=None, weekday=1):
        """执行 _run_inventory(临时日志目录 + 固定星期 + 免等待), 返回 (coord, outcome)。

        weekday 默认周一: 保证 PR-A-1 等测试用关卡处于开放日, 不依赖运行当天星期。
        """
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(mc, "MAA_LOG_DIR", tmp), \
                 mock.patch.object(mc.time, "sleep", lambda _s: None), \
                 mock.patch.object(mc, "_today_weekday", lambda: weekday):
                coord = mc.MaaCoordinator()
                coord._options = {"inventory": inv_opt or self.INV_OPT,
                                  "fight": {"medicine_mode": "off"}}
                coord.status.start_run(["inventory", "combat"])
                self.addCleanup(coord.status._close_log_file)
                # 与 _run 一致: 先建立回调, 脚本化核心才能上报 DepotInfo/任务完成,
                # 否则 Depot 任务永不结束(真机上由 _wait_depot 超时兜底)
                scripted.initialize(callback=coord._on_maa_msg)
                outcome = coord._run_inventory(scripted)
                # 模拟真实运行收尾: 关闭日志文件句柄(否则 Windows 下临时目录无法清理)
                coord.status.set_final("idle", "完成")
                return coord, outcome

    def test_gap_appends_fight_with_drops(self):
        # 重装芯片缺 5(现有 15/目标 20): 规划 PR-A-1 并追加带 drops 的理智作战
        depot = {**_FULL_CHIP_DEPOT, "3231": 15}
        scripted = _ScriptedAssistant(error_indexes=(), depot_data=depot)
        coord, outcome = self._run_inventory(scripted)
        self.assertTrue(outcome.ok)
        self.assertEqual(scripted.appended, ["Depot", "Fight"])
        parsed = json.loads(scripted.params[1])
        self.assertEqual(parsed["stage"], "PR-A-1")
        self.assertEqual(parsed["drops"], {"3231": 5})
        self.assertEqual(parsed["times"], mc.FIGHT_TIMES_AUTO)
        # 状态里记录规划结果(供前端显示"下次理智作战打什么/哪一关")
        plan = coord.status.snapshot()["inventory_plan"]
        self.assertEqual(plan["stage"], "PR-A-1")
        self.assertEqual(plan["gap"], 5)
        logs = "\n".join(coord.status.snapshot()["log"])
        self.assertIn("正在扫描仓库", logs)
        self.assertIn("规划理智作战 -> PR-A-1", logs)

    def test_all_met_skips_fight(self):
        # 全部达标: 只扫描仓库, 不追加理智作战, 状态无规划
        scripted = _ScriptedAssistant(error_indexes=(), depot_data=_FULL_CHIP_DEPOT)
        coord, outcome = self._run_inventory(scripted)
        self.assertTrue(outcome.ok)
        self.assertEqual(scripted.appended, ["Depot"])
        self.assertIsNone(coord.status.snapshot()["inventory_plan"])
        self.assertIn("均已达标", "\n".join(coord.status.snapshot()["log"]))

    def test_no_item_selected_skips_scan(self):
        # 未勾选任何保持项: 直接跳过, 不执行仓库识别
        opt = {k: {"enabled": False, "count": 20} for k in mc.INVENTORY_ORDER}
        scripted = _ScriptedAssistant(error_indexes=())
        coord, outcome = self._run_inventory(scripted, inv_opt=opt)
        self.assertTrue(outcome.ok)
        self.assertEqual(scripted.appended, [])
        self.assertIn("未勾选任何保持项", "\n".join(coord.status.snapshot()["log"]))

    def test_depot_without_data_fails_gracefully(self):
        # 识别未返回数据: 库存保持失败, 但不追加战斗、不阻断后续任务
        scripted = _ScriptedAssistant(error_indexes=())   # depot_data=None -> 无数据
        coord, outcome = self._run_inventory(scripted)
        self.assertFalse(outcome.ok)
        self.assertEqual(scripted.appended, ["Depot"])
        self.assertIn("仓库识别失败", "\n".join(coord.status.snapshot()["log"]))

    def test_closed_stage_logs_next_open(self):
        # 周六(6) 且只勾选技巧概要(CA-5 周六关闭): 不追加战斗, 日志提示下次开放日
        opt = {k: {"enabled": False, "count": 20} for k in mc.INVENTORY_ORDER}
        opt["skill_summary"] = {"enabled": True, "count": 200}
        scripted = _ScriptedAssistant(error_indexes=(), depot_data={"3303": 10})
        coord, outcome = self._run_inventory(scripted, inv_opt=opt, weekday=6)
        self.assertTrue(outcome.ok)
        self.assertEqual(scripted.appended, ["Depot"])   # 不追加理智作战
        self.assertIsNone(coord.status.snapshot()["inventory_plan"])
        logs = "\n".join(coord.status.snapshot()["log"])
        self.assertIn("今日不开放", logs)
        self.assertIn("下次开放: 周日", logs)   # CA-5(周二三五日) 之后最近开放日为周日


class TestTaskLabels(unittest.TestCase):
    """任务来源可辨识: 理智作战与每周剿灭同用 Fight 类型, 日志必须能区分。

    背景: 每周剿灭在 MaaCore 侧复用 Fight, 日志若只显示 "Fight", 容易误判为
          "没勾选理智作战却仍在执行"。
    """

    def test_display_name_mapping(self):
        self.assertEqual(mc.task_display_name("annihilation", "Fight"), "每周剿灭(Fight)")
        self.assertEqual(mc.task_display_name("combat", "Fight"), "理智作战(Fight)")
        self.assertEqual(mc.task_display_name("awaken", "StartUp"), "开始唤醒(StartUp)")
        # 未收录的 key 回退为 MaaCore 类型名, 保证日志始终可读
        self.assertEqual(mc.task_display_name("unknown", "Custom"), "Custom")

    def test_chain_callbacks_label_task_source(self):
        coord = mc.MaaCoordinator()
        coord.status.start_run(["annihilation"])
        self.addCleanup(coord.status._close_log_file)
        coord._current_key, coord._current_type = "annihilation", "Fight"
        coord._on_maa_msg(10001, json.dumps({"taskchain": "Fight"}))
        coord._on_maa_msg(10002, json.dumps({"taskchain": "Fight"}))
        logs = "\n".join(coord.status.snapshot()["log"])
        self.assertIn("任务开始: 每周剿灭(Fight)", logs)
        self.assertIn("任务完成: 每周剿灭(Fight)", logs)

    def test_sub_task_error_falls_back_to_subtask_name(self):
        # MaaCore 的 why 可能为空: 此时用出错节点名兜底, 避免日志出现无信息的 "()"
        coord = mc.MaaCoordinator()
        coord.status.start_run(["annihilation"])
        self.addCleanup(coord.status._close_log_file)
        coord._current_key, coord._current_type = "annihilation", "Fight"
        coord._on_maa_msg(20000, json.dumps(
            {"taskchain": "Fight", "subtask": "StartButton", "why": ""}))
        logs = "\n".join(coord.status.snapshot()["log"])
        self.assertIn("每周剿灭(Fight)", logs)
        self.assertIn("StartButton", logs)

    def test_sub_task_error_without_reason(self):
        # 原因与节点名都缺失时, 也要给出明确说明而不是空括号
        coord = mc.MaaCoordinator()
        coord.status.start_run(["combat"])
        self.addCleanup(coord.status._close_log_file)
        coord._current_key, coord._current_type = "combat", "Fight"
        coord._on_maa_msg(20000, json.dumps({"taskchain": "Fight"}))
        self.assertIn("未提供原因", "\n".join(coord.status.snapshot()["log"]))

    def test_run_logs_readable_task_sequence(self):
        # 全流程日志应使用中文名: 任务序列 / 追加任务 / 剿灭跳过说明
        scripted = _ScriptedAssistant(error_indexes=())
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(mc, "MAA_LOG_DIR", tmp), \
                 mock.patch("maa_core_wrapper.MaaCoreAssistant", lambda *a, **k: scripted), \
                 mock.patch.object(mc.time, "sleep", lambda _s: None):
                coord = mc.MaaCoordinator()
                coord._run(["annihilation", "combat"])
        logs = "\n".join(coord.status.snapshot()["log"])
        self.assertIn("任务序列: 每周剿灭(Fight), 理智作战(Fight)", logs)
        self.assertIn("追加任务成功: 每周剿灭(Fight)", logs)
        self.assertIn("追加任务成功: 理智作战(Fight)", logs)
        self.assertIn("本周合成玉已达上限时 MAA 会直接跳过", logs)


if __name__ == "__main__":
    unittest.main(verbosity=2)