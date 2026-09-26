"""
MAA 每日任务协调器:在后台线程内用 MaaCore 官方核心(经假 adb 桥)执行「一键长草」。

设计要点:
- 任务执行跑在独立子线程(MaaCore 的任务为阻塞式接口, 直接占用调用线程),
  server.py 的 asyncio 事件循环只通过线程安全的共享状态读取进度。
- 设备链路: MaaCore --adb 命令--> fake_adb 桥 --HTTP--> server.py 本进程接口,
  复用进程内已建立的云游戏连接(app_state), 不建立第二条连接。
- 日志: 每次执行自动落盘 logs/maa_YYYYMMDD_HHMMSS.log(含 MaaCore 回调、
  假 adb 上报的命令与任务进度), 便于远端调试; 内存环形列表供 WebUI 轮询。
- 任务逐个执行(追加 → 启动 → 等待结束): 单个任务内的子任务出错达到
  MAX_SUB_TASK_ERRORS 次后跳过该任务并继续后续任务, 避免某个任务反复出错
  卡死整轮(逐个执行是前提: AsstStop 会停止队列中的所有任务)。
- 任务参数: 严格对齐 https://docs.maa.plus/zh-cn/protocol/integration.html
  (AsstAppendTask 的各任务 params 字段)。
"""

import datetime
import json
import logging
import os
import threading
import time
from typing import Dict, List, NamedTuple, Optional

from maa_settings import (
    INFRAST_DRONES,
    INFRAST_FACILITIES,
    INFRAST_MODE_CUSTOM,
)

logger = logging.getLogger("maa_coordinator")

