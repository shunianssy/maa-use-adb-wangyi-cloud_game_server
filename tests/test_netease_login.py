"""
netease_login 单元测试: 手机号规整、短信/密码登录、签到与 token 落盘。

全部通过 mock requests 完成, 不依赖真实网易云游戏接口;
密码加密使用与网页端 crypto-js 对照得到的固定向量, 防止实现回归。

运行: .venv\\Scripts\\python.exe -m unittest tests.test_netease_login -v
"""

import base64
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import netease_login as nl  # noqa: E402


def obfuscate(plain: str, key: int) -> str:
    """测试辅助: 按偏移 key 混淆明文(模拟服务端响应格式)。"""
    return base64.b64encode(
        bytes((b + key) % 256 for b in plain.encode("utf-8"))).decode("ascii")


class TestNormalizePhone(unittest.TestCase):
    def test_accepts_common_formats(self):
        # 前端常见输入: 纯数字 / 空格 / 横线 / 86 或 +86 前缀
        for raw in ("13800001111", " 138 0000 1111 ", "138-0000-1111",
                    "+8613800001111", "8613800001111"):
            self.assertEqual(nl.normalize_phone(raw), "13800001111", repr(raw))

    def test_rejects_invalid(self):
        for raw in ("", None, "1380000", "23800001111", "1380000111a",
                    "138000011112", "abc"):
            self.assertEqual(nl.normalize_phone(raw), "", repr(raw))

    def test_mask_phone(self):
        self.assertEqual(nl.mask_phone("13800001111"), "138****1111")
        self.assertEqual(nl.mask_phone(""), "***")


class TestRequestSmsCode(unittest.TestCase):
    def test_success(self):
        resp = mock.Mock(status_code=200, text="{}")
        with mock.patch.object(nl.requests, "post", return_value=resp) as m_post:
            result = nl.request_sms_code("13800001111")
        self.assertTrue(result["ok"])
        # 端点必须带 86- 区号前缀(与 sdk/wsconnect.login 一致)
        self.assertIn("/api/v1/phone-captchas/86-13800001111",
                      m_post.call_args.args[0])

    def test_invalid_phone_does_not_call_api(self):
        with mock.patch.object(nl.requests, "post") as m_post:
            result = nl.request_sms_code("123")
        self.assertFalse(result["ok"])
        self.assertIn("手机号", result["message"])
        m_post.assert_not_called()

    def test_http_error(self):
        resp = mock.Mock(status_code=429, text="too many requests")
        with mock.patch.object(nl.requests, "post", return_value=resp):
            result = nl.request_sms_code("13800001111")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 429)

    def test_network_error(self):
        err = nl.requests.RequestException("connection refused")
        with mock.patch.object(nl.requests, "post", side_effect=err):
            result = nl.request_sms_code("13800001111")
        self.assertFalse(result["ok"])
        self.assertIn("网络异常", result["message"])


class TestLoginWithCaptcha(unittest.TestCase):
    def test_success_returns_token(self):
        resp = mock.Mock(status_code=200, text=json.dumps({"token": "tok-123"}))
        with mock.patch.object(nl.requests, "post", return_value=resp) as m_post:
            result = nl.login_with_captcha("13800001111", " 123456 ")
        self.assertTrue(result["ok"])
        self.assertEqual(result["token"], "tok-123")
        # 请求体必须包含手机号登录所需字段
        body = m_post.call_args.kwargs["data"]
        self.assertIn('"auth_method": "phone-captcha"', body)
        self.assertIn('"phone": "13800001111"', body)
        self.assertIn('"captcha": "123456"', body)

    def test_missing_captcha_does_not_call_api(self):
        with mock.patch.object(nl.requests, "post") as m_post:
            result = nl.login_with_captcha("13800001111", "   ")
        self.assertFalse(result["ok"])
        m_post.assert_not_called()

    def test_rejected_by_server(self):
        resp = mock.Mock(status_code=400, text=json.dumps({"message": "验证码错误"}))
        with mock.patch.object(nl.requests, "post", return_value=resp):
            result = nl.login_with_captcha("13800001111", "000000")
        self.assertFalse(result["ok"])
        self.assertEqual(result["token"], "")
        self.assertIn("验证码错误", result["message"])

    def test_200_without_token_treated_as_failure(self):
        # 接口 200 但无 token 字段: 必须判为失败, 不能把空 token 落盘
        resp = mock.Mock(status_code=200, text="{}")
        with mock.patch.object(nl.requests, "post", return_value=resp):
            result = nl.login_with_captcha("13800001111", "123456")
        self.assertFalse(result["ok"])
        self.assertEqual(result["token"], "")

    def test_non_json_response_treated_as_failure(self):
        resp = mock.Mock(status_code=502, text="<html>bad gateway</html>")
        resp.json.side_effect = ValueError("not json")
        with mock.patch.object(nl.requests, "post", return_value=resp):
            result = nl.login_with_captcha("13800001111", "123456")
        self.assertFalse(result["ok"])


