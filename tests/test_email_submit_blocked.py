# -*- coding: utf-8 -*-
"""邮箱提交后被 OpenAI 风控重置回授权入口的识别（2026-10-06 job 189）。

job 189 提交邮箱后，页面被静默重置回 `/api/accounts/authorize`（OAuth 授权入口），
该页既没有邮箱输入框，也没有密码/OTP 表单。旧逻辑把它归入 unknown，于是继续走
「重填邮箱」分支，最终抛出：

    RuntimeError: 找不到邮箱输入框/邮箱入口（未使用文字识别）

—— 一个把「出口 IP 被风控」伪装成「页面选择器缺陷」的误导性报错，排查时极易
误判为代码 bug。现在必须单独识别该页：等待循环返回 blocked，调用点直接抛出
明确的「疑似出口 IP 被限流/风控」错误，不再浪费一轮重填。

注意区分风控的两种表现：
  ① /auth/error?error=...  显式错误页（旧代码已覆盖）
  ② /api/accounts/authorize 静默重置（本模块负责）
"""
import time
import unittest
from unittest.mock import patch

from core import roxy_registration as rr


class _Driver:
    """最小 driver 替身：需要 current_url，并给出返回标题的 execute_script。"""

    def __init__(self, url, title=""):
        self.current_url = url
        self._title = title

    def execute_script(self, _script):
        return self._title


class AuthorizeResetPageTests(unittest.TestCase):
    def _is_reset(self, url, inputs):
        driver = _Driver(url)
        with patch.object(rr, "_email_input_value_state", return_value={"url": url, "inputs": inputs}):
            return rr._is_authorize_reset_page(driver)

    def test_authorize_without_inputs_is_reset(self):
        self.assertTrue(
            self._is_reset(
                "https://auth.openai.com/api/accounts/authorize?client_id=app_x&response_type=code",
                [],
            )
        )

    def test_authorize_with_inputs_is_not_reset(self):
        # 万一同 URL 上出现了表单输入框，说明是正常阶段，不能误判成风控。
        self.assertFalse(
            self._is_reset(
                "https://auth.openai.com/api/accounts/authorize?client_id=app_x",
                [{"value": "a@b.c"}],
            )
        )

    def test_other_url_is_not_reset(self):
        self.assertFalse(self._is_reset("https://chatgpt.com/auth/login", []))


class WaitEmailSubmitNextStateBlockedTests(unittest.TestCase):
    def test_authorize_reset_returns_blocked(self):
        driver = _Driver("https://auth.openai.com/api/accounts/authorize?client_id=app_x")
        with patch.object(rr, "_has_access_token", return_value=False), \
             patch.object(rr, "_is_login_password_page", return_value=False), \
             patch.object(rr, "_is_email_verification_page", return_value=False), \
             patch.object(rr, "_is_signup_password_page", return_value=False), \
             patch.object(rr, "_is_authorize_reset_page", return_value=True), \
             patch.object(rr, "_is_cloudflare_challenge", return_value=False):
            self.assertEqual(
                rr._wait_email_submit_next_state(driver, "a@b.c", timeout=1),
                "blocked",
            )

    def test_normal_page_still_returns_unknown(self):
        # 回归保护：非风控页仍应走原有 unknown 分支（例如页面仍在渲染）。
        driver = _Driver("https://chatgpt.com/auth/login")
        with patch.object(rr, "_has_access_token", return_value=False), \
             patch.object(rr, "_is_login_password_page", return_value=False), \
             patch.object(rr, "_is_email_verification_page", return_value=False), \
             patch.object(rr, "_is_signup_password_page", return_value=False), \
             patch.object(rr, "_is_authorize_reset_page", return_value=False), \
             patch.object(rr, "_email_input_value_state", return_value={"url": driver.current_url, "inputs": []}), \
             patch.object(rr, "_is_email_login_page_still_present", return_value=False):
            self.assertEqual(
                rr._wait_email_submit_next_state(driver, "a@b.c", timeout=1),
                "unknown",
            )


