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
import unittest
from unittest.mock import patch

from core import roxy_registration as rr


class _Driver:
    """只提供 current_url 的最小 driver 替身。"""

    def __init__(self, url):
        self.current_url = url


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
             patch.object(rr, "_is_authorize_reset_page", return_value=True):
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
