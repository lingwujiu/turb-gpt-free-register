# -*- coding: utf-8 -*-
"""在已登录的浏览器会话里补设 OpenAI 账号密码（UI 流）。

为什么需要这个模块：
    新版 OpenAI 注册流默认提供「一次性验证码（passwordless）」入口，
    roxy_registration._fill_password_page_if_present 也会主动点击该入口，
    因此整条注册流程根本不会出现 /create-account/password 页，
    extra.registration_password 恒为 None，导出的账号是没有密码的。

    要拿到真实密码，只能在拿到会话后走「设置 → Security and login → Password → Add」，
    而这属于敏感操作，OpenAI 会先要求做一次身份验证（默认发一封邮件验证码）。

流程：
    1. 打开 chatgpt.com/#settings/Security
    2. 找到 Password 行的「Add」并点击（触发的瞬间 OpenAI 会发一封身份验证邮件）
    3. 若出现身份验证页：取邮件验证码 → 填入 → 提交
    4. 出现创建密码页：填入新密码（两次口若存在则填两次）→ 提交
    5. 回到 Security 页复核 Password 行已不再是「Add」，确认设置成功

与 core.browser_2fa 的关系：
    两者都作用于「设置 → Security」页，共用日志风格与 JS 可见性判定；
    OTP 输入复用 roxy_registration._type_otp / _clear_otp_inputs / _click_continue。
"""
from __future__ import annotations

import logging
import os
import re
import time

from core.browser_2fa import _open_security_settings, _visible_filter_js
from core.roxy_registration import (
    _clear_otp_inputs,
    _click_continue,
    _registration_password,
    _submit_email_otp_and_settle,
    _type_otp,
    _wait_after_email_otp_submit,
)

logger = logging.getLogger(__name__)

# 密码环节总预算。需要容纳「首次取码 + 提交 + 卡住重载重提 + 万一换码后等重发邮件」，
# 而单次取码最长就可能吃掉 300s，240s 会因为一次取码超时而把预算直接耗尽（job 172）。
_PASSWORD_TIMEOUT = 600
# 触发重发后单次取码的等待上限：实测 OpenAI 重发有节流，新码可能 6~7 分钟才到。
_RESEND_OTP_MAX_WAIT = 450

# 设置页 Password 行处于「未设置」状态时的特征：Password 标签后紧跟 Add/Set up
_PASSWORD_ROW_UNSET_RE = re.compile(r"Password\s*\n\s*(Add|Set up|Create)", re.I)


def _log(prefix: str, msg: str, *args) -> None:
    logger.info("%s " + msg, prefix, *args)


def _current_url(driver) -> str:
    try:
        return str(driver.current_url or "")
    except Exception:
        return ""


def _page_state(driver) -> dict:
    """抓取当前页面的输入框/按钮/文本，用于状态机判断。"""
    js = _visible_filter_js() + r"""
    const inputs = [...document.querySelectorAll('input')].filter(vis).map(el => ({
      type: (el.getAttribute('type') || '').toLowerCase(),
      name: el.getAttribute('name') || '',
      id: el.id || '',
      ac: (el.getAttribute('autocomplete') || '').toLowerCase(),
      im: (el.getAttribute('inputmode') || '').toLowerCase(),
      ph: el.getAttribute('placeholder') || '',
      filled: !!(el.value || '').length
    }));
    const buttons = [...document.querySelectorAll('button,[role=button],a')].filter(vis)
      .map(el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim())
      .filter(t => t && t.length < 60);
    return {
      url: location.href,
      inputs,
      buttons,
      text: (document.body ? document.body.innerText : '').replace(/\u00a0/g, ' ').slice(0, 1500)
    };
    """
    try:
        return driver.execute_script(js) or {}
    except Exception as exc:
        logger.debug("[Password] 读取页面状态失败：%s: %s", type(exc).__name__, exc)
        return {"url": _current_url(driver), "inputs": [], "buttons": [], "text": ""}


