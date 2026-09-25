"""
网易云游戏每日签到(平台奖励时长)调用。

说明: 网易云游戏的「福利-签到」接口未公开文档化, 端点可能随版本调整。
本模块实现为「探测式」调用:
- 依次尝试候选端点(带 Authorization: Bearer token);
- 任一候选返回 2xx 即视为成功, 其余候选记录失败原因;
- 端点列表可通过环境变量 NETEASE_SIGNIN_ENDPOINTS(逗号分隔)覆盖,
  也可通过传入参数指定——若已知精确接口, 请优先配置以直达。
"""

import json
import logging
import os
import time

import requests

logger = logging.getLogger("signin")

# 候选签到端点(按出现概率排序, 抓包后可替换/追加)
DEFAULT_ENDPOINTS: list = [
    "https://n.cg.163.com/api/v2/users/@me/sign-in",
    "https://n.cg.163.com/api/v2/sign-in",
    "https://n.cg.163.com/api/v1/users/@me/sign-in",
]


def _env_endpoints() -> list:
    """从环境变量 NETEASE_SIGNIN_ENDPOINTS 读取端点(逗号分隔, 空则忽略)。"""
    raw = os.environ.get("NETEASE_SIGNIN_ENDPOINTS", "").strip()
    if not raw:
        return []
    return [e.strip() for e in raw.split(",") if e.strip()]


def _headers(token: str) -> dict:
    return {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    }


def netease_signin(token: str, endpoints: list = None) -> dict:
    """执行签到, 返回结构化结果(不抛异常)。

    Returns:
        {"ok": bool, "endpoint": str, "status": int,
         "body": str, "message": str}
    """
    eps = (endpoints or []) or (_env_endpoints() or DEFAULT_ENDPOINTS)
    started = time.time()
    for url in eps:
        try:
            resp = requests.post(url, headers=_headers(token), timeout=10)
        except Exception as e:  # 网络/超时等: 记录后继续探测下一候选
            logger.warning("签到候选 %s 请求失败: %s", url, e)
            continue
        body = resp.text[:300]
        if resp.status_code < 300:
            logger.info("签到成功 endpoint=%s status=%s 耗时%.0fms",
                        url, resp.status_code, (time.time() - started) * 1000)
            return {
                "ok": True,
                "endpoint": url,
                "status": resp.status_code,
                "body": body,
                "message": "签到请求已成功发送",
            }
        logger.warning("签到候选 %s 返回 HTTP %s: %s", url, resp.status_code, body)
    return {
        "ok": False,
        "endpoint": "",
        "status": 0,
        "body": "",
        "message": "无可用签到端点: 可在环境变量 NETEASE_SIGNIN_ENDPOINTS 配置抓包到的真实接口",
    }