class IsCloudflareChallengeTests(unittest.TestCase):
    def test_title_matching(self):
        self.assertTrue(rr._is_cloudflare_challenge(_Driver("u", title="Just a moment...")))
        self.assertTrue(rr._is_cloudflare_challenge(_Driver("u", title="Attention Required! | Cloudflare")))
        self.assertTrue(rr._is_cloudflare_challenge(_Driver("u", title="Checking your browser before accessing")))
        self.assertFalse(rr._is_cloudflare_challenge(_Driver("u", title="ChatGPT")))

    def test_execute_script_failure_is_not_challenge(self):
        class _Bad:
            current_url = "u"

            def execute_script(self, _s):
                raise RuntimeError("no js")

        self.assertFalse(rr._is_cloudflare_challenge(_Bad()))


class CloudflareChallengeGraceTests(unittest.TestCase):
    """挑战页必须与风控重置区分：前者会自行放行，值得延长等待。"""

    def _base_patches(self):
        return [
            patch.object(rr, "_has_access_token", return_value=False),
            patch.object(rr, "_is_login_password_page", return_value=False),
            patch.object(rr, "_is_signup_password_page", return_value=False),
        ]

    def test_challenge_without_clearance_waits_then_blocks(self):
        driver = _Driver("https://auth.openai.com/api/accounts/authorize?client_id=app_x")
        patches = self._base_patches() + [
            patch.object(rr, "_is_email_verification_page", return_value=False),
            patch.object(rr, "_is_authorize_reset_page", return_value=True),
            patch.object(rr, "_is_cloudflare_challenge", return_value=True),
            patch.object(rr, "_CF_CHALLENGE_GRACE", 2.0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        started = time.time()
        result = rr._wait_email_submit_next_state(driver, "a@b.c", timeout=1)
        elapsed = time.time() - started
        self.assertEqual(result, "blocked")
        # 确实延长等待过：否则 1 秒窗口就结束了。
        self.assertGreaterEqual(elapsed, 2.0)

    def test_challenge_cleared_returns_real_state(self):
        driver = _Driver(
            "https://auth.openai.com/api/accounts/authorize?client_id=app_x",
            title="Just a moment...",
        )
        # 前两轮仍停在挑战页，第三轮已进入 OTP 页。
        reset_seq = iter([True, True, False])
        otp_seq = iter([False, False, True])
        patches = self._base_patches() + [
            patch.object(rr, "_is_email_verification_page", side_effect=lambda *a: next(otp_seq, False)),
            patch.object(rr, "_is_authorize_reset_page", side_effect=lambda *a: next(reset_seq, False)),
            patch.object(rr, "_is_cloudflare_challenge", return_value=True),
            patch.object(rr, "_CF_CHALLENGE_GRACE", 30.0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        result = rr._wait_email_submit_next_state(driver, "a@b.c", timeout=1)
        self.assertEqual(result, "otp")


class SubmitEmailRaisesBlockedTests(unittest.TestCase):
    def test_blocked_raises_clear_error(self):
        url = "https://auth.openai.com/api/accounts/authorize?client_id=app_x&response_type=code"
        driver = _Driver(url)
        email = "a@b.c"
        with patch.object(rr, "_type_email_address", return_value=None), \
             patch.object(rr, "_email_input_value_state", return_value={"inputs": [{"value": email}]}), \
             patch.object(rr, "_submit_email_step", return_value=None), \
             patch.object(rr, "_wait_email_submit_next_state", return_value="blocked"), \
             patch.object(rr, "human_delay", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                rr._submit_email_and_wait_next(driver, email, attempts=3)
        msg = str(ctx.exception)
        self.assertIn("授权入口", msg)
        self.assertIn("限流", msg)
        # 关键：不能再出现误导性的「找不到邮箱输入框」表述。
        self.assertNotIn("找不到邮箱输入框", msg)


if __name__ == "__main__":
    unittest.main()
