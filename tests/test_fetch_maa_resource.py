"""
scripts/fetch_maa_resource.py 单元测试(全部离线, 不发起真实网络请求)。

覆盖:
    - 下载候选地址构造(直连优先 / 镜像优先 / 关闭回退)
    - 版本 API 资产解析(命中 Linux 包、版本精确匹配、无匹配报错)
    - 安装校验(动态库 + resource/)
    - 安全解压(拒绝目录穿越)
    - 目录规范化(单层嵌套包 / 库与资源分离包 / 缺库或缺资源报错)
    - fetch 主流程(已安装幂等跳过、联网下载安装、镜像回退重试)

运行(标准库 unittest):
    python -m unittest discover -s tests -v
"""

import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import fetch_maa_resource as fmr  # noqa: E402  (需先补 sys.path)

ASSET_URL = "https://github.com/MaaAssistantArknights/MaaAssistantArknights/" \
            "releases/download/v6.18.0/MAA-v6.18.0-linux-x86_64.tar.gz"


def _api_json(version: str = "v6.18.0", size: int = 200) -> dict:
    """构造与 MAA OTA 一致的版本 API 样例数据(含新旧两个 Linux 资产)。

    Args:
        version: 模拟的当前版本号
        size: Linux 资产声明大小(流程测试需与真实负载一致, 否则会触发大小校验失败)
    """
    return {
        "version": version,
        "details": {
            "assets": [
                {
                    "name": "MAA-v6.0.0-linux-x86_64.tar.gz",
                    "size": 100,
                    "browser_download_url": ASSET_URL.replace("v6.18.0", "v6.0.0"),
                    "mirrors": [],
                },
                {
                    "name": f"MAA-{version}-linux-x86_64.tar.gz",
                    "size": size,
                    "browser_download_url": ASSET_URL,
                    "mirrors": [],
                },
                {
                    "name": f"MAA-{version}-win-x64.zip",
                    "size": 300,
                    "browser_download_url": ASSET_URL.replace("linux-x86_64.tar.gz", "win-x64.zip"),
                    "mirrors": [],
                },
            ]
        },
    }


def _make_tree(root: str, entries: dict) -> None:
    """按 {相对路径: 内容} 生成目录与文件。"""
    for rel, content in entries.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content if isinstance(content, bytes) else content.encode())


def _pack_targz(src_dir: str, archive: str) -> None:
    """把 src_dir 内容打包为 tar.gz(条目名以 ./ 开头, 与官方包一致)。"""
    with tarfile.open(archive, "w:gz") as tf:
        for entry in sorted(os.listdir(src_dir)):
            tf.add(os.path.join(src_dir, entry), arcname="./" + entry)


class FakeResponse:
    """最小可用的 urlopen 返回对象(read / headers / status / 上下文管理)。"""

    def __init__(self, payload: bytes, status: int = 200):
        self._buf = io.BytesIO(payload)
        self.status = status
        self.headers = {"Content-Length": str(len(payload))}

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestBuildCandidates(unittest.TestCase):
    """下载候选地址构造。"""

    def test_direct_first_then_builtin_mirrors(self):
        """未指定镜像: 直连优先, 内置镜像兜底。"""
        candidates = fmr.build_candidates(ASSET_URL)
        self.assertEqual(candidates[0], ASSET_URL)
        self.assertEqual(candidates[1], fmr.DEFAULT_MIRRORS[0].rstrip("/") + "/" + ASSET_URL)

    def test_mirror_first_when_configured(self):
        """显式镜像: 镜像优先, 直连兜底。"""
        candidates = fmr.build_candidates(ASSET_URL, mirror="https://ghfast.top/")
        self.assertEqual(candidates, ["https://ghfast.top/" + ASSET_URL, ASSET_URL])

    def test_disable_default_mirrors(self):
        """关闭回退: 仅保留直连。"""
        candidates = fmr.build_candidates(ASSET_URL, mirror="", allow_default_mirrors=False)
        self.assertEqual(candidates, [ASSET_URL])

    def test_no_duplicate_entries(self):
        """用户镜像等于内置镜像时不重复。"""
        candidates = fmr.build_candidates(ASSET_URL, mirror=fmr.DEFAULT_MIRRORS[0])
        self.assertEqual(len(candidates), 2)  # 镜像 + 直连


