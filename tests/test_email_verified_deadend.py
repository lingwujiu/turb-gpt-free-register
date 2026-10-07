# -*- coding: utf-8 -*-
"""邮箱已验证、但 SPA 停在 /email-verification 死页（2026-10-07 job 206/214/216/219/220/221）。

失败链路：
  1. OTP 填入后第一次 Continue 点击**未生效**（页面不动）→ `_wait_after_email_otp_submit`
     返回 'stuck'；
  2. 恢复逻辑 `refresh()` 重载页面 → 服务端已确认邮箱通过，标题变成 "Email verified"，
     判定 accepted；
  3. 但重载后落在**静态死页** "Email verified / Your email has already been verified"
     （无输入框、无按钮），SPA **不会**自动跳到 about-you；
  4. `_complete_profile_page` 一直等资料页 → 超时 → 任务被判失败。

修复：检测到该死页时，手动 `driver.get('https://auth.openai.com/about-you')` 续跑，
并给恢复次数设上限，避免导航失败时空转到超时。
"""
import time as _time
import unittest
from unittest.mock import patch

from core import registration_service as rsrv
from core import roxy_registration as rr


class _Driver:
    def __init__(self, url="https://auth.openai.com/email-verification"):
        self.current_url = url
        self.visited = []

    def get(self, url):
        self.visited.append(url)


class RecoverFromEmailVerifiedDeadendTests(unittest.TestCase):
    def test_navigates_when_title_says_verified(self):
        driver = _Driver()
        snap = {
            "url": "https://auth.openai.com/email-verification",
            "title": "Email verified - OpenAI",
            "text": "Email verified",
        }
        self.assertTrue(rr._recover_from_email_verified_deadend(driver, snap))
        self.assertEqual(driver.visited, ["https://auth.openai.com/about-you"])

    def test_navigates_when_body_says_already_verified(self):
        driver = _Driver()
        snap = {
            "url": "https://auth.openai.com/email-verification",
            "title": "OpenAI",
            "text": "Email verified\nYour email (a@b.c) has already been verified",
        }
        self.assertTrue(rr._recover_from_email_verified_deadend(driver, snap))
        self.assertEqual(driver.visited, ["https://auth.openai.com/about-you"])

    def test_noop_outside_email_verification(self):
        driver = _Driver("https://auth.openai.com/about-you")
        snap = {"url": "https://auth.openai.com/about-you", "title": "Email verified - OpenAI"}
        self.assertFalse(rr._recover_from_email_verified_deadend(driver, snap))
        self.assertEqual(driver.visited, [])

    def test_noop_without_verified_signal(self):
        driver = _Driver()
        snap = {
            "url": "https://auth.openai.com/email-verification",
            "title": "Check your inbox - OpenAI",
            "text": "Check your inbox\nEnter the verification code",
        }
        self.assertFalse(rr._recover_from_email_verified_deadend(driver, snap))
        self.assertEqual(driver.visited, [])


class CompleteProfilePageDeadendTests(unittest.TestCase):
    def test_deadend_recovery_is_capped(self):
        """死页恢复最多触发 3 次，仍拿不到资料页则按超时失败（不再空等）。"""
        driver = _Driver()
        calls = {"n": 0}

        def fake_recover(_driver, _snap):
            calls["n"] += 1
            return True

        with patch.object(rr, "_has_access_token", return_value=False), \
             patch.object(rr, "_page_snapshot",
                          return_value={"url": "https://auth.openai.com/email-verification"}), \
             patch.object(rr, "_is_profile_like", return_value=False), \
             patch.object(rr, "_recover_from_email_verified_deadend", side_effect=fake_recover), \
             patch.object(rr, "_is_chrome_error_page", return_value=False):
            with self.assertRaises(RuntimeError):
                rr._complete_profile_page(driver, "John Doe", "1990-01-01", timeout=3)
        self.assertEqual(calls["n"], 3)

    def test_recovers_then_submits_profile(self):
        """死页恢复后拿到资料页，正常填写并提交，返回 True。"""
        driver = _Driver()
        snapshot = {"url": "https://auth.openai.com/email-verification"}

        with patch.object(rr, "_has_access_token", return_value=False), \
             patch.object(rr, "_page_snapshot", side_effect=lambda _d: dict(snapshot)), \
             patch.object(rr, "_is_profile_like",
                          side_effect=lambda snap: "about-you" in str(snap.get("url"))), \
             patch.object(rr, "_recover_from_email_verified_deadend",
                          side_effect=lambda _d, _s: snapshot.update(url="https://auth.openai.com/about-you") or True), \
             patch.object(rr, "_is_chrome_error_page", return_value=False), \
             patch.object(rr, "_select_or_type", return_value=True), \
             patch.object(rr, "_fill_birthday_or_age", return_value="age"), \
             patch.object(rr, "_accept_profile_consents", return_value=None), \
             patch.object(rr, "_click_if_enabled_submit", return_value=True), \
             patch.object(rr, "human_delay", return_value=None):
            self.assertTrue(rr._complete_profile_page(driver, "John Doe", "1990-01-01", timeout=10))


class StartStaggerTests(unittest.TestCase):
    def test_noop_when_interval_zero(self):
        rsrv._last_job_start_ts = _time.time()
        started = _time.time()
        with patch("config.register.REGISTER_TASK_STAGGER_SECONDS", 0), \
             patch("config.register.REGISTER_TASK_STAGGER_JITTER_SECONDS", 0):
            rsrv._apply_start_stagger(1)
        self.assertLess(_time.time() - started, 0.5)

    def test_waits_when_interval_set(self):
        rsrv._last_job_start_ts = _time.time()  # 假装上一个任务刚启动
        started = _time.time()
        with patch("config.register.REGISTER_TASK_STAGGER_SECONDS", 1), \
             patch("config.register.REGISTER_TASK_STAGGER_JITTER_SECONDS", 0):
            rsrv._apply_start_stagger(1)
        self.assertGreaterEqual(_time.time() - started, 0.8)


if __name__ == "__main__":
    unittest.main()