class TestTokenFile(unittest.TestCase):
    def test_save_and_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "token")   # 目录不存在时自动创建
            self.assertTrue(nl.save_token("tok-abc", path))
            self.assertEqual(nl.read_token(path), "tok-abc")

    def test_read_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(nl.read_token(os.path.join(tmp, "nope")))

    def test_save_empty_token_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "token")
            self.assertFalse(nl.save_token("", path))
            self.assertFalse(os.path.exists(path))

    def test_save_failure_returns_false(self):
        # 目标"目录"被文件占用: 必须返回 False 而不抛异常
        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, "token")
            with open(blocker, "w", encoding="utf-8") as f:
                f.write("x")
            self.assertFalse(nl.save_token("tok", os.path.join(blocker, "t")))


class TestOffsetObfuscation(unittest.TestCase):
    """响应解码 / 请求加密的偏移混淆工具。"""

    def test_derive_and_decode_object(self):
        key = 7
        text = obfuscate('{"user_id":1}', key)
        self.assertEqual(nl.derive_offset_key(text), key)
        self.assertEqual(nl.decode_mess(text), '{"user_id":1}')

    def test_derive_and_decode_array(self):
        # 签到接口返回数组([{...), 推导逻辑需兼容
        key = 200
        text = obfuscate('[{"sign_msg":"ok"}]', key)
        self.assertEqual(nl.derive_offset_key(text), key)
        self.assertEqual(nl.decode_mess(text), '[{"sign_msg":"ok"}]')

    def test_plain_text_passthrough(self):
        plain = '{"errmsg":"need encryption"}'
        self.assertIsNone(nl.derive_offset_key(plain))
        self.assertEqual(nl.decode_mess(plain), plain)

    def test_encode_mess_roundtrip(self):
        payload = "{}"
        key = 42
        self.assertEqual(nl.decode_mess(nl.encode_mess(payload, key), key), payload)

    def test_parse_response_handles_both(self):
        self.assertEqual(nl.parse_response('{"a":1}')["a"], 1)
        self.assertEqual(nl.parse_response(obfuscate('{"a":2}', 99))["a"], 2)
        self.assertEqual(nl.parse_response("not-json"), {})


class TestBuildPwdCipher(unittest.TestCase):
    """密码加密必须与网页端 crypto-js 逐位一致(固定向量来自 Node 对照实验)。"""

    VECTOR = {
        "salt": "E1RPKJdo",
        "key": "0123456789abcdefghijklmn",
        "iv": 1774619926849,
        "random": "8rJXeno2",
        "password": "test123",
        "expect_b64": ("RVGJ1pmfxBEZ/z4PSO62QWJYosPVMH7/SnJneCVCCVZRnCdO/R9722kUCWSPOfOam"
                       "GehOEwAftBDxSzZNLukesMyIHbnenzEPqWLQe4FNEA="),
    }

    def test_matches_web_crypto_js_vector(self):
        cipher = nl.build_pwd_cipher(
            self.VECTOR["salt"], self.VECTOR["key"], self.VECTOR["iv"],
            self.VECTOR["random"], self.VECTOR["password"])
        self.assertEqual(cipher, self.VECTOR["expect_b64"])

    def test_rejects_bad_key_length(self):
        # 密钥长度非 24 字节时必须抛错(避免把非法密文发给服务端)
        with self.assertRaises(RuntimeError):
            nl.build_pwd_cipher("s", "short-key", 123, "r", "p")


