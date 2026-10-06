# -*- coding: utf-8 -*-
"""
代理池配置

每次注册随机抽取一个代理，保证不同 sid 之间彼此独立，避免风控关联。

协议说明：
    - http:// / https://   HTTP(S) 代理
    - socks5://            SOCKS5（DNS 本地解析，可能泄漏）
    - socks5h://           SOCKS5（DNS 在代理端解析，推荐，避免 DNS-IP 错配）
"""
from config.env_loader import apply_env_overrides
import logging
import random
import re
import secrets
import string

logger = logging.getLogger(__name__)


# 本地代理入口；实际出口地区以代理/分流规则为准。
# 推荐使用 socks5h://（DNS 在代理端解析），避免本地 DNS 与出口 IP 地区错配。
PROXY_POOL = [
    "socks5://127.0.0.1:7897",
]

# ============================================================================
# 商业轮换代理（自动代理）
# ============================================================================
# 与 PROXY_POOL 的区别：
#   PROXY_POOL      —— 固定入口列表，pick_proxy() 只是 random.choice。
#                      想轮换 IP 就必须手工维护 N 个不同出口的入口。
#   ROTATING_* 模板 —— 一个商业轮换代理网关地址，pick_proxy() 每次**现场生成**
#                      一个全新的「粘性会话」代理 URL，实现「一个账号一个出口 IP」
#                      的自动轮换，无需人工维护 N 条入口。
#
# 为什么必须是「粘性会话（sticky session）」，不能用按请求轮换：
#   注册一个账号要连续跑 2~5 分钟（打开登录页 → 收信验证 → 设密码 → 开 2FA）。
#   按请求轮换会让同一次注册中途不断换出口 IP，OpenAI / Cloudflare 会直接判定
#   会话异常。粘性会话能保证「同一个 session id 在 TTL 内始终走同一个出口 IP」。
#
# 模板占位符（每次 pick_proxy() 调用时替换）：
#   {session}  随机会话 ID（长度由 ROTATING_PROXY_SESSION_LEN 控制，每账号唯一）
#   {sid}      {session} 的别名
#   {country}  ROTATING_PROXY_COUNTRY 的值（小写，如 us / jp / de）
#   {city}     ROTATING_PROXY_CITY 的值（小写，留空则为空串）
#   {rand}     8 位随机串（不带黏性语义，仅用于拼接杂项参数）
#
# 例（Bright Data）：
#   ROTATING_PROXY_TEMPLATE="http://brd-customer-hl_xxx-zone-resi-country-{country}-session-{session}:密码@brd.superproxy.io:33335"
# 例（Oxylabs）：
#   ROTATING_PROXY_TEMPLATE="http://user-xxx-country-{country}-session-{session}:密码@pr.oxylabs.io:7777"
# 例（Smartproxy / Decodo）：
#   ROTATING_PROXY_TEMPLATE="http://user-xxx-country-{country}-session-{session}:密码@gate.smartproxy.com:7000"
# 例（IPRoyal）：
#   ROTATING_PROXY_TEMPLATE="http://user-xxx-country-{country}-session-{session}:密码@geo.iproyal.com:12321"
#
# 注意事项：
#   1. 不支持 SOCKS 带账号密码：Chromium 不支持 SOCKS5 认证，
#      socks5://user:pass@host 会直接连接失败，请改用 http:// 或 https:// 入口。
#   2. 【大陆网络重要】能用 https:// 就优先用 https://：
#      http:// 代理发给网关的 `CONNECT 目标域名:443` 是**明文**，中间网络（GFW 域名关键字
#      过滤）识别到 chatgpt.com / www.google.com 等会直接 RST 重置连接，而 ipinfo.io 之类
#      正常——表象是「代理对部分站点失效」。换成 https:// 后整条 CONNECT 走 TLS 加密，
#      域名不可见，问题消失。前提是厂商网关支持 TLS（实测 DataImpulse 823/824/10000 均支持
#      TLSv1.3）；网关不支持 TLS 时才退回 http://。
#   3. 会话 TTL 要 ≥ 单账号注册耗时。多数厂商默认 5~10 分钟，
#      也可在用户名里追加 `-sesstime-10`（各家语法不同，以厂商文档为准）。
#   4. 出口地区尽量与账号画像一致；CHROME_GEOIP=True 时会按出口 IP 自动匹配
#      语言/时区，地区跨度太大反而容易触发风控。
ROTATING_PROXY_TEMPLATE = ""

