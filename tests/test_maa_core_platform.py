"""
maa_core_wrapper 跨平台改造的单元测试(不加载真实 MaaCore)。

覆盖:
    - 动态库文件名随平台切换(MaaCore.dll / libMaaCore.so)
    - 假 adb 默认路径随平台切换(adb.bat / adb.sh)且文件确实存在
    - 动态库缺失时抛出带修复提示的 MaaCoreError

运行(标准库 unittest):
    python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import maa_core_wrapper as mcw  # noqa: E402  (需先补 sys.path)


class TestPlatformDefaults(unittest.TestCase):
    """平台相关默认值。"""

    def test_library_name_matches_platform(self):
        """动态库文件名应与当前平台一致。"""
        expected = "MaaCore.dll" if os.name == "nt" else "libMaaCore.so"
        self.assertEqual(mcw.LIB_NAME, expected)

    def test_fake_adb_default_exists(self):
        """默认假 adb 脚本应随平台选择且真实存在。"""
        expected_suffix = "adb.bat" if os.name == "nt" else "adb.sh"
        self.assertTrue(mcw.FAKE_ADB_PATH.endswith(expected_suffix), mcw.FAKE_ADB_PATH)
        self.assertTrue(os.path.isfile(mcw.FAKE_ADB_PATH), f"假 adb 脚本缺失: {mcw.FAKE_ADB_PATH}")

    def test_lib_dir_default_follows_platform(self):
        """Windows 库目录在 data/lib 下, Linux 与 resource 同级。"""
        if os.name == "nt":
            self.assertEqual(mcw.MAA_LIB_DIR, os.path.join(mcw.MAA_DATA_DIR, "lib"))
        else:
            self.assertEqual(mcw.MAA_LIB_DIR, mcw.MAA_DATA_DIR)


class TestLoadFailure(unittest.TestCase):
    """动态库缺失时的错误路径。"""

    def test_missing_library_raises_with_hint(self):
        """目录中没有动态库时应抛出 MaaCoreError 且给出修复提示。"""
        with self.assertRaises(mcw.MaaCoreError) as ctx:
            mcw.MaaCoreAssistant(lib_dir=os.path.join(REPO_ROOT, "no_such_lib_dir"))
        message = str(ctx.exception)
        self.assertIn(mcw.LIB_NAME, message)
        # 提示信息应指明下一步操作(环境变量或自动拉取脚本)
        self.assertTrue("MAA_LIB_DIR" in message or "fetch_maa_resource" in message, message)


if __name__ == "__main__":
    unittest.main(verbosity=2)