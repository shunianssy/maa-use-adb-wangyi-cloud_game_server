"""
账号独立配置: 为每个账号保存一份「一键长草」运行配置(任务开关/作战/基建/领奖/库存/签到)。

存储文件默认 account_settings.json(环境变量 NETEASE_ACCOUNT_SETTINGS_FILE 可覆盖):
{
  "13800001111": {
    "tasks": {...}, "fight": {...}, "annihilation": {...},
    "infrast": {...}, "signin": {...}, "inventory": {...}, "award": {...},
    "weekdays": [1, 3, 5]
  }
}

设计要点:
- 运行字段(tasks/fight/annihilation/infrast/signin/inventory/award)按账号保存;
- 每日定时的「每周执行日」(weekdays)也按账号保存, enabled/time 仍为全局;
- 未单独配置的字段自动回落到全局设置, 与旧版本行为一致;
- 读取时与全局设置做深度合并, 缺失字段自动补全, 避免旧配置缺字段导致 KeyError;
- 文件只含账号配置(不含 token), 但同属运行数据, 已加入 .gitignore/.dockerignore。
"""

import json
import logging
import os
import tempfile
import threading
from typing import Dict, List, Optional

from netease_login import normalize_phone

logger = logging.getLogger("account_settings")

# 账号可独立覆盖的运行配置字段(其余字段如 daily.enabled/time 保持全局)
RUN_SETTING_KEYS = (
    "tasks", "fight", "annihilation", "infrast", "signin", "inventory", "award",
)


class AccountSettingsStore:
    """账号独立配置的读写封装(内存缓存 + 原子化落盘)。"""

    def __init__(self, path: str) -> None:
        """path: 配置文件路径(目录不存在会自动创建)。"""
        self._path = path
        self._lock = threading.RLock()
        self._data: Dict[str, Dict] = {}
        self.load()

    @property
    def path(self) -> str:
        return self._path

    def load(self) -> None:
        """从磁盘加载账号配置(非法条目自动跳过)。"""
        try:
            # utf-8-sig: 容忍 Windows 记事本保存时写入的 BOM
            with open(self._path, "r", encoding="utf-8-sig") as f:
                disk = json.load(f)
        except FileNotFoundError:
            disk = {}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("读取账号配置失败, 使用空配置: %s", e)
            disk = {}
        if not isinstance(disk, dict):
            disk = {}

        data: Dict[str, Dict] = {}
        for phone, cfg in disk.items():
            pnum = normalize_phone(phone)
            if not pnum or not isinstance(cfg, dict):
                continue
            data[pnum] = cfg
        self._data = data

    def save(self) -> None:
        """原子化写入磁盘: 先写临时文件再替换, 避免写一半损坏配置。"""
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        """锁内落盘(调用方需已持有 self._lock)。"""
        try:
            directory = os.path.dirname(self._path) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix="account_settings.", suffix=".tmp", dir=directory)
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
            logger.error("保存账号配置失败: %s", e)

    # ------------------------------------------------------------------ #
    # 查询                                                                #
    # ------------------------------------------------------------------ #
    def get(self, phone: str) -> Dict:
        """取账号的原始覆盖配置(不存在返回空 dict)。"""
        pnum = normalize_phone(phone)
        if not pnum:
            return {}
        with self._lock:
            return json.loads(json.dumps(self._data.get(pnum, {})))

    def phones(self) -> List[str]:
        """返回已保存独立配置的账号手机号列表。"""
        with self._lock:
            return list(self._data)

    @staticmethod
    def _normalize_weekdays(raw) -> List[int]:
        """把每周执行日规整为 ISO 星期升序列表(非法值忽略)。"""
        if not isinstance(raw, list):
            return []
        days = set()
        for item in raw:
            try:
                day = int(item)
            except (TypeError, ValueError):
                continue
            if 1 <= day <= 7:
                days.add(day)
        return sorted(days)

    def get_weekdays(self, phone: str) -> Optional[List[int]]:
        """返回账号独立配置的每周执行日; 未配置返回 None(表示沿用全局)。

        注意: 返回空列表表示该账号明确设置为"不执行任何一天"。
        """
        pnum = normalize_phone(phone)
        if not pnum:
            return None
        with self._lock:
            cfg = self._data.get(pnum) or {}
            if "weekdays" not in cfg:
                return None
            return self._normalize_weekdays(cfg.get("weekdays"))

    def merged(self, phone: str, base: Dict) -> Dict:
        """把账号覆盖配置深度合并到全局设置上, 返回完整设置。

        Args:
            phone: 账号手机号(为空时直接返回全局设置副本)
            base: 全局设置快照(maa_settings.get())

        Returns:
            深拷贝后的完整设置(可直接下发给前端 / 传执行流程)
        """
        result = json.loads(json.dumps(base))
        patch = self.get(phone)
        for key in RUN_SETTING_KEYS:
            if key not in patch:
                continue
            if isinstance(patch[key], dict) and isinstance(result.get(key), dict):
                self._merge_into(result[key], patch[key])
            else:
                result[key] = json.loads(json.dumps(patch[key]))
        # 每周执行日: 账号配置优先; 未配置时保留全局 daily.weekdays
        stored_days = self.get_weekdays(phone)
        if stored_days is not None:
            daily = result.get("daily")
            if not isinstance(daily, dict):
                daily = {}
                result["daily"] = daily
            daily["weekdays"] = list(stored_days)
        return result

    @staticmethod
    def _merge_into(base, patch) -> None:
        """把 patch 深度合并进 base(仅覆盖已有结构; 类型不一致时整体替换)。"""
        if not isinstance(patch, dict) or not isinstance(base, dict):
            return
        for key, val in patch.items():
            if key in base and isinstance(val, dict) and isinstance(base[key], dict):
                AccountSettingsStore._merge_into(base[key], val)
            else:
                base[key] = json.loads(json.dumps(val))

    # ------------------------------------------------------------------ #
    # 修改                                                                #
    # ------------------------------------------------------------------ #
    def update(self, phone: str, patch: Dict) -> Dict:
        """合并更新账号的独立配置并落盘, 返回更新后的原始覆盖配置。

        仅接受 RUN_SETTING_KEYS 中的字段, 其余字段(如 daily)由全局设置管理。
        """
        pnum = normalize_phone(phone)
        if not pnum or not isinstance(patch, dict):
            return {}
        with self._lock:
            current = self._data.setdefault(pnum, {})
            for key in RUN_SETTING_KEYS:
                if key not in patch:
                    continue
                val = patch[key]
                if isinstance(val, dict) and isinstance(current.get(key), dict):
                    self._merge_into(current[key], val)
                else:
                    current[key] = json.loads(json.dumps(val))
            # 每周执行日: 只保存该账号自己的选择(空列表=不执行任何一天)
            if "weekdays" in patch:
                current["weekdays"] = self._normalize_weekdays(patch["weekdays"])
            self._save_locked()
            return json.loads(json.dumps(current))

    def remove(self, phone: str) -> bool:
        """删除账号的独立配置(账号被移出列表时调用); 不存在返回 False。"""
        pnum = normalize_phone(phone)
        with self._lock:
            if pnum not in self._data:
                return False
            self._data.pop(pnum, None)
            self._save_locked()
            return True
