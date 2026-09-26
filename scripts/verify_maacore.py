"""
验证 ctypes 能否驱动 MAA 官方核心(Windows: MaaCore.dll / Linux: libMaaCore.so)。

按 maa-cli 的强制初始化顺序:
    1. 定位动态库目录(Windows 额外 SetDllDirectoryW) -> 依赖可解析
    2. AsstSetUserDir(存在的用户目录路径)
    3. AsstLoadResource(resource 的父目录)
    4. AsstCreate() / AsstCreateEx()
    5. AsstDestroy()

跳出点:
- AsstGetVersion 正常 + AsstLoadResource 返回 1 + AsstCreate 非空
  => 证明 MAA 官方核心可被 Python 驱动, 且能解析含 ClickSelf 的官方资源。

运行:
    Windows: .venv\\Scripts\\python.exe scripts/verify_maacore.py
    容器内:  docker compose exec netease-maa python scripts/verify_maacore.py
"""

import ctypes
import os
import sys

# 复用主程序的环境变量与默认路径(避免两处维护)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from maa_core_wrapper import (  # noqa: E402  (需先补 sys.path 才能导入)
    LIB_NAME,
    MAA_DATA_DIR,
    MAA_LIB_DIR,
    USER_DIR,
    _IS_WINDOWS,
)


def ensure_user_dir() -> None:
    """创建用户目录(AsstSetUserDir 要求目录真实存在)。"""
    os.makedirs(USER_DIR, exist_ok=True)
    print(f"[OK] 用户目录就绪: {USER_DIR}")


def load_core():
    """按平台加载动态库。

    Returns:
        动态库对象; 加载失败返回 None。
    """
    lib_path = os.path.join(MAA_LIB_DIR, LIB_NAME)
    if not os.path.exists(lib_path):
        print(f"[FAIL] 未找到动态库: {lib_path}")
        print("       可运行 python scripts/fetch_maa_resource.py 自动拉取(或设置 MAA_LIB_DIR)")
        return None

    if _IS_WINDOWS:
        # 依赖 DLL 搜索: 复刻 runtime.rs 的 SetDllDirectoryW
        os.environ["PATH"] = MAA_LIB_DIR + ";" + os.environ.get("PATH", "")
        try:
            ctypes.windll.kernel32.SetDllDirectoryW(MAA_LIB_DIR)
            print(f"[OK] SetDllDirectoryW -> {MAA_LIB_DIR}")
        except Exception as e:
            print(f"[WARN] SetDllDirectoryW 失败(依赖可能仍可解析): {e}")
        loader = ctypes.WinDLL
    else:
        if MAA_LIB_DIR not in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep):
            print(f"[WARN] LD_LIBRARY_PATH 未包含 {MAA_LIB_DIR}(容器 entrypoint 已自动设置)")
        loader = ctypes.CDLL

    try:
        core = loader(lib_path)
    except OSError as e:
        print(f"[FAIL] 无法加载 {LIB_NAME}: {e}")
        return None
    print(f"[OK] 已加载 {LIB_NAME} -> {lib_path}")
    return core


def main() -> int:
    """验证流程, 返回进程退出码。"""
    # 1) 加载动态库
    core = load_core()
    if core is None:
        return 1

    # 2) 声明接口签名(依据官方 AsstCaller.h)
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

    print(f"\n>>> 结论: ctypes 已能完整驱动 {LIB_NAME}(加载资源 + 创建实例) <<<")
    print(">>> 这证明可直接用官方 Asst* 接口跑明日方舟任务 <<<")
    return 0


if __name__ == "__main__":
    sys.exit(main())