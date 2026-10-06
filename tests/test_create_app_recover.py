# -*- coding: utf-8 -*-
"""create_app 默认不得执行「启动恢复」（2026-10-06 job 192 被误杀）。

`create_app()` 早先无条件调用 5 个 `recover_interrupted_*`，它们会把账本里
running/stopping 的记录一律判为失败。而测试普遍调用 `create_app()` 只是为了拿一个
test_client —— 于是**跑一次测试就会把正在运行的注册任务杀掉**。

实际事故：job 192 在 OTP 阶段时，本机执行了 `unittest discover -s tests`，
被标记为「WebUI 重启或进程异常退出，任务未完成」，账号因此没入库。

修复：恢复动作改为显式开关，默认关闭（fail-safe）；只有真正的服务启动入口
`web.py` 传 `recover_interrupted=True`。
"""
import unittest
from unittest.mock import patch

from webui import app as app_module
from webui.app import create_app

_RECOVER_FUNCS = [
    "recover_interrupted_plan_checks",
    "recover_interrupted_extract_links",
    "recover_interrupted_live_checks",
    "recover_interrupted_codex_agents",
    "recover_interrupted_jobs",
]


class CreateAppRecoverTests(unittest.TestCase):
    def _start(self):
        patchers = [patch.object(app_module.db, name) for name in _RECOVER_FUNCS]
        mocks = [p.start() for p in patchers]
        self.addCleanup(lambda: [p.stop() for p in patchers])
        return mocks

    def test_default_does_not_touch_production_state(self):
        mocks = self._start()
        create_app(auth_code="test-auth")
        for name, mock in zip(_RECOVER_FUNCS, mocks):
            mock.assert_not_called()

    def test_explicit_flag_recovers(self):
        mocks = self._start()
        create_app(auth_code="test-auth", recover_interrupted=True)
        for name, mock in zip(_RECOVER_FUNCS, mocks):
            mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
