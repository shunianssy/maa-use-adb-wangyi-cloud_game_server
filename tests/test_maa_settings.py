"""
设置持久化(maa_settings)与云游戏签到(signin)单元测试。

覆盖:
- MaaSettings: 默认结构、合并更新、落盘后可重新载入、未知字段不写入
- netease_signin: 命中 2xx 视为成功; 端点全部失败返回失败; 异常端点跳过继续

运行: .venv\\Scripts\\python.exe -m unittest tests.test_maa_settings -v
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# test_actions 在测试收集阶段会把 sys.modules 中的 sdk / sdk.signin 替换为桩;
# 本测试需要真实实现, 先清除污染再导入真实包, 避免拿到桩模块。
sys.modules.pop("sdk", None)
sys.modules.pop("sdk.signin", None)

from maa_settings import (  # noqa: E402
    INFRAST_FACILITIES,
    MaaSettings,
    default_settings,
)
from sdk import signin  # noqa: E402


class TestMaaSettings(unittest.TestCase):
    def test_default_structure(self):
        s = default_settings()
        self.assertIn("tasks", s)
        self.assertIn("fight", s)
        self.assertIn("annihilation", s)
        self.assertIn("signin", s)
        self.assertIn("daily", s)
        self.assertIn("last_daily_run", s)
        # 新字段默认值: 理智药 AUTO、剿灭默认不勾选(需手动启用, 避免与理智作战混淆)、
        # 签到默认开启
        self.assertEqual(s["fight"]["medicine_mode"], "auto")
        self.assertFalse(s["annihilation"]["enabled"])
        self.assertTrue(s["annihilation"]["auto"])
        self.assertTrue(s["signin"]["enabled"])
        # 领取奖励六项细分默认全开(对应 Award 任务)
        self.assertTrue(s["award"]["award"])
        self.assertTrue(s["award"]["mail"])
        self.assertTrue(s["award"]["orundum"])

    def test_infrast_defaults(self):
        # 基建设置默认: 常规模式 + 全设施 + 贸易站-龙门币 + 心情阈值 0.3
        s = default_settings()
        self.assertIn("infrast", s)
        infra = s["infrast"]
        self.assertEqual(infra["mode"], 0)
        self.assertEqual(infra["facility"], list(INFRAST_FACILITIES))
        self.assertEqual(infra["drones"], "Money")
        self.assertEqual(infra["threshold"], 0.3)
        self.assertTrue(infra["replenish"])
        self.assertFalse(infra["dorm_notstationed_enabled"])
        self.assertTrue(infra["dorm_trust_enabled"])
        self.assertEqual(infra["filename"], "")
        self.assertEqual(infra["plan_index"], 0)
        # 默认值必须深拷贝: 修改返回值不能污染后续 default_settings()
        infra["facility"].append("Bogus")
        self.assertNotIn("Bogus", default_settings()["infrast"]["facility"])

    def test_inventory_defaults(self):
        # 库存保持默认: 任务开关关闭, 4 个保持项均未勾选(预设目标数量)
        s = default_settings()
        self.assertIn("inventory", s)
        self.assertFalse(s["tasks"]["inventory"])
        inv = s["inventory"]
        self.assertEqual(set(inv), {"chip_low", "chip_high", "certificate", "skill_summary"})
        for key, count in (("chip_low", 20), ("chip_high", 20),
                           ("certificate", 20), ("skill_summary", 200)):
            self.assertFalse(inv[key]["enabled"], key)
            self.assertEqual(inv[key]["count"], count, key)

    def test_inventory_persisted(self):
        # 保持项需可落盘并重新载入(部分覆盖时其余字段保持默认)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "maa_settings.json")
            ms = MaaSettings(path)
            ms.update({"inventory": {"chip_low": {"enabled": True, "count": 30}}})
            data = MaaSettings(path).get()["inventory"]
            self.assertTrue(data["chip_low"]["enabled"])
            self.assertEqual(data["chip_low"]["count"], 30)
            self.assertFalse(data["chip_high"]["enabled"])
            self.assertEqual(data["skill_summary"]["count"], 200)

    def test_infrast_persisted(self):
        # 基建设置需可落盘并重新载入(含设施列表整体替换)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "maa_settings.json")
            ms = MaaSettings(path)
            ms.update({"infrast": {"mode": 20000, "facility": ["Mfg", "Trade"],
                                   "threshold": 0.5}})
            data = MaaSettings(path).get()["infrast"]
            self.assertEqual(data["mode"], 20000)
            self.assertEqual(data["facility"], ["Mfg", "Trade"])
            self.assertEqual(data["threshold"], 0.5)
            self.assertEqual(data["drones"], "Money")   # 未覆盖字段保持默认

    def test_save_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "maa_settings.json")
            ms = MaaSettings(path)
            ms.update({"fight": {"stage": "1-7", "times": 9},
                       "daily": {"enabled": True, "time": "07:30"}})
            # 重新实例化(模拟重启)应能从磁盘恢复
            ms2 = MaaSettings(path)
            data = ms2.get()
            self.assertEqual(data["fight"]["stage"], "1-7")
            self.assertEqual(data["fight"]["times"], 9)
            self.assertEqual(data["daily"]["enabled"], True)
            self.assertEqual(data["daily"]["time"], "07:30")

    def test_unknown_keys_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            ms = MaaSettings(os.path.join(tmp, "s.json"))
            ms.update({"hacked_key": "x", "fight": {"bogus": 1}})
            data = ms.get()
            self.assertNotIn("hacked_key", data)
            self.assertNotIn("bogus", data["fight"])

    def test_corrupt_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            ms = MaaSettings(path)   # 不应抛异常
            self.assertEqual(ms.get()["fight"]["times"], 5)


class TestNeteaseSignin(unittest.TestCase):
    def test_returns_ok_on_2xx(self):
        resp = mock.Mock(status_code=200, text="{}")
        with mock.patch.object(signin.requests, "post", return_value=resp) as m_post:
            result = signin.netease_signin("tok", endpoints=["https://e1", "https://e2"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["endpoint"], "https://e1")
        self.assertEqual(m_post.call_count, 1)  # 命中即停, 不再探测后续

    def test_tries_all_and_fails(self):
        resp = mock.Mock(status_code=404, text="nope")
        with mock.patch.object(signin.requests, "post", return_value=resp):
            result = signin.netease_signin("tok", endpoints=["https://e1", "https://e2"])
        self.assertFalse(result["ok"])
        self.assertIn("无可用签到端点", result["message"])

    def test_network_error_skips_to_next(self):
        def side_effect(*args, **kwargs):
            call = side_effect.calls
            side_effect.calls += 1
            if call == 0:
                raise OSError("conn refused")
            return mock.Mock(status_code=200, text="{}")
        side_effect.calls = 0
        with mock.patch.object(signin.requests, "post", side_effect=side_effect):
            result = signin.netease_signin(
                "tok", endpoints=["https://e1", "https://e2"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["endpoint"], "https://e2")


if __name__ == "__main__":
    unittest.main(verbosity=2)