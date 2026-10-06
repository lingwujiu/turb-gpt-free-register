# -*- coding: utf-8 -*-
"""
本机 Chrome 驱动配置（Playwright channel="chrome"）

与 RoxyBrowser / CloakBrowser 等指纹浏览器不同，本驱动直接驱动本机已安装的
Google Chrome，特点是：

    - 零凭证依赖：不需要 ROXY_API_TOKEN / CLOAK_LICENSE_KEY
    - 复用真实用户环境，Cloudflare 放行率高

运行模式：2026-10 实测结论——

    - headless 下 Chrome 的 UA 会带 `HeadlessChrome/` 标记，Cloudflare 直接 403；
    - 仅当 headless 同时开启 CHROME_HEADLESS_UA_MASK（把 UA 还原成常规 Chrome）时才放行，
      实测 chatgpt.com 返回 200 且正常渲染登录入口。

因此「后台无窗口全自动跑」= CHROME_HEADLESS=True + CHROME_HEADLESS_UA_MASK=True。
"""
from config.env_loader import apply_env_overrides

# 是否无头（后台无窗口）运行。True 时不会弹出任何浏览器窗口。
# 注意：必须同时保持 CHROME_HEADLESS_UA_MASK=True，否则会被 Cloudflare 403。
CHROME_HEADLESS = False

# 无头模式下是否自动伪装 UA（把 UA 里的 HeadlessChrome 还原为常规 Chrome）。
# 实测这是 headless 能否通过 Cloudflare 的决定性开关，除非有特殊理由否则不要关。
CHROME_HEADLESS_UA_MASK = True

# 是否使用代理。True 时优先用传入 proxy，未传则从 config.proxy.PROXY_POOL 随机抽取。
CHROME_USE_PROXY = True

# 页面操作 / 导航超时（秒）
CHROME_SELENIUM_TIMEOUT = 90

# 视口尺寸
CHROME_VIEWPORT_WIDTH = 1440
CHROME_VIEWPORT_HEIGHT = 900

# 语言 / 时区。留空则按出口 IP 自动推断（复用 config.browser.build_browser_environment）。
CHROME_LOCALE = ""
CHROME_TIMEZONE = ""
# 是否按出口 IP 自动匹配语言/时区。出口地区与 locale 错配会显著提高风控概率。
CHROME_GEOIP = True

# 浏览器启动附加参数
CHROME_EXTRA_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
]

# 持久化用户目录。留空则每次使用一次性 context（保证账号间环境干净、互不关联）。
CHROME_USER_DATA_DIR = ""

# 注册完成后保持浏览器打开（调试用）
CHROME_KEEP_BROWSER_OPEN = False

# 注册后是否到「设置 → Security and login」补设账号密码。
# 新版 OpenAI 注册流默认走无密码（一次性验证码）入口，不会出现 create-account/password 页，
# 因此注册完成时 extra.registration_password 恒为空。打开本开关后会在拿到会话后
# 走 UI 流补设密码（可能需要一封身份验证邮件），使导出的账号带真实密码。
CHROME_PASSWORD_SETUP = True

# Chrome 可执行文件路径。留空时用 Playwright 的 channel="chrome" 自动发现本机 Chrome。
CHROME_EXECUTABLE_PATH = ""

# 打开页面时若命中 Cloudflare「Just a moment...」挑战页，最多等待多少秒让其自动放行。
# 有头 Chrome 通常在数秒内通过；出口 IP 被 Cloudflare 盯上时会明显变慢，故给足等待时间，
# 避免刚落地就被判为「找不到邮箱输入框」而整体失败。
CHROME_CF_CHALLENGE_WAIT = 90


# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'CHROME_HEADLESS': 'bool',
    'CHROME_HEADLESS_UA_MASK': 'bool',
    'CHROME_USE_PROXY': 'bool',
    'CHROME_SELENIUM_TIMEOUT': 'int',
    'CHROME_VIEWPORT_WIDTH': 'int',
    'CHROME_VIEWPORT_HEIGHT': 'int',
    'CHROME_LOCALE': 'str',
    'CHROME_TIMEZONE': 'str',
    'CHROME_GEOIP': 'bool',
    'CHROME_EXTRA_ARGS': 'list_str_multiline',
    'CHROME_USER_DATA_DIR': 'str',
    'CHROME_KEEP_BROWSER_OPEN': 'bool',
    'CHROME_EXECUTABLE_PATH': 'str',
    'CHROME_PASSWORD_SETUP': 'bool',
    'CHROME_CF_CHALLENGE_WAIT': 'int',
})
