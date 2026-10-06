# -*- coding: utf-8 -*-
"""本机 Chrome（Playwright channel="chrome"）驱动适配层。

与 CloakBrowser 适配层同构：底层都是 Playwright，对外暴露 Roxy 注册流程所依赖的
Selenium 风格 WebDriver 子集（见 core.roxy_registration），因此可以直接复用
roxy_registration 里的全部页面操作函数。

与 Roxy/Cloak 的唯一区别在 driver 构建方式：本驱动不需要任何指纹浏览器凭证，
直接驱动本机安装的 Google Chrome。

运行模式：默认有头（headful）；`CHROME_HEADLESS=True` 且 `CHROME_HEADLESS_UA_MASK=True`
时可后台无窗口运行——headless 下 Chrome 的 UA 会带 `HeadlessChrome/` 标记并被
Cloudflare 直接 403，故 headless 时自动把 UA 还原成本机 Chrome 的常规 UA。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from config import chrome as _cfg
from core.cloakbrowser_driver import (
    CloakElement,
    CloakSeleniumDriver,
    # 出口 IP 地理检测与代理 URL 归一化是通用逻辑，直接复用避免重复实现。
    _detect_cloak_exit_geo,
    _normalize_proxy,
)

logger = logging.getLogger(__name__)


def mask_proxy(proxy_url: str | None) -> str:
    """把代理 URL 的**密码**替换成 ***，保留用户名（含粘性会话 ID，便于排查）。

    例：http://user-country-us-session-a1b2c3d4:***@gate.example.com:8000

    只隐藏密码而不是整段 user:pass，是因为轮换代理的会话 ID 就写在用户名里，
    会话 ID 是排查「哪个账号走了哪个出口」的唯一线索，不能一起抹掉。
    """
    url = str(proxy_url or "").strip()
    if not url:
        return "无"
    try:
        parts = urlsplit(url)
    except Exception:  # pragma: no cover - urlsplit 极少抛错
        return "已配置"
    if not parts.password:
        return url
    user = unquote(parts.username) if parts.username else ""
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    userinfo = f"{user}:***@" if user else "***@"
    return urlunsplit((parts.scheme, f"{userinfo}{host}", "", "", ""))


def _playwright_proxy_kwargs(proxy_url: str | None) -> dict | None:
    """把代理 URL 拆成 Playwright `proxy=` 需要的结构。

    为什么必须拆：
        Playwright 的 proxy 参数是 `{server, username, password}` 三件套。
        商业轮换代理基本都带账密（形如 `http://user-session-xxx:pass@gate:8000`），
        直接把整条带凭据的 URL 塞进 `server` 在部分 Chrome/Playwright 组合下会
        认证失败。这里统一把 username/password 解出来单独传。

    另：Chromium 不支持 SOCKS5 用户名/密码认证，遇到 `socks5://user:pass@...`
    会直接连不上，这里给出明确警告并引导改用 http:// 入口。
    """
    url = str(proxy_url or "").strip()
    if not url:
        return None

    try:
        parts = urlsplit(url)
    except Exception:  # pragma: no cover
        return {"server": url}

    username = unquote(parts.username) if parts.username else ""
    password = unquote(parts.password) if parts.password else ""
    if not username and not password:
        return {"server": url}

    host = parts.hostname or ""
    if not host:
        return {"server": url}

    scheme = (parts.scheme or "http").lower()
    if scheme.startswith("socks"):
        logger.warning(
            "[Chrome] 代理 %s 是 SOCKS 且带账号密码。Chromium 不支持 SOCKS5 认证，"
            "该代理会连接失败——商业轮换代理请改用 http:// 入口。",
            mask_proxy(url),
        )

    netloc = f"{host}:{parts.port}" if parts.port else host
    kwargs: dict = {"server": urlunsplit((scheme, netloc, "", "", ""))}
    if username:
        kwargs["username"] = username
    if password:
        kwargs["password"] = password
    return kwargs


# ---------------------------------------------------------------------------
# Selenium 按键语义映射
# ---------------------------------------------------------------------------
# 项目里的拟人化输入 (_human_type_text) 完全按真实 Selenium 的 send_keys 语义编写：
# 逐字符调用 send_keys 依赖「追加」，send_keys(Keys.COMMAND, "a") 依赖「修饰键按住」。
# 因此本驱动必须还原这套语义，不能直接用 locator.fill() 整体替换。
try:  # pragma: no cover - 依赖 selenium 是否安装
    from selenium.webdriver.common.keys import Keys as _Keys
except Exception:  # pragma: no cover
    _Keys = None

_MODIFIER_CHARS: dict[str, str] = {}
_SPECIAL_KEY_CHARS: dict[str, str] = {}
_MODIFIER_ALIASES = {
    "command": "Meta",
    "cmd": "Meta",
    "meta": "Meta",
    "control": "Control",
    "ctrl": "Control",
    "shift": "Shift",
    "alt": "Alt",
    "option": "Alt",
}

if _Keys is not None:  # pragma: no cover - 依赖 selenium 是否安装
    for _attr, _name in (("SHIFT", "Shift"), ("CONTROL", "Control"), ("ALT", "Alt"), ("META", "Meta")):
        _val = getattr(_Keys, _attr, None)
        if _val:
            _MODIFIER_CHARS[str(_val)] = _name
    for _attr, _name in (
        ("BACKSPACE", "Backspace"),
        ("DELETE", "Delete"),
        ("TAB", "Tab"),
        ("ENTER", "Enter"),
        ("RETURN", "Enter"),
        ("ESCAPE", "Escape"),
        ("HOME", "Home"),
        ("END", "End"),
        ("LEFT", "ArrowLeft"),
        ("RIGHT", "ArrowRight"),
        ("UP", "ArrowUp"),
        ("DOWN", "ArrowDown"),
        ("PAGE_UP", "PageUp"),
        ("PAGE_DOWN", "PageDown"),
        ("INSERT", "Insert"),
        ("SPACE", "Space"),
    ):
        _val = getattr(_Keys, _attr, None)
        if _val:
            _SPECIAL_KEY_CHARS.setdefault(str(_val), _name)

# 兜底：selenium 不可用时按 WebDriver 规范硬编码（这些码位是规范固定的）。
_MODIFIER_CHARS.setdefault("\ue03d", "Meta")
_MODIFIER_CHARS.setdefault("\ue009", "Control")
_MODIFIER_CHARS.setdefault("\ue008", "Shift")
_MODIFIER_CHARS.setdefault("\ue00a", "Alt")
_SPECIAL_KEY_CHARS.setdefault("\ue003", "Backspace")
_SPECIAL_KEY_CHARS.setdefault("\ue017", "Delete")
_SPECIAL_KEY_CHARS.setdefault("\ue004", "Tab")
_SPECIAL_KEY_CHARS.setdefault("\ue007", "Enter")
_SPECIAL_KEY_CHARS.setdefault("\ue006", "Enter")
_SPECIAL_KEY_CHARS.setdefault("\ue00c", "Escape")
_SPECIAL_KEY_CHARS.setdefault("\ue010", "End")
_SPECIAL_KEY_CHARS.setdefault("\ue011", "Home")


@dataclass
class ChromeOpenResult:
    """对齐 CloakOpenResult 的返回结构，供 save_account_data 的 extra 字段使用。"""

    profile_id: str = "local-chrome"
    raw: dict | None = None


class ChromeElement(CloakElement):
    """本机 Chrome 上的元素门面。

    与 Cloak 版的关键差异：**`send_keys` 还原真实 Selenium 的「追加输入」语义**。

    Cloak 版底层用 `locator.fill(text)`，是整体替换。而项目的拟人化输入
    `roxy_registration._human_type_text` 是逐字符调用 `send_keys` 的，依赖追加语义；
    在替换语义下逐字符写入只会留下最后一个字符（实测邮箱被写成 `'p'`），
    随后「邮箱写入校验失败」→ 注册直接失败。

    这里改用 `page.keyboard` 发送真实按键事件（isTrusted，对 React 受控输入
    和风控都更友好），并完整支持修饰键组合（如 `send_keys(Keys.COMMAND, "a")`）。
    """

    def _ensure_focused(self) -> None:
        """确保焦点在元素上。已聚焦则不动，避免点击把光标落在文本中间。"""
        try:
            if bool(self._eval("el => el === document.activeElement")):
                return
        except Exception:
            pass
        try:
            self.click()
        except Exception:
            try:
                self._eval("el => el.focus()")
            except Exception:
                pass
        try:
            # 点击可能把光标落在文本中间，追加输入前统一移到末尾。
            self.page.keyboard.press("End")
        except Exception:
            pass

    def _type_text(self, text: str, held: list[str]) -> None:
        keyboard = self.page.keyboard
        type_fn = getattr(keyboard, "press_sequentially", None) or getattr(keyboard, "type", None)
        for mod in held:
            try:
                keyboard.down(mod)
            except Exception:
                pass
        try:
            if type_fn is not None:
                type_fn(text)
        finally:
            for mod in reversed(held):
                try:
                    keyboard.up(mod)
                except Exception:
                    pass

    def send_keys(self, *values: Any) -> None:
        self._ensure_focused()
        held: list[str] = []
        try:
            for value in values:
                text = "" if value is None else str(value)
                if not text:
                    continue
                alias = _MODIFIER_ALIASES.get(text.strip().lower())
                if alias:
                    held.append(alias)
                    continue
                buf = ""
                for ch in text:
                    mod = _MODIFIER_CHARS.get(ch)
                    if mod:
                        # 修饰键只记录，待后续按键/文本一起发送。
                        held.append(mod)
                        continue
                    name = _SPECIAL_KEY_CHARS.get(ch)
                    if name:
                        if buf:
                            self._type_text(buf, held)
                            held = []
                            buf = ""
                        try:
                            self.page.keyboard.press(name)
                        except Exception:
                            pass
                        continue
                    buf += ch
                if buf:
                    self._type_text(buf, held)
                    held = []
        finally:
            for mod in reversed(held):
                try:
                    self.page.keyboard.up(mod)
                except Exception:
                    pass


class ChromeSeleniumDriver(CloakSeleniumDriver):
    """本机 Chrome 上的 Selenium 风格门面（页面操作复用 Cloak 实现）。

    额外托管 Playwright 实例，确保 quit() 时把驱动进程一并收干净——
    Cloak 那边由 cloakbrowser 包自己管生命周期，本驱动需要自己管。
    """

    def __init__(self, browser: Any, context: Any | None, page: Any, playwright: Any = None):
        super().__init__(browser=browser, context=context, page=page)
        self._playwright = playwright

    # ---- 元素工厂：统一返回 ChromeElement（追加输入语义） ----

    def find_elements(self, by: Any, selector: str) -> list[ChromeElement]:
        loc = self._locator(by, selector)
        try:
            count = min(int(loc.count()), 200)
        except Exception:
            count = 0
        return [ChromeElement(self.page, loc.nth(i)) for i in range(count)]

    def find_element(self, by: Any, selector: str) -> ChromeElement:
        els = self.find_elements(by, selector)
        if not els:
            raise RuntimeError(f"找不到页面元素: {selector}")
        return els[0]

    @staticmethod
    def _unwrap_js_result(page: Any, handle: Any) -> Any:
        try:
            element = handle.as_element()
        except Exception:
            element = None
        if element is not None:
            return ChromeElement(page, handle=element)
        return CloakSeleniumDriver._unwrap_js_result(page, handle)

    def quit(self) -> None:
        super().quit()
        pw, self._playwright = self._playwright, None
        if pw is not None:
            try:
                pw.stop()
            except Exception as exc:
                logger.debug("[Chrome] 停止 Playwright 失败：%s: %s", type(exc).__name__, exc)


def _detect_chrome_version_by_cli() -> str:
    """通过命令行探测本机 Chrome 版本号；探测不到返回空串。"""
    import subprocess

    candidates: list[str] = []
    explicit = str(getattr(_cfg, "CHROME_EXECUTABLE_PATH", "") or "").strip()
    if explicit:
        candidates.append(explicit)
    candidates.extend(
        [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
    )
    for exe in candidates:
        try:
            proc = subprocess.run(
                [exe, "--version"], capture_output=True, text=True, timeout=5
            )
        except Exception:
            continue
        matched = re.search(r"(\d+\.\d+\.\d+\.\d+)", (proc.stdout or "") + (proc.stderr or ""))
        if matched:
            return matched.group(1)
    return ""


def _masked_chrome_user_agent(browser: Any = None) -> str:
    """构造与本机 Chrome 一致的常规 UA，用于 headless 下掩盖 HeadlessChrome 标记。

    headless（含 --headless=new）的 UA 形如
        Mozilla/5.0 (...) HeadlessChrome/154.0.8037.98 Safari/537.36
    Cloudflare 一旦看到 `HeadlessChrome` 就直接返回 403；按「同平台 + 同主版本号」
    还原成常规 Chrome UA 后实测正常放行。
    """
    import platform

    version = ""
    if browser is not None:
        try:
            version = str(getattr(browser, "version", "") or "")
        except Exception:
            version = ""
    if not version:
        version = _detect_chrome_version_by_cli()
    matched = re.match(r"(\d+)", version or "")
    major = matched.group(1) if matched else "131"

    system = platform.system()
    if system == "Darwin":
        plat = "Macintosh; Intel Mac OS X 10_15_7"
    elif system == "Windows":
        plat = "Windows NT 10.0; Win64; x64"
    else:
        plat = "X11; Linux x86_64"

    return (
        f"Mozilla/5.0 ({plat}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


def _build_chrome_locale_options(proxy_url: str | None = None) -> dict:
    """生成本机 Chrome 的语言/时区配置，语义与 Cloak 版保持一致。

    显式配置优先；否则按出口 IP 自动推断——出口地区与 locale 错配会显著提高被
    Cloudflare / OpenAI 风控的概率。
    """
    explicit_locale = str(getattr(_cfg, "CHROME_LOCALE", "") or "").strip()
    explicit_timezone = str(getattr(_cfg, "CHROME_TIMEZONE", "") or "").strip()
    out: dict = {}
    if explicit_locale:
        out["locale"] = explicit_locale
        out["accept_language"] = (
            f"{explicit_locale},{explicit_locale.split('-')[0]};q=0.9,en-US;q=0.8,en;q=0.7"
        )
    if explicit_timezone:
        out["timezone"] = explicit_timezone
    if explicit_locale and explicit_timezone:
        return out
    if not bool(getattr(_cfg, "CHROME_GEOIP", True)):
        return out
    try:
        from config.browser import build_browser_environment

        geo = _detect_cloak_exit_geo(proxy_url)
        profile = build_browser_environment(geo)
        out.setdefault("locale", str(profile.get("navigator_language") or ""))
        out.setdefault("timezone", str(profile.get("timezone_iana") or ""))
        out.setdefault("accept_language", str(profile.get("accept_language") or ""))
        out["geo"] = geo
    except Exception as exc:
        logger.debug("[Chrome] 自动语言/时区推断失败：%s: %s", type(exc).__name__, exc)
    return {k: v for k, v in out.items() if v}


def build_chrome_driver(proxy: str | None = None) -> tuple[ChromeSeleniumDriver, ChromeOpenResult]:
    """启动本机 Chrome 并返回 Selenium 风格 driver。

    proxy=None  时按 config.proxy.PROXY_POOL 随机抽取（受 CHROME_USE_PROXY 控制）
    proxy=""    时显式禁用代理
    proxy="..." 时使用指定代理
    """
    if proxy is None and bool(getattr(_cfg, "CHROME_USE_PROXY", True)):
        try:
            from config.proxy import pick_proxy

            proxy = pick_proxy()
        except Exception:
            proxy = None

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "未安装 playwright，请执行：pip install playwright && playwright install chromium"
        ) from exc

    headless = bool(getattr(_cfg, "CHROME_HEADLESS", False))
    # headless 的 UA 带 HeadlessChrome 标记会被 Cloudflare 直接 403，
    # 因此无头运行时默认把 UA 还原成本机 Chrome 的常规 UA（实测可放行）。
    mask_ua = headless and bool(getattr(_cfg, "CHROME_HEADLESS_UA_MASK", True))
    if headless and not mask_ua:
        logger.warning(
            "[Chrome] CHROME_HEADLESS=True 但 CHROME_HEADLESS_UA_MASK=False："
            "UA 携带 HeadlessChrome 标记，实测会被 Cloudflare 403，注册大概率失败"
        )

    proxy_url = _normalize_proxy(proxy) if bool(getattr(_cfg, "CHROME_USE_PROXY", True)) else None
    locale_opts = _build_chrome_locale_options(proxy_url)

    playwright = sync_playwright().start()
    try:
        launch_kwargs: dict = {
            "headless": headless,
            "args": list(getattr(_cfg, "CHROME_EXTRA_ARGS", []) or []),
        }
        executable = str(getattr(_cfg, "CHROME_EXECUTABLE_PATH", "") or "").strip()
        if executable:
            launch_kwargs["executable_path"] = executable
        else:
            # channel="chrome" 让 Playwright 自动发现本机已安装的 Chrome，
            # 无需额外下载 Chromium，也无需 chromedriver。
            launch_kwargs["channel"] = "chrome"
        if proxy_url:
            # 拆成 {server, username, password}：商业轮换代理带账密，整条塞进 server 会认证失败。
            launch_kwargs["proxy"] = _playwright_proxy_kwargs(proxy_url)

        logger.info(
            "[Chrome] 启动本机 Chrome：headless=%s ua伪装=%s channel=%s proxy=%s locale=%s timezone=%s executable=%s",
            headless,
            mask_ua,
            launch_kwargs.get("channel") or "-",
            mask_proxy(proxy_url),
            locale_opts.get("locale") or "自动/默认",
            locale_opts.get("timezone") or "自动/默认",
            executable or "自动发现",
        )

        context_kwargs: dict = {
            "viewport": {
                "width": int(getattr(_cfg, "CHROME_VIEWPORT_WIDTH", 1440) or 1440),
                "height": int(getattr(_cfg, "CHROME_VIEWPORT_HEIGHT", 900) or 900),
            },
        }
        if locale_opts.get("locale"):
            context_kwargs["locale"] = locale_opts["locale"]
        if locale_opts.get("timezone"):
            context_kwargs["timezone_id"] = locale_opts["timezone"]
        if locale_opts.get("accept_language"):
            context_kwargs["extra_http_headers"] = {
                "Accept-Language": locale_opts["accept_language"]
            }

        user_data_dir = str(getattr(_cfg, "CHROME_USER_DATA_DIR", "") or "").strip()
        if user_data_dir:
            if mask_ua:
                # persistent context 不能通过 new_context 指定 UA，改由启动参数注入
                launch_kwargs["args"] = list(launch_kwargs.get("args") or []) + [
                    f"--user-agent={_masked_chrome_user_agent()}"
                ]
            context = playwright.chromium.launch_persistent_context(
                user_data_dir, **launch_kwargs, **context_kwargs
            )
            browser = getattr(context, "browser", None) or context
        else:
            browser = playwright.chromium.launch(**launch_kwargs)
            if mask_ua:
                context_kwargs["user_agent"] = _masked_chrome_user_agent(browser)
            context = browser.new_context(**context_kwargs)
        page = context.new_page()
    except Exception:
        try:
            playwright.stop()
        except Exception:
            pass
        raise

    driver = ChromeSeleniumDriver(browser=browser, context=context, page=page, playwright=playwright)
    # Roxy/Cloak/Chrome 共用页面操作函数，显式指定日志前缀，避免出现 `[Roxy注册]`。
    driver._registration_log_prefix = "[Chrome注册]"
    driver.set_page_load_timeout(int(getattr(_cfg, "CHROME_SELENIUM_TIMEOUT", 90) or 90))
    geo = locale_opts.get("geo") or {}
    return driver, ChromeOpenResult(
        raw={
            "driver": "chrome",
            "channel": launch_kwargs.get("channel"),
            # 存档只落脱敏代理：轮换代理的密码是跨账号复用的凭据，不进账号 JSON。
            # 用户名（含会话 ID）保留，便于回溯该账号走了哪个出口。
            "proxy": mask_proxy(proxy_url),
            "proxy_masked": True,
            "exit_ip": geo.get("ip") or "",
            "exit_country": geo.get("country") or "",
            "locale": locale_opts,
            "headless": headless,
        }
    )