def _has_otp_input(state: dict) -> bool:
    for item in state.get("inputs") or []:
        ac = str(item.get("ac") or "")
        name = str(item.get("name") or "").lower()
        im = str(item.get("im") or "")
        typ = str(item.get("type") or "")
        if ac == "one-time-code" or im == "numeric" or typ == "tel":
            return True
        if any(k in name for k in ("code", "otp", "token")):
            return True
    return False


def _is_new_password_page(state: dict) -> bool:
    """判断当前是否为「创建/设置新密码」页，而不是「输入现有密码」页。"""
    passwords = [i for i in (state.get("inputs") or []) if str(i.get("type")) == "password"]
    if not passwords:
        return False
    if len(passwords) >= 2:
        return True  # 两格（新密码 + 确认）→ 一定是创建页
    if any(str(i.get("ac")) == "new-password" for i in passwords):
        return True
    text = str(state.get("text") or "")
    if re.search(r"(create|set(?:ting)? up|new|choose)\s+(a\s+)?password", text, re.I):
        return True
    return False


def _password_row_unset(state: dict) -> bool:
    return bool(_PASSWORD_ROW_UNSET_RE.search(str(state.get("text") or "")))


def _probe_password_row(driver) -> dict:
    """探测设置页 Password 行的状态（不依赖换行格式，直接看 DOM 结构）。

    Returns:
        {"found": bool, "hasAdd": bool, "hasChange": bool, "row": str}
        found=False 表示右侧 Security 面板还没渲染出来。
    """
    js = _visible_filter_js() + r"""
    const norm = el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    const labels = [...document.querySelectorAll('div,span,p,label,dt,h2,h3')]
      .filter(el => vis(el) && /^password$/i.test((el.innerText || '').trim()));
    if (!labels.length) return {found: false, hasAdd: false, hasChange: false, row: ''};
    const label = labels[labels.length - 1];
    // 注意：必须从 label.parentElement 开始向上爬。
    // 若从 label 自身开始，norm(label) === 'Password' 会立刻命中 /password/i 并 break，
    // 导致永远读不到同一行里的 Add/Change。
    let scope = label.parentElement;
    let rowText = '';
    for (let i = 0; i < 6 && scope; i++, scope = scope.parentElement) {
      const t = norm(scope);
      if (t.length > 300) continue;
      if (/(Add|Set up|Create|Change|Remove|Reset)\b/i.test(t)) { rowText = t; break; }
    }
    if (!rowText) return {found: true, hasAdd: false, hasChange: false, row: ''};
    return {
      found: true,
      hasAdd: /(^|\s)(Add|Set up|Create)(\s|$)/i.test(rowText),
      hasChange: /(^|\s)(Change|Remove|Reset)(\s|$)/i.test(rowText),
      row: rowText.slice(0, 160)
    };
    """
    try:
        result = driver.execute_script(js)
        return result if isinstance(result, dict) else {"found": False, "hasAdd": False, "hasChange": False, "row": ""}
    except Exception as exc:
        logger.debug("[Password] 探测 Password 行失败：%s: %s", type(exc).__name__, exc)
        return {"found": False, "hasAdd": False, "hasChange": False, "row": ""}


def _click_security_tab(driver) -> bool:
    """显式点击设置面板左侧的「Security and login」标签，确保右侧内容渲染。"""
    return _click_text(driver, r"^security and login$", timeout=1.5)


def _dump_debug_html(driver, tag: str) -> None:
    """排障用：把当前页面 HTML 落盘（仅在 CHROME_PASSWORD_DEBUG 打开时）。"""
    if not os.environ.get("CHROME_PASSWORD_DEBUG"):
        return
    try:
        html = driver.execute_script("return document.documentElement.outerHTML") or ""
        path = f"/tmp/browser_password_debug_{tag}.html"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        logger.info("[Password] 调试 HTML 已落盘：%s (%d bytes)", path, len(html))
    except Exception as exc:
        logger.debug("[Password] 落盘调试 HTML 失败：%s: %s", type(exc).__name__, exc)


# ---------------------------------------------------------------------------
# 页面操作
# ---------------------------------------------------------------------------

