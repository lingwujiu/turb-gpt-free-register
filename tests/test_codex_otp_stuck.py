# -*- coding: utf-8 -*-
"""Codex OAuth 路径的 OTP 提交后状态判定。

该模块早先自己维护了一份 _wait_after_email_otp_submit，超时一律返回 invalid，
把「提交动作没生效（stuck）」误判成「验证码错误」，上层于是重发换码——而重发有
数分钟节流，会把等待窗耗尽（同一 bug 类，2026-10-06 job 172）。
现在超时且无错误标记必须返回 stuck，只有页面明确报错才算 invalid。
"""
import unittest
from unittest.mock import patch

from core import roxy_codex_oauth as oauth


class _StuckDriver:
    """始终停在 email-verification，且页面不报任何错。"""

    def __init__(self):
        self.current_url = "https://auth.openai.com/email-verification"


class WaitAfterEmailOtpSubmitTests(unittest.TestCase):
    def _wait(self, state, timeout=1):
        driver = _StuckDriver()
        with patch.object(oauth, "_read_email_otp_validate_dead_code", return_value=None), \
             patch.object(oauth, "_is_callback_url", return_value=False), \
             patch.object(oauth, "_has_strict_add_phone_form", return_value=False), \
             patch.object(oauth, "_is_phone_code_page", return_value=False), \
             patch.object(oauth, "_email_otp_page_state", return_value=state):
            return oauth._wait_after_email_otp_submit(driver, timeout=timeout)

    def test_timeout_without_error_mark_is_stuck(self):
        self.assertEqual(self._wait({}), "stuck")

    def test_explicit_error_is_invalid(self):
        self.assertEqual(self._wait({"errors": ["Invalid code"]}), "invalid")

    def test_aria_invalid_is_invalid(self):
        self.assertEqual(self._wait({"inputs": [{"ariaInvalid": "true"}]}), "invalid")

    def test_deactivated_account_is_reported(self):
        driver = _StuckDriver()
        with patch.object(oauth, "_read_email_otp_validate_dead_code", return_value="account_deactivated"), \
             patch.object(oauth, "_is_callback_url", return_value=False), \
             patch.object(oauth, "_has_strict_add_phone_form", return_value=False), \
             patch.object(oauth, "_is_phone_code_page", return_value=False):
            self.assertEqual(
                oauth._wait_after_email_otp_submit(driver, timeout=1),
                "deactivated:account_deactivated",
            )

    def test_leaving_verification_page_is_accepted(self):
        driver = _StuckDriver()
        driver.current_url = "https://auth.openai.com/add-phone"
        with patch.object(oauth, "_read_email_otp_validate_dead_code", return_value=None), \
             patch.object(oauth, "_is_callback_url", return_value=False), \
             patch.object(oauth, "_has_strict_add_phone_form", return_value=False), \
             patch.object(oauth, "_is_phone_code_page", return_value=False):
            self.assertEqual(oauth._wait_after_email_otp_submit(driver, timeout=1), "accepted")

    def test_same_code_retry_rounds_constant_exists(self):
        self.assertGreaterEqual(oauth._STUCK_SAME_CODE_ROUNDS, 1)


if __name__ == "__main__":
    unittest.main()