# 轮换代理默认国家代码（模板里的 {country}）。留空则由厂商默认地区决定。
ROTATING_PROXY_COUNTRY = "us"

# 轮换代理默认城市（模板里的 {city}）。留空则不注入城市参数。
ROTATING_PROXY_CITY = ""

# 每次生成的会话 ID 长度（4~32，建议 8~16）。太短可能与其他会话撞车。
ROTATING_PROXY_SESSION_LEN = 8

# 会话粘性 TTL（分钟）。仅作配置留档 + 体检工具校验提示，不参与 URL 拼接。
ROTATING_PROXY_SESSION_TTL = 10

# 生成粘性会话后是否校验出口国家（用 ROTATING_PROXY_COUNTRY 比对）。
# 部分厂商的国家定向并非 100% 生效（实测 DataImpulse 偶发把 us 会话落到葡萄牙），
# 出口地区与账号画像错配会显著抬高风控概率，故默认开启：不符就换一个新会话重试。
# 关闭后表现与旧版本一致（只生成、不校验）。
ROTATING_PROXY_VERIFY_COUNTRY = True

# 出口国家校验的最多尝试次数（含首次）。只有「探测到国家但不符合」才会换会话重试；
# 探测本身失败（网络不通 / 接口异常）不会重试，避免白白等待。
ROTATING_PROXY_VERIFY_ATTEMPTS = 3

# 套餐/Plus 试用资格查询与 Codex Agent Token 生成共用这组独立网络策略，
# 避免批量请求被注册代理池中的临时本地代理拖垮，也避免无条件直连造成出口策略失控。
#   auto   = 优先使用 PLAN_CHECK_PROXY 或代理池；本地代理端口未监听时回退直连
#   proxy  = 强制使用 PLAN_CHECK_PROXY 或代理池，失败直接报错
#   direct = 始终直连
PLAN_CHECK_PROXY_MODE = "auto"

# 套餐查询 / Codex Agent Token 生成专用代理。留空时 auto/proxy 模式从 PROXY_POOL 选择。
# 代理可能包含账号密码，因此 WebUI 会把它保存到 .env。
PLAN_CHECK_PROXY = ""

# 查套餐 / 生成 Codex Agent Token 使用独立的短超时和有限重试，避免后台任务长时间卡住。
PLAN_CHECK_TIMEOUT = 15.0
PLAN_CHECK_MAX_ATTEMPTS = 2
PLAN_CHECK_RETRY_DELAY = 1.5

# 新注册账号的权益可能存在短暂同步延迟。首次查询失败，或返回 free 且暂未发现
# Plus 试用资格时，等待该秒数后再复查一次；设为 0 可关闭复查。
PLAN_CHECK_REGISTRATION_RECHECK_DELAY = 2.0

# 自动、手动和批量套餐查询共用同一个后台队列；Codex Agent Token 使用独立队列，
# 但复用这里的网络模式、请求启动间隔与随机抖动，避免批量后台请求过于集中。
PLAN_CHECK_WORKERS = 3
PLAN_CHECK_QUEUE_LIMIT = 500
PLAN_CHECK_MIN_INTERVAL = 0.4
PLAN_CHECK_JITTER = 0.3


_SESSION_ALPHABET = string.ascii_lowercase + string.digits


def generate_proxy_session_id(length: int | None = None) -> str:
    """生成一个用于粘性会话的随机 ID（小写字母 + 数字，易读且对各家厂商都安全）。"""
    try:
        size = int(length if length is not None else ROTATING_PROXY_SESSION_LEN)
    except (TypeError, ValueError):
        size = 8
    size = max(4, min(size, 32))
    return "".join(secrets.choice(_SESSION_ALPHABET) for _ in range(size))


