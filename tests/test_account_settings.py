"""
账号独立配置(account_settings.AccountSettingsStore)单元测试。

覆盖:
- 新增/合并更新账号配置并持久化、重新载入
- merged: 账号覆盖与全局默认深度合并, 不影响全局快照
- 仅保存运行字段(tasks/fight/... ), daily 等全局字段被忽略
- 删除账号配置、非法手机号、损坏文件与 BOM 容错

运行: python -m unittest tests.test_account_settings -v
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from account_settings import AccountSettingsStore  # noqa: E402
from maa_settings import default_settings  # noqa: E402


class TestAccountSettingsStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "account_settings.json")

    def test_empty_defaults(self):
        store = AccountSettingsStore(self.path)
        self.assertEqual(store.get("13800001111"), {})
        self.assertEqual(store.phones(), [])

    def test_update_and_reload(self):
        store = AccountSettingsStore(self.path)
        store.update("13800001111", {"fight": {"stage": "1-7", "times": 9}})
        # 重新实例化(模拟重启)应能从磁盘恢复
        reloaded = AccountSettingsStore(self.path)
        self.assertEqual(reloaded.get("13800001111")["fight"]["stage"], "1-7")
        self.assertEqual(reloaded.get("13800001111")["fight"]["times"], 9)

    def test_partial_update_merges(self):
        store = AccountSettingsStore(self.path)
        store.update("13800001111", {"fight": {"stage": "1-7"}})
        store.update("13800001111", {"fight": {"times": 3}})
        fight = store.get("13800001111")["fight"]
        self.assertEqual(fight["stage"], "1-7")
        self.assertEqual(fight["times"], 3)

    def test_merged_over_global_defaults(self):
        store = AccountSettingsStore(self.path)
        store.update("13800001111", {"tasks": {"combat": False},
                                     "fight": {"stage": "CE-6"}})
        base = default_settings()
        merged = store.merged("13800001111", base)
        self.assertFalse(merged["tasks"]["combat"])
        self.assertTrue(merged["tasks"]["awaken"])       # 未覆盖字段保持全局
        self.assertEqual(merged["fight"]["stage"], "CE-6")
        self.assertEqual(merged["fight"]["times"], 5)     # 全局默认保留
        self.assertEqual(merged["daily"], base["daily"])  # daily 仍为全局
        # 不修改传入的全局快照
        self.assertTrue(base["tasks"]["combat"])

    def test_merged_without_override_returns_base(self):
        store = AccountSettingsStore(self.path)
        base = default_settings()
        merged = store.merged("13800001111", base)
        self.assertEqual(merged, base)
        merged["tasks"]["awaken"] = False
        self.assertTrue(base["tasks"]["awaken"])          # 返回深拷贝

    def test_global_only_fields_ignored(self):
        store = AccountSettingsStore(self.path)
        store.update("13800001111", {"daily": {"enabled": True},
                                     "last_daily_run": "2026-01-01",
                                     "tasks": {"awaken": False}})
        cfg = store.get("13800001111")
        self.assertNotIn("daily", cfg)
        self.assertNotIn("last_daily_run", cfg)
        self.assertFalse(cfg["tasks"]["awaken"])

    def test_remove(self):
        store = AccountSettingsStore(self.path)
        store.update("13800001111", {"tasks": {"awaken": False}})
        self.assertTrue(store.remove("13800001111"))
        self.assertFalse(store.remove("13800001111"))
        self.assertEqual(AccountSettingsStore(self.path).get("13800001111"), {})

    def test_weekdays_default_none(self):
        # 未配置执行日时返回 None, 表示沿用全局设置
        store = AccountSettingsStore(self.path)
        self.assertIsNone(store.get_weekdays("13800001111"))

    def test_weekdays_update_and_merge(self):
        store = AccountSettingsStore(self.path)
        store.update("13800001111", {"weekdays": [3, 1, 3, 9, "x"]})
        # 去重 + 升序 + 忽略非法值
        self.assertEqual(store.get_weekdays("13800001111"), [1, 3])
        base = default_settings()
        base["daily"]["weekdays"] = [1, 2, 3, 4, 5]
        merged = store.merged("13800001111", base)
        self.assertEqual(merged["daily"]["weekdays"], [1, 3])
        # 不修改全局快照
        self.assertEqual(base["daily"]["weekdays"], [1, 2, 3, 4, 5])

    def test_weekdays_empty_means_no_day(self):
        # 空列表是有效配置(该账号不执行任何一天), 不能回落全局
        store = AccountSettingsStore(self.path)
        store.update("13800001111", {"weekdays": []})
        self.assertEqual(store.get_weekdays("13800001111"), [])
        merged = store.merged("13800001111", default_settings())
        self.assertEqual(merged["daily"]["weekdays"], [])

    def test_weekdays_persisted(self):
        store = AccountSettingsStore(self.path)
        store.update("13800001111", {"weekdays": [5, 6]})
        self.assertEqual(
            AccountSettingsStore(self.path).get_weekdays("13800001111"), [5, 6])

    def test_invalid_phone(self):
        store = AccountSettingsStore(self.path)
        self.assertEqual(store.update("bad", {"tasks": {"awaken": False}}), {})
        self.assertEqual(store.get("bad"), {})
        self.assertFalse(store.remove("bad"))

    def test_utf8_bom_tolerated(self):
        with open(self.path, "w", encoding="utf-8-sig") as f:
            json.dump({"13800001111": {"tasks": {"awaken": False}}}, f)
        store = AccountSettingsStore(self.path)
        self.assertFalse(store.get("13800001111")["tasks"]["awaken"])

    def test_corrupt_file_falls_back(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        store = AccountSettingsStore(self.path)   # 不应抛异常
        self.assertEqual(store.phones(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