class TestParseApiAsset(unittest.TestCase):
    """版本 API 资产解析。"""

    def test_picks_linux_asset(self):
        name, url, size = fmr.parse_api_asset(_api_json(), fmr.DEFAULT_ASSET_PATTERN)
        self.assertEqual(name, "MAA-v6.18.0-linux-x86_64.tar.gz")
        self.assertEqual(url, ASSET_URL)
        self.assertEqual(size, 200)

    def test_prefers_exact_version(self):
        """多个匹配时优先当前版本(避免匹配到历史资产)。"""
        data = _api_json("v6.18.0")
        data["details"]["assets"].append({
            "name": "MAA-v6.19.0-linux-x86_64.tar.gz", "size": 400,
            "browser_download_url": ASSET_URL.replace("6.18.0", "6.19.0"), "mirrors": [],
        })
        name, _, _ = fmr.parse_api_asset(data, fmr.DEFAULT_ASSET_PATTERN)
        self.assertEqual(name, "MAA-v6.18.0-linux-x86_64.tar.gz")

    def test_raises_when_no_match(self):
        with self.assertRaises(fmr.FetchError):
            fmr.parse_api_asset(_api_json(), "MAA-*-macos-*.dmg")


class TestValidation(unittest.TestCase):
    """资源目录 / 安装完成度校验。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="maa_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_valid_resource_dir(self):
        resource = os.path.join(self.tmp, "resource")
        os.makedirs(os.path.join(resource, "tasks"))
        self.assertTrue(fmr.is_valid_resource_dir(resource))

        pipeline = os.path.join(self.tmp, "bundle")
        os.makedirs(os.path.join(pipeline, "pipeline"))
        self.assertTrue(fmr.is_valid_resource_dir(pipeline))

    def test_invalid_resource_dir(self):
        self.assertFalse(fmr.is_valid_resource_dir(self.tmp))
        self.assertFalse(fmr.is_valid_resource_dir(os.path.join(self.tmp, "missing")))

    def test_is_installed(self):
        os.makedirs(os.path.join(self.tmp, "resource", "tasks"))
        self.assertFalse(fmr.is_installed(self.tmp))                  # 缺动态库
        _make_tree(self.tmp, {"libMaaCore.so": b"x"})
        self.assertTrue(fmr.is_installed(self.tmp))


class TestExtract(unittest.TestCase):
    """安全解压。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="maa_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_zip_rejects_path_traversal(self):
        archive = os.path.join(self.tmp, "evil.zip")
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../evil.txt", b"boom")
        with self.assertRaises(fmr.FetchError):
            fmr.extract_archive(archive, os.path.join(self.tmp, "out"))

    def test_unsupported_format(self):
        archive = os.path.join(self.tmp, "plain.bin")
        _make_tree(self.tmp, {"plain.bin": b"not an archive"})
        with self.assertRaises(fmr.FetchError):
            fmr.extract_archive(archive, os.path.join(self.tmp, "out"))

    def test_targz_extracts(self):
        src = os.path.join(self.tmp, "src")
        _make_tree(src, {"libMaaCore.so": b"x", "resource/tasks/tasks.json": "{}"})
        archive = os.path.join(self.tmp, "pkg.tar.gz")
        _pack_targz(src, archive)

        out = os.path.join(self.tmp, "out")
        os.makedirs(out)
        fmr.extract_archive(archive, out)
        self.assertTrue(os.path.isfile(os.path.join(out, "libMaaCore.so")))


