# -*- coding: utf-8 -*-
"""邮箱填写异常后必须复核页面真实状态，不能直接判死（2026-10-07 job 232）。

失败链路：
  1. 邮箱提交后页面停滞了一会儿（高负载下 OpenAI 前进变慢）；
  2. 「填邮箱」的等待窗口（20s 探针 + 6s 复核）先到期，重试逻辑进入
     `_type_email_address` 去「重填邮箱」；
  3. 可此时页面其实已经悄悄跳到了 OTP 页（`/email-verification`，
     title='Check your inbox - OpenAI'，有 code 输入框 + validate/resend 按钮），
     在 OTP 页上当然找不到邮箱输入框；
  4. `_type_email_address` 抛 `RuntimeError: 找不到邮箱输入框` → 任务被判失败，
     而日志里的 state 明明显示 url 已经是 email-verification。

修复：`_submit_email_and_wait_next` 里，任何邮箱填写异常都先
`_wait_email_submit_next_state` 复核一次；若页面实际已进入 otp/password/logged_in
就直接返回，不再判死。
"""
import unittest
from unittest.mock import patch

from core import roxy_registration as rr


class _Driver:
    def __init__(self, url="https://chatgpt.com/auth/login?email=a%40b.c"):
        self.current_url = url


_TYPE_ERR = RuntimeError(
    "找不到邮箱输入框/邮箱入口（未使用文字识别），"
    "state={'url': 'https://auth.openai.com/email-verification', 'title': 'Check your inbox - OpenAI'}"
)


class EmailSubmitReprobeTests(unittest.TestCase):
    def test_returns_otp_when_page_actually_advanced(self):
        """填邮箱报错，但复核发现页面已是 OTP 页 → 返回 otp（不再判死）。"""
        driver = _Driver()
        with patch.object(rr, "_type_email_address", side_effect=_TYPE_ERR), \
             patch.object(rr, "_wait_email_submit_next_state", return_value="otp"):
            self.assertEqual(rr._submit_email_and_wait_next(driver, "a@b.c", attempts=3), "otp")

    def test_returns_password_when_page_actually_advanced(self):
        driver = _Driver()
        with patch.object(rr, "_type_email_address", side_effect=_TYPE_ERR), \
             patch.object(rr, "_wait_email_submit_next_state", return_value="password"):
            self.assertEqual(rr._submit_email_and_wait_next(driver, "a@b.c", attempts=3), "password")

    def test_returns_logged_in_when_page_actually_advanced(self):
        driver = _Driver()
        with patch.object(rr, "_type_email_address", side_effect=_TYPE_ERR), \
             patch.object(rr, "_wait_email_submit_next_state", return_value="logged_in"):
            self.assertEqual(rr._submit_email_and_wait_next(driver, "a@b.c", attempts=3), "logged_in")

    def test_retries_then_succeeds_on_second_probe(self):
        """首次复核仍是邮箱页 → 重试；第二次复核发现已进 OTP → 返回 otp。"""
        driver = _Driver()
        calls = {"n": 0}

        def probe(_d, _e, timeout=6):
            calls["n"] += 1
            return "email_page" if calls["n"] == 1 else "otp"

        with patch.object(rr, "_type_email_address", side_effect=_TYPE_ERR), \
             patch.object(rr, "_wait_email_submit_next_state", side_effect=probe), \
             patch.object(rr, "_is_chrome_error_page", return_value=False), \
             patch.object(rr.time, "sleep", return_value=None):
            self.assertEqual(rr._submit_email_and_wait_next(driver, "a@b.c", attempts=3), "otp")

    def test_blocked_when_reprobe_says_blocked(self):
        driver = _Driver()
        sentinel = RuntimeError("出口IP被限流/风控")
        with patch.object(rr, "_type_email_address", side_effect=_TYPE_ERR), \
             patch.object(rr, "_wait_email_submit_next_state", return_value="blocked"), \
             patch.object(rr, "_blocked_ip_error", return_value=sentinel):
            with self.assertRaises(RuntimeError) as cm:
                rr._submit_email_and_wait_next(driver, "a@b.c", attempts=1)
            self.assertIn("限流", str(cm.exception))

    def test_persistent_failure_dumps_and_raises(self):
        """复核仍是邮箱页且已用尽尝试 → 落盘诊断并抛出（不再误报成简单的选择器错误）。"""
        driver = _Driver()
        with patch.object(rr, "_type_email_address", side_effect=_TYPE_ERR), \
             patch.object(rr, "_wait_email_submit_next_state", return_value="email_page"), \
             patch.object(rr, "_dump_stuck_login_page") as dump, \
             patch.object(rr, "_email_input_value_state", return_value={"inputs": []}):
            with self.assertRaises(RuntimeError) as cm:
                rr._submit_email_and_wait_next(driver, "a@b.c", attempts=1)
            self.assertTrue(dump.called)
            self.assertIn("页面未进入下一步", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
