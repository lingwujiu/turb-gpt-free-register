# 商业轮换代理（自动代理）配置指南

> 目标：批量注册时**不用手工维护 N 条代理入口**，让每个账号自动拿到一个独立且稳定的出口 IP。
> 相关代码：`config/proxy.py`、`core/chrome_driver.py`、`tools/check_rotating_proxy.py`

## 1. 为什么批量必须换 IP

实测结论（见 `tools/check_proxy_pool.py` 注释）：

- 同一出口 IP 建到第 **4~6** 个账号就会撞 `chatgpt.com/auth/error?error=undefined`（OpenAI 注册节流）；
- 同一 `/24` 网段的相邻 IP 会被**一起限流**；
- 冷却窗口随次数拉长（11 分钟 → 40 分钟+）。

所以批量注册的正确姿势是：**一个账号一个出口 IP**，且同一账号整个流程内 IP 不能变。

## 2. 三种代理形态，只有一种能用

| 形态 | 表现 | 能否用于本项目 |
|:---|:---|:---|
| **固定入口池**（现有 `PROXY_POOL`） | 手工维护 N 条不同出口的入口，`pick_proxy()` 随机抽 | ✅ 可用，但要人工维护，且入口质量不可控 |
| **按请求轮换网关** | 每次 TCP 连接换一个 IP | ❌ 单账号注册 2~5 分钟内会反复换 IP，必被风控 |
| **粘性会话网关**（商业轮换代理） | 用户名里带 `session-xxxx`，同一 session 在 TTL 内固定一个 IP | ✅ **推荐**，即本项目 `ROTATING_PROXY_TEMPLATE` 支持的形态 |

判断方法：商业轮换代理的套餐一般同时提供「按请求轮换」和「粘性会话」两种端口/参数，
**必须选粘性会话**，并把 TTL 设到 ≥10 分钟。

## 3. 配置（二选一：WebUI 或 .env）

WebUI → 配置页 → 「代理池」分组，填写 **商业轮换代理模板**；或直接改 `.env`：

```env
# 模板里的密码就是你平时登录代理商后台的那个代理密码
ROTATING_PROXY_TEMPLATE="http://user-xxx-country-{country}-session-{session}:你的代理密码@gate.厂商域名:端口"
ROTATING_PROXY_COUNTRY="us"
ROTATING_PROXY_CITY=""
ROTATING_PROXY_SESSION_LEN="8"
ROTATING_PROXY_SESSION_TTL="10"
```

**模板填了就优先用它，`PROXY_POOL` 会被忽略**（留空模板则回退旧逻辑，不影响现有跑法）。

### 占位符

| 占位符 | 含义 |
|:---|:---|
| `{session}` / `{sid}` | 随机会话 ID，**每账号唯一**，长度由 `ROTATING_PROXY_SESSION_LEN` 控制 |
| `{country}` | `ROTATING_PROXY_COUNTRY` 的值（小写两位国家码） |
| `{city}` | `ROTATING_PROXY_CITY` 的值，留空则整段连同分隔符一起省略 |
| `{rand}` | 8 位随机串，无粘性语义，仅用于拼接杂项参数 |

### 各家用户名语法（**以厂商当期文档为准**）

```
Bright Data : brd-customer-<客户ID>-zone-<zone>-country-us-session-{session}   @brd.superproxy.io:33335
Oxylabs     : user-<用户名>-country-us-session-{session}                      @pr.oxylabs.io:7777
Smartproxy  : user-<用户名>-country-us-session-{session}                      @gate.smartproxy.com:7000
IPRoyal     : user-<用户名>-country-us-session-{session}                      @geo.iproyal.com:12321
```

> 关键差异就在**会话参数名**：有的叫 `session-`，有的叫 `sessid-`、`sid1-`。
> 写错的表现是「IP 完全不换」——用第 4 节的体检工具 30 秒就能验出来。

## 4. 必须先体检，再跑批量

```bash
.venv/bin/python tools/check_rotating_proxy.py --sessions 8 --rounds 3
```

它会同时验证两件事，缺一不可：

- **连打性（轮换）**：换会话是否真的换出口 IP；
- **粘性**：同一会话连探多次，出口 IP 是否保持不变。

```
断点性(粘性)      : ✅ 会话内出口稳定
连打性(轮换)      : ✅ 换会话即换 IP
  ✅ 轮换 + 粘性都正常：批量注册时每个账号会自动拿到独立且稳定的出口 IP。
```

若报 `❌ 换会话出口不变`，按提示顺序排查：会话参数名 → 套餐是否支持粘性 → 地区池是否太小。

## 5. 批量跑法

```bash
REGISTRATION_DRIVER=chrome python main.py --count 10 --delay 60 --continue-on-fail
```

- `main.py` 的批量循环**不传 proxy**，`build_chrome_driver()` 会对每个账号调用一次
  `pick_proxy()`，配了模板时即生成一个新的粘性会话 → 自动换 IP，**无需改任何业务代码**。
- `--delay` 仍建议保留（60 秒以上）。换 IP 解决的是「同 IP 连打」，但同一站点的
  注册总速率过高仍会被盯上。
- 多线程 `--workers` 会把任务压到同一条本机出口链路上，且 headful Chrome 多开吃内存，
  建议先用 `--workers 1` 验证模板有效，再逐步加压。

## 6. 三个容易踩的坑

1. **必须用 `http://`，不能用 `socks5://`**
   Chromium **不支持 SOCKS5 用户名/密码认证**。带账密的商业代理走 SOCKS 会直接连不上。
   代码在 `core/chrome_driver.py::_playwright_proxy_kwargs()` 里会显式告警。

2. **Playwright 需要独立的 username/password 字段**
   把 `http://user:pass@host:port` 整条塞进 `proxy={"server": ...}` 在部分组合下认证失败。
   本项目已自动拆分为 `{server, username, password}`。

3. **凭据不会进日志和账号存档**
   启动日志与账号 JSON 里落的是 `http://user-...-session-ab12cd34:***@gate:8000` 形式，
   只隐藏密码、保留会话 ID，既能排查「哪个账号走了哪个出口」，又不泄漏跨账号复用的代理密码。

## 7. 地区选择建议

- 出口地区尽量与账号画像一致；`CHROME_GEOIP=True` 会按出口 IP 自动匹配语言/时区，
  地区跨度太大反而更容易被风控。
- 不要只薅一个城市。同一 `/24` 段密集出号同样会被限流，优先选支持多地区、
  多 ISP 的住宅/移动代理池。
- 数据中心 IP 的放行率明显低于住宅 IP，注册类场景不建议用。