class TestInstall(unittest.TestCase):
    """目录规范化安装。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="maa_test_")
        self.dest = os.path.join(self.tmp, "maa_data")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _extract_dir(self, name: str, entries: dict) -> str:
        """生成解压目录(不打包, 直接模拟解压结果)。"""
        path = os.path.join(self.tmp, name)
        _make_tree(path, entries)
        return path

    def test_nested_single_root_layout(self):
        """官方包结构: <root>/<pkg>/..."。"""
        extracted = self._extract_dir("nested", {
            "MAA-v6.18.0-linux-x86_64/libMaaCore.so": b"core",
            "MAA-v6.18.0-linux-x86_64/libonnxruntime.so.1": b"onnx",
            "MAA-v6.18.0-linux-x86_64/resource/tasks/tasks.json": "{}",
            "MAA-v6.18.0-linux-x86_64/resource/version.json": '{"version": "v1"}',
        })
        fmr.install_from_extract(extracted, self.dest)
        self.assertTrue(fmr.is_installed(self.dest))
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "libonnxruntime.so.1")))
        self.assertEqual(fmr._resource_version(self.dest), "v1")

    def test_resource_version_prefers_last_updated(self):
        """官方 version.json 使用 last_updated 字段, 应优先取该字段。"""
        extracted = self._extract_dir("ver", {
            "libMaaCore.so": b"core",
            "resource/tasks/tasks.json": "{}",
            "resource/version.json": '{"last_updated": "2026-09-20 12:47:33.000", "version": "v9"}',
        })
        fmr.install_from_extract(extracted, self.dest)
        self.assertEqual(fmr._resource_version(self.dest), "2026-09-20 12:47:33.000")

    def test_flat_layout(self):
        """平铺结构: 压缩包根即包根(官方 Linux tar.gz 的实际结构)。"""
        extracted = self._extract_dir("flat", {
            "libMaaCore.so": b"core",
            "libMaaUtils.so": b"utils",
            "AsstCaller.h": b"header",
            "resource/tasks/tasks.json": "{}",
        })
        fmr.install_from_extract(extracted, self.dest)
        self.assertTrue(fmr.is_installed(self.dest))
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "AsstCaller.h")))

    def test_split_layout_lib_and_resource(self):
        """库与资源分离: bin/ 放库, 根放 resource/。"""
        extracted = self._extract_dir("split", {
            "pkg/bin/libMaaCore.so": b"core",
            "pkg/resource/pipeline/main.json": "{}",
        })
        fmr.install_from_extract(extracted, self.dest)
        self.assertTrue(fmr.is_installed(self.dest))
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "libMaaCore.so")))

    def test_missing_lib_raises(self):
        extracted = self._extract_dir("nolib", {"resource/tasks/tasks.json": "{}"})
        with self.assertRaises(fmr.FetchError):
            fmr.install_from_extract(extracted, self.dest)

    def test_missing_resource_raises(self):
        extracted = self._extract_dir("nores", {"libMaaCore.so": b"core"})
        with self.assertRaises(fmr.FetchError):
            fmr.install_from_extract(extracted, self.dest)