def has_rotating_proxy() -> bool:
    """是否配置了商业轮换代理模板。"""
    return bool(str(ROTATING_PROXY_TEMPLATE or "").strip())


def build_rotating_proxy(
    template: str | None = None,
    country: str | None = None,
    city: str | None = None,
    session_id: str | None = None,
) -> str:
    """按模板现场生成一个「粘性会话」代理 URL。

    每次调用都会生成新的 session id（除非显式传入 session_id），因此同一账号
    在整个注册流程里用的是同一个出口 IP，而不同账号之间出口 IP 自动分散。
    模板为空时返回空串。
    """
    tpl = str(template if template is not None else ROTATING_PROXY_TEMPLATE or "").strip()
    if not tpl:
        return ""

    sid = str(session_id or generate_proxy_session_id())
    ctry = str(country if country is not None else ROTATING_PROXY_COUNTRY or "").strip().lower()
    cty = str(city if city is not None else ROTATING_PROXY_CITY or "").strip().lower()

    url = tpl

    # 空值占位符：连同后面紧跟的一个分隔符一起吃掉，避免留下
    # `user-country--session-` 这类碎片（同时不会误伤密码里本来就有的 '-'）。
    for token, value in (("{country}", ctry), ("{city}", cty)):
        if value:
            continue
        url = re.sub(re.escape(token) + r"-?", "", url, count=1)

    mapping = {
        "{session}": sid,
        "{sid}": sid,
        "{country}": ctry,
        "{city}": cty,
        "{rand}": "".join(secrets.choice(_SESSION_ALPHABET) for _ in range(8)),
    }
    for token, value in mapping.items():
        url = url.replace(token, value)
    return url


# 出口探测接口返回的国家可能是 2 字母码（US），也可能是英文全称（UNITED STATES），
# 这里统一归一化，避免「US」与「UNITED STATES」被误判成不同国家而白白换会话重试。
_COUNTRY_NAME_TO_CODE = {
    "UNITED STATES": "US",
    "UNITED STATES OF AMERICA": "US",
    "UNITED KINGDOM": "GB",
    "GREAT BRITAIN": "GB",
    "PORTUGAL": "PT",
    "JAPAN": "JP",
    "GERMANY": "DE",
    "FRANCE": "FR",
    "CANADA": "CA",
    "AUSTRALIA": "AU",
    "SINGAPORE": "SG",
    "NETHERLANDS": "NL",
    "SPAIN": "ES",
    "ITALY": "IT",
    "BRAZIL": "BR",
    "INDIA": "IN",
    "SOUTH KOREA": "KR",
    "KOREA": "KR",
    "POLAND": "PL",
    "SWEDEN": "SE",
    "SWITZERLAND": "CH",
}


def normalize_country_code(raw: str | None) -> str:
    """把探测到的国家名归一化为 2 字母代码；无法识别时返回空串（视为无法判定）。"""
    value = str(raw or "").strip().upper()
    if not value:
        return ""
    if len(value) == 2 and value.isalpha():
        return value
    return _COUNTRY_NAME_TO_CODE.get(value, "")


