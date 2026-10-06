# -*- coding: utf-8 -*-
"""在已登录的浏览器会话里开启 TOTP MFA（UI 流）。

为什么走 UI 流，而不是 core.account_export.setup_2fa 的协议流：
    setup_2fa 依赖 curl_cffi 会话，而该协议驱动的会话特征正被 OpenAI shadow block
    （流程能走通但静默不发信）。浏览器 UI 流与注册同会话、同指纹、同 IP，风控一致性
    最好，且 2026-10-04 已用 Playwright 实测跑通。
    另外 setup_2fa 要求 accessToken 内嵌的 pwd_auth_time 足够新鲜，需要额外做一次
    邮箱 OTP 重认证；UI 流在刚注册/刚登录的会话下可直接开启，无需重认证。

密钥获取策略（按可靠性排序）：
    1. 弹窗内「Trouble scanning?」——点击后页面直接以文本展示 32 位 base32 密钥，
       比解二维码可靠得多，作为首选。
    2. otpauth:// URI 正则兜底（部分版本会渲染出二维码的 alt/文本）。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

import pyotp

logger = logging.getLogger(__name__)

_SETTINGS_HASH = "#settings/Security"
# base32 密钥：TOTP secret 固定 32 位、字符集 A-Z2-7
_SECRET_RE = re.compile(r"\b[A-Z2-7]{32}\b")
_OTPAUTH_RE = re.compile(r"otpauth://[^\s\"'<>\\]+")


def _log(prefix: str, msg: str, *args) -> None:
    logger.info("%s " + msg, prefix, *args)


def _current_url(driver) -> str:
    try:
        return str(driver.current_url or "")
    except Exception:
        return ""


def _dismiss_onboarding(driver) -> bool:
    """注册后首屏可能是「You're all set」引导层，会盖住设置页导致 hash 跳转无效。

    只在识别到引导文案时才点按钮，避免误点页面上的其他 Continue。
    """
    try:
        text = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
    except Exception:
        text = ""
    if not re.search(r"you'?re all set|welcome to chatgpt|by continuing, you agree", str(text), re.I):
        return False
    clicked = _click_text(driver, r"^(continue|ok(ay)?|got it|next)$", timeout=8)
    if clicked:
        logger.info("[2FA] 检测到注册引导层，已点击「Continue」关闭")
        time.sleep(3)
    return clicked


def _settings_ready(driver) -> bool:
    """判断「设置 → Security」面板是否真的渲染出来了。"""
    try:
        return bool(driver.execute_script(
            _visible_filter_js()
            + r"""
            const hasSearch = [...document.querySelectorAll('input')].some(
                el => vis(el) && (el.placeholder || '').toLowerCase().includes('search settings'));
            const hasSecTab = [...document.querySelectorAll('button,[role=tab]')].some(
                el => vis(el) && /security and login/i.test(el.innerText || ''));
            return hasSearch && hasSecTab;
            """
        ))
    except Exception:
        return False


def _open_security_settings(driver, timeout: int = 45) -> None:
    """打开「设置 → Security and login」，并等面板真正就绪。"""
    _dismiss_onboarding(driver)
    if "chatgpt.com" not in _current_url(driver):
        driver.get("https://chatgpt.com/")
        time.sleep(5)
        _dismiss_onboarding(driver)

    # 设置页是 hash 路由：直接 get 带 hash 的 URL 在已加载页面上不一定重新渲染，
    # 因此先改 hash，等面板出现；仍未出现时再整页跳带 hash 的 URL 兜底。
    end = time.time() + timeout
    while time.time() < end:
        try:
            driver.execute_script(f"location.hash = '{_SETTINGS_HASH}';")
        except Exception:
            pass
        wait_end = time.time() + 12
        while time.time() < wait_end:
            if _settings_ready(driver):
                return
            time.sleep(1)
        try:
            driver.get(f"https://chatgpt.com/{_SETTINGS_HASH}")
        except Exception:
            pass
        time.sleep(4)
        _dismiss_onboarding(driver)
    logger.info("[2FA] 设置页在 %ss 内未就绪，继续尝试后续步骤", timeout)


def _visible_filter_js() -> str:
    return (
        "const vis = el => { const r = el.getBoundingClientRect();"
        " return !!el && r.width > 0 && r.height > 0; };"
    )


def _find_mfa_switch_js() -> str:
    """定位 MFA 行里的开关。返回 DOM 元素或 null。"""
    return (
        _visible_filter_js()
        + r"""
        const rows = [...document.querySelectorAll('div,li,section')].filter(e => {
            const t = e.innerText || '';
            return vis(e) && /Authenticator app/i.test(t) && t.length < 260
                && !!e.querySelector('[role=switch],button');
        });
        rows.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
        if (!rows.length) return null;
        return rows[0].querySelector('[role=switch],button');
        """
    )


def _read_switch_state(driver) -> dict | None:
    """读取 MFA 开关状态：{checked, tag} 或 None。"""
    try:
        result = driver.execute_script(
            _find_mfa_switch_js().replace(
                "return rows[0].querySelector('[role=switch],button');",
                "const sw = rows[0].querySelector('[role=switch],button');"
                " return sw ? {checked: sw.getAttribute('aria-checked'), tag: sw.tagName} : null;",
            )
        )
        return result if isinstance(result, dict) else None
    except Exception as exc:
        logger.debug("[2FA] 读取 MFA 开关状态失败：%s: %s", type(exc).__name__, exc)
        return None


def _click_text(driver, pattern: str, timeout: float = 8.0) -> bool:
    """按可见文本点击 button/a/[role=button]。"""
    js = (
        _visible_filter_js()
        + f"""
        const re = new RegExp({pattern!r}, 'i');
        const cands = [...document.querySelectorAll('button,a,[role=button]')].filter(vis);
        const btn = cands.find(el => re.test((el.innerText || el.textContent || '').trim()));
        if (!btn) return false;
        try {{ btn.scrollIntoView({{block: 'center'}}); }} catch (_) {{}}
        btn.click();
        return true;
        """
    )
    end = time.time() + timeout
    while time.time() < end:
        try:
            if driver.execute_script(js):
                return True
        except Exception as exc:
            logger.debug("[2FA] 点击文本 %r 失败：%s: %s", pattern, type(exc).__name__, exc)
        time.sleep(0.8)
    return False


def _grab_secret(driver) -> str | None:
    """从页面文本里抓 base32 密钥，otpauth URI 兜底。"""
    try:
        text = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
    except Exception:
        text = ""
    match = _SECRET_RE.search(str(text))
    if match:
        return match.group(0)
    uri_match = _OTPAUTH_RE.search(str(text))
    if uri_match:
        uri = uri_match.group(0).replace("\\u0026", "&")
        if "secret=" in uri:
            return uri.split("secret=")[-1].split("&")[0]
    return None


def _fill_totp_code(driver, code: str) -> bool:
    """把动态码写进弹窗输入框（React 受控组件需走 native setter）。"""
    try:
        from selenium.webdriver.common.by import By
        from core.roxy_registration import _visible

        elements = [
            e for e in driver.find_elements(
                By.CSS_SELECTOR,
                "input[name='totp_otp'],input[autocomplete='one-time-code'],input[inputmode='numeric']",
            )
            if _visible(e)
        ]
        if elements:
            elements[0].send_keys(code)
            return True
    except Exception as exc:
        logger.debug("[2FA] 真实输入动态码失败，回退 JS：%s: %s", type(exc).__name__, exc)

    js = (
        _visible_filter_js()
        + f"""
        const inp = [...document.querySelectorAll(
            "input[name='totp_otp'],input[autocomplete='one-time-code'],input[inputmode='numeric']"
        )].find(vis);
        if (!inp) return false;
        inp.focus();
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        if (setter) setter.call(inp, {code!r}); else inp.value = {code!r};
        inp.dispatchEvent(new Event('input', {{bubbles: true}}));
        inp.dispatchEvent(new Event('change', {{bubbles: true}}));
        return true;
        """
    )
    try:
        return bool(driver.execute_script(js))
    except Exception as exc:
        logger.debug("[2FA] JS 填码失败：%s: %s", type(exc).__name__, exc)
        return False


def _handle_reauth(driver, password: str | None, prefix: str) -> bool:
    """点开开关后若跳到 auth.openai.com，完成身份验证。返回是否成功通过。"""
    _log(prefix, "开关要求身份验证，尝试用账号密码通过 ...")
    if not password:
        _log(prefix, "未提供密码，无法自动通过身份验证")
        return False
    if not _click_text(driver, r"continue with password", timeout=10):
        _log(prefix, "未找到「Continue with password」入口")
        return False
    time.sleep(3)
    js = (
        _visible_filter_js()
        + """
        const inp = [...document.querySelectorAll("input[type='password']")].find(vis);
        if (!inp) return false;
        inp.focus();
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        if (setter) setter.call(inp, %s); else inp.value = %s;
        inp.dispatchEvent(new Event('input', {bubbles: true}));
        inp.dispatchEvent(new Event('change', {bubbles: true}));
        return true;
        """
    ) % (_js_str(password), _js_str(password))
    try:
        driver.execute_script(js)
    except Exception as exc:
        _log(prefix, f"填写密码失败：{type(exc).__name__}: {exc}")
        return False
    time.sleep(1)
    _click_text(driver, r"^(continue|verify|next)$", timeout=8)
    time.sleep(5)
    return "auth.openai.com" not in _current_url(driver)


def _js_str(value: str) -> str:
    """生成安全的 JS 字符串字面量（避免引号/反斜杠注入）。"""
    import json

    return json.dumps(str(value))


class _PlaywrightPageDriver:
    """把原生 Playwright Page 适配成本模块所需的 driver 子集。

    browser_use / skyvern 路径直接持有 Playwright Page（没有 Selenium 门面），
    这里补一层薄适配，让它们复用同一套 2FA UI 流，避免两套实现漂移。
    """

    def __init__(self, page: Any, timeout_ms: int = 60000):
        self._page = page
        self._timeout_ms = timeout_ms

    @property
    def current_url(self) -> str:
        try:
            return str(getattr(self._page, "url", "") or "")
        except Exception:
            return ""

    def get(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)

    def execute_script(self, script: str, *args: Any) -> Any:
        # 与 CloakSeleniumDriver._evaluate 保持同一语义：脚本主体里写 `return ...`
        wrapper = """({script, args}) => {
          const fn = new Function(...args.map((_, i) => 'a' + i), script);
          return fn(...args);
        }"""
        return self._page.evaluate(wrapper, {"script": script, "args": list(args)})

    def find_elements(self, by: Any, selector: str) -> list:
        from core.cloakbrowser_driver import CloakElement

        loc = self._page.locator(selector)
        try:
            count = min(int(loc.count()), 200)
        except Exception:
            count = 0
        return [CloakElement(self._page, loc.nth(i)) for i in range(count)]


def adapt_playwright_page(page: Any, timeout_ms: int = 60000) -> _PlaywrightPageDriver:
    """把原生 Playwright Page 包装成 driver 门面，供 browser_use / skyvern 复用。"""
    return _PlaywrightPageDriver(page, timeout_ms=timeout_ms)


def _setup_2fa_once(
    driver,
    email: str,
    password: str | None = None,
    prefix: str = "[2FA]",
    timeout: int = 90,
) -> str | None:
    """单次尝试：在已登录的浏览器会话里开启 TOTP MFA。

    失败点几乎都是「SPA 还没渲染完」这类时序问题（设置页未就绪 / 开关找不到 /
    弹窗未出现 / 页面文本里抓不到 base32），因此由 setup_2fa_via_browser 负责重试，
    本函数只做一次干净尝试。

    Returns:
        TOTP secret（32 位 base32）；本次未能开启时返回 None
    """
    _log(prefix, f"开始浏览器内 2FA 设置（UI 流）：{email}")
    _open_security_settings(driver)

    # 等设置项出现（hash 路由 + SPA 渲染存在延迟）
    end = time.time() + 45
    state = None
    round_i = 0
    while time.time() < end:
        state = _read_switch_state(driver)
        if state:
            break
        round_i += 1
        if round_i % 4 == 0:
            # 兜底：显式点击「Security and login」标签，确保右侧面板渲染
            _click_text(driver, r"^security and login$", timeout=1.5)
        time.sleep(1.5)
    if state is None:
        _log(prefix, "未能在设置页找到 MFA 开关（'Authenticator app' 行）")
        return None

    _log(prefix, f"MFA 开关状态：{state}")
    if str(state.get("checked") or "").lower() == "true":
        _log(prefix, "MFA 开关已处于开启状态，跳过（无法回读既有 secret）")
        return None

    # 点击开关 → 打开配置弹窗
    try:
        switch = driver.execute_script(_find_mfa_switch_js())
    except Exception as exc:
        _log(prefix, f"定位 MFA 开关失败：{type(exc).__name__}: {exc}")
        return None
    if switch is None:
        _log(prefix, "MFA 开关元素为空")
        return None
    try:
        switch.click()
    except Exception:
        # 元素被遮挡等情况下退回 JS 点击
        try:
            driver.execute_script(
                _visible_filter_js()
                + """
                const rows = [...document.querySelectorAll('div,li,section')].filter(e => {
                    const t = e.innerText || '';
                    return vis(e) && /Authenticator app/i.test(t) && t.length < 260
                        && !!e.querySelector('[role=switch],button');
                });
                rows.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
                const sw = rows.length ? rows[0].querySelector('[role=switch],button') : null;
                if (sw) sw.click();
                """
            )
        except Exception as exc:
            _log(prefix, f"点击 MFA 开关失败：{type(exc).__name__}: {exc}")
            return None
    time.sleep(4)

    # 可能要求重新验证身份
    if "auth.openai.com" in _current_url(driver):
        if not _handle_reauth(driver, password, prefix):
            _log(prefix, "身份验证未通过，2FA 设置中止")
            return None

    # 等弹窗里的动态码输入框出现
    end = time.time() + timeout
    dialog_ready = False
    while time.time() < end:
        try:
            if driver.execute_script(
                _visible_filter_js()
                + """
                return !!document.querySelector(
                    "input[name='totp_otp'],input[autocomplete='one-time-code'],input[inputmode='numeric']"
                );
                """
            ):
                dialog_ready = True
                break
        except Exception:
            pass
        time.sleep(1.5)
    if not dialog_ready:
        _log(prefix, "配置弹窗未出现（未等到动态码输入框）")
        return None

    # 取密钥：优先「Trouble scanning?」
    secret = None
    if _click_text(driver, r"trouble\s*scanning", timeout=6):
        time.sleep(2)
        secret = _grab_secret(driver)
        if secret:
            _log(prefix, f"经「Trouble scanning」取得密钥：{secret[:4]}...{secret[-4:]}")
    if not secret:
        secret = _grab_secret(driver)
        if secret:
            _log(prefix, f"从页面文本取得密钥：{secret[:4]}...{secret[-4:]}")
    if not secret:
        _log(prefix, "未能取得 TOTP 密钥")
        return None

    # 填码激活
    code = pyotp.TOTP(secret).now()
    if not _fill_totp_code(driver, code):
        _log(prefix, "无法填写动态码")
        return None
    time.sleep(1)
    _click_text(driver, r"^(verify|continue|confirm|done|next)$", timeout=10)
    time.sleep(5)

    # 校验：开关应变为开启
    final = _read_switch_state(driver)
    if final and str(final.get("checked") or "").lower() == "true":
        _log(prefix, f"✅ 2FA 已开启，secret={secret[:4]}...{secret[-4:]}")
        return secret
    if final:
        _log(prefix, f"2FA 激活后开关状态仍为 {final}，视为失败")
        return None
    # 弹窗关闭、读不到开关：保守认为已提交，交由调用方后续校验
    _log(prefix, "激活已提交，但未回读到开关状态（按成功处理）")
    return secret


def _mfa_switch_enabled(driver) -> bool:
    """读取 MFA 开关是否已处于开启态。读不到时按「未开启」处理（保守但不危险）。"""
    try:
        state = _read_switch_state(driver)
    except Exception:
        return False
    return bool(state) and str(state.get("checked") or "").lower() == "true"


def setup_2fa_via_browser(
    driver,
    email: str,
    password: str | None = None,
    prefix: str = "[2FA]",
    timeout: int = 90,
    attempts: int = 2,
) -> str | None:
    """在已登录的浏览器会话里开启 TOTP MFA（带安全重试）。

    为什么需要重试：2026-10-06 的 Job 165/166/167/168/172 全部败在「设置页未就绪 /
    开关找不到 / 取不到密钥」这类**渲染时序**问题上——同一套代码在 Job 176 一次通过。
    这些失败点都在「提交动态码」之前，重试是安全的，直接放弃才是把随机失败固化成
    残废账号（没有 2FA 的账号后续无法完成登录校验）。

    安全阀：每轮之间重新读取开关状态，一旦发现开关**已处于开启态**却仍没有 secret，
    说明上一轮可能已经真的开启了（只是没能回读密钥），此时再点一次开关会走到
    「已开启且无法回读既有 secret」的死角、把账号彻底锁死——因此必须立刻停手。

    Args:
        driver: 已完成注册/登录的浏览器 driver（Selenium 风格门面）
        email: 账号邮箱（仅用于日志）
        password: 账号密码，开关触发身份验证时使用；没有则无法自动通过
        prefix: 日志前缀
        timeout: 单次尝试内等待弹窗/开关生效的超时（秒）
        attempts: 总尝试次数（>=1）

    Returns:
        TOTP secret（32 位 base32）；全部尝试失败时返回 None
    """
    total = max(1, int(attempts))
    for attempt in range(1, total + 1):
        if attempt > 1:
            _log(prefix, f"第 {attempt}/{total} 次尝试开启 2FA（上轮失败多为页面未渲染完）")
            # 重载一次换取干净的 SPA 状态：设置页的 hash 路由在脏会话里常常不重渲染。
            try:
                driver.get("https://chatgpt.com/")
            except Exception as exc:
                logger.debug("[2FA] 重试前重载页面失败：%s: %s", type(exc).__name__, exc)
            time.sleep(5)
        try:
            secret = _setup_2fa_once(
                driver, email, password=password, prefix=prefix, timeout=timeout
            )
        except Exception as exc:
            _log(prefix, f"2FA 设置异常：{type(exc).__name__}: {exc}")
            secret = None
        if secret:
            return secret
        if attempt < total and _mfa_switch_enabled(driver):
            _log(prefix, "⚠️ MFA 开关已处于开启态却未取得 secret，停止重试以免锁死账号")
            return None
    _log(prefix, f"已尝试 {total} 次仍未取得 TOTP secret，本次账号不含 2FA")
    return None
