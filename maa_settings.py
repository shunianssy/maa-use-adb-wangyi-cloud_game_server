"""
设置持久化模块: WebUI 一键长草配置保存到 maa_settings.json。

职责:
- 提供默认设置结构与原子化读写(临时文件 + rename, 防写一半损坏)
- 全局单例由 server.py 持有, 前端通过 GET/POST /maa/settings 读写

结构示例:
{
  "tasks": {"awaken": true, "combat": true, ...},   # 任务开关
  "fight": {                                        # 理智作战
      "stage": "1-7",                               # 关卡, 留空=识别当前/上次
      "times": 5,                                   # 战斗次数: 正整数 或 "auto"(刷完理智自动停)
      "medicine_mode": "auto", "medicine": 0        # 理智药: off/auto/num
  },
  "annihilation": {"enabled": false, "times": 1},   # 每周剿灭
  "infrast": {                                      # 基建换班
      "mode": 0,                                    # 0=常规 / 10000=自定义 / 20000=队列轮换
      "facility": ["Mfg", "Trade", ...],            # 参与换班的设施
      "drones": "Money", "threshold": 0.3, ...
  },
  "inventory": {                                    # 库存保持(仓库扫描+缺口规划)
      "chip_low": {"enabled": false, "count": 20},  # 低级芯片(全职业)目标数量
      "chip_high": {"enabled": false, "count": 20}, # 高级芯片组(全职业)
      "certificate": {"enabled": false, "count": 20},      # 采购凭证(红票)
      "skill_summary": {"enabled": false, "count": 200},   # 技巧概要·卷3
  },
  "daily": {"enabled": false, "time": "08:00"},     # 每日定时执行
  "last_daily_run": ""                              # 上次定时执行日期(YYYY-MM-DD)
}
"""

import json
import logging
import os
import tempfile
from typing import Dict

logger = logging.getLogger("maa_settings")

# 基建换班: 换班模式取值(对齐官方集成文档 Infrast.mode)
INFRAST_MODE_DEFAULT = 0        # Default: 自动计算效率较高的干员组合
INFRAST_MODE_CUSTOM = 10000     # Custom: 读取自定义排班配置
INFRAST_MODE_ROTATION = 20000   # Rotation: 队列轮换(跳过中枢/发电站/宿舍/办公室)

# 基建换班: 合法设施名(对齐官方集成文档 Infrast.facility)
INFRAST_FACILITIES = [
    "Mfg", "Trade", "Control", "Power", "Reception",
    "Office", "Dorm", "Processing", "Training",
]

# 基建换班: 合法无人机用途(对齐官方集成文档 Infrast.drones)
INFRAST_DRONES = [
    "_NotUse", "Money", "SyntheticJade", "CombatRecord",
    "PureGold", "OriginStone", "Chip",
]