class TestDownloadResume(unittest.TestCase):
    """断点续传行为(弱网环境下 200MB+ 包体的关键保障)。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="maa_test_")
        self.target = os.path.join(self.tmp, "pkg.part")
        self.payload = b"A" * 4096 + b"B" * 4096
        self.url = "https://example.com/pkg.tar.gz"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_resume_from_partial_file(self):
        """已有分片时携带 Range, 且最终文件内容完整。"""
        half = len(self.payload) // 2
        with open(self.target, "wb") as fh:
            fh.write(self.payload[:half])

        seen = {}

        def fake_open(url: str, timeout: int, headers=None):
            seen["headers"] = headers or {}
            return FakeResponse(self.payload[half:], status=206)

        with mock.patch.object(fmr, "_open", side_effect=fake_open):
            fmr.download(self.url, self.target, timeout=5, retries=1,
                         expect_size=len(self.payload))

        self.assertEqual(seen["headers"].get("Range"), f"bytes={half}-")
        with open(self.target, "rb") as fh:
            self.assertEqual(fh.read(), self.payload)

    def test_restart_when_server_ignores_range(self):
        """服务端不支持续传(返回 200)时从头覆盖, 不产生拼接错位。"""
        with open(self.target, "wb") as fh:
            fh.write(b"garbage")

        def fake_open(url: str, timeout: int, headers=None):
            return FakeResponse(self.payload, status=200)

        with mock.patch.object(fmr, "_open", side_effect=fake_open):
            fmr.download(self.url, self.target, timeout=5, retries=1,
                         expect_size=len(self.payload))

        with open(self.target, "rb") as fh:
            self.assertEqual(fh.read(), self.payload)


class TestFetchFlow(unittest.TestCase):
    """fetch 主流程(网络层被 mock)。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="maa_test_")
        self.dest = os.path.join(self.tmp, "maa_data")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _args(self, *extra: str):
        return fmr.build_parser().parse_args([self.dest, *extra])

    def _package_bytes(self) -> bytes:
        """构造一个最小可用的官方风格 tar.gz。"""
        src = os.path.join(self.tmp, "pkg_src")
        _make_tree(src, {
            "libMaaCore.so": b"core",
            "resource/tasks/tasks.json": "{}",
            "resource/version.json": '{"version": "v-test"}',
        })
        archive = os.path.join(self.tmp, "pkg.tar.gz")
        _pack_targz(src, archive)
        with open(archive, "rb") as fh:
            return fh.read()

    def test_skips_when_installed(self):
        """已安装时应跳过下载(不做任何网络请求)。"""
        _make_tree(self.dest, {"libMaaCore.so": b"x", "resource/tasks/tasks.json": "{}"})
        with mock.patch.object(fmr, "_open", side_effect=AssertionError("不应发起网络请求")):
            self.assertEqual(fmr.fetch(self._args()), 0)

    def test_end_to_end_with_mocked_network(self):
        """完整流程: 版本 API -> 下载 -> 解压 -> 安装 -> 校验。"""
        payload = self._package_bytes()

        def fake_open(url: str, timeout: int, headers=None):
            if url.endswith("stable.json"):
                return FakeResponse(json.dumps(_api_json(size=len(payload))).encode())
            if url == ASSET_URL:
                return FakeResponse(payload)
            raise OSError(f"未预期的请求: {url}")

        with mock.patch.object(fmr, "_open", side_effect=fake_open):
            self.assertEqual(fmr.fetch(self._args()), 0)

        self.assertTrue(fmr.is_installed(self.dest))
        self.assertEqual(fmr._resource_version(self.dest), "v-test")
        # 成功安装后不应残留下载缓存
        self.assertFalse(os.path.exists(os.path.join(self.dest, ".maa_download")))

    def test_mirror_fallback_after_direct_failure(self):
        """直连失败后自动切换内置镜像。"""
        payload = self._package_bytes()
        mirror_url = fmr.DEFAULT_MIRRORS[0].rstrip("/") + "/" + ASSET_URL

        def fake_open(url: str, timeout: int, headers=None):
            if url.endswith("stable.json"):
                return FakeResponse(json.dumps(_api_json(size=len(payload))).encode())
            if url == ASSET_URL:
                raise OSError("connection reset")
            if url == mirror_url:
                return FakeResponse(payload)
            raise OSError(f"未预期的请求: {url}")

        with mock.patch.object(fmr, "_open", side_effect=fake_open):
            self.assertEqual(fmr.fetch(self._args()), 0)
        self.assertTrue(fmr.is_installed(self.dest))

    def test_size_mismatch_is_rejected(self):
        """声明大小与实际不符时应判定失败(防止半截包被安装)。"""
        payload = self._package_bytes()

        def fake_open(url: str, timeout: int, headers=None):
            if url.endswith("stable.json"):
                return FakeResponse(json.dumps(_api_json(size=len(payload) + 10)).encode())
            return FakeResponse(payload)

        with mock.patch.object(fmr, "_open", side_effect=fake_open):
            self.assertEqual(fmr.main([self.dest, "--retries", "1"]), 2)
        self.assertFalse(fmr.is_installed(self.dest))

    def test_download_failure_keeps_partial_for_resume(self):
        """下载失败时保留分片目录, 且不产生"已安装"错觉。"""
        with mock.patch.object(fmr, "_open", side_effect=OSError("network down")):
            self.assertEqual(fmr.main([self.dest, "--retries", "1"]), 2)
        self.assertFalse(fmr.is_installed(self.dest))


if __name__ == "__main__":
    unittest.main(verbosity=2)