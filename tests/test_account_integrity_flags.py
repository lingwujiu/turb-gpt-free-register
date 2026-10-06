# -*- coding: utf-8 -*-
"""残废账号的可见性：账本派生标记 + 落库告警。

背景：开启 2FA 却拿不到 secret 的账号照样被当成「注册成功」入库，只能事后靠人肉
比对才能发现（2026-10-06 Job 165/166/167/168/172）。现在要求：
  1. 展示层派生 mfa_missing / password_missing / incomplete，历史账号一并生效；
  2. 落库入口（save_account_data）在 ENABLE_2FA=True 却无 secret 时显式 ERROR 告警。
"""
import logging
import unittest
from unittest.mock import patch

from core import account_export, db


class DecorateAccountFlagsTests(unittest.TestCase):
    def test_full_account_is_complete(self):
        row = {
            "id": 1,
            "email": "ok@test.com",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "extra_json": '{"registration_password": "pw"}',
        }
        out = db._decorate_account(row)
        self.assertFalse(out["mfa_missing"])
        self.assertFalse(out["password_missing"])
        self.assertFalse(out["incomplete"])

    def test_account_without_password_and_2fa_is_incomplete(self):
        row = {"id": 2, "email": "wb8fd7ji@aigc.help", "totp_secret": "", "extra_json": "{}"}
        out = db._decorate_account(row)
        self.assertTrue(out["mfa_missing"])
        self.assertTrue(out["password_missing"])
        self.assertTrue(out["incomplete"])

    def test_password_but_no_2fa_is_half_broken(self):
        row = {
            "id": 3,
            "email": "1ih461dj@aigc.help",
            "extra_json": {"registration_password": "pwC%r6kj%43-af"},
        }
        out = db._decorate_account(row)
        self.assertTrue(out["mfa_missing"])
        self.assertFalse(out["password_missing"])
        self.assertFalse(out["incomplete"])

    def test_malformed_extra_json_does_not_raise(self):
        row = {"id": 4, "email": "x@test.com", "extra_json": "not-json"}
        out = db._decorate_account(row)
        self.assertTrue(out["mfa_missing"])


class SaveAccountDataDegradeTests(unittest.TestCase):
    def _save(self, totp_secret, enable_2fa):
        captured = {}

        def fake_insert(**kwargs):
            captured.update(kwargs)
            return 99

        with patch("core.db.insert_account", side_effect=fake_insert), \
             patch.object(account_export, "_append_batch_archive", return_value=None), \
             patch("core.plan_check_service.enqueue_account_plan_check",
                   return_value={"accepted": False}), \
             patch("config.twofa.ENABLE_2FA", enable_2fa):
            row_id = account_export.save_account_data(
                email="u@test.com", access_token="tok", totp_secret=totp_secret
            )
        return row_id, captured

    def test_missing_2fa_is_marked_and_logged_as_error(self):
        with self.assertLogs("core.account_export", level=logging.ERROR) as cm:
            row_id, captured = self._save(totp_secret=None, enable_2fa=True)
        self.assertEqual(row_id, 99)
        self.assertTrue(captured["extra"].get("mfa_missing"))
        self.assertTrue(any("2FA 未取得 secret" in m for m in cm.output))

    def test_no_flag_when_2fa_disabled(self):
        with self.assertLogs("core.account_export", level=logging.INFO) as cm:
            _, captured = self._save(totp_secret=None, enable_2fa=False)
        self.assertNotIn("mfa_missing", captured["extra"])
        self.assertFalse(any(r.levelno >= logging.ERROR for r in cm.records))

    def test_no_flag_when_secret_present(self):
        with self.assertLogs("core.account_export", level=logging.INFO) as cm:
            _, captured = self._save(totp_secret="JBSWY3DPEHPK3PXP", enable_2fa=True)
        self.assertNotIn("mfa_missing", captured["extra"])
        self.assertFalse(any(r.levelno >= logging.ERROR for r in cm.records))


if __name__ == "__main__":
    unittest.main()
