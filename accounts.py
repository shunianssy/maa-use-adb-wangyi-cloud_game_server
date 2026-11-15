"""
多账号管理: 保存多个网易云游戏账号的登录 token, 支持按列表顺序轮流执行。

存储文件默认 accounts.json(环境变量 NETEASE_ACCOUNTS_FILE 可覆盖):
{
  "auto_switch": false,
  "current": "13800001111",
  "accounts": [
    {"phone": "13800001111", "token": "...", "enabled": true}
  ]
}

设计要点:
- token 属敏感信息: 文件已加入 .gitignore, HTTP 接口只下发脱敏手机号与状态;
- current 账号的 token 与根目录 token 文件保持同步(单账号逻辑继续复用 token 文件);
- 轮流执行时由 server.py 按 accounts 列表顺序逐个切换账号并执行一键长草。
"""

import json
import logging
import os
import tempfile
import threading
from typing import Dict, List, Optional

from netease_login import mask_phone, normalize_phone

logger = logging.getLogger("accounts")

# 账号数量上限: 防止误操作/异常调用把文件撑大
MAX_ACCOUNTS = 20

# 默认数据(首次运行或文件损坏时使用)
DEFAULT_DATA: Dict = {"auto_switch": False, "current": "", "accounts": []}


class AccountStore:
    """多账号列表的读写封装(内存缓存 + 原子化落盘)。"""

    def __init__(self, path: str) -> None:
        """path: 账号文件路径(目录不存在会自动创建)。"""
        self._path = path
        self._lock = threading.RLock()
        self._data: Dict = json.loads(json.dumps(DEFAULT_DATA))
        self.load()

    @property
    def path(self) -> str:
        return self._path

    def load(self) -> None:
        """从磁盘加载账号列表(校验手机号/去重, 异常时回退空列表)。"""
        try:
            # utf-8-sig: 容忍 Windows 记事本保存时写入的 BOM
            with open(self._path, "r", encoding="utf-8-sig") as f:
                disk = json.load(f)
        except FileNotFoundError:
            disk = {}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("读取多账号文件失败, 使用空列表: %s", e)
            disk = {}
        if not isinstance(disk, dict):
            disk = {}

        accounts: List[Dict] = []
        seen = set()
        for item in disk.get("accounts") or []:
            if not isinstance(item, dict):
                continue
            phone = normalize_phone(item.get("phone"))
            token = str(item.get("token") or "").strip()
            if not phone or not token or phone in seen:
                continue
            seen.add(phone)
            accounts.append({
                "phone": phone,
                "token": token,
                "enabled": bool(item.get("enabled", True)),
            })
            if len(accounts) >= MAX_ACCOUNTS:
                break

        current = normalize_phone(disk.get("current"))
        if current not in seen:
            current = accounts[0]["phone"] if accounts else ""

        self._data = {
            "auto_switch": bool(disk.get("auto_switch", False)),
            "current": current,
            "accounts": accounts,
        }

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
                prefix="accounts.", suffix=".tmp", dir=directory)
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
            logger.error("保存多账号文件失败: %s", e)

    # ------------------------------------------------------------------ #
    # 查询                                                                #
    # ------------------------------------------------------------------ #
    def public_list(self) -> List[Dict]:
        """返回可下发给前端的账号列表(脱敏手机号, 绝不包含 token)。"""
        with self._lock:
            current = self._data["current"]
            return [{
                "phone": acc["phone"],
                "masked": mask_phone(acc["phone"]),
                "enabled": acc["enabled"],
                "current": acc["phone"] == current,
            } for acc in self._data["accounts"]]

    def enabled_accounts(self) -> List[Dict]:
        """返回启用的账号(含 token, 供轮流执行使用), 保持列表顺序。"""
        with self._lock:
            return [dict(acc) for acc in self._data["accounts"]
                    if acc.get("enabled") and acc.get("token")]

    def get(self, phone: str) -> Optional[Dict]:
        """按手机号取账号完整信息(含 token); 不存在返回 None。"""
        pnum = normalize_phone(phone)
        with self._lock:
            for acc in self._data["accounts"]:
                if acc["phone"] == pnum:
                    return dict(acc)
            return None

    def current(self) -> Optional[Dict]:
        """返回当前账号(含 token); 无当前账号返回 None。"""
        with self._lock:
            for acc in self._data["accounts"]:
                if acc["phone"] == self._data["current"]:
                    return dict(acc)
            return None

    def current_phone(self) -> str:
        """返回当前账号手机号(未设置时为空串)。"""
        with self._lock:
            return str(self._data["current"] or "")

    @property
    def auto_switch(self) -> bool:
        """是否启用「多账号轮流执行」。"""
        with self._lock:
            return bool(self._data["auto_switch"])

    # ------------------------------------------------------------------ #
    # 修改                                                                #
    # ------------------------------------------------------------------ #
    def upsert(self, phone: str, token: str) -> bool:
        """新增或更新账号(登录成功后调用), 并把该账号设为当前账号。

        Returns:
            True 表示新增账号; False 表示更新已有账号或参数非法。
        """
        pnum = normalize_phone(phone)
        tok = str(token or "").strip()
        if not pnum or not tok:
            return False
        with self._lock:
            for acc in self._data["accounts"]:
                if acc["phone"] == pnum:
                    acc["token"] = tok
                    acc["enabled"] = True   # 重新登录视为启用
                    self._data["current"] = pnum
                    self._save_locked()
                    return False
            if len(self._data["accounts"]) >= MAX_ACCOUNTS:
                logger.warning("账号数量已达上限(%d), 忽略新增账号", MAX_ACCOUNTS)
                return False
            self._data["accounts"].append(
                {"phone": pnum, "token": tok, "enabled": True})
            self._data["current"] = pnum
            self._save_locked()
            return True

    def remove(self, phone: str) -> bool:
        """从列表移除账号(不影响 token 文件与当前云游戏会话)。

        当前账号被移除时, 自动顺延到列表中的第一个剩余账号。
        """
        pnum = normalize_phone(phone)
        with self._lock:
            before = len(self._data["accounts"])
            self._data["accounts"] = [
                acc for acc in self._data["accounts"] if acc["phone"] != pnum]
            if len(self._data["accounts"]) == before:
                return False
            if self._data["current"] == pnum:
                self._data["current"] = (
                    self._data["accounts"][0]["phone"]
                    if self._data["accounts"] else "")
            self._save_locked()
            return True

    def set_enabled(self, phone: str, enabled: bool) -> bool:
        """设置账号是否参与轮流执行; 账号不存在返回 False。"""
        pnum = normalize_phone(phone)
        with self._lock:
            for acc in self._data["accounts"]:
                if acc["phone"] == pnum:
                    acc["enabled"] = bool(enabled)
                    self._save_locked()
                    return True
            return False

    def set_current(self, phone: str) -> bool:
        """把指定账号设为当前账号; 账号不存在返回 False。"""
        pnum = normalize_phone(phone)
        with self._lock:
            if not any(acc["phone"] == pnum for acc in self._data["accounts"]):
                return False
            self._data["current"] = pnum
            self._save_locked()
            return True

    def set_auto_switch(self, enabled: bool) -> None:
        """启用/关闭「多账号轮流执行」。"""
        with self._lock:
            self._data["auto_switch"] = bool(enabled)
            self._save_locked()
