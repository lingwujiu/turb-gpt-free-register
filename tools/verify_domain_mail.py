#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
域名邮箱链路自检（方案一：自有域名 + Cloudflare Catch-all + QQ IMAP）

用途：在正式跑批量注册之前，用一封真实的测试邮件验证整条取码链路是否打通。

它会依次执行：
    1. 读取项目配置 EMAIL_SOURCE / EMAIL_DOMAIN / QQ_EMAIL / QQ_IMAP_PASSWORD
    2. 校验 QQ 邮箱 IMAP 登录（这一步失败 = 授权码或 IMAP 服务没开）
    3. 生成一个随机域名邮箱 {8位}@{EMAIL_DOMAIN}
    4. 通过 QQ SMTP 往该地址发一封带随机验证码的测试信
    5. 轮询 QQ IMAP 收件箱，确认这封信被 Cloudflare 转发回来了
    6. 用项目自带的 otp_utils 尝试提取验证码，验证识别逻辑

用法（在项目根目录执行）：
    .venv/bin/python tools/verify_domain_mail.py
    # 或
    python3 tools/verify_domain_mail.py

退出码：
    0 = 全链路通过
    1 = 配置缺失
    2 = IMAP 登录失败
    3 = 发信失败
    4 = 超时未收到转发邮件
