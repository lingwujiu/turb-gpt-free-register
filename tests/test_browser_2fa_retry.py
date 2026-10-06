# -*- coding: utf-8 -*-
"""2FA UI 流的重试与安全阀。

背景：2026-10-06 Job 165/166/167/168/172 全部败在「设置页未就绪 / 开关找不到 /
取不到密钥」这类渲染时序问题上，同一套代码在 Job 176 一次通过。这些失败点都在
「提交动态码」之前，因此可以安全重试；但一旦开关已被真的开启却回读不到 secret，
必须立刻停手，否则会把账号锁死。
"""
import unittest
from unittest.mock import patch

from core import browser_2fa

SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


class _FakeDriver:
    def __init__(self):
        self.visited = []

    def get(self, url):
        self.visited.append(url)


class Setup2faRetryTests(unittest.TestCase):
    def _run(self, once_side_effect, enabled_side_effect, attempts=2):
        driver = _FakeDriver()
        with patch.object(browser_2fa, "_setup_2fa_once", side_effect=once_side_effect) as once, \
             patch.object(browser_2fa, "_mfa_switch_enabled", side_effect=enabled_side_effect) as enabled, \
             patch.object(browser_2fa.time, "sleep", return_value=None):
            secret = browser_2fa.setup_2fa_via_browser(
                driver, "u@test.com", password="pw", attempts=attempts
            )
        return secret, once, enabled, driver

    def test_first_attempt_failure_is_retried_and_succeeds(self):
        secret, once, _, driver = self._run(
            once_side_effect=[None, SECRET],
            enabled_side_effect=[False, False],
        )
        self.assertEqual(secret, SECRET)
        self.assertEqual(once.call_count, 2)
        # 重试前应重载一次页面，换取干净的 SPA 状态
        self.assertIn("https://chatgpt.com/", driver.visited)

    def test_exception_in_attempt_is_swallowed_then_retried(self):
        secret, once, _, _ = self._run(
            once_side_effect=[RuntimeError("boom"), SECRET],
            enabled_side_effect=[False, False],
        )
        self.assertEqual(secret, SECRET)
        self.assertEqual(once.call_count, 2)

    def test_stops_retry_when_switch_already_enabled_without_secret(self):
        """安全阀：开关已开启却拿不到 secret —— 再点会锁死账号，必须停手。"""
        secret, once, enabled, _ = self._run(
            once_side_effect=[None, SECRET],
            enabled_side_effect=[True],
            attempts=3,
        )
        self.assertIsNone(secret)
        self.assertEqual(once.call_count, 1)
        self.assertEqual(enabled.call_count, 1)

    def test_returns_none_after_all_attempts_fail(self):
        secret, once, _, _ = self._run(
            once_side_effect=[None, None, None],
            enabled_side_effect=[False, False, False],
            attempts=3,
        )
        self.assertIsNone(secret)
        self.assertEqual(once.call_count, 3)

    def test_single_attempt_mode_does_not_retry(self):
        secret, once, _, _ = self._run(
            once_side_effect=[None],
            enabled_side_effect=[False],
            attempts=1,
        )
        self.assertIsNone(secret)
        self.assertEqual(once.call_count, 1)


if __name__ == "__main__":
    unittest.main()
