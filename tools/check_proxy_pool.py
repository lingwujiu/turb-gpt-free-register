# -*- coding: utf-8 -*-
"""代理池体检：验证 PROXY_POOL 里每个入口的真实出口 IP 是否彼此独立。

为什么需要这个：
    批量注册时 main.py 的 run_one_batch_item() 不传 proxy，build_chrome_driver()
    会对每个账号调用一次 config.proxy.pick_proxy() 随机抽取代理。
    所以「能否轮换 IP」完全取决于 PROXY_POOL 里有几个**不同出口**的入口。

    今天实测：同一出口 IP 连续注册到第 4~6 个即触发 OpenAI 注册节流
    （chatgpt.com/auth/error?error=undefined），且同 /24 段的相邻 IP 会被一起限流。
    因此体检要同时看「出口 IP 是否唯一」和「是否落在不同网段」。

用法：
    .venv/bin/python tools/check_proxy_pool.py
    .venv/bin/python tools/check_proxy_pool.py --rounds 3   # 每个入口多探几次，看是否会漂移
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.proxy import PROXY_POOL  # noqa: E402

IP_SERVICES = [
    "https://ipinfo.io/json",
    "https://api.ipify.org?format=json",
]


def mask_url(url: str) -> str:
    """隐藏代理 URL 中的账号密码。"""
    return re.sub(r"://([^:@/]+):([^@/]+)@", r"://\1:***@", url)


def net24(ip: str) -> str:
    parts = ip.split(".")
    return ".".join(parts[:3]) + ".0/24" if len(parts) == 4 else ip


def _http_get_json(url: str, proxy: str, timeout: float) -> dict:
    try:
        import requests  # type: ignore
    except ImportError:
        requests = None  # noqa: N806

    if requests is not None:
        r = requests.get(url, proxies={"http": proxy, "https": proxy}, timeout=timeout)
        r.raise_for_status()
        return r.json()

    # 兜底：用项目自带的 curl_cffi
    from curl_cffi import requests as cffi_requests  # type: ignore

    r = cffi_requests.get(url, proxies={"http": proxy, "https": proxy}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def probe(proxy: str, timeout: float = 12.0) -> dict:
    last_err = ""
    for svc in IP_SERVICES:
        try:
            data = _http_get_json(svc, proxy, timeout)
            return {
                "ok": True,
                "ip": str(data.get("ip") or data.get("query") or "").strip(),
                "country": str(data.get("country") or "").strip(),
                "city": str(data.get("city") or "").strip(),
                "org": str(data.get("org") or data.get("isp") or "").strip(),
            }
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {str(exc)[:80]}"
    return {"ok": False, "error": last_err}


def main() -> int:
    ap = argparse.ArgumentParser(description="代理池出口 IP 体检")
    ap.add_argument("--rounds", type=int, default=1, help="每个入口探测次数（默认 1）")
    ap.add_argument("--timeout", type=float, default=12.0, help="单次请求超时秒数")
    args = ap.parse_args()

    print("=" * 74)
    print("代理池体检 —— 每个入口的真实出口 IP")
    print("=" * 74)

    if not PROXY_POOL:
        print("PROXY_POOL 为空 → 注册将不使用代理（直连）。")
        return 1

    print(f"入口数量：{len(PROXY_POOL)}   每入口探测 {args.rounds} 次\n")

    per_entry: list[dict] = []
    all_ips: list[str] = []

    for idx, entry in enumerate(PROXY_POOL, 1):
        print(f"[{idx}/{len(PROXY_POOL)}] {mask_url(entry)}")
        rounds = []
        for r_i in range(args.rounds):
            res = probe(entry, timeout=args.timeout)
            rounds.append(res)
            if res["ok"]:
                all_ips.append(res["ip"])
                tag = "" if args.rounds == 1 else f" (第{r_i + 1}次)"
                print(f"      ✅ {res['ip']:<16} {res['country']}/{res['city']:<14} {res['org'][:34]}{tag}")
            else:
                print(f"      ❌ 探测失败：{res['error']}")
        per_entry.append({"entry": entry, "rounds": rounds})
        print()

    ok_ips = [i for i in all_ips if i]
    uniq_ips = sorted(set(ok_ips))
    uniq_seg = sorted({net24(i) for i in uniq_ips})

    print("=" * 74)
    print("汇总")
    print("=" * 74)
    print(f"  成功探测次数      : {len(ok_ips)}/{len(PROXY_POOL) * args.rounds}")
    print(f"  唯一出口 IP 数    : {len(uniq_ips)}")
    print(f"  唯一 /24 网段数   : {len(uniq_seg)}")

    if len(uniq_ips) <= 1 and ok_ips:
        print("\n  ⚠️  所有入口出口相同 —— 批量等于「同一 IP 连打」，必被 OpenAI 节流。")
        print("     需要拆成多个不同出口的入口（不同端口/不同节点/不同地区）。")
    elif len(uniq_seg) < len(uniq_ips):
        print("\n  ⚠️  出口 IP 虽不同，但存在同 /24 网段 —— 该网段可能被一起限流。")
        print("     建议尽量使用跨地区、跨服务商的出口。")
    elif ok_ips and len(set(uniq_seg)) > 1:
        print("\n  ✅ 网段分散良好，项目会自动为每个账号随机抽取一个入口。")

    if ok_ips:
        print("\n  出口明细（去重）：")
        cnt = Counter(ok_ips)
        for ip in uniq_ips:
            seg = net24(ip)
            hits = [r for r in per_entry for x in r["rounds"] if x.get("ok") and x["ip"] == ip]
            meta = hits[0] if hits else {}
            print(f"    {ip:<16} {seg:<18} ×{cnt[ip]}"
                  f"  {meta.get('country', '')}/{meta.get('city', '')}")

    print("\n" + "-" * 74)
    print("配置方式（.env）：PROXY_POOL 用**换行**分隔多个入口，例如")
    print('  PROXY_POOL="http://127.0.0.1:7890')
    print('  http://127.0.0.1:7891')
    print('  http://127.0.0.1:7892"')
    print("-" * 74)
    failed = [r for r in per_entry if not any(x.get("ok") for x in r["rounds"])]
    if failed:
        print("\n探测失败的入口（注册时会在这些入口上直接报错）：")
        for r in failed:
            print(f"  - {mask_url(r['entry'])}  {r['rounds'][0].get('error', '')}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