def _click_password_add(driver) -> dict:
    """点击 Password 行的「Add」按钮。"""
    js = _visible_filter_js() + r"""
    const norm = el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    // 1) 找到文本恰为 Password 的最小可见元素
    const labels = [...document.querySelectorAll('div,span,p,label,dt,h2,h3')]
      .filter(el => vis(el) && /^password$/i.test((el.innerText || '').trim()));
    if (!labels.length) return {ok: false, reason: 'no_password_label'};
    const label = labels[labels.length - 1];
    // 2) 逐层向上，在足够小的容器里找文本为 Add 的可见元素
    let scope = label;
    let target = null;
    let rowText = '';
    for (let i = 0; i < 6 && scope; i++, scope = scope.parentElement) {
      const t = (scope.innerText || '');
      if (t.length > 400) continue;
      const clickables = [...scope.querySelectorAll('button,[role=button],a')].filter(vis);
      const add = clickables.find(el => /^(add|set up|create)$/i.test(norm(el)));
      if (add) { target = add; rowText = t.replace(/\s+/g, ' ').slice(0, 140); break; }
    }
    if (!target) {
      // 兜底：容器内文本恰为 Add 的任意元素（可能是无 role 的 div）
      scope = label;
      for (let i = 0; i < 6 && scope; i++, scope = scope.parentElement) {
        const t = (scope.innerText || '');
        if (t.length > 400) continue;
        const any = [...scope.querySelectorAll('*')].filter(el => vis(el) && /^add$/i.test((el.innerText || '').trim()));
        if (any.length) { target = any[any.length - 1]; rowText = t.replace(/\s+/g, ' ').slice(0, 140); break; }
      }
    }
    if (!target) return {ok: false, reason: 'no_add_in_row', labelText: (label.parentElement ? (label.parentElement.innerText || '') : '').replace(/\s+/g, ' ').slice(0, 200)};
    try { target.scrollIntoView({block: 'center'}); } catch (_) {}
    let clicked = false;
    try { target.click(); clicked = true; } catch (_) {}
    if (!clicked) {
      try {
        target.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
        clicked = true;
      } catch (_) {}
    }
    return {ok: clicked, row: rowText, tag: target.tagName, role: target.getAttribute('role'), text: (target.innerText || '').trim().slice(0, 40)};
    """
    try:
        result = driver.execute_script(js)
        return result if isinstance(result, dict) else {"ok": False, "reason": "non_dict_result"}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _fill_new_password(driver, password: str) -> dict:
    """在创建密码页填入新密码（React 受控组件走 native setter）。"""
    import json

    pwd = json.dumps(str(password))
    js = _visible_filter_js() + f"""
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    const setVal = (el, v) => {{
      el.focus();
      if (setter) setter.call(el, v); else el.value = v;
      el.dispatchEvent(new Event('input', {{bubbles: true}}));
      el.dispatchEvent(new Event('change', {{bubbles: true}}));
    }};
    const inputs = [...document.querySelectorAll("input[type='password']")].filter(vis);
    if (!inputs.length) return {{ok: false, reason: 'no_password_input'}};
    for (const el of inputs) setVal(el, {pwd});
    return {{ok: true, count: inputs.length}};
    """
    try:
        result = driver.execute_script(js)
        return result if isinstance(result, dict) else {"ok": False, "reason": "non_dict_result"}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _click_text(driver, pattern: str, timeout: float = 6.0) -> bool:
    """按可见文本点击 button/a/[role=button]。"""
    js = _visible_filter_js() + f"""
    const re = new RegExp({pattern!r}, 'i');
    const cands = [...document.querySelectorAll('button,a,[role=button]')].filter(vis);
    const btn = cands.find(el => re.test((el.innerText || el.textContent || '').trim()));
    if (!btn) return false;
    try {{ btn.scrollIntoView({{block: 'center'}}); }} catch (_) {{}}
    btn.click();
    return true;
    """
    end = time.time() + timeout
    while time.time() < end:
        try:
            if driver.execute_script(js):
                return True
        except Exception as exc:
            logger.debug("[Password] 点击文本 %r 失败：%s: %s", pattern, type(exc).__name__, exc)
        time.sleep(0.7)
    return False


