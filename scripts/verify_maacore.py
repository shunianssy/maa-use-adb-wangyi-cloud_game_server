"""
验证 ctypes 能否驱动 MAA 官方核心 MaaCore.dll(路线 1 可行性验证)。

按 maa-cli 的强制初始化顺序:
    1. SetDllDirectoryW(指向 lib 目录)   -> 依赖可解析
    2. AsstSetUserDir(存在的用户目录路径)
    3. AsstLoadResource(resource 的父目录)
    4. AsstCreate() / AsstCreateEx()
    5. AsstDestroy()

跳出点:
- AsstGetVersion 正常 + AsstLoadResource 返回 1 + AsstCreate 非空
  => 证明 MAA 官方核心可被 Python 驱动, 且能解析含 ClickSelf 的官方资源。

运行: .venv\\Scripts\\python.exe scripts/verify_maacore.py
"""

import ctypes
import os
import sys

# --- 由 `maa dir` 输出得到的路径 ---
MAA_LIB_DIR = r"C:\Users\user\AppData\Roaming\loong\maa\data\lib"
MAA_DATA_DIR = r"C:\Users\user\AppData\Roaming\loong\maa\data"   # resource 的父目录
RESOURCE_DIR = os.path.join(MAA_DATA_DIR, "resource")
# 用户目录(AsstSetUserDir 要求已存在): 使用 maa 数据目录下的 debug/log 根
USER_DIR = os.path.join(MAA_DATA_DIR, "debug")


def ensure_user_dir() -> None:
    """创建用户目录(AsstSetUserDir 要求目录真实存在)。"""
    os.makedirs(USER_DIR, exist_ok=True)
    print(f"[OK] 用户目录就绪: {USER_DIR}")


def main() -> int:
    """验证流程, 返回进程退出码。"""
    # 0) 依赖 DLL 搜索: 复刻 runtime.rs 的 SetDllDirectoryW
    os.environ["PATH"] = MAA_LIB_DIR + ";" + os.environ.get("PATH", "")
    try:
        ctypes.windll.kernel32.SetDllDirectoryW(MAA_LIB_DIR)
        print(f"[OK] SetDllDirectoryW -> {MAA_LIB_DIR}")
    except Exception as e:
        print(f"[WARN] SetDllDirectoryW 失败(依赖可能仍可解析): {e}")

    # 1) 加载 MaaCore.dll
    try:
        core = ctypes.WinDLL(os.path.join(MAA_LIB_DIR, "MaaCore.dll"))
    except OSError as e:
        print(f"[FAIL] 无法加载 MaaCore.dll: {e}")
        return 1
    print(f"[OK] 已加载 MaaCore.dll")

    # 2) 声明接口签名(依据官方 AsstCaller.h / maa.wo)
    core.AsstGetVersion.restype = ctypes.c_char_p
    core.AsstSetUserDir.argtypes = [ctypes.c_char_p]
    core.AsstSetUserDir.restype = ctypes.c_uint8
    core.AsstLoadResource.argtypes = [ctypes.c_char_p]
    core.AsstLoadResource.restype = ctypes.c_uint8
    core.AsstCreate.restype = ctypes.c_void_p
    core.AsstCreateEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    core.AsstCreateEx.restype = ctypes.c_void_p
    core.AsstDestroy.argtypes = [ctypes.c_void_p]

    # 3) 版本确认
    ver = core.AsstGetVersion()
    print(f"[OK] MaaCore 版本: {ver.decode() if ver else '(空)'}")

    # 4) 设置用户目录(必须在 load_resource 之前)
    ensure_user_dir()
    if not core.AsstSetUserDir(USER_DIR.encode("utf-8")):
        print("[FAIL] AsstSetUserDir 返回 false")
        return 1
    print(f"[OK] AsstSetUserDir({USER_DIR})")

    # 5) 加载官方资源(传 resource 的父目录)
    if not core.AsstLoadResource(MAA_DATA_DIR.encode("utf-8")):
        print(f"[FAIL] AsstLoadResource({MAA_DATA_DIR}) 返回 false")
        return 1
    print(f"[OK] AsstLoadResource({MAA_DATA_DIR}) 资源加载成功(含 ClickSelf 等官方动作)")

    # 6) 创建实例
    handle = core.AsstCreate()
    if not handle:
        print("[FAIL] AsstCreate 仍返回 NULL")
        return 1
    print(f"[OK] AsstCreate 成功, handle = {hex(handle)}")

    # 7) 清理
    core.AsstDestroy(handle)
    print("[OK] AsstDestroy 完成")

    print("\n>>> 结论: ctypes 已能完整驱动 MaaCore.dll(加载资源 + 创建实例) <<<")
    print(">>> 这证明路线 1 可行: 可直接用官方 Asst* 接口跑明日方舟任务 <<<")
    return 0


if __name__ == "__main__":
    sys.exit(main())