# -*- coding: utf-8 -*-
"""OTP 已通过但 SPA 不换 URL 的情况（2026-10-06 job 193）。

OTP 校验成功后，OpenAI 的 SPA 有时**不换 URL**（仍停在 /email-verification），
只把 document.title 改成 "Email verified - OpenAI"。旧逻辑只凭 URL 判断「是否还在
验证码页」，于是把已经成功的流程判成 stuck → 重载重提 → 再升级为「重发换码」；
而页面此时早已撤掉重发入口，最终抛：

    RuntimeError: 找不到可点击的重新发送验证码按钮

现在标题是明确的成功信号，必须据此判 accepted；「找不到重发按钮」也不再直接判死，
而是先复核一次真实状态。
"""
import unittest
from unittest.mock import patch

from core import chrome_registration as cr
from core import roxy_registration as rr


class _Driver:
    def __init__(self, url="https://auth.openai.com/email-verification"):
        self.current_url = url


class TitleIndicatesVerifiedTests(unittest.TestCase):
    def test_matching_titles(self):
        self.assertTrue(rr._title_indicates_email_verified({"title": "Email verified - OpenAI"}))
        self.assertTrue(rr._title_indicates_email_verified({"title": "EMAIL VERIFIED"}))
        self.assertFalse(rr._title_indicates_email_verified({"title": "Check your inbox - OpenAI"}))
        self.assertFalse(rr._title_indicates_email_verified({}))
        self.assertFalse(rr._title_indicates_email_verified(None))


class WaitAfterOtpSubmitVerifiedTitleTests(unittest.TestCase):
    def test_verified_title_on_same_url_is_accepted(self):
        driver = _Driver()  # URL 仍停在 email-verification
        with patch.object(rr, "_is_email_verification_page", return_value=True), \
             patch.object(rr, "_email_otp_page_state",
                          return_value={"title": "Email verified - OpenAI", "inputs": []}):
            self.assertEqual(rr._wait_after_email_otp_submit(driver, timeout=2), "accepted")

    def test_still_checking_inbox_is_stuck(self):
        driver = _Driver()
        with patch.object(rr, "_is_email_verification_page", return_value=True), \
             patch.object(rr, "_email_otp_page_state",
                          return_value={"title": "Check your inbox - OpenAI", "inputs": []}):
            self.assertEqual(rr._wait_after_email_otp_submit(driver, timeout=1), "stuck")


class ResendOrAcceptTests(unittest.TestCase):
    def test_no_resend_button_but_verified_is_accepted(self):
        driver = _Driver()
        with patch.object(cr, "_click_resend_email_otp",
                          side_effect=RuntimeError("找不到可点击的重新发送验证码按钮")), \
             patch.object(cr, "_wait_after_email_otp_submit", return_value="accepted"):
            self.assertTrue(cr._resend_email_otp_or_accept_if_passed(driver))

    def test_no_resend_button_and_not_verified_raises(self):
        driver = _Driver()
        with patch.object(cr, "_click_resend_email_otp",
                          side_effect=RuntimeError("找不到可点击的重新发送验证码按钮")), \
             patch.object(cr, "_wait_after_email_otp_submit", return_value="stuck"):
            with self.assertRaises(RuntimeError):
                cr._resend_email_otp_or_accept_if_passed(driver)

    def test_normal_resend_returns_false(self):
        driver = _Driver()
        with patch.object(cr, "_click_resend_email_otp", return_value=None):
            self.assertFalse(cr._resend_email_otp_or_accept_if_passed(driver))


if __name__ == "__main__":
    unittest.main()