def _ensure_totp_switch_disabled(driver, prefix: str) -> None:
    """身份验证页偶发只剩「Continue with password / passkey」时，尝试切到邮箱验证。"""
    for pattern in (
        r"^(email|use email|email me|send (me )?(a )?code|send code|resend)$",
        r"continue with email",
    ):
        if _click_text(driver, pattern, timeout=2):
            _log(prefix, f"已点击邮箱验证入口：{pattern}")
            return


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def setup_password_via_browser(
    driver,
    email: str,
    otp_fetcher=None,
    password: str | None = None,
    prefix: str = "[Password]",
    timeout: int = _PASSWORD_TIMEOUT,
) -> str | None:
    """在已登录会话里补设账号密码。

    Args:
        driver: 已完成注册/登录的浏览器 driver（Selenium 风格门面）
        email: 账号邮箱（日志用）
        otp_fetcher: 可调用对象 (after_ts: float) -> str|None，
            用于取「身份验证」邮件验证码；为 None 时无法过身份验证
        password: 指定密码；为 None 时走 config.register.REGISTER_PASSWORD
            或自动随机生成 14 位强密码
        prefix: 日志前缀
        timeout: 总超时（秒），需要给邮件到达留够时间

    Returns:
        设置成功的密码；失败或无需设置时返回 None
    """
    pwd = password or _registration_password()
    if not pwd:
        _log(prefix, "未能生成密码，跳过")
        return None

    _log(prefix, f"开始补设账号密码：{email}（密码 {len(pwd)} 位）")
    _open_security_settings(driver)

    # 等右侧 Security 面板渲染出 Password 行（hash 路由 + SPA 渲染存在延迟）。
    # 兜底：显式点击「Security and login」标签，避免 hash 未触发面板切换。
    end = time.time() + 45
    probe: dict = {}
    while time.time() < end:
        probe = _probe_password_row(driver)
        if probe.get("hasAdd") or probe.get("hasChange"):
            break
        _click_security_tab(driver)
        time.sleep(2)

    if not probe.get("hasAdd"):
        if probe.get("hasChange"):
            _log(prefix, f"Password 行已是「已设置」状态，跳过（无法回读既有密码）：{probe.get('row')!r}")
        else:
            snippet = ""
            try:
                state = _page_state(driver)
                snippet = str(state.get("text") or "")[-400:]
            except Exception:
                pass
            _log(prefix, f"未识别到 Password 行的「Add」入口，跳过。probe={probe} 页面片段={snippet!r}")
            _dump_debug_html(driver, "no_add_row")
        return None

    # 点击 Add —— 这一下会触发 OpenAI 发出身份验证邮件，记下时间戳
    click_ts = time.time()
    clicked = _click_password_add(driver)
    _log(prefix, f"点击 Password → Add：{clicked}")
    if not clicked.get("ok"):
        return None
    time.sleep(4)

    deadline = time.time() + timeout
    password_filled = False
    password_submitted_at: float | None = None
    otp_requested_at = click_ts
    otp_tries = 0
    # 记录「每个验证码被提交了几次」，用于区分两种情况：
    #   · 提交一次后页面未前进且无报错 → 多半是提交动作没生效（需重新提交），**不是**码失效；
    #   · 同一个码连提交两次仍不前进 → 才判定该码对本次操作无效，需要换新码。
    # 反面教材（job 172）：旧实现用 set 只记「提交过」，页面一卡住就直接当成「码已失效」
    # 而触发重发，误伤了本来有效的码。
    code_submits: dict[str, int] = {}
    last_url = ""
    hinted_email_switch = False
    # 允许「提交 → 卡住 → 重载重提 → 再卡住 → 换码 → 重提」整条恢复链走完。
    max_otp_tries = 5
    # 触发过重发后，取码等待窗要放大：实测 OpenAI 重发有节流，新码可能 6~7 分钟才到，
    # 配置里的 300s 等待窗会在邮件到达前就超时（job 172 的教训）。
    resend_otp_max_wait = _RESEND_OTP_MAX_WAIT
    otp_wait_override: int | None = None

    while time.time() < deadline:
        state = _page_state(driver)
        url = str(state.get("url") or "")

        if url != last_url:
            _log(prefix, f"页面变化：{url[:120]} | 输入框={[(i.get('type'), i.get('name') or i.get('ac')) for i in (state.get('inputs') or [])]}")
            last_url = url

        # ---- 完成判定：已提交密码，且回到设置页、Password 行不再是 Add ----
        if password_submitted_at and "chatgpt.com" in url and "auth.openai.com" not in url:
            if not _password_row_unset(state):
                _log(prefix, f"✅ 账号密码已设置成功：{email}")
                return pwd

        # ---- 创建密码页 ----
        if _is_new_password_page(state):
            filled = _fill_new_password(driver, pwd)
            _log(prefix, f"创建密码页填写结果：{filled}")
            if filled.get("ok"):
                time.sleep(1)
                _click_continue(driver)
                password_filled = True
                password_submitted_at = time.time()
                time.sleep(5)
                continue

        # ---- 身份验证（邮件验证码）----
        if _has_otp_input(state) and not password_filled:
            if otp_tries >= max_otp_tries:
                _log(prefix, "身份验证码多次尝试未通过，中止")
                return None
            code = None
            if otp_fetcher is not None:
                try:
                    if otp_wait_override:
                        # 重发后的等待窗需要放大；旧签名不支持 max_wait 时自动降级。
                        try:
                            code = otp_fetcher(otp_requested_at, max_wait=otp_wait_override)
                        except TypeError:
                            code = otp_fetcher(otp_requested_at)
                    else:
                        code = otp_fetcher(otp_requested_at)
                except Exception as exc:
                    _log(prefix, f"取验证码失败：{type(exc).__name__}: {str(exc)[:160]}")
            if not code:
                _log(prefix, "未取得身份验证码，尝试切换/重发邮件验证")
                if not hinted_email_switch:
                    _ensure_totp_switch_disabled(driver, prefix)
                    hinted_email_switch = True
                otp_requested_at = time.time()
                time.sleep(3)
                continue

            submits = code_submits.get(code, 0)
            if submits >= 2:
                # 同一个码连提交两次页面都不前进 → 这个码对本次操作确实无效了，必须换新码：
                # 主动让 OpenAI 重新发送，并前移取码时间窗 + 放大等待窗去等那封**新**邮件。
                otp_tries += 1
                _log(prefix, f"验证码 {code} 已连续提交 {submits} 次仍停在验证码页，判定该码无效，触发重新发送")
                try:
                    from core.roxy_registration import _click_resend_email_otp

                    resend = _click_resend_email_otp(driver, timeout=20)
                    _log(prefix, f"重新发送结果：{resend.get('ok')}")
                except Exception as exc:
                    _log(prefix, f"触发重发验证码失败：{type(exc).__name__}: {str(exc)[:120]}")
                # 旧码已确认无效，继续读它毫无意义：前移时间窗 + 放大等待窗，等新邮件。
                otp_requested_at = time.time()
                otp_wait_override = resend_otp_max_wait
                time.sleep(3)
                continue

            otp_tries += 1
            code_submits[code] = submits + 1
            _log(prefix, f"填入身份验证码：{code}（该码第 {submits + 1} 轮提交）")
            # 提交并自动处理 stuck：页面未前进且无报错时**重载页面后重提同一个码**
            # （这类情况多半是提交动作没生效，而不是码失效）。
            # 只有连续多轮都 stuck，才由上方的 code_submits 判定升级为「换新码」。
            # 注意：此处**不**前移 otp_requested_at。该变量语义是「本轮请求发码的时刻」，
            # 只有真正换码时才前移；否则会把刚提交、其实已到达的那封邮件过滤掉。
            outcome = _submit_email_otp_and_settle(driver, code, attempts=2, wait_timeout=25)
            _log(prefix, f"验证码提交结果：{outcome}")
            time.sleep(2)
            continue

        # ---- auth.openai.com 上尚未出现可操作控件：尝试切到邮箱验证 ----
        if "auth.openai.com" in url and not password_filled and not hinted_email_switch:
            _ensure_totp_switch_disabled(driver, prefix)
            hinted_email_switch = True

        time.sleep(2)

    _log(prefix, f"补设密码超时（{timeout}s），最后 URL={_current_url(driver)[:120]}")
    return None
