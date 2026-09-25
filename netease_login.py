"""
网易云游戏账号登录与签到(短信验证码 / 手机号密码)非交互实现。

接口来源: 由 cg.163.com 线上前端资源与真实请求抓包确认(2026-09), 关键结论:
- 短信登录:   POST /api/v1/tokens          {auth_method:"phone-captcha", ctcode, phone, captcha, device_info}
- 密码准备:   GET  /api/v2/user-pwd-info   ?phone=&ctcode=  → salt/3DES key/iv/随机码(需有效 token)
- 密码登录:   POST /api/v1/user-pwd-tokens {phone, ctcode, pwd, login_mode:"password"}
              其中 pwd = Base64(3DES-CBC-Pkcs7(SHA256(salt+password).hex + 随机码))
- 每日签到:   POST /api/v2/sign-today      请求体需字节偏移混淆, 返回当日奖励列表
- 响应混淆:   多数接口的响应按"字节偏移 key"混淆, 需解码后才能解析 JSON

职责:
- 把上述流程封装为可直接调用的函数(供 WebUI/HTTP 接口使用);
- token 原子化落盘到 token 文件, 供 server.py 启动云游戏时复用。
"""

import base64
import hashlib
import json
import logging
import os
import re
import tempfile
from typing import Dict, Optional

import requests

logger = logging.getLogger("netease_login")

# 3DES 加密依赖(密码登录); cryptography 43+ 把算法移到 decrepit 子模块
try:
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    from cryptography.hazmat.primitives import padding as _sym_padding
    from cryptography.hazmat.primitives.ciphers import Cipher, modes
    _CRYPTO_OK = True
except ImportError:  # 旧版本兼容
    try:
        from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES  # type: ignore
        from cryptography.hazmat.primitives import padding as _sym_padding
        from cryptography.hazmat.primitives.ciphers import Cipher, modes
        _CRYPTO_OK = True
    except ImportError:
        _CRYPTO_OK = False
        logger.warning("cryptography 不可用: 密码登录功能将不可用")

# 云游戏开放接口根地址(环境变量可覆盖, 便于测试/代理)
API_BASE = os.environ.get("NETEASE_API_BASE", "https://n.cg.163.com").rstrip("/")

# 中国大陆手机号: 11 位, 以 1 开头
PHONE_RE = re.compile(r"^1\d{10}$")

# 单次 HTTP 请求超时(秒)
REQUEST_TIMEOUT = 10

# 区号(请求验证码与登录均按 86-<手机号> 形式)
CTCODE = "86"

# 设备信息(与网页端一致; 短信登录接口要求携带)
DEVICE_INFO: Dict = {
    "userAgent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:91.0) "
                  "Gecko/20100101 Firefox/91.0"),
    "appVersion": "5.0 (Macintosh)",
    "codecs": ["h264", "vp8", "vp9"],
}


# --------------------------------------------------------------------------- #
# 基础工具                                                                      #
# --------------------------------------------------------------------------- #
def mask_phone(phone: str) -> str:
    """手机号脱敏(日志用): 13800001111 -> 138****1111。"""
    pnum = str(phone or "")
    if len(pnum) < 7:
        return "***"
    return f"{pnum[:3]}****{pnum[-4:]}"


def normalize_phone(phone) -> str:
    """规整手机号为 11 位纯数字; 非法输入返回空串。

    容忍前端常见形式: 空格/横线分隔、"86" 或 "+86" 前缀。
    """
    text = re.sub(r"[\s\-]", "", str(phone or ""))
    if text.startswith("+86"):
        text = text[3:]
    elif text.startswith("86") and len(text) == 13:
        text = text[2:]
    return text if PHONE_RE.match(text) else ""


# --------------------------------------------------------------------------- #
# 响应混淆/请求加密(偏移 key)                                                    #
# --------------------------------------------------------------------------- #
def derive_offset_key(text: str) -> Optional[int]:
    """从混淆响应中推导字节偏移 key; 非混淆内容返回 None。

    原理对齐 sdk/wsconnect.decode_mess: 明文首字节为 "[" 或 "{"。
    """
    stripped = (text or "").strip()
    if not stripped or stripped[0] in "[{":
        return None  # 已是明文
    try:
        raw = base64.b64decode(stripped, validate=True)
    except Exception:
        return None
    if len(raw) < 3:
        return None
    for key in range(256):
        c0, c1, c2 = (chr((raw[i] - key) % 256) for i in range(3))
        if (c0, c1, c2) == ("[", "{", '"') or (c0 == "{" and c1 == '"'):
            return key
    return None


