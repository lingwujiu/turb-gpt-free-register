# -*- coding: utf-8 -*-
"""
注册基础信息（默认值）

CLI 走 main.py 时会优先读这里；Web 控制台批量注册时也会用同样的默认值。
留空字段会触发交互式输入或自动生成（仅 USE_EMAIL_SERVICE=True 时邮箱会从 Outlook 池领取）。
"""
from config.env_loader import apply_env_overrides

# 注册邮箱（留空 + USE_EMAIL_SERVICE=True 时从 Outlook 池领取）
REGISTER_EMAIL = ""

# 注册密码（OTP-only 流程已不需要，留作备用）
REGISTER_PASSWORD = ""

# 用户名（注册完成后设置的显示名称，留空会自动生成 "Foo Bar" 形式）
# OpenAI 限制：name_invalid_chars —— 只允许字母和空格
REGISTER_NAME = ""

# 串行批量注册时，相邻任务「启动」的最小间隔（秒）。0 = 不额外等待（仅靠 workers=1 自然串行）。
# 用于在当前出口 IP 被 OpenAI 短时频控时，把任务摊开、降低突发特征。
REGISTER_TASK_STAGGER_SECONDS = 0

# 在上述最小间隔之上叠加的随机抖动上限（秒），避免任务启动时刻过于规律。
REGISTER_TASK_STAGGER_JITTER_SECONDS = 0

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'REGISTER_EMAIL': 'str',
    'REGISTER_NAME': 'str',
    'REGISTER_TASK_STAGGER_SECONDS': 'int',
    'REGISTER_TASK_STAGGER_JITTER_SECONDS': 'int',
})
