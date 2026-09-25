"""
MaaCore 官方核心的 Python ctypes 封装(路线 1 实现)。

将 MaaCore.dll 的 C 接口(Asst*)封装为友好的 Python 类, 供一键长草协调器使用。

初始化顺序(必须遵循 maa-cli 相同流程):
    1. SetDllDirectoryW(lib 目录)  -> 依赖可解析
    2. AsstSetUserDir(存在的目录)
    3. AsstLoadResource(resource 的父目录)
    4. AsstCreate()
    5. AsstSetInstanceOption(TouchMode=adb)  -> 规避 minitouch 部署
    6. AsstConnect(adb_path=fake_adb, address, config)  -> 接云游戏
    7. AsstAppendTask / AsstStart / AsstRunning / AsstStop

参考: 官方 AsstCaller.h 与 maa-cli crates/maa-sys 源码。
"""

import ctypes
import os
import threading
from typing import Callable, Optional

# --- 默认路径(可由环境变量覆盖, 便于部署) ---
MAA_LIB_DIR = os.environ.get(
    "MAA_LIB_DIR",
    r"C:\Users\user\AppData\Roaming\loong\maa\data\lib",
)
MAA_DATA_DIR = os.environ.get(
    "MAA_DATA_DIR",
    r"C:\Users\user\AppData\Roaming\loong\maa\data",
)
USER_DIR = os.environ.get(
    "MAA_USER_DIR",
    os.path.join(MAA_DATA_DIR, "debug"),
)

# 假 adb 可执行文件(MaaCore 以它为 adb 调用)
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
FAKE_ADB_PATH = os.environ.get(
    "FAKE_ADB_PATH",
    os.path.join(_THIS_DIR, "fake_adb", "adb.bat"),
)

# 云游戏"设备地址"(与 fake_adb 的 FAKE_ADB_SERIAL 对应)
DEVICE_ADDRESS = os.environ.get("FAKE_ADB_SERIAL", "127.0.0.1:5555")

# --- 枚举(来自 AsstTypes.h) ---
TouchMode = 2      # InstanceOptionKey::TouchMode, 值 "adb"
KillAdbOnExit = 5  # InstanceOptionKey::KillAdbOnExit

# MAA 连接配置名(见 resource/config.json connection)
CONFIG_GENERAL = "General"

# AsstMsg 类型(来自 AsstMsg.h 常用值)
MSG_CONNECTION_INFO = 1            # 连接信息回调

AsstMsgId = ctypes.c_int32
AsstTaskId = ctypes.c_int32
AsstAsyncCallId = ctypes.c_int32
AsstHandle = ctypes.c_void_p
CALLBACK_FUNC = ctypes.CFUNCTYPE(None, AsstMsgId, ctypes.c_char_p, ctypes.c_void_p)


class MaaCoreError(RuntimeError):
    """MaaCore 调用异常。"""