class TestPasswordLogin(unittest.TestCase):
    def _info_response(self):
        info = {
            "has_pwd": True,
            "user_pwd_login_salt": "E1RPKJdo",
            "pwd_encrypt_key": "0123456789abcdefghijklmn",
            "login_iv": 1774619926849,
            "login_random_text": "8rJXeno2",
        }
        return mock.Mock(status_code=200, text=json.dumps(info))

    def test_login_with_password_flow(self):
        login_resp = mock.Mock(status_code=200, text=json.dumps({"token": "new-token"}))
        with mock.patch.object(nl.requests, "get", return_value=self._info_response()), \
             mock.patch.object(nl.requests, "post", return_value=login_resp) as m_post:
            result = nl.login_with_password("old-token", "13800001111", "passw0rd")
        self.assertTrue(result["ok"])
        self.assertEqual(result["token"], "new-token")
        # 请求体字段与加密串必须符合网页端逻辑
        body = json.loads(m_post.call_args.kwargs["data"])
        self.assertEqual(body["phone"], "13800001111")
        self.assertEqual(body["ctcode"], "86")
        self.assertEqual(body["login_mode"], "password")
        self.assertEqual(body["pwd"], nl.build_pwd_cipher(
            "E1RPKJdo", "0123456789abcdefghijklmn", 1774619926849, "8rJXeno2", "passw0rd"))

    def test_login_without_token_rejected(self):
        # 无 token 时无法获取加密参数: 必须明确提示改用短信登录, 且不发请求
        with mock.patch.object(nl.requests, "get") as m_get:
            result = nl.login_with_password("", "13800001111", "passw0rd")
        self.assertFalse(result["ok"])
        self.assertIn("短信", result["message"])
        m_get.assert_not_called()

    def test_account_without_password(self):
        info = mock.Mock(status_code=200, text=json.dumps({"has_pwd": False}))
        with mock.patch.object(nl.requests, "get", return_value=info):
            result = nl.login_with_password("tok", "13800001111", "passw0rd")
        self.assertFalse(result["ok"])
        self.assertIn("未设置密码", result["message"])

    def test_wrong_password_message_from_server(self):
        login_resp = mock.Mock(status_code=401, text=json.dumps(
            {"errcode": 33507, "errmsgcn": "密码错误，请重新输入"}))
        with mock.patch.object(nl.requests, "get", return_value=self._info_response()), \
             mock.patch.object(nl.requests, "post", return_value=login_resp):
            result = nl.login_with_password("old-token", "13800001111", "wrong")
        self.assertFalse(result["ok"])
        self.assertIn("密码错误", result["message"])


class TestNeteaseSignin(unittest.TestCase):
    """签到: 先推导偏移 key(users/@me), 再提交混淆请求体到 sign-today。"""

    def test_signin_success(self):
        key = 7
        me_resp = mock.Mock(status_code=200, text=obfuscate('{"user_id":1}', key))
        awards = [{"sign_msg": "5分钟端游时长已到账", "title": "端游免费时长"}]
        sign_resp = mock.Mock(status_code=200,
                              text=obfuscate(json.dumps(awards, ensure_ascii=False), key))
        with mock.patch.object(nl.requests, "get", return_value=me_resp), \
             mock.patch.object(nl.requests, "post", return_value=sign_resp) as m_post:
            result = nl.netease_signin("tok")
        self.assertTrue(result["ok"])
        self.assertIn("端游", result["message"])
        # 必须 POST 到 sign-today, 且请求体是混淆后的空对象
        self.assertIn("/api/v2/sign-today", m_post.call_args.args[0])
        self.assertEqual(m_post.call_args.kwargs["data"], nl.encode_mess("{}", key))

    def test_signin_already_done(self):
        key = 3
        me_resp = mock.Mock(status_code=200, text=obfuscate('{"user_id":1}', key))
        sign_resp = mock.Mock(status_code=400, text=obfuscate(
            json.dumps({"errmsgcn": "今日已签到"}, ensure_ascii=False), key))
        with mock.patch.object(nl.requests, "get", return_value=me_resp), \
             mock.patch.object(nl.requests, "post", return_value=sign_resp):
            result = nl.netease_signin("tok")
        self.assertFalse(result["ok"])
        self.assertIn("今日已签到", result["message"])

    def test_signin_without_token(self):
        with mock.patch.object(nl.requests, "get") as m_get:
            result = nl.netease_signin("")
        self.assertFalse(result["ok"])
        m_get.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)