"""
MAA 每日任务协调器:在后台线程内用 MaaCore 官方核心(经假 adb 桥)执行「一键长草」。

设计要点:
- 任务执行跑在独立子线程(MaaCore 的任务为阻塞式接口, 直接占用调用线程),
  server.py 的 asyncio 事件循环只通过线程安全的共享状态读取进度。
- 设备链路: MaaCore --adb 命令--> fake_adb 桥 --HTTP--> server.py 本进程接口,
  复用进程内已建立的云游戏连接(app_state), 不建立第二条连接。
- 日志: 每次执行自动落盘 logs/maa_YYYYMMDD_HHMMSS.log(含 MaaCore 回调、
  假 adb 上报的命令与任务进度), 便于远端调试; 内存环形列表供 WebUI 轮询。
- 任务参数: 严格对齐 https://docs.maa.plus/zh-cn/protocol/integration.html
  (AsstAppendTask 的各任务 params 字段)。
"""

import datetime
import logging
import os
import threading
from typing import Dict, List, Optional

logger = logging.getLogger("maa_coordinator")

# 日志落盘目录(环境变量可覆盖, 便于 Docker 挂载)
MAA_LOG_DIR = os.environ.get(
    "MAA_LOG_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))


# 每日任务 key -> MaaCore 任务类型(AsstAppendTask 的第一个参数)
# 注意: "库存保持"(DepotMaintain)在官方集成文档中是 UI 层功能, MaaCore 协议层
#       没有名为 Hog 的任务类型; 此处用官方支持的 Depot(仓库识别)近似承接,
#       保证 AppendTask 不会因类型非法而失败。
# "annihilation" 为每周剿灭: 复用 Fight 任务并指定 stage=Annihilation,
#       MaaCore 会在合成玉达本周上限后自动停止代理速刷。
DEFAULT_TASK_MAP = {
    "awaken": "StartUp",          # 开始唤醒
    "recruit": "Recruit",         # 自动公招
    "infrast": "Infrast",         # 基建换班
    "combat": "Fight",            # 理智作战
    "annihilation": "Fight",      # 每周剿灭(代理速刷)
    "inventory": "Depot",         # 库存保持(协议层用仓库识别替代)
    "credit": "Mall",             # 信用收支
    "reward": "Award",            # 领取奖励
}

# 优先顺序: 因果依赖前置(先唤醒 → 剿灭 → 理智作战 ...)
# 剿灭放在理智作战之前, 保证先清掉本周剿灭再刷普通关;
# 当前剿灭合成玉已满时 MAA 会自动跳过, 不影响后续任务。
TASK_EXEC_ORDER = [
    "awaken", "reward", "recruit", "infrast", "annihilation", "combat",
    "credit", "inventory",
]


def _resolve_medicine(fight_opt: dict) -> int:
    """从作战设置解析理智药数量, 返回传入 Fight 的 medicine 值。

    优先使用新字段 medicine_mode:
      "off"  -> 0(不用药)
      "auto" -> 999(用完当前可用理智药, MAA 会在没有药时停用)
      "num"  -> medicine 数量
    兼容旧设置: 无 medicine_mode 字段时按 medicine_enabled 推导。
    """
    mode = fight_opt.get("medicine_mode")
    if mode is None:
        # 旧版设置迁移
        mode = "num" if bool(fight_opt.get("medicine_enabled", False)) else "off"
    if mode == "auto":
        return 999
    if mode == "off":
        return 0
    try:
        return int(fight_opt.get("medicine", 0) or 0)
    except (TypeError, ValueError):
        return 0


def build_task_params(task_key: str, task_type: str, options: Optional[dict] = None) -> str:
    """根据官方集成文档(AsstAppendTask)构造各任务默认 params(JSON 字符串)。

    Args:
        task_key: 前端任务 key(awaken/combat/annihilation/...)
        task_type: MaaCore 任务类型(StartUp/Fight/...)
        options: 设置面板透传的作战选项, 取 {"fight": {...}, "annihilation": {...}}
                 fight: stage/times/series/medicine_enabled/medicine
                 annihilation: enabled/times

    Returns:
        合法 JSON 字符串, 传给 AsstAppendTask。
    """
    options = options or {}
    fight_opt = options.get("fight") or {}
    anni_opt = options.get("annihilation") or {}

    if task_type == "StartUp":
        # 云游戏已在游戏内, 必须禁用"启动客户端"(am start 会空转/切桌面)
        params = {
            "start_game_enabled": False,
            "client_type": "Official",
        }
    elif task_type == "Fight":
        series = int(fight_opt.get("series", 0))
        if task_key == "annihilation":
            # 每周剿灭: 指定当期剿灭关卡。auto=True 时给大次数(999), 让 MAA
            # 连刷直到本周合成玉达上限自动停止; 合成玉已满时 MAA 直接跳过。
            anni_auto = bool(anni_opt.get("auto", True))
            times = 999 if anni_auto else int(anni_opt.get("times", 4) or 1)
            params = {
                "stage": "Annihilation",
                "times": times,
                "series": series,               # AUTO 或指定代理倍率
                "medicine": 0,                  # 剿灭不吃理智药
                "stone": 0,
                "client_type": "Official",
            }
        else:
            # 理智作战: stage 留空 -> 识别当前/上次关卡
            medicine = _resolve_medicine(fight_opt)
            params = {
                "stage": str(fight_opt.get("stage", "") or ""),
                "medicine": medicine,
                "stone": 0,                     # 不碎石
                "times": int(fight_opt.get("times", 5) or 1),
                "series": series,               # 自动切换当前可用的最大代理倍率
                "client_type": "Official",      # 崩溃后自动重连回游戏继续刷
            }
    elif task_type == "Recruit":
        params = {
            "refresh": False,               # 不刷新三星/无用 Tag
            "select": [5, 4],               # 会去点击的 Tag 等级
            "confirm": [4, 3],              # 会去确认的 Tag 等级
            "times": 4,                     # 本次招募次数
            "set_time": True,               # 设置 9 小时时限最大化高星概率
            "expedite": False,              # 不使用加急许可
            "extra_tags_mode": 0,           # 默认选择策略
            "server": "CN",
        }
    elif task_type == "Infrast":
        params = {
            "mode": 0,                      # 默认自动排班
            "facility": [                   # 参与换班的设施
                "Mfg", "Trade", "Reception", "Control",
                "Office", "Dorm", "Power", "Processing", "Training",
            ],
            "drones": "Money",              # 无人机用于搓赤金
            "threshold": 0.3,               # 工作心情阈值
            "replenish": True,              # 贸易站源石碎片自动补货
            "dorm_trust_enabled": True,     # 宿舍空位填入信赖未满干员
            "reception_message_board": True,  # 领取会客室信息板信用
            "reception_clue_exchange": True,  # 线索交流
            "reception_send_clue": True,      # 赠送线索
        }
    elif task_type == "Mall":
        params = {
            "shopping": True,
            "visit_friends": True,          # 访问好友基建获得信用
            "buy_first": [],                # 优先购买列表(留空不指定)
            "blacklist": [],                # 黑名单(留空不指定)
            "force_shopping_if_credit_full": True,  # 信用溢出时无视黑名单
        }
    elif task_type == "Award":
        # 领取奖励细分项全部来自设置(options.award), 未提供时默认全开
        award_opt = options.get("award") or {
            "award": True, "mail": True, "recruit": True,
            "orundum": True, "mining": True, "specialaccess": True,
        }
        params = {
            "award": bool(award_opt.get("award", True)),            # 每日/每周任务奖励
            "mail": bool(award_opt.get("mail", True)),              # 所有邮件奖励
            "recruit": bool(award_opt.get("recruit", True)),        # 限定池每日免费单抽
            "orundum": bool(award_opt.get("orundum", False)),       # 幸运墙合成玉
            "mining": bool(award_opt.get("mining", False)),         # 限时开采许可合成玉
            "specialaccess": bool(award_opt.get("specialaccess", False)),  # 周年赠送月卡
        }
    elif task_type == "Depot":
        params = {}            # 仓库识别无必填参数
    else:
        params = {}
    return __import__("json").dumps(params, ensure_ascii=False)


class TaskStatus:
    """线程安全的任务状态快照, 并负责把运行日志自动落盘(便于远程调试)。"""

    IDLE = "idle"
    RUNNING = "running"
    ERROR = "error"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = self.IDLE
        self._current = ""
        self._finished: List[str] = []
        self._total: List[str] = []
        self._message = ""
        self._error = ""   # 最近一次错误, 展示用
        self._log: List[str] = []        # 一键长草的运行日志(环形列表, 供 WebUI 轮询)
        self._max_log_lines = 100        # 日志行数上限, 防止无限增长
        self._log_path: Optional[str] = None   # 本次运行落盘日志文件路径
        self._log_file = None            # 打开的日志文件句柄(整个运行周期保持)

    # ---- 写(协调器线程) ----
    def start_run(self, tasks: List[str]) -> None:
        with self._lock:
            self._state = self.RUNNING
            self._current = ""
            self._finished = []
            self._total = list(tasks)
            self._error = ""
            self._log = ["开始执行, 任务序列: " + ", ".join(tasks)]
            self._open_log_file()

    def _open_log_file(self) -> None:
        """按时间戳创建本次运行的日志文件(失败不阻断执行, 仅告警)。"""
        import json as _json
        # 关闭上一次运行遗留的文件句柄
        self._close_log_file()
        try:
            os.makedirs(MAA_LOG_DIR, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(MAA_LOG_DIR, f"maa_{stamp}.log")
            self._log_file = open(path, "w", encoding="utf-8")
            self._log_path = path
            header = _json.dumps({
                "run_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "tasks": self._total,
            }, ensure_ascii=False)
            self._write_file(f"# {header}")
        except OSError as e:
            logger.warning("创建 MAA 日志文件失败(继续执行): %s", e)
            self._log_path = None

    def _close_log_file(self) -> None:
        """关闭日志文件句柄(finally 语义, 幂等)。"""
        f = self._log_file
        self._log_file = None
        if f:
            try:
                f.close()
            except OSError:
                pass

    def _write_file(self, line: str) -> None:
        """把一行日志同步追加到落盘文件(锁内调用, 免二次加锁)。"""
        f = self._log_file
        if f is None:
            return
        try:
            f.write(line + "\n")
            f.flush()
        except (OSError, ValueError):
            # 文件关闭/写满后静默降级为内存日志
            self._log_file = None

    def log(self, message: str) -> None:
        """追加一条运行日志(带时间戳), 同步写落盘文件。"""
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        with self._lock:
            line = f"[{ts}] {message}"
            self._log.append(line)
            if len(self._log) > self._max_log_lines:
                self._log = self._log[-self._max_log_lines:]
            self._write_file(line)

    def log_adb(self, command: str, ok: bool, note: str = "") -> None:
        """记录一条由假 adb 上报的执行命令(带采样比例由 server 端节流)。"""
        status = "OK " if ok else "ERR"
        suffix = f" ({note})" if note else ""
        self.log(f"[adb] {status} {command}{suffix}")

    def set_current(self, task: str) -> None:
        with self._lock:
            self._current = task

    def mark_done(self, task: str, ok: bool, msg: str = "") -> None:
        with self._lock:
            self._current = ""
            if task not in self._finished:
                self._finished.append(task)
            if not ok:
                self._error = f"[{task}] {msg}"
                self._log.append(f"任务 {task} 失败: {msg}")
                self._write_file(f"任务 {task} 失败: {msg}")

    def set_final(self, state: str, message: str = "") -> None:
        with self._lock:
            self._state = state
            self._current = ""
            self._message = message
            self._log.append(message)
            self._write_file(message)
            # 运行结束: 关闭文件句柄, 保证落盘完整
            self._close_log_file()

    # ---- 读(任意线程) ----
    def snapshot(self) -> Dict:
        with self._lock:
            return {
                "state": self._state,
                "current": self._current,
                "finished": list(self._finished),
                "total": list(self._total),
                "message": self._message,
                "error": self._error,
                "log": list(self._log),
                "log_path": self._log_path,
            }

    @property
    def state(self) -> str:
        with self._lock:
            return self._state


class MaaCoordinator:
    """管理在后台线程中执行的 MAA 每日任务(使用 MaaCore 官方核心 + 假 adb 桥)。"""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._options: Dict = {}        # 本次运行的作战选项(fight/annihilation)
        self._signin: bool = False      # 运行前先执行网易云游戏签到
        self._token: Optional[str] = None  # 签到时使用的云游戏登录 token
        self.status = TaskStatus()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, enabled_tasks: List[str], options: Optional[dict] = None,
              signin: bool = False, token: Optional[str] = None) -> bool:
        """启动协调线程执行指定任务(按依赖顺序过滤)。

        Args:
            enabled_tasks: 前端勾选的任务 key 列表(可含 annihilation 剿灭)
            options: 作战选项 {"fight": {...}, "annihilation": {...}}
            signin: 是否在任务前先执行网易云游戏签到
            token: 签到时使用的登录 token
        """
        if self.running:
            logger.warning("MAA coordinator already running, ignored")
            return False
        if not enabled_tasks:
            self.status.set_final("idle", "未选择任何任务")
            return False

        # 按执行顺序, 仅保留已启用任务
        tasks = [t for t in TASK_EXEC_ORDER if t in enabled_tasks]
        if not tasks:
            self.status.set_final("idle", "未选择任何任务")
            return False

        self._options = options or {}
        self._signin = bool(signin)
        self._token = token
        self._stop_flag.clear()
        self._thread = threading.Thread(
            target=self._run, args=(tasks,), name="maa-coordinator", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> bool:
        """请求停止: 设置停止标志, 等待当前任务退出。"""
        if not self.running:
            return False
        self._stop_flag.set()
        # 仅等待有限时间, 避免阻塞 HTTP 请求过久
        self._thread.join(timeout=5)
        return True

    def _run(self, tasks: List[str]) -> None:
        """线程内执行 MAA 任务(阻塞式), 使用 MaaCore 官方核心 + 假 adb 桥。

        进度与错误通过 self.status 上报给 WebUI 轮询; 全部日志自动落盘到
        logs/maa_*.log(由 TaskStatus.start_run 打开)。
        """
        self.status.start_run(tasks)
        try:
            logger.info("启动 MAA 一键长草, 任务序列: %s", ", ".join(tasks))
            self.status.log("正在初始化 MaaCore 引擎...")

            # 0) 可选前置: 网易云游戏签到(平台奖励; 失败不阻断后续任务)
            if self._signin:
                self._do_signin()

            # 惰性导入: 避免 server 启动时因缺 DLL/环境失败
            from maa_core_wrapper import MaaCoreAssistant

            assist = MaaCoreAssistant()
            self.status.log(f"MaaCore 版本: {assist.version}")

            # 1) 初始化(设置用户目录 + 加载官方资源)
            self.status.log("加载 MAA 官方资源...")
            assist.initialize(callback=self._on_maa_msg)
            self.status.log("MAA 资源加载成功")

            # 2) 通过假 adb 连接云游戏
            self.status.log("连接云游戏(假 adb 桥)...")
            assist.connect()
            self.status.log("云游戏已连接")

            # 3) 按顺序追加任务(参数严格对齐官方集成文档)
            for task in tasks:
                task_type = DEFAULT_TASK_MAP.get(task, task)
                self.status.set_current(task_type)
                params = build_task_params(task, task_type, self._options)
                logger.info("[一键长草] 追加任务 %s(%s) params=%s", task_type, task, params)
                self.status.log(f"追加任务: {task_type} params={params}")

                try:
                    tid = assist.append_task(task_type, params)
                except Exception as e:
                    # 单个任务参数/类型不被接受时不整体中断, 记录后继续后续任务
                    logger.error("[一键长草] 追加任务 %s 失败(跳过): %s", task_type, e)
                    self.status.log(f"⚠ 追加任务 {task_type} 失败, 已跳过: {e}")
                    continue
                self.status.log(f"追加任务成功: {task_type} (task_id={tid})")

            # 4) 启动全部任务(异步, 统一排队执行)
            self.status.log("开始执行...")
            assist.start()

            # 5) 等待完成, 支持中途停止
            import time
            self.status.log("任务执行中...")
            while assist.running():
                if self._stop_flag.is_set():
                    logger.info("收到停止请求, 停止 MaaCore 任务")
                    self.status.log("收到停止请求, 正在停止...")
                    assist.stop()
                    break
                time.sleep(0.5)

            stopped = self._stop_flag.is_set()
            self.status.set_final(
                "idle",
                "全部任务执行完成" if not stopped else "已手动停止",
            )
            for t in tasks:
                self.status.mark_done(t, True, "")
            logger.info("一键长草结束: %s", self.status.snapshot()["message"])
        except Exception as e:
            logger.exception("一键长草执行异常")
            self.status.mark_done("", False, str(e))
            self.status.log(f"执行异常: {e}")
            self.status.set_final("error", str(e))

    def _do_signin(self) -> None:
        """任务序列前置: 执行网易云游戏签到(失败仅记录, 不阻断一键长草)。"""
        self.status.log("执行网易云游戏签到...")
        if not self._token:
            self.status.log("⚠ 签到跳过: 未获取到登录 token(请先启动云游戏)")
            return
        try:
            # 惰性导入 + 同步调用(协调器在线程内, 不阻塞事件循环)
            from sdk.signin import netease_signin
            result = netease_signin(self._token)
        except Exception as e:
            self.status.log(f"⚠ 签到异常(忽略): {e}")
            logger.warning("签到异常(忽略): %s", e)
            return
        if result.get("ok"):
            self.status.log(f"✅ 云游戏签到成功: {result.get('endpoint')}")
        else:
            self.status.log(f"⚠ 云游戏签到失败: {result.get('message')}")

    def _on_maa_msg(self, msg_id: int, details_json: str) -> None:
        """MaaCore 异步消息回调: 提取任务进度与错误写入状态。

        msg 取值对齐官方回调协议(callback-schema):
            TaskChainError=10000  TaskChainStart=10001  TaskChainCompleted=10002
            TaskChainExtraInfo=10003  TaskChainStopped=10004
            SubTaskError=20000  AllTasksCompleted=3  ConnectionInfo=2
        """
        import json
        try:
            details = json.loads(details_json) if details_json else {}
        except Exception:
            details = {}
        taskchain = details.get("taskchain", "")
        what = details.get("what", "")
        why = details.get("why", "")

        # ---- 全局信息 ----
        if msg_id == 2 and what:  # ConnectionInfo(连接阶段信息/错误)
            if what in ("ConnectFailed", "ResolutionError", "ScreencapFailed",
                        "TouchModeNotAvailable", "Disconnect"):
                logger.error("[MaaCore] 连接问题: %s %s", what, why)
                self.status.log(f"[MaaCore] 连接问题: {what} {why}")
            elif what in ("Connected", "Reconnected", "UuidGot"):
                logger.info("[MaaCore] 连接状态: %s", what)
                self.status.log(f"[MaaCore] 连接状态: {what}")
        elif msg_id == 3:  # AllTasksCompleted(全部任务完成)
            logger.info("[MaaCore] 全部任务完成: %s", details.get("finished_tasks", []))
            self.status.log("[MaaCore] 全部任务完成")
        # ---- 任务链信息 ----
        elif msg_id == 10000:  # TaskChainError
            logger.error("[MaaCore] 任务链出错: %s -> %s", taskchain, why)
            self.status.log(f"[MaaCore] ❌ 任务链出错: {taskchain} ({why})")
        elif msg_id == 10001:  # TaskChainStart
            logger.info("[MaaCore] 任务开始: %s", taskchain)
            self.status.log(f"[MaaCore] ▶ 任务开始: {taskchain}")
        elif msg_id == 10002:  # TaskChainCompleted
            logger.info("[MaaCore] 任务完成: %s", taskchain)
            self.status.log(f"[MaaCore] ✅ 任务完成: {taskchain}")
        elif msg_id == 10004:  # TaskChainStopped
            logger.info("[MaaCore] 任务被停止: %s", taskchain)
            self.status.log(f"[MaaCore] 任务被停止: {taskchain}")
        # ---- 子任务错误(逐条记录便于定位) ----
        elif msg_id == 20000:  # SubTaskError
            logger.error("[MaaCore] 子任务出错: %s -> %s", taskchain, why)
            self.status.log(f"[MaaCore] ⚠ 子任务出错: {taskchain} ({why})")

    def snapshot(self) -> Dict:
        snap = self.status.snapshot()
        snap["running"] = self.running
        return snap