def decode_mess(text: str, key: Optional[int] = None) -> str:
    """把偏移混淆的响应还原为明文(非混淆内容原样返回)。

    还原后的字节按 UTF-8 解码(接口可能返回未转义的中文)。
    """
    offset = key if key is not None else derive_offset_key(text)
    if offset is None:
        return text
    try:
        raw = base64.b64decode((text or "").strip())
    except Exception:
        return text
    plain = bytes((b - offset) % 256 for b in raw)
    return plain.decode("utf-8", errors="replace")


def encode_mess(payload: str, key: int) -> str:
    """按偏移 key 混淆请求体(与 decode_mess 互为逆运算), 返回 base64。"""
    raw = payload.encode("utf-8")
    return base64.b64encode(bytes((b + key) % 256 for b in raw)).decode("ascii")


def parse_response(text: str) -> Dict:
    """解析接口响应: 先按明文 JSON, 失败再按偏移混淆解码(均失败返回 {}）。"""
    for candidate in (text, decode_mess(text)):
        try:
            data = json.loads(candidate)
            return data if isinstance(data, dict) else {"data": data}
        except Exception:
            continue
    return {}


# --------------------------------------------------------------------------- #
# 短信验证码登录                                                                 #
# --------------------------------------------------------------------------- #
def request_sms_code(phone: str) -> dict:
    """请求发送短信验证码(阻塞式, 调用方应放线程池执行)。

    Returns:
        {"ok": bool, "status": int, "message": str}
    """
    pnum = normalize_phone(phone)
    if not pnum:
        return {"ok": False, "status": 0,
                "message": "手机号格式不正确(需 11 位中国大陆号码)"}

    url = f"{API_BASE}/api/v1/phone-captchas/{CTCODE}-{pnum}"
    try:
        resp = requests.post(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        logger.warning("发送短信验证码失败(网络异常): %s", e)
        return {"ok": False, "status": 0, "message": f"网络异常, 请稍后重试: {e}"}

    if resp.status_code < 300:
        logger.info("短信验证码已发送: %s", mask_phone(pnum))
        return {"ok": True, "status": resp.status_code,
                "message": "验证码已发送, 请查看短信"}

    logger.warning("发送短信验证码被拒绝: HTTP %s %s",
                   resp.status_code, (resp.text or "")[:200])
    return {"ok": False, "status": resp.status_code,
            "message": f"发送失败(HTTP {resp.status_code}), 请确认手机号或稍后重试"}


def login_with_captcha(phone: str, code: str) -> dict:
    """用手机号 + 短信验证码换取登录 token(阻塞式)。

    Returns:
        {"ok": bool, "token": str, "message": str}
    """
    pnum = normalize_phone(phone)
    if not pnum:
        return {"ok": False, "token": "", "message": "手机号格式不正确"}

    captcha = str(code or "").strip()
    if not captcha:
        return {"ok": False, "token": "", "message": "请输入短信验证码"}

    payload = {
        "auth_method": "phone-captcha",
        "ctcode": CTCODE,
        "phone": pnum,
        "captcha": captcha,
        "device_info": DEVICE_INFO,
    }
    try:
        resp = requests.post(
            f"{API_BASE}/api/v1/tokens",
            headers={"Content-Type": "application/json;charset=utf-8"},
            data=json.dumps(payload),
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        logger.warning("短信登录请求失败(网络异常): %s", e)
        return {"ok": False, "token": "", "message": f"网络异常, 请稍后重试: {e}"}

    obj = parse_response(resp.text)
    token = str(obj.get("token") or "").strip()
    if resp.status_code < 300 and token:
        logger.info("短信登录成功: %s", mask_phone(pnum))
        return {"ok": True, "token": token, "message": "登录成功"}

    reason = _error_message(obj, resp)
    logger.warning("短信登录失败: HTTP %s %s", resp.status_code, reason)
    return {"ok": False, "token": "", "message": f"登录失败: {reason}"}


# --------------------------------------------------------------------------- #
# 手机号密码登录                                                                 #
# --------------------------------------------------------------------------- #
def build_pwd_cipher(salt: str, encrypt_key: str, login_iv, random_text: str,
                     password: str) -> str:
    """构造密码登录的加密串(与网页端 crypto-js 逻辑逐位一致)。

    步骤:
      1. digest = SHA256(salt + password) 的十六进制字符串;
      2. 明文 = digest + 随机码;
      3. 3DES-CBC(PKCS7) 加密, 密钥为 encrypt_key 的 UTF-8 字节, IV 为 login_iv
         的 16 位十六进制(左补零)字节;
      4. 返回 Base64(密文)。

    Raises:
        RuntimeError: cryptography 不可用或参数非法。
    """
    if not _CRYPTO_OK:
        raise RuntimeError("缺少 cryptography 依赖, 无法执行密码登录加密")

    digest_hex = hashlib.sha256((str(salt) + str(password)).encode("utf-8")).hexdigest()
    key_bytes = str(encrypt_key).encode("utf-8")
    if len(key_bytes) != 24:
        raise RuntimeError(f"密码加密密钥长度异常: {len(key_bytes)} 字节(期望 24)")

    iv = bytes.fromhex(format(int(login_iv), "016x"))
    data = (digest_hex + str(random_text)).encode("utf-8")

    # cryptography 的 Cipher 不做自动填充, 需显式 PKCS7(块大小 8 字节)
    padder = _sym_padding.PKCS7(TripleDES.block_size).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = Cipher(TripleDES(key_bytes), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode("ascii")


def fetch_pwd_login_info(token: str, phone: str) -> dict:
    """获取密码登录所需的加密参数(GET /api/v2/user-pwd-info)。

    注意: 该接口要求携带有效 token(服务端未登录时返回 401 invalid token)。

    Returns:
        {"ok": bool, "data": dict, "message": str}
    """
    pnum = normalize_phone(phone)
    if not pnum:
        return {"ok": False, "data": {}, "message": "手机号格式不正确"}
    if not token:
        return {"ok": False, "data": {},
                "message": "当前登录已失效, 请先用短信验证码登录后再使用密码登录"}

    try:
        resp = requests.get(
            f"{API_BASE}/api/v2/user-pwd-info",
            headers={"Authorization": f"Bearer {token}"},
            params={"phone": pnum, "ctcode": CTCODE},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        return {"ok": False, "data": {}, "message": f"网络异常, 请稍后重试: {e}"}

    obj = parse_response(resp.text)
    if resp.status_code < 300 and obj:
        return {"ok": True, "data": obj, "message": ""}
    return {"ok": False, "data": {},
            "message": _error_message(obj, resp) or "获取密码登录参数失败"}


def login_with_password(token: str, phone: str, password: str) -> dict:
    """用手机号 + 密码换取登录 token(阻塞式)。

    Args:
        token: 当前有效的登录 token(仅用于获取加密参数; 登录成功后会返回新 token)
        phone: 手机号
        password: 账号密码

    Returns:
        {"ok": bool, "token": str, "message": str}
    """
    pnum = normalize_phone(phone)
    if not pnum:
        return {"ok": False, "token": "", "message": "手机号格式不正确"}
    if not password:
        return {"ok": False, "token": "", "message": "请输入密码"}

    info = fetch_pwd_login_info(token, pnum)
    if not info.get("ok"):
        return {"ok": False, "token": "", "message": info.get("message", "获取加密参数失败")}

    data = info["data"]
    if data.get("has_pwd") is False:
        return {"ok": False, "token": "", "message": "该账号未设置密码, 请使用短信验证码登录"}
    required = ("user_pwd_login_salt", "pwd_encrypt_key", "login_iv", "login_random_text")
    if not all(data.get(k) for k in required):
        return {"ok": False, "token": "",
                "message": "密码登录参数不完整, 请稍后重试或改用短信验证码登录"}

    try:
        pwd = build_pwd_cipher(data["user_pwd_login_salt"], data["pwd_encrypt_key"],
                              data["login_iv"], data["login_random_text"], password)
    except (KeyError, ValueError, RuntimeError) as e:
        logger.error("密码加密失败: %s", e)
        return {"ok": False, "token": "", "message": f"密码加密失败: {e}"}

    payload = {"phone": pnum, "ctcode": CTCODE, "pwd": pwd, "login_mode": "password"}
    try:
        resp = requests.post(
            f"{API_BASE}/api/v1/user-pwd-tokens",
            headers={"Content-Type": "application/json;charset=utf-8"},
            data=json.dumps(payload),
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        return {"ok": False, "token": "", "message": f"网络异常, 请稍后重试: {e}"}

    obj = parse_response(resp.text)
    new_token = str(obj.get("token") or "").strip()
    if resp.status_code < 300 and new_token:
        logger.info("密码登录成功: %s", mask_phone(pnum))
        return {"ok": True, "token": new_token, "message": "登录成功"}

    reason = _error_message(obj, resp)
    logger.warning("密码登录失败: HTTP %s %s", resp.status_code, reason)
    return {"ok": False, "token": "", "message": f"登录失败: {reason}"}


# --------------------------------------------------------------------------- #
# 每日签到(福利时长)                                                             #
# --------------------------------------------------------------------------- #
def netease_signin(token: str) -> dict:
    """执行网易云游戏每日签到(POST /api/v2/sign-today)。

    请求体需按响应的偏移 key 混淆(空对象即可), 因此先请求一次 users/@me
    推导偏移 key, 再提交签到。

    Returns:
        {"ok": bool, "message": str, "awards": list}
    """
    if not token:
        return {"ok": False, "message": "缺少登录 token, 请先登录", "awards": []}

    headers = {"Authorization": f"Bearer {token}"}
    # 1) 推导偏移 key(签名接口的响应为混淆数据)
    try:
        probe = requests.get(f"{API_BASE}/api/v2/users/@me", headers=headers,
                             timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        return {"ok": False, "message": f"网络异常, 请稍后重试: {e}", "awards": []}

    offset = derive_offset_key(probe.text)
    if offset is None:
        logger.warning("无法推导偏移 key, 签到可能失败(响应: %s)", (probe.text or "")[:120])
        return {"ok": False, "message": "登录状态异常(无法解析接口响应), 请重新登录", "awards": []}

    # 2) 提交签到(空对象同样需要混淆)
    try:
        resp = requests.post(
            f"{API_BASE}/api/v2/sign-today",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/octet-stream"},
            data=encode_mess("{}", offset),
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        return {"ok": False, "message": f"网络异常, 请稍后重试: {e}", "awards": []}

    text = decode_mess(resp.text, offset)
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None

    if resp.status_code < 300:
        awards = parsed if isinstance(parsed, list) else []
        titles = [str(a.get("sign_msg") or a.get("title") or "") for a in awards if isinstance(a, dict)]
        message = "; ".join(t for t in titles if t) or "签到成功"
        logger.info("签到成功: %s", message)
        return {"ok": True, "message": message, "awards": awards}

    # 失败: 提取服务端错误描述(如"今日已签到")
    reason = ""
    if isinstance(parsed, dict):
        reason = str(parsed.get("errmsgcn") or parsed.get("errmsg") or "")
    if not reason:
        reason = (text or "")[:200] or f"HTTP {resp.status_code}"
    logger.warning("签到失败: HTTP %s %s", resp.status_code, reason)
    return {"ok": False, "message": reason, "awards": []}


def _error_message(obj: Dict, resp) -> str:
    """从接口响应里提取可展示的错误描述(带兜底)。"""
    if isinstance(obj, dict):
        reason = str(obj.get("errmsgcn") or obj.get("errmsg")
                     or obj.get("message") or obj.get("error") or "").strip()
        if reason:
            return reason
    body = (getattr(resp, "text", "") or "")[:200].strip()
    if body and not body.startswith("{"):
        return body
    return f"HTTP {getattr(resp, 'status_code', 0)}"


# --------------------------------------------------------------------------- #
# token 文件读写                                                                #
# --------------------------------------------------------------------------- #
def save_token(token: str, path: str) -> bool:
    """把 token 原子化写入文件(先写临时文件再替换, 避免写一半损坏)。

    Returns:
        True 写入成功; False 写入失败(调用方仅告警, 不影响本次会话使用)
    """
    if not token:
        return False
    try:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="token.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(token)
            os.replace(tmp_path, path)
        except Exception:
            # 清理残留临时文件后继续上抛, 由外层统一降级为告警
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        logger.info("token 已保存: %s", path)
        return True
    except OSError as e:
        logger.error("token 落盘失败: %s", e)
        return False


def read_token(path: str) -> Optional[str]:
    """读取 token 文件(不存在/读失败返回 None)。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            token = f.read().strip()
        return token or None
    except OSError:
        return None