def probe_exit_country(proxy_url: str) -> str:
    """通过指定代理探测出口国家代码（2 字母大写，如 US）；探测失败返回空串。

    复用 Cloak 驱动的出口探测逻辑（同一套 IP_GEO_ENDPOINTS / IP_GEO_TIMEOUT）。
    由于实测粘性会话在 TTL 内出口 IP 固定，这里探测到的国家与随后浏览器实际使用的出口一致。
    返回空串表示「没探到 / 认不出」，调用方应据此跳过校验，不要据此换会话。
    """
    if not proxy_url:
        return ""
    try:
        from core.cloakbrowser_driver import _detect_cloak_exit_geo

        geo = _detect_cloak_exit_geo(proxy_url) or {}
        return normalize_country_code(geo.get("country"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[代理] 出口国家探测失败：%s: %s", type(exc).__name__, exc)
        return ""


def _pick_rotating_proxy_verified() -> str:
    """生成粘性会话并按需校验出口国家；不符则换新会话重试。"""
    expected = str(ROTATING_PROXY_COUNTRY or "").strip().upper()
    verify = bool(ROTATING_PROXY_VERIFY_COUNTRY) and bool(expected)
    try:
        attempts = max(1, int(ROTATING_PROXY_VERIFY_ATTEMPTS or 1))
    except (TypeError, ValueError):
        attempts = 1

    url = ""
    for idx in range(attempts):
        url = build_rotating_proxy()
        if not url or not verify:
            return url
        actual = probe_exit_country(url)
        if not actual:
            # 探测无结果（接口不可达 / 超时）时不重试，避免无谓的网络等待。
            logger.debug("[代理] 出口国家未探测到，跳过国家校验")
            return url
        if actual == expected:
            if idx:
                logger.info("[代理] 第 %s 次换会话后拿到期望出口国家 %s", idx + 1, expected)
            return url
        logger.warning(
            "[代理] 出口国家不符：期望 %s，实际 %s（第 %s/%s 次），换新会话重试",
            expected,
            actual,
            idx + 1,
            attempts,
        )
    logger.warning("[代理] %s 次尝试后出口国家仍不符合期望 %s，沿用最后一次会话", attempts, expected)
    return url


def pick_proxy(verify: bool | None = None) -> str:
    """为「一个新账号」挑选一个代理 URL。

    优先级：
        1. 配了 ROTATING_PROXY_TEMPLATE → 现场生成一个全新的粘性会话（自动轮换）；
        2. 否则从 PROXY_POOL 随机抽取一个固定入口；
        3. 都没有 → 返回空串（直连）。

    配了模板时默认会校验出口国家（ROTATING_PROXY_VERIFY_COUNTRY），不符则换会话重试，
    以保证账号的语言/时区画像与出口地区一致。verify=False 可显式跳过校验（如模块导入期）。
    """
    if has_rotating_proxy():
        try:
            url = _pick_rotating_proxy_verified() if verify is not False else build_rotating_proxy()
            if url:
                return url
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[代理] 轮换代理模板生成失败，回退 PROXY_POOL：%s: %s", type(exc).__name__, exc
            )
    return random.choice(PROXY_POOL) if PROXY_POOL else ""


# 兼容入口：默认每次进程启动随机选一个，作为本次注册全程的固定代理。
# 导入期不做出口国家探测，避免拖慢启动 / 卡在不可达的探测接口上。
PROXY = pick_proxy(verify=False)

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'PROXY_POOL': 'list_str_multiline',
    'ROTATING_PROXY_TEMPLATE': 'str',
    'ROTATING_PROXY_COUNTRY': 'str',
    'ROTATING_PROXY_CITY': 'str',
    'ROTATING_PROXY_SESSION_LEN': 'int',
    'ROTATING_PROXY_SESSION_TTL': 'int',
    'ROTATING_PROXY_VERIFY_COUNTRY': 'bool',
    'ROTATING_PROXY_VERIFY_ATTEMPTS': 'int',
    'PLAN_CHECK_PROXY_MODE': 'str',
    'PLAN_CHECK_PROXY': 'str',
    'PLAN_CHECK_TIMEOUT': 'float',
    'PLAN_CHECK_MAX_ATTEMPTS': 'int',
    'PLAN_CHECK_RETRY_DELAY': 'float',
    'PLAN_CHECK_REGISTRATION_RECHECK_DELAY': 'float',
    'PLAN_CHECK_WORKERS': 'int',
    'PLAN_CHECK_QUEUE_LIMIT': 'int',
    'PLAN_CHECK_MIN_INTERVAL': 'float',
    'PLAN_CHECK_JITTER': 'float',
})
PROXY = pick_proxy()