class MaaCoreAssistant:
    """MaaCore 实例封装: 负责加载动态库、管理实例与任务。"""

    def __init__(
        self,
        lib_dir: str = MAA_LIB_DIR,
        data_dir: str = MAA_DATA_DIR,
        user_dir: Optional[str] = None,
        adb_path: Optional[str] = None,
        address: str = DEVICE_ADDRESS,
        resource_parent: Optional[str] = None,
    ) -> None:
        """初始化(仅加载库并记录路径, 实例创建由 connect() 完成)。

        Args:
            lib_dir: MaaCore.dll 所在目录
            data_dir: 数据目录(内含 resource/ 子目录)
            user_dir: 用户数据目录(必须存在)
            adb_path: 假 adb 可执行文件路径(adb.bat)
            address: 云游戏设备地址
            resource_parent: resource 目录的父目录(默认 data_dir)
        """
        self._lib_dir = lib_dir
        self._data_dir = data_dir
        self._user_dir = user_dir or os.path.join(data_dir, "debug")
        self._adb_path = adb_path or os.path.abspath(FAKE_ADB_PATH)
        self._address = address
        self._resource_parent = resource_parent or data_dir

        self._core: Optional[ctypes.WinDLL] = None
        self._handle: Optional[AsstHandle] = None
        self._callback_ref = None  # 保持回调引用防止被 GC
        self._lock = threading.Lock()  # MaaCore 非线程安全, 串行调用

        self._load_library()

    # ------------------------------------------------------------------ #
    # 动态库加载与签名声明                                                   #
    # ------------------------------------------------------------------ #
    def _load_library(self) -> None:
        """加载 MaaCore.dll, 声明 C 接口签名。"""
        dll_path = os.path.join(self._lib_dir, "MaaCore.dll")
        if not os.path.exists(dll_path):
            raise MaaCoreError(f"MaaCore.dll 不存在: {dll_path}")

        # 1) DLL 搜索路径(复刻 maa-cli runtime.rs)
        os.environ["PATH"] = self._lib_dir + ";" + os.environ.get("PATH", "")
        try:
            ctypes.windll.kernel32.SetDllDirectoryW(self._lib_dir)
        except Exception:
            pass

        # 2) 加载
        try:
            self._core = ctypes.WinDLL(dll_path)
        except OSError as e:
            raise MaaCoreError(f"MaaCore.dll 加载失败(检查依赖): {e}") from e

        core = self._core
        # 签名声明(依据 AsstCaller.h)
        core.AsstGetVersion.restype = ctypes.c_char_p
        core.AsstSetUserDir.argtypes = [ctypes.c_char_p]
        core.AsstSetUserDir.restype = ctypes.c_uint8
        core.AsstLoadResource.argtypes = [ctypes.c_char_p]
        core.AsstLoadResource.restype = ctypes.c_uint8
        core.AsstCreate.restype = AsstHandle
        core.AsstCreateEx.argtypes = [CALLBACK_FUNC, ctypes.c_void_p]
        core.AsstCreateEx.restype = AsstHandle
        core.AsstDestroy.argtypes = [AsstHandle]
        core.AsstSetInstanceOption.argtypes = [AsstHandle, ctypes.c_int32, ctypes.c_char_p]
        core.AsstSetInstanceOption.restype = ctypes.c_uint8
        core.AsstConnect.argtypes = [AsstHandle, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
        core.AsstConnect.restype = ctypes.c_uint8
        core.AsstAppendTask.argtypes = [AsstHandle, ctypes.c_char_p, ctypes.c_char_p]
        core.AsstAppendTask.restype = AsstTaskId
        core.AsstSetTaskParams.argtypes = [AsstHandle, AsstTaskId, ctypes.c_char_p]
        core.AsstSetTaskParams.restype = ctypes.c_uint8
        core.AsstStart.argtypes = [AsstHandle]
        core.AsstStart.restype = ctypes.c_uint8
        core.AsstStop.argtypes = [AsstHandle]
        core.AsstStop.restype = ctypes.c_uint8
        core.AsstRunning.argtypes = [AsstHandle]
        core.AsstRunning.restype = ctypes.c_uint8
        core.AsstConnected.argtypes = [AsstHandle]
        core.AsstConnected.restype = ctypes.c_uint8
        core.AsstBackToHome.argtypes = [AsstHandle]
        core.AsstBackToHome.restype = ctypes.c_uint8
        core.AsstGetTasksList.argtypes = [AsstHandle, ctypes.POINTER(AsstTaskId), ctypes.c_uint64]
        core.AsstGetTasksList.restype = ctypes.c_uint64

    @property
    def version(self) -> str:
        ver = self._core.AsstGetVersion()
        return ver.decode() if ver else ""

    # ------------------------------------------------------------------ #
    # 生命周期                                                              #
    # ------------------------------------------------------------------ #
    def initialize(self, callback: Optional[Callable[[int, str], None]] = None) -> None:
        """创建 MaaCore 实例, 加载资源。

        Args:
            callback: 可选回调 (msg_id, details_json)
        """
        with self._lock:
            if self._handle:
                return
            core = self._core

            # 1) 用户目录
            os.makedirs(self._user_dir, exist_ok=True)
            if not core.AsstSetUserDir(self._user_dir.encode("utf-8")):
                raise MaaCoreError("AsstSetUserDir 失败")

            # 2) 加载资源(传 resource 的父目录)
            if not core.AsstLoadResource(self._resource_parent.encode("utf-8")):
                raise MaaCoreError(f"AsstLoadResource 失败: {self._resource_parent}")

            # 3) 创建实例(带回调则用 Ex 版本)
            if callback:
                self._callback_ref = CALLBACK_FUNC(
                    lambda msg, details, arg: callback(int(msg), details.decode("utf-8", "replace") if details else "")
                )
                handle = core.AsstCreateEx(self._callback_ref, None)
            else:
                handle = core.AsstCreate()
            if not handle:
                raise MaaCoreError("AsstCreate 失败(资源未加载或目录异常)")
            self._handle = handle

            # 4) 强制 adb 触控, 规避 minitouch 部署
            self._set_option(TouchMode, "adb")

    def _set_option(self, key: int, value: str) -> None:
        """AsstSetInstanceOption 封装。"""
        if not self._core.AsstSetInstanceOption(self._handle, key, value.encode("utf-8")):
            raise MaaCoreError(f"AsstSetInstanceOption({key}={value}) 失败")

    def connect(self) -> bool:
        """连接设备(指向假 adb -> 云游戏)。

        Returns:
            True 连接成功
        Raises:
            MaaCoreError: 失败
        """
        if not self._handle:
            raise MaaCoreError("请先 initialize()")
        with self._lock:
            ok = self._core.AsstConnect(
                self._handle,
                self._adb_path.encode("utf-8"),
                self._address.encode("utf-8"),
                CONFIG_GENERAL.encode("utf-8"),
            )
            if not ok:
                raise MaaCoreError("AsstConnect 失败(假 adb 未响应)")
            return True

    def destroy(self) -> None:
        """销毁实例, 释放回调引用。"""
        with self._lock:
            if self._handle:
                self._core.AsstDestroy(self._handle)
                self._handle = None
            self._callback_ref = None

    def __enter__(self):
        self.initialize()
        return self

    def __exit__(self, *args):
        self.destroy()

    # ------------------------------------------------------------------ #
    # 任务管理                                                              #
    # ------------------------------------------------------------------ #
    def append_task(self, task_type: str, params_json: str = "") -> int:
        """追加任务, 返回 task_id。"""
        if not self._handle:
            raise MaaCoreError("请先 initialize()")
        with self._lock:
            tid = self._core.AsstAppendTask(self._handle, task_type.encode("utf-8"), params_json.encode("utf-8"))
            if tid < 0:
                raise MaaCoreError(f"AsstAppendTask({task_type}) 失败")
            return int(tid)

    def start(self) -> None:
        """启动任务执行(异步, 不阻塞)。"""
        if not self._handle:
            raise MaaCoreError("请先 initialize()")
        with self._lock:
            if not self._core.AsstStart(self._handle):
                raise MaaCoreError("AsstStart 失败")

    def stop(self) -> None:
        """停止任务(请求停止, 等待当前节点结束)。"""
        if not self._handle:
            return
        with self._lock:
            self._core.AsstStop(self._handle)

    def running(self) -> bool:
        """是否正在运行任务。"""
        if not self._handle:
            return False
        with self._lock:
            return bool(self._core.AsstRunning(self._handle))

    def connected(self) -> bool:
        """设备是否已连接。"""
        if not self._handle:
            return False
        with self._lock:
            return bool(self._core.AsstConnected(self._handle))

    def get_tasks_list(self) -> list:
        """获取已追加的任务 id 列表。"""
        if not self._handle:
            return []
        with self._lock:
            buf = (AsstTaskId * 64)()
            n = self._core.AsstGetTasksList(self._handle, buf, 64)
            return [int(buf[i]) for i in range(n)]