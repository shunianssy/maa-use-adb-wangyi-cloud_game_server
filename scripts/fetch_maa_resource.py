#!/usr/bin/env python3
"""
MAA 核心与资源自动拉取脚本(容器 / 服务器部署用)。

功能概览:
    1. 从 MAA 官方版本 API 解析「Linux x64 动态库包」下载地址
       (包内同时含 libMaaCore.so 与官方 resource, 一次拉取即可运行官方任务);
    2. 支持 --url 显式指定压缩包, 以及 --mirror 镜像前缀(国内加速);
    3. 流式下载(断点续传 / 大小校验 / 失败重试)后安全解压;
    4. 目录规范化: 无论压缩包内层级如何, 最终保证目标目录结构为
           <dest>/libMaaCore.so   (MaaCore 动态库, 对应环境变量 MAA_LIB_DIR)
           <dest>/resource/       (官方资源, 对应环境变量 MAA_DATA_DIR)
       MaaCore 运行期还会在 <dest>/debug/ 下写用户数据。

下载源与通道:
    1. 地址: --url / MAA_RESOURCE_URL 优先; 否则查官方版本 API
       (--api / MAA_RESOURCE_API, 默认 stable 渠道)并匹配资产
       MAA-*-linux-x86_64.tar.gz(--asset-pattern 可改)
    2. 通道: 设置 --mirror / MAA_RESOURCE_MIRROR 时镜像优先、直连兜底;
       未设置时直连优先, 失败后自动切换内置镜像(ghfast.top / gh-proxy.com /
       ghproxy.net), 可用 --no-mirror-fallback 关闭自动切换

用法示例:
    python scripts/fetch_maa_resource.py                       # 拉到 ./maa_data
    python scripts/fetch_maa_resource.py ./maa_data --force    # 强制重新拉取
    MAA_RESOURCE_MIRROR=https://ghfast.top/ \
        python scripts/fetch_maa_resource.py                   # 国内镜像加速

退出码:
    0  成功(含"已安装, 跳过")
    2  参数/下载/解压/校验失败
    130 用户中断(Ctrl+C)
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import shutil
import sys
import tarfile
import time
import urllib.request
import zipfile
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger("fetch-maa")

# --- 常量配置 ---------------------------------------------------------------
# 默认目标目录(容器内挂载到 /app/maa_data)
DEFAULT_DEST = "maa_data"
# 官方版本 API(渠道模板: stable / beta / alpha)
DEFAULT_API = "https://api.maa.plus/MaaAssistantArknights/api/version/{channel}.json"
# 默认资产匹配模式: Linux x64 动态库包(内含 libMaaCore.so + resource)
DEFAULT_ASSET_PATTERN = "MAA-*-linux-x86_64.tar.gz"
# 动态库文件名候选(Linux 优先, 兼容 Windows 包以便本机复用同一脚本)
LIB_NAMES = ("libMaaCore.so", "MaaCore.dll")
# 内置镜像前缀(仅当直连失败时作为兜底, 国内网络常用; 可用 --no-mirror-fallback 关闭)
DEFAULT_MIRRORS = (
    "https://ghfast.top/",
    "https://gh-proxy.com/",
    "https://ghproxy.net/",
)
# 资源目录标志: 官方 MAA 用 tasks/, MaaFramework bundle 用 pipeline/
RESOURCE_MARKERS = ("tasks", "pipeline")
# 流式下载分块与进度日志间隔
CHUNK_SIZE = 1 << 20          # 1 MiB
PROGRESS_STEP = 32 << 20      # 每 32 MiB 打印一次进度
# 解压后向内搜索包根的层数上限(压缩包常见为 <name>/... 一层)
SEARCH_MAX_DEPTH = 3


class FetchError(RuntimeError):
    """拉取/解压/校验过程中的可预期错误。"""


# --- 校验类工具 -------------------------------------------------------------
def is_valid_resource_dir(path: str) -> bool:
    """判断目录是否为可用的 MAA 资源目录(含 tasks/ 或 pipeline/ 子目录)。"""
    if not os.path.isdir(path):
        return False
    return any(os.path.isdir(os.path.join(path, marker)) for marker in RESOURCE_MARKERS)


def is_installed(dest: str, lib_names: Sequence[str] = LIB_NAMES) -> bool:
    """判断目标目录是否已安装完成(动态库 + resource/ 均就位)。

    Args:
        dest: 目标目录
        lib_names: 动态库文件名候选

    Returns:
        True 表示已安装且结构正确, 无需重新拉取。
    """
    has_lib = any(os.path.isfile(os.path.join(dest, name)) for name in lib_names)
    return has_lib and is_valid_resource_dir(os.path.join(dest, "resource"))


# --- 下载源解析 -------------------------------------------------------------
def parse_api_asset(api_json: dict, pattern: str) -> Tuple[str, str, int]:
    """从官方版本 API JSON 中挑选匹配的资产。

    API 结构(MAA OTA): {"version": "v6.x.y", "details": {"assets": [
        {"name": ..., "size": ..., "browser_download_url": ..., "mirrors": [...]}]}}

    Args:
        api_json: 已解析的 API JSON
        pattern: 资产名 fnmatch 模式, 如 MAA-*-linux-x86_64.tar.gz

    Returns:
        (资产名, 下载地址, 字节大小)

    Raises:
        FetchError: 无匹配资产或字段缺失。
    """
    version = str(api_json.get("version") or "")
    details = api_json.get("details") if isinstance(api_json.get("details"), dict) else {}
    assets: List[dict] = []
    for source in (details.get("assets"), api_json.get("assets")):
        if isinstance(source, list):
            assets.extend(item for item in source if isinstance(item, dict))

    matched = [a for a in assets if fnmatch.fnmatch(str(a.get("name", "")), pattern)]
    if not matched:
        names = ", ".join(str(a.get("name")) for a in assets) or "(空)"
        raise FetchError(f"版本 API 中无匹配资产: pattern={pattern}; 可选: {names}")

    # 优先精确命中当前版本号的资产, 避免匹配到历史残留资产
    if version:
        exact = [a for a in matched if version in str(a.get("name", ""))]
        matched = exact or matched

    asset = matched[0]
    url = str(asset.get("browser_download_url") or "")
    if not url:
        raise FetchError(f"资产 {asset.get('name')} 缺少 browser_download_url")
    logger.info("版本 API 命中资产: %s (%.1f MiB, MAA %s)",
                asset.get("name"), int(asset.get("size") or 0) / 1048576, version or "?")
    return str(asset.get("name")), url, int(asset.get("size") or 0)


def build_candidates(url: str, mirror: str = "", allow_default_mirrors: bool = True) -> List[str]:
    """构造候选下载地址列表(按优先级排序, 直连兜底)。

    Args:
        url: 原始下载地址
        mirror: 用户指定镜像前缀, 如 https://ghfast.top/(设置后优先使用)
        allow_default_mirrors: 是否在直连失败后追加内置镜像兜底

    Returns:
        去重后的候选地址列表。
    """
    candidates: List[str] = []

    def add(prefix: str) -> None:
        candidate = prefix.rstrip("/") + "/" + url if prefix else url
        if candidate not in candidates:
            candidates.append(candidate)

    if mirror:
        add(mirror)                      # 用户指定镜像优先
        add("")                          # 再回退直连
    else:
        add("")                          # 直连优先
        if allow_default_mirrors:
            for prefix in DEFAULT_MIRRORS:
                add(prefix)              # 直连不可用时自动切镜像(国内网络)
    return candidates


def resolve_download_url(args: argparse.Namespace) -> Tuple[str, int]:
    """确定下载地址: 显式 URL 优先, 否则查询官方版本 API。

    Returns:
        (下载地址, 预期字节大小; 大小未知时为 0)
    """
    explicit = args.url or os.environ.get("MAA_RESOURCE_URL", "").strip()
    if explicit:
        logger.info("使用显式下载地址: %s", explicit)
        return explicit, 0

    api_url = (args.api or os.environ.get("MAA_RESOURCE_API", "")).strip() or \
        DEFAULT_API.format(channel=args.channel)
    logger.info("查询 MAA 版本 API: %s", api_url)
    try:
        with _open(api_url, args.timeout) as resp:
            api_json = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # 网络异常 / JSON 解析失败
        raise FetchError(f"版本 API 请求失败({api_url}): {e}") from e

    name, url, size = parse_api_asset(api_json, args.asset_pattern)
    logger.info("目标资产: %s", name)
    return url, size


# --- 网络与解压 -------------------------------------------------------------
def _open(url: str, timeout: int, headers: Optional[dict] = None):
    """统一入口: 便于测试替换(monkeypatch)与统一 UA/超时/Range 头。"""
    merged = {"User-Agent": "netease-maa-fetch/1.0"}
    merged.update(headers or {})
    return urllib.request.urlopen(urllib.request.Request(url, headers=merged), timeout=timeout)


def _download_once(url: str, target_file: str, timeout: int, expect_size: int) -> int:
    """单次下载(支持断点续传), 返回本地文件总大小。

    续传规则: 本地已有 `.part` 且小于预期时携带 `Range`; 服务端未返回 206(不支持续传)
    则从头覆盖写入, 保证文件内容完整。

    Raises:
        FetchError: 下载大小与预期不符(半截包不允许进入解压环节)。
    """
    resumed = os.path.getsize(target_file) if os.path.isfile(target_file) else 0
    if expect_size and resumed >= expect_size:
        resumed = 0  # 异常残留, 重新下载
    headers = {"Range": f"bytes={resumed}-"} if resumed else None

    started = time.time()
    with _open(url, timeout, headers) as resp:
        status = int(getattr(resp, "status", None) or resp.getcode() or 200)
        if resumed and status != 206:
            logger.info("服务端未返回 206(不支持续传), 从头下载")
            resumed = 0
        elif resumed:
            logger.info("断点续传: 从 %.1f MiB 处继续", resumed / 1048576)

        total = expect_size or (int(resp.headers.get("Content-Length") or 0) + resumed)
        written = 0
        last_log = 0
        with open(target_file, "ab" if resumed else "wb") as fh:
            while True:  # 流式读取, 避免大文件占满内存
                chunk = resp.read(CHUNK_SIZE)
                if not chunk:
                    break
                fh.write(chunk)
                written += len(chunk)
                if written - last_log >= PROGRESS_STEP:
                    last_log = written
                    _log_progress(resumed + written, total, started)

    size = resumed + written
    if total and size != total:
        raise FetchError(f"下载大小不符: {size} != {total}")
    _log_progress(size, total or size, started, final=True)
    return size


def download(url: str, target_file: str, timeout: int, retries: int,
             mirror: str = "", expect_size: int = 0,
             allow_default_mirrors: bool = True) -> str:
    """下载到本地文件, 支持镜像回退、重试与断点续传。

    Args:
        url: 原始下载地址
        target_file: 保存路径(.part 临时名, 失败的已下载部分会被保留用于续传)
        timeout: 单次请求超时(秒)
        retries: 整体重试次数(每轮会依次尝试候选地址)
        mirror: 用户指定镜像前缀(优先于直连)
        expect_size: 预期大小(字节, 0 表示未知), 用于校验与进度展示
        allow_default_mirrors: 直连失败后是否自动切换内置镜像

    Returns:
        实际写入字节数

    Raises:
        FetchError: 所有候选均失败。
    """
    candidates = build_candidates(url, mirror, allow_default_mirrors)
    last_error: Optional[Exception] = None

    for attempt in range(1, max(1, retries) + 1):
        for candidate in candidates:
            try:
                logger.info("开始下载(第 %d 轮): %s", attempt, candidate)
                return _download_once(candidate, target_file, timeout, expect_size)
            except Exception as e:  # noqa: BLE001 - 需对镜像/直连逐个兜底
                last_error = e
                logger.warning("下载失败(%s): %s", candidate, e)

    _remove_quietly(target_file)
    raise FetchError(f"下载失败(已尝试 {len(candidates) * max(1, retries)} 次): {last_error}")


def _log_progress(written: int, total: int, started: float, final: bool = False) -> None:
    """打印下载进度(百分比 + 速率)。"""
    speed = written / max(0.001, time.time() - started) / 1048576
    if total:
        logger.info("下载进度: %.1f/%.1f MiB (%d%%, %.1f MiB/s)",
                    written / 1048576, total / 1048576, written * 100 // max(1, total), speed)
    elif final:
        logger.info("下载完成: %.1f MiB (%.1f MiB/s)", written / 1048576, speed)


def _is_within(base: str, candidate: str) -> bool:
    """防目录穿越: 校验 candidate 是否位于 base 内。"""
    base_abs = os.path.abspath(base)
    cand_abs = os.path.abspath(candidate)
    return cand_abs == base_abs or cand_abs.startswith(base_abs + os.sep)


def extract_archive(archive: str, target: str) -> None:
    """安全解压 zip / tar.gz 到目标目录。

    Args:
        archive: 压缩包路径
        target: 解压目标目录(需已存在)

    Raises:
        FetchError: 不支持的格式或包含非法路径。
    """
    logger.info("解压: %s -> %s", os.path.basename(archive), target)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                if not _is_within(target, os.path.join(target, member.filename)):
                    raise FetchError(f"压缩包含非法路径: {member.filename}")
            zf.extractall(target)
        return

    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                if not _is_within(target, os.path.join(target, member.name)):
                    raise FetchError(f"压缩包含非法路径: {member.name}")
            # Python 3.12+ 支持 data 过滤器(自动剥离危险属性); 老版本回退普通解压
            try:
                tf.extractall(target, filter="data")
            except TypeError:
                tf.extractall(target)
        return

    raise FetchError(f"不支持的压缩包格式: {archive}")


# --- 目录规范化 -------------------------------------------------------------
def find_package_root(root: str, lib_names: Sequence[str] = LIB_NAMES) -> Optional[str]:
    """在解压结果中定位「包根」: 包含动态库的目录。

    Returns:
        包根目录路径; 未找到返回 None。
    """
    queue: List[Tuple[str, int]] = [(root, 0)]
    while queue:
        current, depth = queue.pop(0)
        if any(os.path.isfile(os.path.join(current, name)) for name in lib_names):
            return current
        if depth >= SEARCH_MAX_DEPTH:
            continue
        for entry in sorted(os.listdir(current)):
            child = os.path.join(current, entry)
            if os.path.isdir(child):
                queue.append((child, depth + 1))
    return None


def find_resource_dir(root: str) -> Optional[str]:
    """在解压结果中定位资源目录(含 tasks/ 或 pipeline/)。

    优先匹配名为 resource 的目录, 其次接受任意合法资源目录。
    """
    found: List[str] = []
    queue: List[Tuple[str, int]] = [(root, 0)]
    while queue:
        current, depth = queue.pop(0)
        for entry in sorted(os.listdir(current)):
            child = os.path.join(current, entry)
            if not os.path.isdir(child):
                continue
            if entry == "resource" and is_valid_resource_dir(child):
                return child
            if depth < SEARCH_MAX_DEPTH:
                queue.append((child, depth + 1))
        if is_valid_resource_dir(current):
            found.append(current)
    return found[0] if found else None


def _merge_move(src: str, dst: str) -> None:
    """把 src 内容合并移动到 dst(同名目录递归合并, 其余直接覆盖)。"""
    os.makedirs(dst, exist_ok=True)
    for entry in os.listdir(src):
        src_item = os.path.join(src, entry)
        dst_item = os.path.join(dst, entry)
        if os.path.isdir(src_item) and os.path.isdir(dst_item):
            _merge_move(src_item, dst_item)
            _remove_quietly(src_item)
        else:
            if os.path.isdir(dst_item):
                _remove_quietly(dst_item)
            elif os.path.exists(dst_item):
                os.remove(dst_item)
            shutil.move(src_item, dst_item)


def _remove_quietly(path: str) -> None:
    """删除文件/目录, 失败不抛异常(清理临时产物用)。"""
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            os.remove(path)
    except OSError as e:
        logger.debug("清理失败(忽略): %s -> %s", path, e)


def install_from_extract(extracted: str, dest: str,
                         lib_names: Sequence[str] = LIB_NAMES) -> None:
    """把解压结果规范化安装到目标目录。

    最终结构: <dest>/<lib> + <dest>/resource/ ; 其余随包文件(依赖库等)一并保留。

    Raises:
        FetchError: 未找到动态库或资源目录。
    """
    pkg_root = find_package_root(extracted, lib_names)
    if not pkg_root:
        raise FetchError(f"解压结果中未找到动态库({', '.join(lib_names)}), 请检查下载源")

    resource_dir = find_resource_dir(extracted)
    if not resource_dir:
        raise FetchError("解压结果中未找到资源目录(需含 tasks/ 或 pipeline/)")

    os.makedirs(dest, exist_ok=True)
    logger.info("规范化安装: %s -> %s", pkg_root, dest)
    _merge_move(pkg_root, dest)

    # 资源目录可能已随包根一起合并到 dest/resource; 仅当它不在目标位置时才移动
    target_resource = os.path.join(dest, "resource")
    if not is_valid_resource_dir(target_resource) and os.path.isdir(resource_dir):
        logger.info("移动资源目录: %s -> %s", resource_dir, target_resource)
        if os.path.isdir(target_resource):
            _remove_quietly(target_resource)
        shutil.move(resource_dir, target_resource)

    if not is_installed(dest, lib_names):
        raise FetchError(f"安装校验失败: {dest} 缺少动态库或 resource/, 请检查压缩包内容")


# --- 主流程 -----------------------------------------------------------------
def _resource_version(dest: str) -> str:
    """读取 resource/version.json 中的资源版本信息(缺失时返回空串)。

    官方 MAA 该文件的实际字段为 last_updated(资源更新时间, 另含活动/卡池信息),
    这里优先取 last_updated, 并兼容自定义包可能使用的 version 字段。
    """
    try:
        with open(os.path.join(dest, "resource", "version.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return str(data.get("last_updated") or data.get("version") or "")
    except Exception:
        return ""


def fetch(args: argparse.Namespace) -> int:
    """执行完整拉取流程, 返回进程退出码。"""
    dest = os.path.abspath(args.dest)

    # 1) 幂等: 已安装则直接跳过(除非 --force)
    if not args.force and is_installed(dest):
        _remove_quietly(os.path.join(dest, ".maa_download"))  # 清理历史残留分片, 释放空间
        logger.info("已安装 MAA(MaaCore + resource): %s (资源更新 %s), 跳过下载",
                    dest, _resource_version(dest) or "未知")
        return 0

    # 2) 解析下载地址
    url, size = resolve_download_url(args)
    if size:
        logger.info("预计大小: %.1f MiB", size / 1048576)
    logger.info("下载策略: %s", "镜像优先(" + args.mirror + ")" if args.mirror
                else ("仅直连" if args.no_mirror_fallback else "直连优先, 失败自动切换内置镜像"))

    # 3) 下载到 <dest>/.maa_download/(卷内持久化, 便于跨次断点续传)
    #    解压也放在该目录内, 安装成功后整体清理; 下载失败则保留分片供下次续传
    download_dir = os.path.join(dest, ".maa_download")
    os.makedirs(download_dir, exist_ok=True)
    archive = os.path.join(download_dir, os.path.basename(url.split("?")[0]) or "maa_package")
    extracted = os.path.join(download_dir, "extract")

    try:
        download(url, archive, args.timeout, args.retries, args.mirror, size,
                 allow_default_mirrors=not args.no_mirror_fallback)
    except FetchError:
        logger.warning("下载未完成: 已保留分片 %s, 下次启动会自动断点续传", download_dir)
        raise

    try:
        os.makedirs(extracted, exist_ok=True)
        extract_archive(archive, extracted)
        install_from_extract(extracted, dest)
    except FetchError:
        _remove_quietly(download_dir)  # 包体损坏/结构异常: 清理后下次重新下载
        raise

    _remove_quietly(download_dir)

    logger.info("完成: MaaCore 已安装到 %s (资源更新 %s)",
                dest, _resource_version(dest) or "未知")
    logger.info("容器内对应环境变量: MAA_LIB_DIR=%s MAA_DATA_DIR=%s", dest, dest)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="拉取并安装 MAA(MaaCore + 官方 resource), 供容器/服务器部署使用")
    parser.add_argument("dest", nargs="?", default=os.environ.get("MAA_RESOURCE_DEST", DEFAULT_DEST),
                        help=f"安装目标目录, 默认 {DEFAULT_DEST}")
    parser.add_argument("--url", default="", help="显式压缩包地址(zip/tar.gz), 跳过版本 API")
    parser.add_argument("--mirror", default=os.environ.get("MAA_RESOURCE_MIRROR", ""),
                        help="下载镜像前缀, 如 https://ghfast.top/ (也可用 MAA_RESOURCE_MIRROR)")
    parser.add_argument("--no-mirror-fallback", action="store_true",
                        help="直连失败时不自动尝试内置镜像(默认会尝试)")
    parser.add_argument("--api", default="", help="版本 API 地址(默认官方 stable 渠道)")
    parser.add_argument("--channel", default="stable", choices=["stable", "beta", "alpha"],
                        help="版本渠道, 默认 stable")
    parser.add_argument("--asset-pattern", default=DEFAULT_ASSET_PATTERN,
                        help=f"资产名匹配模式, 默认 {DEFAULT_ASSET_PATTERN}")
    parser.add_argument("--timeout", type=int, default=60, help="单次请求超时秒数, 默认 60")
    parser.add_argument("--retries", type=int, default=3, help="下载重试轮数, 默认 3")
    parser.add_argument("--force", action="store_true", help="已安装时强制重新拉取")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="日志级别, 默认 INFO")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """脚本入口: 统一异常处理与退出码。"""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        return fetch(args)
    except FetchError as e:
        logger.error("拉取失败: %s", e)
        return 2
    except KeyboardInterrupt:
        logger.warning("已中断, 临时文件已清理")
        return 130
    except Exception as e:  # 兜底: 避免容器入口脚本因未捕获异常丢失日志
        logger.exception("未预期错误: %s", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())