# 默认设置(前端缺省值也以此为准, 两端字段保持一致)
DEFAULT_SETTINGS: Dict = {
    "tasks": {
        "awaken": True,
        "recruit": True,
        "infrast": True,
        "combat": True,
        "credit": True,
        "reward": True,
        # 库存保持默认不勾选: 需在控制台勾选后才能勾选具体保持项并参与规划
        "inventory": False,
    },
    "fight": {
        "stage": "",            # 关卡, 留空=识别当前/上次
        # 战斗次数: 正整数 或 "auto"(刷完当前理智自动停, 由 MAA 自行结束)
        "times": 5,
        "medicine_mode": "auto",   # 理智药: "off"关闭 / "auto"全部使用 / "num"指定数量
        "medicine": 3,          # medicine_mode="num" 时的理智药数量
    },
    "annihilation": {
        # 每周剿灭默认不勾选(需在控制台「作战设置」中手动勾选才纳入日常):
        # 该任务在 MaaCore 侧复用 Fight 类型, 默认执行容易与「理智作战」混淆
        "enabled": False,
        "auto": True,           # True=打满本周合成玉即止(AUTO); False=固定 times 场次
        "times": 4,             # auto=False 时的固定场次
    },
    "infrast": {
        # 换班模式: 0=常规模式 / 10000=自定义基建模式 / 20000=队列轮换
        "mode": INFRAST_MODE_DEFAULT,
        # 参与换班的设施(缺省与 MAA「常规设置」默认全选一致)
        "facility": list(INFRAST_FACILITIES),
        "drones": "Money",      # 无人机用途: 贸易站-龙门币
        "threshold": 0.3,       # 工作心情阈值 [0, 1.0]
        "replenish": True,      # 贸易站「源石碎片」自动补货
        "dorm_notstationed_enabled": False,  # 宿舍「未进驻」选项
        "dorm_trust_enabled": True,          # 宿舍空位填入信赖未满干员
        "reception_message_board": True,     # 领取会客室信息板信用
        "reception_clue_exchange": True,     # 线索交流
        "reception_send_clue": True,         # 赠送线索
        "filename": "",         # 自定义排班配置路径(仅 mode=10000 生效)
        "plan_index": 0,        # 使用配置中的方案序号(仅 mode=10000 生效)
    },
    "signin": {
        "enabled": True,        # 一键长草运行前先执行网易云游戏签到
    },
    "inventory": {
        # 库存保持(对齐 MAA GUI「仓库维持」的简化版):
        # 勾选后, 运行到该任务时先扫描仓库(Depot 任务), 按缺口规划一次理智作战补货。
        # count 为目标持有数量, 每种物品独立计算缺口; 保持项与关卡映射见
        # maa_coordinator.INVENTORY_PRESETS(原地硬编码, 均为固定资源关卡)。
        "chip_low": {"enabled": False, "count": 20},       # 低级芯片(全职业, 8 职业各 count 个)
        "chip_high": {"enabled": False, "count": 20},      # 高级芯片组(全职业)
        "certificate": {"enabled": False, "count": 20},    # 采购凭证(红票, AP-5)
        "skill_summary": {"enabled": False, "count": 200}, # 技巧概要·卷3(CA-5)
    },
    "award": {
        # 领取奖励细分项(对应 Award 任务参数, 默认与抓图配置一致全勾选)
        "award": True,          # 领取每日/每周任务奖励
        "mail": True,           # 领取所有邮件奖励
        "recruit": True,        # 进行限定池赠送的每日免费单抽
        "orundum": True,        # 领取幸运墙的每日合成玉奖励
        "mining": True,         # 领取限时开采许可的每日合成玉奖励
        "specialaccess": True,  # 领取周年赠送月卡奖励
    },
    "daily": {
        "enabled": False,       # 每日定时自动执行(启动云游戏 + 一键长草)
        "time": "08:00",        # 24 小时制 HH:MM
    },
    "last_daily_run": "",       # 上次定时执行日期, 防止当日重复触发
}


def default_settings() -> Dict:
    """深拷贝默认设置(避免调用方修改污染默认值)。"""
    return json.loads(json.dumps(DEFAULT_SETTINGS, ensure_ascii=False))


class MaaSettings:
    """设置文件的读写封装(线程安全体现在调用方加锁/单线程访问)。"""

    def __init__(self, path: str) -> None:
        """path: 设置文件路径(目录不存在会自动创建)。"""
        self._path = path
        self._data: Dict = default_settings()
        self.load()

    @property
    def path(self) -> str:
        return self._path

    def load(self) -> None:
        """从磁盘加载设置, 与默认值做浅合并, 缺失字段回退默认。"""
        m = default_settings()
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                disk = json.load(f)
            self._deep_merge(m, disk)
        except FileNotFoundError:
            pass  # 首次运行: 使用默认设置
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("读取设置失败, 使用默认设置: %s", e)
        self._data = m

    @staticmethod
    def _deep_merge(base: Dict, patch: Dict) -> None:
        """把 patch 深度合并进 base(仅覆盖 base 中存在的键, 避免写入未知字段)。"""
        for key, val in patch.items():
            if key not in base:
                continue
            if isinstance(val, dict) and isinstance(base[key], dict):
                MaaSettings._deep_merge(base[key], val)
            else:
                base[key] = val

    def save(self) -> None:
        """原子化写入磁盘: 先写临时文件再 rename, 避免写一半崩溃损坏配置。"""
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix="settings.", suffix=".tmp",
                dir=os.path.dirname(self._path) or ".")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self._path)
            except Exception:
                try:
                    os.unlink(tmp_path)  # 清理残留临时文件
                except OSError:
                    pass
                raise
        except OSError as e:
            logger.error("保存设置失败: %s", e)

    def get(self) -> Dict:
        """返回设置快照(浅拷贝, 防止外部篡改内部引用)。"""
        return json.loads(json.dumps(self._data, ensure_ascii=False))

    def update(self, patch: Dict) -> Dict:
        """合并更新设置并立即落盘, 返回更新后的快照。"""
        self._deep_merge(self._data, patch)
        self.save()
        return self.get()