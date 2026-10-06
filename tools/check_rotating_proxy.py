# -*- coding: utf-8 -*-
"""商业轮换代理体检：验证 ROTATING_PROXY_TEMPLATE 真的能「一账号一出口」。

为什么需要这个：
    config.proxy.pick_proxy() 在配了 ROTATING_PROXY_TEMPLATE 时，会为每个账号
    现场生成一个**粘性会话**代理 URL。但「换会话是否真的换出口 IP」「同一会话
    在注册这几分钟内是否真的不漂移」，这两点完全由代理商网关决定，代码看不出来。
    一旦网关不认你的 session 参数（参数名写错、套餐不支持粘性、地区池太小），
    批量注册会退化成「同一个 IP 连打」，直接撞 OpenAI 限流。

本工具做三件事：
    1. 连打性检查 —— 按模板生成 N 个不同会话，看出口 IP 是否彼此不同；
    2. 粘性检查   —— 对同一个会话连探 R 次，看出口 IP 是否保持不变；
    3. 模板体检   —— 占位符是否残留、协议是否是 Chromium 能带认证的 http(s)。

用法（在项目根目录）：
    .venv/bin/python tools/check_rotating_proxy.py
    .venv/bin/python tools/check_rotating_proxy.py --sessions 10 --rounds 3
    .venv/bin/python tools/check_rotating_proxy.py --country jp

    # 不改 .env，临时指定模板验证（推荐先这样试，避免坏模板顶掉兜底代理）
    .venv/bin/python tools/check_rotating_proxy.py \
        --template "http://登录名__cr.{country};sessid.{session}:密码@gw.dataimpulse.com:823" \
        --sessions 8 --rounds 3

退出码：
    0 = 轮换 + 粘性都正常
    1 = 未配置模板 / 模板有明显问题
    2 = 探测失败或粘性不成立
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.proxy import (  # noqa: E402
    ROTATING_PROXY_COUNTRY,
    ROTATING_PROXY_SESSION_LEN,
    ROTATING_PROXY_SESSION_TTL,
    ROTATING_PROXY_TEMPLATE,
    build_rotating_proxy,
    generate_proxy_session_id,
)

IP_SERVICES = [
    "https://api.ipify.org?format=json",
    "https://ipinfo.io/json",
]

_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z_]+\}")


def mask_url(url: str) -> str:
    """脱敏：只隐藏密码，保留用户名里的会话 ID（排查要用）。"""
    return re.sub(r"(://[^:@/]*):([^@/]+)@", r"\1:***@", url)


def net24(ip: str) -> str:
    parts = ip.split(".")
    return ".".join(parts[:3]) + ".0/24" if len(parts) == 4 else ip


def _http_get_json(url: str, proxy: str, timeout: float) -> dict:
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
               "Cache-Control": "no-cache", "Pragma": "no-cache"}
    try:
        import requests  # type: ignore
    except ImportError:
        requests = None  # noqa: N806

    if requests is not None:
        r = requests.get(url, proxies={"http": proxy, "https": proxy},
                         headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()

    from curl_cffi import requests as cffi_requests  # type: ignore

    r = cffi_requests.get(url, proxies={"http": proxy, "https": proxy},
                          headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def probe(proxy: str, timeout: float = 15.0) -> dict:
    """探测一次出口 IP。返回 {'ok', 'ip', 'country', 'city', 'org'} 或 {'ok': False, 'error'}。"""
    last_err = ""
    for svc in IP_SERVICES:
        try:
            data = _http_get_json(svc, proxy, timeout)
            return {
                "ok": True,
                "ip": str(data.get("ip") or data.get("query") or "").strip(),
                "country": str(data.get("country") or data.get("country_code") or "").strip(),
                "city": str(data.get("city") or "").strip(),
                "org": str(data.get("org") or data.get("isp") or "").strip(),
            }
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {str(exc)[:90]}"
    return {"ok": False, "error": last_err}


def sanity_check_template(template: str) -> list[str]:
    """模板静态体检，返回问题列表（空列表 = 没问题）。"""
    problems: list[str] = []
    tpl = str(template or "").strip()

    if not tpl:
        return ["ROTATING_PROXY_TEMPLATE 为空 —— 尚未配置商业轮换代理"]

    leftovers = sorted(set(_PLACEHOLDER_RE.findall(tpl)))
    known = {"{session}", "{sid}", "{country}", "{city}", "{rand}"}
    unknown = [p for p in leftovers if p not in known]
    if unknown:
        problems.append(
            f"模板里存在不认识的占位符 {unknown}；支持 {sorted(known)}，"
            "这些占位符不会被替换，会原样发到代理服务器导致认证失败"
        )

    if not re.match(r"^[a-zA-Z0-9]+://", tpl):
        problems.append("模板缺少 scheme（应以 http:// 或 https:// 开头）")

    scheme = ""
    try:
        scheme = (urlsplit(tpl).scheme or "").lower()
    except Exception:  # pragma: no cover
        pass

    if scheme.startswith("socks"):
        if "@" in tpl.split("://", 1)[-1]:
            problems.append(
                "模板是 SOCKS 且带账号密码 —— Chromium 不支持 SOCKS5 认证，"
                "浏览器会直接连不上；商业轮换代理请改用 http:// 入口"
            )
        else:
            problems.append(
                "模板是 SOCKS 代理 —— 虽然无认证时可用，但多数商业轮换代理靠"
                "用户名传会话参数，会拿不到账号密码；建议改用 http:// 入口"
            )
    elif scheme not in ("http", "https"):
        problems.append(f"不常见的 scheme={scheme!r}，建议使用 http:// 或 https://")

    if "{session}" not in tpl and "{sid}" not in tpl:
        problems.append(
            "模板里没有 {session}/{sid} 占位符 —— 所有账号会共用同一个会话，"
            "出口 IP 不会随账号变化，等于没有轮换"
        )

    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="商业轮换代理体检")
    ap.add_argument("--sessions", type=int, default=5, help="生成多少个不同会话（默认 5）")
    ap.add_argument("--rounds", type=int, default=2, help="每个会话连探几次以验证粘性（默认 2）")
    ap.add_argument("--country", type=str, default=None, help="覆盖模板里的 {country}")
    ap.add_argument("--timeout", type=float, default=15.0, help="单次请求超时秒数")
    ap.add_argument(
        "--template", type=str, default=None,
        help="临时指定代理模板（仅本次体检使用，不读取也不写入 .env）；"
             "支持 {session}/{sid}/{country}/{city}/{rand} 占位符",
    )
    args = ap.parse_args()

    template = args.template if args.template is not None else ROTATING_PROXY_TEMPLATE
    country = args.country if args.country is not None else ROTATING_PROXY_COUNTRY

    print("=" * 78)
    print("商业轮换代理体检 —— ROTATING_PROXY_TEMPLATE")
    print("=" * 78)

    problems = sanity_check_template(template)
    if problems:
        print("\n模板静态检查未通过：")
        for p in problems:
            print(f"  ✗ {p}")
        print(
            "\n配置方法（.env 或 WebUI「代理池」分组）：\n"
            '  ROTATING_PROXY_TEMPLATE="http://user-xxx-country-{country}-session-{session}:密码@gate.厂商域名:端口"\n'
            '  ROTATING_PROXY_COUNTRY="us"\n'
            "\n也可以不改 .env，用 --template 临时验证（推荐先这样试）：\n"
            '  .venv/bin/python tools/check_rotating_proxy.py \\\n'
            '      --template "http://登录名__cr.{country};sessid.{session}:密码@gw.dataimpulse.com:823"\n'
            "\n各家模板语法见 docs/rotating_proxy_setup.md。\n"
        )
        return 1

    print("\n模板静态检查通过：")
    print(f"  模板        : {mask_url(template)}")
    print(f"  国家/会话长度: {country or '(厂商默认)'} / "
          f"{ROTATING_PROXY_SESSION_LEN} 位，配置 TTL {ROTATING_PROXY_SESSION_TTL} 分钟")
    print(f"  采样计划    : {args.sessions} 个会话 × 每个 {args.rounds} 次 = "
          f"{args.sessions * args.rounds} 次探测\n")

    if ROTATING_PROXY_SESSION_TTL < 10:
        print(f"  ⚠️  会话粘性 TTL={ROTATING_PROXY_SESSION_TTL} 分钟偏短。单账号注册"
              "（注册→收码→设密码→开 2FA）通常要 3~6 分钟，建议 ≥10 分钟，否则流程中途会掉 IP。\n")

    sessions: list[dict] = []
    all_ips: list[str] = []
    first_error = ""

    for idx in range(1, args.sessions + 1):
        sid = generate_proxy_session_id()
        url = build_rotating_proxy(template=template, country=country, session_id=sid)
        print(f"[{idx}/{args.sessions}] session-{sid}  {mask_url(url)}")
        rounds: list[dict] = []
        for r_i in range(args.rounds):
            res = probe(url, timeout=args.timeout)
            rounds.append(res)
            if res["ok"]:
                all_ips.append(res["ip"])
                tag = "" if args.rounds == 1 else f" (第{r_i + 1}次)"
                print(f"      ✅ {res['ip']:<16} {res['country']}/{res['city']:<14} "
                      f"{(res['org'] or '')[:34]}{tag}")
            else:
                first_error = first_error or res["error"]
                print(f"      ❌ 探测失败：{res['error']}")
            if r_i < args.rounds - 1:
                time.sleep(1.0)
        sessions.append({"sid": sid, "url": url, "rounds": rounds})
        print()

    ok_ips = [i for i in all_ips if i]
    if not ok_ips:
        print("=" * 78)
        print("全部探测失败 —— 模板或账密有问题，注册时也一定跑不通。")
        if first_error:
            print(f"  最后一次错误：{first_error}")
        print("  排查顺序：① 账密是否正确 ② 网关域名/端口 ③ 套餐是否已开通该功能")
        print("            ④ 本机是否能直连该网关（换 ping/curl 试）")
        return 2

    # ---- 轮换性：跨会话出口是否分散 ----
    per_session_ips = [
        sorted({r["ip"] for r in s["rounds"] if r.get("ok") and r["ip"]})
        for s in sessions
    ]
    session_first_ips = [ips[0] for ips in per_session_ips if ips]
    uniq_ips = sorted(set(ok_ips))
    uniq_seg = sorted({net24(i) for i in uniq_ips})
    rotating = len(set(session_first_ips)) > 1

    # ---- 粘性：同一会话内部是否漂移 ----
    drifting = [s["sid"] for s, ips in zip(sessions, per_session_ips)
                if len(ips) > 1 and args.rounds > 1]
    sticky = not drifting

    print("=" * 78)
    print("汇总")
    print("=" * 78)
    print(f"  成功探测          : {len(ok_ips)}/{args.sessions * args.rounds}")
    print(f"  唯一出口 IP       : {len(uniq_ips)}（跨 {len(set(session_first_ips))} 个会话）")
    print(f"  唯一 /24 网段     : {len(uniq_seg)}")
    print(f"  断点性(粘性)      : {'✅ 会话内出口稳定' if sticky else '❌ 会话内出口漂移'}")
    print(f"  连打性(轮换)      : {'✅ 换会话即换 IP' if rotating else '❌ 换会话出口不变'}")

    verdict_bad = False
    if not rotating:
        verdict_bad = True
        print("\n  ❌ 不同会话的出口 IP 完全相同 —— 轮换没生效。可能原因：")
        print("     · 会话参数名写错（各家不同：session- / sessid / sid1 / -session-）")
        print("     · 套餐不支持粘性会话，网关忽略了用户名里的 session")
        print("     · 该国家可用出口池太小（免费/试用套餐常见）")
        print("     · 其实是「按请求轮换」型网关：那也不行，同一账号流程内会掉 IP")
        if len(uniq_ips) > 1:
            print(f"     （注：{len(uniq_ips)} 个唯一 IP 说明网关确实在换，只是没按会话换）")
    if not sticky:
        verdict_bad = True
        print(f"\n  ❌ 同一会话内出口 IP 漂移（{', '.join(drifting)}）——")
        print("     粘性不成立意味着单账号注册中途会换 IP，风控概率极高。")
        print("     需要向代理商确认粘性 TTL 配置，或在用户名里补 sesstime 参数。")

    if not verdict_bad:
        print("\n  ✅ 轮换 + 粘性都正常：批量注册时每个账号会自动拿到独立且稳定的出口 IP。")

    print("\n  出口明细（去重）：")
    cnt = Counter(ok_ips)
    for ip in uniq_ips:
        hit = next((r for s in sessions for r in s["rounds"]
                    if r.get("ok") and r["ip"] == ip), {})
        sid = next((s["sid"] for s in sessions
                    if any(r.get("ok") and r["ip"] == ip for r in s["rounds"])), "?")
        print(f"    {ip:<16} {net24(ip):<18} ×{cnt[ip]}  session-{sid}  "
              f"{hit.get('country', '')}/{hit.get('city', '')}  {(hit.get('org') or '')[:28]}")

    print("\n" + "-" * 78)
    print("接下来：项目已无需改动，批量注册时 main.py → pick_proxy() 会自动按上述")
    print("      模板为每个账号生成新会话。直接跑：")
    print("        REGISTRATION_DRIVER=chrome python main.py --count 10 --delay 60 --continue-on-fail")
    print("-" * 78)
    return 2 if verdict_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
