"""
设置持久化模块: WebUI 一键长草配置保存到 maa_settings.json。

职责:
- 提供默认设置结构与原子化读写(临时文件 + rename, 防写一半损坏)
- 全局单例由 server.py 持有, 前端通过 GET/POST /maa/settings 读写

结构示例:
{
  "tasks": {"awaken": true, "combat": true, ...},   # 任务开关
  "fight": {                                        # 理智作战
      "stage": "1-7", "times": 5, "series": 0,
      "medicine_enabled": false, "medicine": 0
  },
  "annihilation": {"enabled": false, "times": 1},   # 每周剿灭
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

# 默认设置(前端缺省值也以此为准, 两端字段保持一致)
DEFAULT_SETTINGS: Dict = {
    "tasks": {
        "awaken": True,
        "recruit": True,
        "infrast": True,
        "combat": True,
        "credit": True,
        "reward": True,
    },
    "fight": {
        "stage": "",            # 关卡, 留空=识别当前/上次
        "times": 5,             # 战斗次数
        "series": 0,            # 代理倍率: -1禁用 / 0 AUTO / 1..10 指定
        "medicine_mode": "auto",   # 理智药: "off"关闭 / "auto"全部使用 / "num"指定数量
        "medicine": 3,          # medicine_mode="num" 时的理智药数量
    },
    "annihilation": {
        "enabled": True,        # 每周剿灭默认纳入日常(合成玉已满时 MAA 自动跳过)
        "auto": True,           # True=打满本周合成玉即止(AUTO); False=固定 times 场次
        "times": 4,             # auto=False 时的固定场次
    },
    "signin": {
        "enabled": True,        # 一键长草运行前先执行网易云游戏签到
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