"""
from __future__ import annotations

import imaplib
import random
import smtplib
import string
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

# 保证从任意目录执行都能 import 到项目模块
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

OK = "\033[92m✔\033[0m"
BAD = "\033[91m✘\033[0m"
WARN = "\033[93m!\033[0m"


def _line(status: str, text: str) -> None:
    print(f"  {status} {text}")


def _header(title: str) -> None:
    print(f"\n=== {title} ===")


# ============================================================
# 1. 配置检查
# ============================================================
def check_config() -> dict:
    _header("1/5 配置检查")
    try:
        from config import email as cfg
    except Exception as exc:  # pragma: no cover
        _line(BAD, f"无法导入 config.email：{exc}")
        sys.exit(1)

    from core.email_provider import parse_email_sources

    sources = parse_email_sources()
    domain = str(getattr(cfg, "EMAIL_DOMAIN", "") or "").strip()
    qq_email = str(getattr(cfg, "QQ_EMAIL", "") or "").strip()
    qq_pass = str(getattr(cfg, "QQ_IMAP_PASSWORD", "") or "").strip()
    imap_server = str(getattr(cfg, "QQ_IMAP_SERVER", "imap.qq.com")).strip()
    imap_port = int(getattr(cfg, "QQ_IMAP_PORT", 993))

    _line(OK if sources else BAD, f"EMAIL_SOURCE = {','.join(sources)}")
    _line(OK if domain else BAD, f"EMAIL_DOMAIN = {domain or '(未配置)'}")
    _line(OK if qq_email else BAD, f"QQ_EMAIL = {qq_email or '(未配置)'}")
    _line(
        OK if qq_pass else BAD,
        f"QQ_IMAP_PASSWORD = {'*' * 8 + qq_pass[-4:] if len(qq_pass) > 4 else '(未配置)'}",
    )
    _line(OK, f"IMAP 服务端 = {imap_server}:{imap_port}")

    missing = []
    if "cloudflare_domain" not in sources:
        missing.append("EMAIL_SOURCE 需包含 cloudflare_domain")
    if not domain:
        missing.append("EMAIL_DOMAIN")
    if not qq_email:
        missing.append("QQ_EMAIL")
    if not qq_pass:
        missing.append("QQ_IMAP_PASSWORD")

    if missing:
        _line(BAD, "配置不完整，请先补齐：" + "、".join(missing))
        print(
            "\n在项目根目录 .env 中补上（或走 WebUI「配置 → 邮箱 / OTP」）：\n"
            "  EMAIL_SOURCE=\"cloudflare_domain\"\n"
            "  EMAIL_DOMAIN=\"你的域名.com\"\n"
            "  QQ_EMAIL=\"你的QQ号@qq.com\"\n"
            "  QQ_IMAP_PASSWORD=\"16位IMAP授权码\"\n"
        )
        sys.exit(1)

    return {
        "domain": domain,
        "qq_email": qq_email,
        "qq_pass": qq_pass,
        "imap_server": imap_server,
        "imap_port": imap_port,
    }


# ============================================================
# 2. IMAP 登录
# ============================================================
def check_imap_login(cfg: dict) -> None:
    _header("2/5 QQ 邮箱 IMAP 登录")
    try:
        mail = imaplib.IMAP4_SSL(cfg["imap_server"], cfg["imap_port"])
        mail.login(cfg["qq_email"], cfg["qq_pass"])
        status, _ = mail.select("INBOX")
        if status != "OK":
            raise RuntimeError(f"SELECT INBOX 返回 {status}")
        mail.logout()
        _line(OK, "IMAP 登录成功，INBOX 可读")
    except imaplib.IMAP4.error as exc:
        _line(BAD, f"IMAP 登录被拒：{exc}")
        _line(WARN, "常见原因：授权码填成了 QQ 密码 / IMAP 服务未开启 / 授权码已重置")
        sys.exit(2)
    except Exception as exc:
        _line(BAD, f"IMAP 连接失败：{type(exc).__name__}: {exc}")
        sys.exit(2)


# ============================================================
# 3. 发测试信
# ============================================================
def send_test_mail(cfg: dict, target: str, code: str) -> None:
    _header("3/5 发送测试邮件")
    msg = EmailMessage()
    msg["Subject"] = f"Domain Mail Self-Test {code}"
    msg["From"] = cfg["qq_email"]
    msg["To"] = target
    msg.set_content(
        f"这是一封用于验证域名邮箱转发链路的测试邮件。\n\n"
        f"your verification code is {code}\n\n"
        f"发送时间：{datetime.now(timezone.utc).isoformat()}\n"
    )
    try:
        with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=20) as smtp:
            smtp.login(cfg["qq_email"], cfg["qq_pass"])
            smtp.send_message(msg)
        _line(OK, f"已通过 smtp.qq.com 发出 → {target}")
    except Exception as exc:
        _line(BAD, f"发信失败：{type(exc).__name__}: {exc}")
        _line(WARN, "请确认 QQ 邮箱已开启 SMTP 服务（与 IMAP 共用同一个授权码）")
        sys.exit(3)


# ============================================================
# 4. 轮询收信
# ============================================================
def wait_for_forward(cfg: dict, target: str, code: str, timeout: int = 90) -> dict | None:
    _header("4/5 等待 Cloudflare 转发回信")
    import email as email_lib

    from core.qqmail_client import _msg_to_dict

    since = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%d-%b-%Y")
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            mail = imaplib.IMAP4_SSL(cfg["imap_server"], cfg["imap_port"])
            mail.login(cfg["qq_email"], cfg["qq_pass"])
            mail.select("INBOX")
            status, ids = mail.search(None, f"(SINCE {since})")
            found = None
            if status == "OK" and ids[0]:
                for mid in ids[0].split()[-25:]:
                    st, data = mail.fetch(mid, "(RFC822)")
                    if st != "OK" or not data or not data[0]:
                        continue
                    raw = data[0][1]
                    msg = email_lib.message_from_bytes(raw)
                    # 直接复用项目自带的解析器，保证与注册流程走同一条代码路径
                    item = _msg_to_dict(msg)
                    if code in item.get("subject", "") or target.lower() in item.get("to", "").lower():
                        found = item
                        break
            mail.logout()
            if found:
                _line(OK, f"第 {attempt} 轮命中：subject={found['subject']!r}")
                _line(OK, f"         To={found['to']!r}")
                return found
            remaining = int(deadline - time.time())
            _line(WARN, f"第 {attempt} 轮未收到，剩余约 {remaining}s…")
        except Exception as exc:
            _line(WARN, f"第 {attempt} 轮异常：{type(exc).__name__}: {exc}")
        time.sleep(5)

    _line(BAD, f"{timeout}s 内未收到转发邮件")
    _line(WARN, "排查清单：① Cloudflare Email Routing 是否开启 Catch-all")
    _line(WARN, "          ② 目标地址是否已在 Cloudflare 完成邮箱验证")
    _line(WARN, "          ③ 域名 MX 记录是否指向 Cloudflare 的 email routing 服务器")
    _line(WARN, "          ④ QQ 邮箱是否把该邮件丢进了垃圾箱（IMAP 只读 INBOX）")
    return None


# ============================================================
# 5. OTP 识别
# ============================================================
def check_otp_extract(mail_item: dict) -> bool:
    _header("5/5 验证码识别")
    try:
        from core.otp_utils import extract_otp, looks_like_openai_email

        otp = extract_otp(mail_item)
        is_openai = looks_like_openai_email(mail_item)
        _line(OK if otp else WARN, f"extract_otp → {otp!r}")
        _line(
            OK if is_openai else WARN,
            f"looks_like_openai_email → {is_openai}"
            + ("" if is_openai else "（测试信不是 OpenAI 发的，这里 False 属正常）"),
        )
        return True
    except Exception as exc:
        _line(WARN, f"OTP 工具调用失败（不影响链路结论）：{type(exc).__name__}: {exc}")
        return False


# ============================================================
# main
# ============================================================
def main() -> int:
    print("域名邮箱链路自检 —— Cloudflare Catch-all + QQ IMAP")
    cfg = check_config()
    check_imap_login(cfg)

    prefix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    target = f"{prefix}@{cfg['domain']}"
    code = f"{random.randint(100000, 999999)}"
    _header("生成测试地址")
    _line(OK, f"target = {target}")
    _line(OK, f"code   = {code}")

    send_test_mail(cfg, target, code)
    found = wait_for_forward(cfg, target, code, timeout=90)
    if not found:
        return 4
    check_otp_extract(found)

    _header("结论")
    _line(OK, "全链路已打通：域名 Catch-all → QQ 邮箱 → IMAP 取信 均正常")
    _line(OK, "可以回到 WebUI 直接投递注册任务了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
