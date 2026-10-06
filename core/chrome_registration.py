# -*- coding: utf-8 -*-
"""通过本机 Chrome（Playwright channel="chrome"）执行 ChatGPT 注册。

与 core.cloakbrowser_registration 同构：页面操作全部复用 roxy_registration 里已
维护好的函数，只有 driver 构建方式和 2FA 处理不同。

本路径相对 Roxy/Cloak 的差异：
    1. driver 由 core.chrome_driver.build_chrome_driver 提供，零凭证依赖；
    2. ENABLE_2FA 打开时会真正执行浏览器内 2FA（core.browser_2fa），
       拿到 totp_secret 后随账号一起落库，输出完整的
       `邮箱----token----totp` 整行。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from config import chrome as _cfg
from config import twofa as _twofa_cfg
from core.account_export import save_account_data
from core.browser_2fa import setup_2fa_via_browser
from core.browser_password import setup_password_via_browser
from core.chrome_driver import build_chrome_driver
from core.email_provider import wait_for_otp, resolve_email_source
from core.humanize import delay as human_delay

# 复用 Roxy 注册流程里已维护好的页面操作函数。
from core.roxy_registration import (  # noqa: F401
    _maybe_accept, _submit_email_and_wait_next, _fill_password_page_if_present,
    _clear_otp_inputs, _type_otp, _click_continue, _wait_after_email_otp_submit,
    _submit_email_otp_and_settle,
    _click_resend_email_otp, _complete_profile_page, _fetch_chatgpt_session, _check_manual_stop,
)

logger = logging.getLogger(__name__)


def _wait_cloudflare_challenge(driver, timeout: float = 90.0) -> bool:
    """等待 Cloudflare「Just a moment...」挑战页自动放行。

    有头 Chrome 一般数秒内就能过，但出口 IP 被 Cloudflare 重点关注时会明显变慢。
    这里轮询页面标题与输入框，直到脱离挑战页或超时。返回 True 表示页面已就绪。
    """
    end = time.time() + timeout
    waited = 0.0
    announced = False
    while time.time() < end:
        try:
            state = driver.execute_script(
                "return {title: document.title || '',"
                " inputs: document.querySelectorAll("
                "'input[type=email],input[name=email],input[type=text]').length,"
                " url: location.href};"
            ) or {}
        except Exception:
            state = {}
        title = str(state.get("title") or "")
        low = title.lower()
        challenge = ("just a moment" in low) or ("attention required" in low) or ("checking your browser" in low)
        if not challenge and (int(state.get("inputs") or 0) > 0 or "chatgpt" in low or "openai" in low):
            if announced:
                logger.info("[Chrome注册] Cloudflare 挑战已放行（耗时约 %.0f 秒）", waited)
            return True
        if challenge and not announced:
            logger.info(
                "[Chrome注册] 检测到 Cloudflare 挑战页（title=%r），等待自动放行（最长 %.0f 秒）…",
                title[:60], timeout,
            )
            announced = True
        time.sleep(3)
        waited += 3
    logger.warning("[Chrome注册] Cloudflare 挑战等待超时（%.0f 秒），继续后续流程", timeout)
    return False


# Chromium 网络栈的瞬时错误：多由轮换代理某个出口节点抖动引起，换一次请求即可恢复，
# 不值得让整个注册任务失败。注意这里只列网络类错误——Cloudflare 403、验证码页这类
# 页面级结果应照常向上抛，避免被重试掩盖。
_TRANSIENT_NAV_ERRORS = (
    "ERR_TIMED_OUT",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_REFUSED",
    "ERR_EMPTY_RESPONSE",
    "ERR_NETWORK_CHANGED",
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_SOCKET_NOT_CONNECTED",
    "ERR_HTTP2_PROTOCOL_ERROR",
    "ERR_SSL_",
    "ERR_CERT_",
    "Page.goto: Timeout",
)


def _open_url_with_retry(driver, url: str, *, attempts: int = 3) -> None:
    """打开页面，并对「代理出口抖动」类瞬时网络错误自动重试。

    chrome_driver 的 get() 直接透传 Playwright goto 的异常，此前一次
    `net::ERR_TIMED_OUT` 就会让整个注册任务失败。实测这类错误多为轮换代理某个
    出口节点的瞬时抖动（同一批会话下一次请求即恢复正常），重试是有效且廉价的。
    """
    last_exc: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            driver.get(url)
            if attempt > 1:
                logger.info("[Chrome注册] 重试第 %s 次后页面打开成功：%s", attempt, url)
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            message = str(exc)
            if not any(token in message for token in _TRANSIENT_NAV_ERRORS):
                raise
            if attempt >= attempts:
                break
            logger.warning(
                "[Chrome注册] 页面打开失败（%s/%s），疑似代理出口抖动，准备重试：%s",
                attempt, attempts, message.splitlines()[0][:140],
            )
            time.sleep(2.0 * attempt)
            try:
                driver.get("about:blank")
            except Exception:
                pass
    raise last_exc or RuntimeError(f"页面打开失败: {url}")


def _clear_known_error_page(driver) -> None:
    """若当前停在 auth 错误页，清掉 hash/query 回到登录页，便于重试。"""
    try:
        url = str(driver.current_url or "")
    except Exception:
        return
    if "auth/error" not in url:
        return
    reason = "限流" if "error=undefined" in url else "服务端错误"
    logger.warning("[Chrome注册] 当前停在 auth 错误页（疑似%s）：%s", reason, url[:120])


def run_chrome_registration(
    email: str,
    name: str,
    birthday: str,
    proxy: str = None,
    otp_code: str = None,
    batch_dir: Path | None = None,
) -> dict:
    """本机 Chrome 自动化注册入口。"""
    driver = None
    opened = None
    create_acknowledged = False
    openai_password: str | None = None
    try:
        driver, opened = build_chrome_driver(proxy=proxy)
        logger.info("[Chrome注册] 开始：%s，profile=%s", email, opened.profile_id)

        otp_after_ts = time.time()
        logger.info("[Chrome注册] 打开登录页：https://chatgpt.com/auth/login")
        _open_url_with_retry(driver, "https://chatgpt.com/auth/login")
        human_delay("navigate")
        _wait_cloudflare_challenge(
            driver, timeout=float(getattr(_cfg, "CHROME_CF_CHALLENGE_WAIT", 90) or 90)
        )
        _maybe_accept(driver)
        _check_manual_stop()

        next_state = _submit_email_and_wait_next(driver, email, attempts=3)
        _check_manual_stop()

        # 命中 create-account/password 页时设置密码；密码来自
        # config.register.REGISTER_PASSWORD，为空则自动随机生成 14 位强密码。
        openai_password = (
            None if next_state == "otp" else _fill_password_page_if_present(driver, email, timeout=25)
        )
        _check_manual_stop()

        current_otp = otp_code
        max_otp_attempts = 3
        for otp_attempt in range(1, max_otp_attempts + 1):
            if current_otp is None:
                logger.info(
                    "[Chrome注册][OTP] 等待验证码：%s（第 %s/%s 次）",
                    email, otp_attempt, max_otp_attempts,
                )
                try:
                    current_otp = wait_for_otp(email, after_ts=otp_after_ts)
                except Exception as exc:
                    if otp_attempt >= max_otp_attempts:
                        raise
                    logger.warning(
                        "[Chrome注册][OTP] 一直未收到验证码，点击“重新发送电子邮件”后继续等待"
                        "（下一轮 %s/%s）：%s: %s",
                        otp_attempt + 1, max_otp_attempts, type(exc).__name__, str(exc)[:180],
                    )
                    otp_after_ts = time.time()
                    _click_resend_email_otp(driver, timeout=25)
                    human_delay("api")
                    current_otp = None
                    continue
            logger.info("[Chrome注册][OTP] 收到验证码：%s", current_otp)
            # 提交验证码并自动处理 stuck：页面未前进且无报错时，**重载页面后重提同一个码**，
            # 避免把「提交动作没生效」误判成「码已失效」而白白换码（job 172/175 的教训）。
            outcome = _submit_email_otp_and_settle(driver, current_otp, attempts=3, wait_timeout=10)
            if outcome == "accepted":
                break
            # invalid（页面明确报错）或 stuck（多次重提仍不前进）→ 换新码再来一轮
            if otp_attempt >= max_otp_attempts:
                raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
            logger.warning(
                "[Chrome注册][OTP] 验证码未被接受（%s），点击“重新发送电子邮件”换新码"
                "（下一轮 %s/%s）", outcome, otp_attempt + 1, max_otp_attempts,
            )
            otp_after_ts = time.time()
            _click_resend_email_otp(driver, timeout=25)
            human_delay("api")
            current_otp = None

        profile_submitted = _complete_profile_page(driver, name, birthday, timeout=60)
        if profile_submitted:
            create_acknowledged = True
            human_delay("post_auth")

        session_info = _fetch_chatgpt_session(driver, timeout=120)
        access_token = session_info["accessToken"]
        logger.info("[Chrome注册] 已拿到 accessToken：%s", email)

        # ============ 补设账号密码（受 config.CHROME_PASSWORD_SETUP 控制）============
        # 新版 OpenAI 注册流默认走无密码（一次性验证码）入口，不会出现
        # create-account/password 页，所以上面 openai_password 通常为 None。
        # 这里到「设置 → Security and login」走 UI 流补设密码，使导出账号带真实密码。
        if openai_password is None and bool(getattr(_cfg, "CHROME_PASSWORD_SETUP", True)):
            try:
                openai_password = setup_password_via_browser(
                    driver,
                    email,
                    # 接受可选的 max_wait：密码环节触发重发后需要更长的等待窗
                    # （OpenAI 重发有数分钟节流，配置默认的 300s 会在邮件到达前超时）。
                    otp_fetcher=lambda after_ts, max_wait=None: wait_for_otp(
                        email, after_ts=after_ts, max_wait=max_wait
                    ),
                    prefix="[Chrome注册][Password]",
                )
                if openai_password:
                    # 密码变更后重拉一次会话，确保落库 token 与最终登录态一致。
                    try:
                        session_info = _fetch_chatgpt_session(driver, timeout=120)
                        access_token = session_info["accessToken"]
                    except Exception as exc:
                        logger.warning(
                            "[Chrome注册][Password] 密码设置后重拉会话失败，沿用旧 token：%s: %s",
                            type(exc).__name__, str(exc)[:160],
                        )
                else:
                    logger.warning("[Chrome注册][Password] 未能设置账号密码，本次账号不含密码")
            except Exception as exc:
                logger.error("[Chrome注册][Password] 设置失败：%s: %s", type(exc).__name__, exc)
                logger.debug("[Chrome注册][Password] 失败详情", exc_info=True)
        elif openai_password:
            logger.info("[Chrome注册] 注册流已自带密码页，已设置 %s 位密码", len(openai_password))
        else:
            logger.debug("[Chrome注册] 已跳过补设密码 (CHROME_PASSWORD_SETUP=False)")

        # ==================== 2FA（受 config.ENABLE_2FA 控制）====================
        totp_secret: str | None = None
        if _twofa_cfg.ENABLE_2FA:
            try:
                totp_secret = setup_2fa_via_browser(
                    driver,
                    email,
                    password=openai_password,
                    prefix="[Chrome注册][2FA]",
                )
                if not totp_secret:
                    logger.warning("[Chrome注册][2FA] 未能取得 TOTP secret，本次账号不含 2FA")
            except Exception as exc:
                logger.error("[Chrome注册][2FA] 设置失败：%s: %s", type(exc).__name__, exc)
                logger.debug("[Chrome注册][2FA] 失败详情", exc_info=True)
        else:
            logger.debug("[Chrome注册] 已跳过 2FA 设置 (ENABLE_2FA=False)")

        # ==================== Codex OAuth（默认关闭）====================
        codex_result = {
            "status": "skipped",
            "ok": True,
            "message": "ENABLE_CODEX_AUTO=False，跳过 Codex",
        }
        try:
            from config import codex as _codex_cfg

            if bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False)):
                from core.roxy_codex_oauth import run_roxy_codex_oauth

                logger.info("[Chrome注册][Codex] ENABLE_CODEX_AUTO=True，复用当前 Chrome 窗口执行 Codex 授权")
                _check_manual_stop()
                codex_result = run_roxy_codex_oauth(
                    email,
                    reuse_existing_profile=True,
                    existing_driver=driver,
                    existing_opened=opened,
                    force=True,
                    clear_existing_state=True,
                )
            else:
                logger.info("[Chrome注册][Codex] ENABLE_CODEX_AUTO=False，注册后跳过 Codex OAuth")
        except Exception as exc:
            codex_result = {
                "status": "failed",
                "ok": False,
                "message": f"{type(exc).__name__}: {str(exc)[:180]}",
            }

        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=resolve_email_source(email),
            proxy_used=((opened.raw or {}).get("proxy") if opened else None) or proxy or None,
            batch_dir=batch_dir,
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "chrome": {"profile_id": opened.profile_id, "open_result": opened.raw},
                "registration_password": openai_password,
                "codex": codex_result,
            },
        )
        codex_ok = codex_result.get("ok") or codex_result.get("status") == "skipped"
        return {
            "success": bool(codex_ok),
            "email": email,
            "account_id": account_id,
            "access_token": access_token,
            "totp_secret": totp_secret,
            "codex": codex_result,
            "error": None if codex_ok else f"Codex 未完成: {codex_result.get('message')}",
        }
    except Exception as exc:
        # 先判断是否停在 OpenAI 的 auth 错误页：这类失败与代码无关，
        # 多为同一出口 IP 短时间注册过多被限流，给出明确提示避免误判。
        try:
            _final_url = str(driver.current_url or "") if driver else ""
        except Exception:
            _final_url = ""
        if "auth/error" in _final_url:
            _tip = (
                "同一出口 IP 短时间注册过多触发 OpenAI 限流"
                if "error=undefined" in _final_url
                else "OpenAI 返回 auth 错误页（可能为限流或服务端拦截）"
            )
            logger.error(
                "[Chrome注册] 本次注册被 OpenAI 拒绝：%s。当前出口 IP 已受限，"
                "建议更换代理节点（不同地区）或等待 20~30 分钟冷却后重试。url=%s",
                _tip, _final_url[:120],
            )
        logger.error("[Chrome注册] 失败：%s: %s", type(exc).__name__, exc)
        logger.debug("[Chrome注册] 失败详情", exc_info=True)
        try:
            from core.email_provider import release_email

            release_email(
                email,
                status="failed" if create_acknowledged else "available",
                note=f"Chrome注册失败: {str(exc)[:180]}",
            )
        except Exception:
            pass
        return {"success": False, "email": email, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    finally:
        if driver and not bool(_cfg.CHROME_KEEP_BROWSER_OPEN):
            try:
                driver.quit()
            except Exception:
                pass
