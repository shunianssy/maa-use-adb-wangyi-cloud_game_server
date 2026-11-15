"""
多账号(accounts.AccountStore)单元测试。

覆盖:
- 新增账号/重复登录更新 token 并去重
- 当前账号设置与移除后的顺延
- 启停与「多账号轮流执行」开关持久化
- 重新载入(模拟重启)、损坏文件与非法条目回退
- 下发列表不含 token(仅脱敏手机号)

运行: python -m unittest tests.test_accounts -v
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from accounts import AccountStore  # noqa: E402


class TestAccountStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "accounts.json")

    def test_empty_defaults(self):
        store = AccountStore(self.path)
        self.assertEqual(store.public_list(), [])
        self.assertFalse(store.auto_switch)
        self.assertEqual(store.current_phone(), "")
        self.assertIsNone(store.current())

    def test_upsert_adds_and_updates(self):
        store = AccountStore(self.path)
        self.assertTrue(store.upsert("13800001111", "tok-a"))     # 新增
        self.assertEqual(store.current_phone(), "13800001111")
        self.assertFalse(store.upsert("13800001111", "tok-a2"))   # 更新
        self.assertEqual(len(store.public_list()), 1)
        self.assertEqual(store.get("13800001111")["token"], "tok-a2")

    def test_upsert_normalizes_phone(self):
        store = AccountStore(self.path)
        store.upsert("+86 138-0000-1111", "tok")
        self.assertIsNotNone(store.get("13800001111"))

    def test_upsert_reenables_account(self):
        store = AccountStore(self.path)
        store.upsert("13800001111", "tok")
        store.set_enabled("13800001111", False)
        store.upsert("13800001111", "tok2")
        self.assertTrue(store.public_list()[0]["enabled"])

    def test_public_list_hides_token(self):
        store = AccountStore(self.path)
        store.upsert("13800001111", "secret")
        item = store.public_list()[0]
        self.assertNotIn("token", item)
        self.assertEqual(item["masked"], "138****1111")
        self.assertTrue(item["current"])

    def test_remove_current_falls_back(self):
        store = AccountStore(self.path)
        store.upsert("13800001111", "a")
        store.upsert("13900002222", "b")
        self.assertEqual(store.current_phone(), "13900002222")
        self.assertTrue(store.remove("13900002222"))
        self.assertEqual(store.current_phone(), "13800001111")
        self.assertTrue(store.remove("13800001111"))
        self.assertEqual(store.current_phone(), "")
        self.assertFalse(store.remove("13800001111"))

    def test_set_current_rejects_unknown(self):
        store = AccountStore(self.path)
        store.upsert("13800001111", "a")
        store.upsert("13900002222", "b")
        self.assertFalse(store.set_current("13700003333"))
        self.assertTrue(store.set_current("13800001111"))
        self.assertEqual(store.current_phone(), "13800001111")

    def test_toggle_and_auto_switch_persisted(self):
        store = AccountStore(self.path)
        store.upsert("13800001111", "a")
        store.upsert("13900002222", "b")
        self.assertTrue(store.set_enabled("13800001111", False))
        store.set_auto_switch(True)
        # 重新实例化(模拟重启)应恢复开关与启用状态
        reloaded = AccountStore(self.path)
        self.assertTrue(reloaded.auto_switch)
        self.assertEqual([a["phone"] for a in reloaded.enabled_accounts()],
                         ["13900002222"])
        self.assertFalse(reloaded.set_enabled("19999999999", True))

    def test_corrupt_file_falls_back(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        store = AccountStore(self.path)   # 不应抛异常
        self.assertEqual(store.public_list(), [])

    def test_utf8_bom_tolerated(self):
        # Windows 记事本保存的 JSON 可能带 BOM, 需能正常读取
        with open(self.path, "w", encoding="utf-8-sig") as f:
            json.dump({"accounts": [{"phone": "13800001111", "token": "x"}]}, f)
        store = AccountStore(self.path)
        self.assertEqual(len(store.public_list()), 1)

    def test_invalid_entries_skipped(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"accounts": [
                {"phone": "bad", "token": "x"},          # 非法手机号
                {"phone": "13800001111", "token": ""},   # 缺 token
                {"phone": "13800001111", "token": "ok"}, # 有效
                "not-a-dict",
            ]}, f)
        store = AccountStore(self.path)
        self.assertEqual(len(store.public_list()), 1)
        self.assertEqual(store.get("13800001111")["token"], "ok")

    def test_duplicate_phone_keeps_first(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"accounts": [
                {"phone": "13800001111", "token": "first"},
                {"phone": "13800001111", "token": "second"},
            ]}, f)
        store = AccountStore(self.path)
        self.assertEqual(len(store.public_list()), 1)
        self.assertEqual(store.get("13800001111")["token"], "first")


if __name__ == "__main__":
    unittest.main(verbosity=2)