# 日志落盘目录(环境变量可覆盖, 便于 Docker 挂载)
MAA_LOG_DIR = os.environ.get(
    "MAA_LOG_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))

# 理智作战「次数」: 默认值 与 auto 语义取值(传大次数, 由 MAA 在理智耗尽时自动结束)
DEFAULT_FIGHT_TIMES = 5
FIGHT_TIMES_AUTO = 999

# 单个任务内子任务出错次数上限: 达到后跳过该任务并继续执行后续任务(避免整轮卡死)
MAX_SUB_TASK_ERRORS = 5

# ---- 库存保持(对齐 MAA GUI「仓库维持」; 物品 ID 与关卡已对照官方资源文件核对) ----
# 每个保持项 = 中文名 + 默认目标数量 + 关卡列表(每关列出该关可掉落的目标物品)。
# 芯片本每关固定掉落两种职业的芯片, 其余为单一物品资源关; 规划时逐关卡计算缺口,
# 同一关卡取缺口最大的目标物品作为理智作战的掉落停止目标(Fight.drops)。
# open_days: 关卡开放日(ISO 星期: 1=周一 ... 7=周日), 数据来源为 PRTS 关卡一览
#   「资源收集」(2026-09 核对): 资源关按星期轮换, 非开放日进不去(导航会失败),
#   规划时直接跳过。
INVENTORY_PRESETS = {
    "chip_low": {
        "label": "低级芯片(全职业)",
        "default_count": 20,
        "stages": [
            # 固若金汤: 重装/医疗(周一四五日)
            {"stage": "PR-A-1", "open_days": [1, 4, 5, 7],
             "items": [("3231", "重装芯片"), ("3261", "医疗芯片")]},
            # 摧枯拉朽: 狙击/术师(周一二五六)
            {"stage": "PR-B-1", "open_days": [1, 2, 5, 6],
             "items": [("3241", "狙击芯片"), ("3251", "术师芯片")]},
            # 势不可挡: 先锋/辅助(周三四六日)
            {"stage": "PR-C-1", "open_days": [3, 4, 6, 7],
             "items": [("3211", "先锋芯片"), ("3271", "辅助芯片")]},
            # 身先士卒: 近卫/特种(周二三六日)
            {"stage": "PR-D-1", "open_days": [2, 3, 6, 7],
             "items": [("3221", "近卫芯片"), ("3281", "特种芯片")]},
        ],
    },
    "chip_high": {
        "label": "高级芯片组(全职业)",
        "default_count": 20,
        "stages": [
            {"stage": "PR-A-2", "open_days": [1, 4, 5, 7],
             "items": [("3232", "重装芯片组"), ("3262", "医疗芯片组")]},
            {"stage": "PR-B-2", "open_days": [1, 2, 5, 6],
             "items": [("3242", "狙击芯片组"), ("3252", "术师芯片组")]},
            {"stage": "PR-C-2", "open_days": [3, 4, 6, 7],
             "items": [("3212", "先锋芯片组"), ("3272", "辅助芯片组")]},
            {"stage": "PR-D-2", "open_days": [2, 3, 6, 7],
             "items": [("3222", "近卫芯片组"), ("3282", "特种芯片组")]},
        ],
    },
    "certificate": {
        "label": "采购凭证(红票)",
        "default_count": 20,
        # 粉碎防御: 周一四六日
        "stages": [{"stage": "AP-5", "open_days": [1, 4, 6, 7],
                    "items": [("4006", "采购凭证")]}],
    },
    "skill_summary": {
        "label": "技巧概要·卷3",
        "default_count": 200,
        # 空中威胁: 周二三五日
        "stages": [{"stage": "CA-5", "open_days": [2, 3, 5, 7],
                    "items": [("3303", "技巧概要·卷3")]}],
    },
}

# 库存保持项的规划顺序(与 WebUI 勾选区从上到下的顺序一致)
INVENTORY_ORDER = ["chip_low", "chip_high", "certificate", "skill_summary"]

# 星期中文名(ISO: 1=周一 ... 7=周日), 用于"下次开放"日志提示
_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 仓库识别等待上限(秒): 仓库物品多需滑动多屏识别, 超时后跳过库存保持并继续后续任务
DEPOT_TIMEOUT = 360.0


# 每日任务 key -> MaaCore 任务类型(AsstAppendTask 的第一个参数)
# 注意: "库存保持"是 UI 层组合功能(对齐 MAA GUI「仓库维持」): 先执行 Depot
#       (仓库识别)获取库存快照, 再按缺口追加 Fight 任务补货, 因此该 key 不走
#       常规任务路径(_run_inventory 特判); 此处的 "Depot" 仅作日志展示, 标明
#       该任务的第一步是仓库识别。
# "annihilation" 为每周剿灭: 复用 Fight 任务并指定 stage=Annihilation,
#       MaaCore 会在合成玉达本周上限后自动停止代理速刷。
DEFAULT_TASK_MAP = {
    "awaken": "StartUp",          # 开始唤醒
    "recruit": "Recruit",         # 自动公招
    "infrast": "Infrast",         # 基建换班
    "combat": "Fight",            # 理智作战
    "annihilation": "Fight",      # 每周剿灭(代理速刷)
    "inventory": "Depot",         # 库存保持(组合任务: 仓库识别 + 理智作战补缺口)
    "credit": "Mall",             # 信用收支
    "reward": "Award",            # 领取奖励
}

# 优先顺序: 因果依赖前置(先唤醒 → 领取奖励 → 剿灭 → 库存保持 → 理智作战 ...)
# 剿灭放在理智作战之前, 保证先清掉本周剿灭再刷普通关;
# 库存保持放在剿灭之后、理智作战之前(优先级: 每周剿灭 > 库存保持 > 理智作战配置):
# 勾选后先按仓库缺口规划并刷关, 剩余理智再由理智作战按用户配置的关卡消耗。
TASK_EXEC_ORDER = [
    "awaken", "reward", "recruit", "infrast", "annihilation", "inventory", "combat",
    "credit",
]

# 任务 key -> 中文展示名(日志/状态栏使用)
# 说明: 理智作战与每周剿灭在 MaaCore 侧都是 Fight 类型, 只看类型名无法区分
#       来源(容易误以为"没勾理智作战却在跑 Fight"), 因此日志统一展示
#       "中文名(MaaCore 类型)" 形式。
TASK_DISPLAY_NAMES = {
    "awaken": "开始唤醒",
    "recruit": "自动公招",
    "infrast": "基建换班",
    "combat": "理智作战",
    "annihilation": "每周剿灭",
    "inventory": "库存保持",
    "credit": "信用收支",
    "reward": "领取奖励",
}


def task_display_name(task_key: str, task_type: str = "") -> str:
    """返回任务的可读展示名, 形如 "每周剿灭(Fight)"。

    Args:
        task_key: 前端任务 key(如 annihilation)
        task_type: MaaCore 任务类型(如 Fight), 为空时只返回中文名

    Returns:
        中文名(MaaCore 类型); 未收录的 key 回退为 key 或类型名。
    """
    name = TASK_DISPLAY_NAMES.get(task_key)
    if name and task_type:
        return f"{name}({task_type})"
    return name or task_type or task_key


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


def _resolve_fight_times(raw) -> int:
    """解析理智作战「次数」, 返回传给 Fight 的 times 值。

    - "auto"(不分大小写): 返回 FIGHT_TIMES_AUTO, 由 MAA 在理智耗尽时自动停止
      (等价"刷完当前理智为止"), 不需要前端计算次数;
    - 数字: 钳制到 [1, FIGHT_TIMES_AUTO];
    - 其他非法值: 回退默认 5 次, 避免把脏数据传给 MaaCore。
    """
    if isinstance(raw, str) and raw.strip().lower() == "auto":
        return FIGHT_TIMES_AUTO
    try:
        times = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_FIGHT_TIMES
    return max(1, min(FIGHT_TIMES_AUTO, times))


def _resolve_infrast_mode(raw) -> int:
    """把前端传来的换班模式规整为合法值(非 0/10000/20000 一律回退常规模式)。

    MAA 官方取值(Infrast.mode):
      0     - Default  常规模式, 自动计算效率较高的干员组合
      10000 - Custom   自定义基建模式, 读取 filename/plan_index 指定的排班方案
      20000 - Rotation 队列轮换, 跳过控制中枢/发电站/宿舍/办公室
    """
    try:
        mode = int(raw)
    except (TypeError, ValueError):
        return 0
    return mode if mode in (0, INFRAST_MODE_CUSTOM, 20000) else 0


def _resolve_infrast_facility(raw) -> List[str]:
    """规整设施列表: 过滤非法名并保序去重; 为空时回退默认全选。

    常规模式下该数组仅表示"启用集合"(顺序不参与调度); 自定义/轮换模式下
    按数组顺序执行换班, 因此这里必须保序去重而不是排序。
    """
    if not isinstance(raw, (list, tuple)):
        return list(INFRAST_FACILITIES)
    seen: List[str] = []
    for item in raw:
        name = str(item)
        if name in INFRAST_FACILITIES and name not in seen:
            seen.append(name)
    return seen or list(INFRAST_FACILITIES)


def _resolve_infrast_drones(raw) -> str:
    """规整无人机用途, 非法值回退「贸易站-龙门币」。"""
    value = str(raw or "")
    return value if value in INFRAST_DRONES else "Money"


def _resolve_threshold(raw, default: float = 0.3) -> float:
    """规整心情阈值到 [0, 1.0], 非法值回退默认。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, value))


def build_infrast_params(infra_opt: Optional[dict] = None) -> Dict:
    """构造 Infrast(基建换班)参数, 字段对齐官方集成文档。

    Args:
        infra_opt: 设置面板的基建设置, 含
                   mode/facility/drones/threshold/replenish/dorm_*/reception_*/
                   filename/plan_index

    Returns:
        可直接 json.dumps 的参数字典。

    说明:
    - mode=10000 时才补充 filename/plan_index(官方标注为 required);
      未填配置路径时回退常规模式, 避免任务因参数非法而整体失败。
    - mode=20000(队列轮换)下 drones/threshold 官方标记为无效, 但仍按
      协议原样发送, 便于日志回溯用户当前设置。
    """
    infra = infra_opt or {}
    mode = _resolve_infrast_mode(infra.get("mode"))

    filename = str(infra.get("filename", "") or "").strip()
    if mode == INFRAST_MODE_CUSTOM and not filename:
        logger.warning("自定义基建模式缺少排班配置路径, 回退常规模式")
        mode = 0

    params: Dict = {
        "enable": True,
        "mode": mode,
        "facility": _resolve_infrast_facility(infra.get("facility")),
        "drones": _resolve_infrast_drones(infra.get("drones")),
        "threshold": _resolve_threshold(infra.get("threshold")),
        "replenish": bool(infra.get("replenish", True)),
        "dorm_notstationed_enabled": bool(infra.get("dorm_notstationed_enabled", False)),
        "dorm_trust_enabled": bool(infra.get("dorm_trust_enabled", True)),
        "reception_message_board": bool(infra.get("reception_message_board", True)),
        "reception_clue_exchange": bool(infra.get("reception_clue_exchange", True)),
        "reception_send_clue": bool(infra.get("reception_send_clue", True)),
    }

    if mode == INFRAST_MODE_CUSTOM:
        params["filename"] = filename
        try:
            params["plan_index"] = int(infra.get("plan_index", 0) or 0)
        except (TypeError, ValueError):
            params["plan_index"] = 0

    return params


def build_task_params(task_key: str, task_type: str, options: Optional[dict] = None) -> str:
    """根据官方集成文档(AsstAppendTask)构造各任务默认 params(JSON 字符串)。

    Args:
        task_key: 前端任务 key(awaken/combat/annihilation/...)
        task_type: MaaCore 任务类型(StartUp/Fight/...)
        options: 设置面板透传的运行选项, 取
                 {"fight": {...}, "annihilation": {...},
                  "infrast": {...}, "award": {...}, "inventory": {...}}
                 其中 inventory 由 _run_inventory 使用(本函数不读取)。

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
        if task_key == "annihilation":
            # 每周剿灭: 指定当期剿灭关卡。auto=True 时给大次数(999), 让 MAA
            # 连刷直到本周合成玉达上限自动停止; 合成玉已满时 MAA 直接跳过。
            anni_auto = bool(anni_opt.get("auto", True))
            times = 999 if anni_auto else int(anni_opt.get("times", 4) or 1)
            params = {
                "stage": "Annihilation",
                "times": times,
                "series": 0,                    # 代理倍率恒为 AUTO(自动切最大可用倍率)
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
                # 次数: 正整数 或 "auto"(刷完当前理智自动停, 由 MAA 自行结束)
                "times": _resolve_fight_times(fight_opt.get("times")),
                "series": 0,                    # 代理倍率恒为 AUTO
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
        # 基建换班: 模式(常规/自定义/队列轮换) + 设施 + 无人机 + 阈值等,
        # 全部来自 WebUI 基建设置(见 maa_settings.DEFAULT_SETTINGS["infrast"])
        params = build_infrast_params(options.get("infrast"))
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
    return json.dumps(params, ensure_ascii=False)


# ---------------------------------------------------------------------- #
# 库存保持: 缺口计算与规划(纯函数, 便于单元测试)                            #
# ---------------------------------------------------------------------- #
def _resolve_inventory_count(raw, default: int) -> int:
    """规整保持项目标库存数量: 正整数, 非法值回退默认(上限 99999 防脏数据)。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(99999, value))


def _today_weekday() -> int:
    """返回今天的 ISO 星期(1=周一 ... 7=周日), 供资源关卡开放日判断。

    单独抽成函数便于单元测试注入固定星期(真实运行取系统日期)。
    """
    return datetime.date.today().isoweekday()


def next_open_weekday(open_days: List[int], weekday: int) -> Optional[int]:
    """返回 open_days 中 weekday 之后最近的一个开放日(ISO 星期, 不含当天)。

    Args:
        open_days: 关卡开放日列表(1=周一 ... 7=周日)
        weekday: 当前星期

    Returns:
        下一个开放日的 ISO 星期; 列表为空/无可用项时返回 None。
    """
    for offset in range(1, 8):
        day = (weekday - 1 + offset) % 7 + 1
        if day in open_days:
            return day
    return None


def inventory_gaps(depot: Optional[dict], inventory_opt: Optional[dict],
                   weekday: Optional[int] = None,
                   include_closed: bool = False) -> List[Dict]:
    """按保持项逐关卡计算库存缺口, 返回需要刷取的计划列表(顺序即优先级)。

    Args:
        depot: 仓库识别结果 {物品ID: 数量}(由 DepotInfo 回调解析所得)
        inventory_opt: 库存保持设置 {保持项key: {"enabled": bool, "count": int}}
        weekday: 星期几(ISO: 1=周一 ... 7=周日), 默认取当天。
                 资源关卡按开放日轮换(如芯片本), 非开放日不列入计划 ——
                 否则 MAA 导航到未开放关卡会一直滑动找不到而卡住。
        include_closed: True 时忽略开放日限制(仅用于"今日不开放"的缺口提示)

    Returns:
        缺口计划列表, 每项含:
        {"key","label","stage","item_id","item_name","have","target","gap","open_days"};
        未勾选/已达标的保持项与关卡不返回; 同一关卡掉落多种目标物品时,
        取缺口最大的那个(缺口相同按预设顺序), 保证 drops 停止条件单一明确。
    """
    depot = depot or {}
    opt = inventory_opt or {}
    day = _today_weekday() if weekday is None else int(weekday)
    plans: List[Dict] = []
    for key in INVENTORY_ORDER:
        preset = INVENTORY_PRESETS.get(key)
        if not preset:
            continue
        conf = opt.get(key)
        if not isinstance(conf, dict) or not conf.get("enabled"):
            continue   # 未勾选的保持项不参与规划
        target = _resolve_inventory_count(conf.get("count"), preset["default_count"])
        for stage_conf in preset["stages"]:
            open_days = list(stage_conf.get("open_days") or [])
            if not include_closed and open_days and day not in open_days:
                continue   # 该关卡今日不开放, 跳过(避免导航失败)
            # 该关卡目标物品的缺口(库存不足为正数; 识别缺失/非法值视为 0)
            candidates: List[Dict] = []
            for item_id, item_name in stage_conf["items"]:
                try:
                    have = int(depot.get(item_id, 0) or 0)
                except (TypeError, ValueError):
                    have = 0
                gap = target - have
                if gap > 0:
                    candidates.append({
                        "item_id": item_id, "item_name": item_name,
                        "have": have, "gap": gap,
                    })
            if not candidates:
                continue   # 该关卡目标物品全部达标
            best = max(candidates, key=lambda c: c["gap"])
            plans.append({
                "key": key,
                "label": preset["label"],
                "stage": stage_conf["stage"],
                "item_id": best["item_id"],
                "item_name": best["item_name"],
                "have": best["have"],
                "target": target,
                "gap": best["gap"],
                "open_days": open_days,
            })
    return plans


def plan_inventory(depot: Optional[dict], inventory_opt: Optional[dict],
                   weekday: Optional[int] = None) -> Optional[Dict]:
    """规划下一次理智作战: 返回第一个缺口计划(按保持项顺序), 无则返回 None。

    说明: 资源关卡按开放日轮换(芯片本/AP-5/CA-5), 非开放日的关卡会被跳过,
    因此"有缺口但今日全部不开放"时返回 None(由调用方提示)。
    """
    plans = inventory_gaps(depot, inventory_opt, weekday=weekday)
    return plans[0] if plans else None


def build_inventory_fight_params(plan: Dict, fight_opt: Optional[dict] = None) -> str:
    """构造库存保持的理智作战参数(指定关卡 + 掉落停止条件)。

    Args:
        plan: inventory_gaps/plan_inventory 产出的计划项
        fight_opt: 「作战设置」的理智药配置(库存保持沿用同一份设置)

    Returns:
        合法 JSON 字符串, 传给 AsstAppendTask("Fight", params)。

    说明(字段对齐官方集成文档 Fight):
    - stage: 规划出的固定资源关卡(芯片本/AP-5/CA-5);
    - times: 固定 auto(大次数) —— 补货战斗由 drops 达标或理智耗尽自然结束;
    - drops: {目标物品ID: 缺口数量} —— MaaCore 按「本次任务累计掉落数」停止
             (Farm 语义), 等价于把库存补到目标值(UI 层 Target Inventory 的
             协议层实现方式);
    - medicine: 沿用作战设置的理智药策略, 保证补货可正常消耗理智药。
    """
    item_id = str(plan.get("item_id", "") or "")
    try:
        gap = max(1, int(plan.get("gap", 1) or 1))
    except (TypeError, ValueError):
        gap = 1
    params: Dict = {
        "stage": str(plan.get("stage", "") or ""),
        "medicine": _resolve_medicine(fight_opt or {}),
        "stone": 0,                       # 不碎石(库存保持只消耗理智与理智药)
        "times": FIGHT_TIMES_AUTO,        # 次数 auto: 刷够缺口或理智耗尽即停
        "series": 0,                      # 代理倍率恒为 AUTO
        "client_type": "Official",        # 崩溃后自动重连回游戏继续刷
    }
    if item_id:
        params["drops"] = {item_id: gap}
    return json.dumps(params, ensure_ascii=False)


class _TaskOutcome(NamedTuple):
    """单个任务的执行结果(_wait_task 返回给主流程)。

    Attributes:
        ok: 任务是否正常完成(被跳过/被停止/任务链出错均为 False)
        message: 失败原因(用于日志与状态快照)
        skipped: 因子任务出错达到上限而跳过
        stopped_all: 因收到全局停止请求而中断(主流程需结束整轮)
    """
    ok: bool
    message: str = ""
    skipped: bool = False
    stopped_all: bool = False


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
        self._inventory_plan: Optional[Dict] = None   # 库存保持规划结果(供 WebUI 展示)

    # ---- 写(协调器线程) ----
    def start_run(self, tasks: List[str], labels: Optional[List[str]] = None) -> None:
        """开始一轮运行: 复位状态并打开日志文件。

        Args:
            tasks: 任务 key 列表(用于总进度与状态快照)
            labels: 可选的可读展示名列表(与 tasks 一一对应, 仅用于日志展示)
        """
        with self._lock:
            self._state = self.RUNNING
            self._current = ""
            self._finished = []
            self._total = list(tasks)
            self._error = ""
            self._inventory_plan = None   # 每轮运行复位, 由库存保持任务重新规划
            self._log = ["开始执行, 任务序列: " + ", ".join(labels or tasks)]
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

    def set_inventory_plan(self, plan: Optional[Dict]) -> None:
        """记录库存保持的规划结果(供 WebUI 显示"下一次打什么/哪一关")。

        Args:
            plan: inventory_gaps/plan_inventory 产出的计划项;
                  None 表示库存充足或未规划(前端据此展示占位符)。
        """
        with self._lock:
            self._inventory_plan = dict(plan) if plan else None

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
                "inventory_plan": dict(self._inventory_plan) if self._inventory_plan else None,
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

        # 当前执行中的任务(key 与 MaaCore 类型), 供回调把任务链名映射为可读名称
        self._current_key: str = ""
        self._current_type: str = ""

        # 子任务错误跟踪(每次任务开始前复位):
        # 回调在 MaaCore 线程写入, 计数用锁保护; 超限仅置标志,
        # 真正的停止由等待循环执行(不在回调内调用核心接口, 避免重入死锁)。
        self._error_lock = threading.Lock()
        self._sub_error_count = 0
        self._skip_flag = threading.Event()         # 子任务出错超限 -> 跳过当前任务
        self._chain_outcome: Dict[str, str] = {}    # 当前任务链最终状态(回调写入)

        # 库存保持: 仓库识别结果由 DepotInfo 回调在 MaaCore 线程写入, 加锁保护
        self._depot_lock = threading.Lock()
        self._depot_data: Dict[str, int] = {}       # {物品ID: 数量}
        self._depot_done = threading.Event()        # 识别完成(done=true)标志

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
        库存保持(inventory)为组合任务, 在任务循环内特判执行(见 _run_inventory),
        不走 build_task_params 的常规参数构造路径。
        """
        self.status.start_run(
            tasks, [task_display_name(t, DEFAULT_TASK_MAP.get(t, t)) for t in tasks])
        try:
            logger.info("启动 MAA 一键长草, 任务序列: %s",
                        ", ".join(task_display_name(t, DEFAULT_TASK_MAP.get(t, t)) for t in tasks))
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

            # 3~5) 逐个任务执行: 追加 -> 启动 -> 等待结束
            #      必须逐个执行: MaaCore 的 AsstStop 会停止队列中的所有任务,
            #      只有"队列中仅有一个任务"时才能实现"单任务出错超限后跳过"。
            stopped_all = False
            skipped: List[str] = []
            for task in tasks:
                # 库存保持是组合任务(仓库识别 + 缺口规划 + 理智作战), 走独立流程
                if task == "inventory":
                    outcome = self._run_inventory(assist)
                    if outcome.stopped_all:
                        stopped_all = True
                        break
                    self.status.mark_done(task, outcome.ok, outcome.message)
                    if outcome.skipped:
                        skipped.append(task)
                    continue

                task_type = DEFAULT_TASK_MAP.get(task, task)
                label = task_display_name(task, task_type)
                params = build_task_params(task, task_type, self._options)
                logger.info("[一键长草] 追加任务 %s params=%s", label, params)

                # 记录当前任务来源, 供回调日志区分"理智作战/每周剿灭"等同类型任务
                self._current_key, self._current_type = task, task_type
                if task == "annihilation":
                    self.status.log("每周剿灭: 本周合成玉已达上限时 MAA 会直接跳过(不进关卡属正常)")

                try:
                    tid = assist.append_task(task_type, params)
                except Exception as e:
                    # 单个任务参数/类型不被接受时不整体中断, 记录后继续后续任务
                    logger.error("[一键长草] 追加任务 %s 失败(跳过): %s", label, e)
                    self.status.log(f"追加任务 {label} 失败, 已跳过: {e}")
                    self.status.mark_done(task, False, f"{label} 追加失败: {e}")
                    continue
                self.status.log(f"追加任务成功: {label} (task_id={tid})")

                # 每次任务开始前复位错误跟踪(仅统计当前任务内的子任务出错)
                self._reset_error_tracking()
                self.status.set_current(label)
                try:
                    assist.start()
                except Exception as e:
                    logger.error("[一键长草] 启动任务 %s 失败(跳过): %s", label, e)
                    self.status.log(f"启动任务 {label} 失败, 已跳过: {e}")
                    self.status.mark_done(task, False, f"{label} 启动失败: {e}")
                    continue

                outcome = self._wait_task(assist)
                if outcome.stopped_all:
                    stopped_all = True
                    break
                self.status.mark_done(task, outcome.ok, outcome.message)
                if outcome.skipped:
                    skipped.append(task)

            if stopped_all:
                self.status.set_final("idle", "已手动停止")
            elif skipped:
                self.status.set_final(
                    "idle",
                    f"全部任务执行完成(跳过 {len(skipped)} 个: {', '.join(skipped)})")
            else:
                self.status.set_final("idle", "全部任务执行完成")
            logger.info("一键长草结束: %s", self.status.snapshot()["message"])
        except Exception as e:
            logger.exception("一键长草执行异常")
            self.status.mark_done("", False, str(e))
            self.status.log(f"执行异常: {e}")
            self.status.set_final("error", str(e))

    def _reset_error_tracking(self) -> None:
        """任务开始前复位子任务错误计数与跳过标志(仅跟踪当前任务)。"""
        with self._error_lock:
            self._sub_error_count = 0
        self._skip_flag.clear()
        self._chain_outcome = {"state": "", "why": ""}

    def _wait_task(self, assist) -> _TaskOutcome:
        """等待当前任务结束, 并处理全局停止与子任务出错超限跳过。

        Args:
            assist: MaaCoreAssistant 实例(仅用 running/stop 两个接口)

        Returns:
            _TaskOutcome: 任务结果; stopped_all 为 True 时主流程应结束整轮
        """
        while assist.running():
            if self._stop_flag.is_set():
                logger.info("收到停止请求, 停止 MaaCore 任务")
                self.status.log("收到停止请求, 正在停止...")
                assist.stop()
                self._wait_not_running(assist)
                return _TaskOutcome(ok=False, message="已手动停止", stopped_all=True)
            if self._skip_flag.is_set():
                msg = f"子任务出错达到上限({MAX_SUB_TASK_ERRORS} 次), 跳过该任务"
                logger.warning("[一键长草] %s", msg)
                self.status.log(msg)
                assist.stop()
                self._wait_not_running(assist)
                return _TaskOutcome(ok=False, message=msg, skipped=True)
            time.sleep(0.5)

        # 任务自然结束: 依据回调记录的最终状态判定成功与否
        state = self._chain_outcome.get("state", "")
        why = self._chain_outcome.get("why", "")
        if state == "error":
            return _TaskOutcome(ok=False, message=why or "任务链出错")
        if state == "stopped":
            return _TaskOutcome(ok=False, message="任务被停止")
        return _TaskOutcome(ok=True, message="")

    def _wait_not_running(self, assist, timeout: float = 30.0) -> None:
        """等待 MaaCore 真正结束当前任务(AsstStop 需等当前节点执行完)。"""
        deadline = time.monotonic() + timeout
        while assist.running() and time.monotonic() < deadline:
            time.sleep(0.5)

    # ------------------------------------------------------------------ #
    # 库存保持(仓库识别 -> 缺口规划 -> 理智作战补货)                        #
    # ------------------------------------------------------------------ #
    def _run_inventory(self, assist) -> _TaskOutcome:
        """库存保持: 扫描仓库 -> 规划缺口 -> 用理智作战补货(对齐 MAA GUI「仓库维持」)。

        流程:
          1. 追加并执行 Depot(仓库识别)任务, 等待 DepotInfo 回调返回库存数据
             (识别中 done=false 的渐进数据同样累计; 停止/跳过/超时均有保护);
          2. 按 INVENTORY_PRESETS 逐关卡计算缺口, 取第一个缺口计划写入状态
             (WebUI 据此展示"下一次打什么/哪一关");
          3. 有缺口时追加理智作战(指定关卡 + drops 缺口数量 + 次数 auto),
             复用 _wait_task 等待结束(子任务出错超限跳过逻辑与普通任务一致)。

        失败不阻断: 未勾选保持项/识别失败或超时/库存充足时记录日志并跳过,
        继续执行后续任务。

        Args:
            assist: MaaCoreAssistant 实例

        Returns:
            _TaskOutcome: stopped_all 为 True 时主流程应结束整轮。
        """
        inv_opt = self._options.get("inventory") or {}
        if not any(isinstance(v, dict) and v.get("enabled") for v in inv_opt.values()):
            self.status.log("库存保持: 未勾选任何保持项, 跳过")
            return _TaskOutcome(ok=True, message="未勾选保持项")

        # 1) 仓库识别: 复位回调数据后执行 Depot 任务
        self._reset_error_tracking()
        with self._depot_lock:
            self._depot_data = {}
        self._depot_done.clear()
        self._current_key, self._current_type = "inventory", "Depot"
        self.status.set_current("库存保持(仓库识别)")
        self.status.log("库存保持: 正在扫描仓库...")
        try:
            tid = assist.append_task("Depot", "{}")
            self.status.log(
                f"追加任务成功: {task_display_name('inventory', 'Depot')} (task_id={tid})")
            assist.start()
        except Exception as e:
            logger.error("[库存保持] 仓库识别启动失败(跳过): %s", e)
            self.status.log(f"库存保持: 仓库识别启动失败, 已跳过: {e}")
            return _TaskOutcome(ok=False, message=f"仓库识别失败: {e}")

        early = self._wait_depot(assist)
        if early is not None:
            return early   # 停止请求/子任务出错跳过等提前结束情形

        with self._depot_lock:
            depot = dict(self._depot_data)
        if not depot:
            why = self._chain_outcome.get("why") or "未返回识别数据"
            self.status.log(f"库存保持: 仓库识别失败({why}), 跳过理智作战")
            return _TaskOutcome(ok=False, message=f"仓库识别失败: {why}")
        if not self._depot_done.is_set():
            self.status.log("库存保持: 未收到识别完成标记, 使用已识别到的数据继续")

        # 2) 规划缺口: 第一个缺口计划即"下一次理智作战", 写入状态供 WebUI 展示。
        #    资源关卡按开放日轮换(芯片本/AP-5/CA-5), 非开放日自动跳过。
        plan = plan_inventory(depot, inv_opt)
        self.status.set_inventory_plan(plan)
        if not plan:
            # 无计划有两种原因: 全部达标 / 有缺口但对应关卡今日不开放
            closed = inventory_gaps(depot, inv_opt, include_closed=True)
            if closed:
                self._log_closed_gaps(closed)
                return _TaskOutcome(ok=True, message="关卡今日不开放")
            self.status.log("库存保持: 所有保持项均已达标, 无需理智作战")
            return _TaskOutcome(ok=True, message="库存充足")
        gaps = inventory_gaps(depot, inv_opt)
        if len(gaps) > 1:
            others = ", ".join(f"{g['stage']}/{g['item_name']}" for g in gaps[1:4])
            suffix = " 等" if len(gaps) > 4 else ""
            self.status.log(
                f"库存保持: 另有 {len(gaps) - 1} 项缺口({others}{suffix})待后续运行补")
        self.status.log(
            f"库存保持: 规划理智作战 -> {plan['stage']} · {plan['item_name']} "
            f"缺 {plan['gap']} (现有 {plan['have']}/目标 {plan['target']})")

        # 3) 追加理智作战补货: 指定关卡 + 掉落停止条件(次数 auto)
        self._reset_error_tracking()
        self._current_key, self._current_type = "inventory", "Fight"
        params = build_inventory_fight_params(plan, self._options.get("fight"))
        label = f"库存保持/{plan['stage']}"
        logger.info("[一键长草] 追加库存保持战斗 %s params=%s", label, params)
        try:
            tid = assist.append_task("Fight", params)
            self.status.log(f"追加任务成功: {label}(Fight) (task_id={tid})")
            self.status.set_current(label)
            assist.start()
        except Exception as e:
            logger.error("[库存保持] 追加/启动理智作战失败(跳过): %s", e)
            self.status.log(f"库存保持: 追加/启动理智作战失败, 已跳过: {e}")
            return _TaskOutcome(ok=False, message=f"理智作战启动失败: {e}")

        outcome = self._wait_task(assist)
        if outcome.ok:
            self.status.log(
                f"库存保持: {plan['item_name']} 补货结束"
                f"(缺口 {plan['gap']}, 掉落达标或理智耗尽自动停止)")
        return outcome

    def _log_closed_gaps(self, closed: List[Dict]) -> None:
        """输出"有缺口但今日不开放"的日志, 并提示各关卡最近的下次开放日。

        Args:
            closed: inventory_gaps(include_closed=True) 的结果 —— 调用时机为
                    "今日可刷计划为空", 因此这里的每一项必然都是今日不开放的关卡。
        """
        names = ", ".join(f"{g['stage']}/{g['item_name']}" for g in closed[:3])
        suffix = " 等" if len(closed) > 3 else ""
        self.status.log(
            f"库存保持: 有 {len(closed)} 项缺口, 但对应关卡今日不开放"
            f"({names}{suffix}), 跳过理智作战")
        # 逐项提示下次开放日(最多 3 条, 避免日志过长)
        day = _today_weekday()
        for gap in closed[:3]:
            nxt = next_open_weekday(gap.get("open_days") or [], day)
            if nxt:
                self.status.log(
                    f"库存保持: {gap['stage']}({gap['item_name']}) 下次开放: "
                    f"{_WEEKDAY_NAMES[nxt - 1]}")

    def _wait_depot(self, assist) -> Optional[_TaskOutcome]:
        """等待仓库识别任务结束, 期间响应停止/跳过/超时保护。

        Args:
            assist: MaaCoreAssistant 实例

        Returns:
            None 表示任务正常结束(进入规划流程);
            非 None 为提前结束的 _TaskOutcome(停止请求 stopped_all / 出错跳过 skipped)。
        """
        deadline = time.monotonic() + DEPOT_TIMEOUT
        while assist.running():
            if self._stop_flag.is_set():
                logger.info("收到停止请求, 停止仓库识别")
                self.status.log("收到停止请求, 正在停止...")
                assist.stop()
                self._wait_not_running(assist)
                return _TaskOutcome(ok=False, message="已手动停止", stopped_all=True)
            if self._skip_flag.is_set():
                msg = f"子任务出错达到上限({MAX_SUB_TASK_ERRORS} 次), 跳过该任务"
                logger.warning("[库存保持] %s", msg)
                self.status.log(msg)
                assist.stop()
                self._wait_not_running(assist)
                return _TaskOutcome(ok=False, message=msg, skipped=True)
            if time.monotonic() > deadline:
                # 超时: 主动停止识别, 尝试使用已累计的数据降级继续
                self.status.log(
                    f"仓库识别超时({int(DEPOT_TIMEOUT)} 秒), 已停止识别")
                assist.stop()
                self._wait_not_running(assist)
                break
            time.sleep(0.5)
        return None

    def _on_depot_info(self, details: Dict) -> None:
        """解析 DepotInfo 回调(仓库识别结果), 累计库存数据并标记识别完成。

        Args:
            details: 回调消息的顶层 details 对象, 其中 details["details"] 为
                     {"done": bool, "data": JSON 字符串 {"物品ID": 数量}}

        说明: 识别过程中 done=false 也会携带已识别部分(渐进上报), 一并累计;
              done=true 置完成事件并输出日志, 供 _run_inventory 判定可用性。
        """
        inner = details.get("details")
        if not isinstance(inner, dict):
            inner = {}
        raw = inner.get("data")
        payload = None
        if isinstance(raw, str) and raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as e:
                logger.warning("DepotInfo data 解析失败: %s", e)
        elif isinstance(raw, dict):
            payload = raw   # 兼容直接返回对象(非字符串)的版本差异
        if isinstance(payload, dict):
            cleaned: Dict[str, int] = {}
            for item_id, count in payload.items():
                try:
                    cleaned[str(item_id)] = int(count)
                except (TypeError, ValueError):
                    continue   # 忽略非数字条目, 防脏数据污染库存表
            if cleaned:
                with self._depot_lock:
                    self._depot_data.update(cleaned)
        if inner.get("done"):
            self._depot_done.set()
            with self._depot_lock:
                count = len(self._depot_data)
            self.status.log(f"仓库识别完成: 共 {count} 种物品")

    def _do_signin(self) -> None:
        """任务序列前置: 执行网易云游戏签到(失败仅记录, 不阻断一键长草)。"""
        self.status.log("执行网易云游戏签到...")
        if not self._token:
            self.status.log("签到跳过: 未获取到登录 token(请先启动云游戏)")
            return
        try:
            # 惰性导入 + 同步调用(协调器在线程内, 不阻塞事件循环)
            # 签到实现位于 netease_login(按线上接口抓包修正, 替代 sdk/signin.py 的探测式实现)
            from netease_login import netease_signin
            result = netease_signin(self._token)
        except Exception as e:
            self.status.log(f"签到异常(忽略): {e}")
            logger.warning("签到异常(忽略): %s", e)
            return
        if result.get("ok"):
            self.status.log(f"云游戏签到成功: {result.get('message', '')}")
        else:
            self.status.log(f"云游戏签到失败: {result.get('message', '')}")

    def _on_maa_msg(self, msg_id: int, details_json: str) -> None:
        """MaaCore 异步消息回调: 提取任务进度与错误写入状态。

        msg 取值对齐官方回调协议(callback-schema):
            TaskChainError=10000  TaskChainStart=10001  TaskChainCompleted=10002
            TaskChainExtraInfo=10003  TaskChainStopped=10004
            SubTaskExtraInfo=20003(含 DepotInfo 仓库识别结果)
            SubTaskError=20000  AllTasksCompleted=3  ConnectionInfo=2
        """
        try:
            details = json.loads(details_json) if details_json else {}
        except Exception:
            details = {}
        taskchain = details.get("taskchain", "")
        what = details.get("what", "")
        why = details.get("why", "")
        subtask = details.get("subtask", "")
        chain = self._label_chain(taskchain)   # "每周剿灭(Fight)" 这类可读名称

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
            logger.error("[MaaCore] 任务链出错: %s -> %s", chain, why)
            self.status.log(f"[MaaCore] 任务链出错: {chain} ({why})")
            self._chain_outcome = {"state": "error", "why": why}
        elif msg_id == 10001:  # TaskChainStart
            logger.info("[MaaCore] 任务开始: %s", chain)
            self.status.log(f"[MaaCore] 任务开始: {chain}")
        elif msg_id == 10002:  # TaskChainCompleted
            logger.info("[MaaCore] 任务完成: %s", chain)
            self.status.log(f"[MaaCore] 任务完成: {chain}")
            self._chain_outcome = {"state": "completed", "why": ""}
        elif msg_id == 10004:  # TaskChainStopped
            logger.info("[MaaCore] 任务被停止: %s", chain)
            self.status.log(f"[MaaCore] 任务被停止: {chain}")
            self._chain_outcome = {"state": "stopped", "why": ""}
        # ---- 子任务额外信息: 仓库识别结果(库存保持的第一步) ----
        elif msg_id in (20003, 10003) and what == "DepotInfo":
            # 官方回调: SubTaskExtraInfo(20003) 携带 DepotInfo(done/data),
            # 兼容 TaskChainExtraInfo(10003) 以防版本差异
            self._on_depot_info(details)
        # ---- 子任务错误(计数并在超过上限后跳过当前任务) ----
        elif msg_id == 20000:  # SubTaskError
            logger.error("[MaaCore] 子任务出错: %s/%s -> %s", chain, subtask, why)
            self._on_sub_task_error(chain, why, subtask)

    def _label_chain(self, taskchain: str) -> str:
        """把 MaaCore 回调的任务链名映射为「中文名(类型)」。

        理智作战与每周剿灭共用 Fight 类型, 仅看类型名无法区分来源;
        任务串行执行, 当前任务 key 已知, 因此可直接映射为可读名称。
        """
        if taskchain and taskchain == self._current_type and self._current_key:
            return task_display_name(self._current_key, taskchain)
        return taskchain

    def _on_sub_task_error(self, taskchain: str, why: str, subtask: str = "") -> None:
        """累计当前任务的子任务出错次数, 达到上限后请求跳过该任务。

        计数在 MaaCore 回调线程内更新(加锁保护); 超限后只设置跳过标志,
        真正的 AsstStop 由等待循环(_wait_task)执行 —— 回调内不调用核心接口,
        避免 MaaCore 重入/死锁。

        Args:
            taskchain: 已映射为可读名称的任务链(如 "每周剿灭(Fight)")
            why: MaaCore 给出的原因
            subtask: 出错节点的名称(why 为空时用于兜底展示, 便于定位)
        """
        with self._error_lock:
            self._sub_error_count += 1
            count = self._sub_error_count
        reason = why or subtask or "MaaCore 未提供原因"
        self.status.log(
            f"[MaaCore] 子任务出错({count}/{MAX_SUB_TASK_ERRORS}): {taskchain} ({reason})")
        if count >= MAX_SUB_TASK_ERRORS:
            self._skip_flag.set()

    def snapshot(self) -> Dict:
        snap = self.status.snapshot()
        snap["running"] = self.running
        return snap