# mintgo-monitor

> 定期抓取 [mintgo.fun](https://mintgo.fun) 的 NFT mint 数据，按规则筛出「值得打」的项目，产出 Markdown 报告，并可推送到企业微信。

[![selftest](https://github.com/xcchc/mintgo-monitor/actions/workflows/selftest.yml/badge.svg)](https://github.com/xcchc/mintgo-monitor/actions/workflows/selftest.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)](https://www.python.org/)

一个「打新雷达」：把 mintgo.fun 上即将开抢和正在 mint 的项目抓下来，用「地板价 ÷ mint 价」的赔率叠加真实成交、creator 历史、进度等信号打分，分四档输出，脏数据（刷量、工厂盘、已售罄）会被硬性排除。

*An automated screener for [mintgo.fun](https://mintgo.fun) NFT mints: scrapes the radar via a real headless browser, scores each drop by floor/mint odds, trading depth and creator history, then renders a Markdown report or pushes it to WeCom.*

---

## 目录

- [为什么需要它](#为什么需要它)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [用法](#用法)
- [输出物](#输出物)
- [目录结构](#目录结构)
- [筛选逻辑](#筛选逻辑)
- [企业微信推送](#企业微信推送)
- [自检](#自检)
- [已知限制](#已知限制)
- [免责声明](#免责声明)

## 为什么需要它

mintgo.fun 的看板是纯前端 SPA，数据走 `/api/view/*`，但有两道门：

1. `POST /api/session` 换 `mg_access` cookie（1 小时有效）
2. `/api/view/*` 校验 **same-origin realtime client** —— 用 curl 伪造 cookie 一律 403 `Same-origin realtime view required`，client id 还必须来自页面自己的 `sessionStorage['mintgo-realtime-client-id']`

所以拿数据只能用一个真实浏览器在同源页面里 fetch。本项目用 Playwright + 无头 Chrome 做这件事。

还有一个坑：站点对默认 headless UA 直接 403（`nginx/1.24.0`），**必须伪装成真实 Chrome UA**。

## 快速开始

需要 Python 3.10+。

```bash
git clone https://github.com/xcchc/mintgo-monitor.git
cd mintgo-monitor

python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate

pip install -r requirements.txt

# 如果 config.yaml 里 chrome_path 留空，需要下载 Playwright 自带的 chromium；
# 想复用系统 Chrome 则跳过这步，直接把 chrome_path 指到 chrome 可执行文件。
playwright install chromium

# 生成配置（config.yaml 不会进版本库）
cp config.example.yaml config.yaml     # Windows: copy config.example.yaml config.yaml

python -X utf8 src/run_once.py
```

跑完看 `data/latest.md`，或者直接打开 `data/reports/` 里最新一份。

> **网络**：中国大陆直连 mintgo.fun 会被 reset，需要在 `config.yaml` 里给 `proxy` 填本地代理；境外主机置空直连更快。

## 配置说明

全部配置在 `config.yaml`（模板见 `config.example.yaml`）：

| 键 | 说明 |
|---|---|
| `proxy` | HTTP 代理，如 `http://127.0.0.1:7990`。留空直连 |
| `chrome_path` | 浏览器可执行文件。留空用 Playwright 自带 chromium |
| `user_agent` | 伪装 UA。**不要用默认 headless UA，会 403** |
| `chains` | 要抓的链，必须和站点链切换器对齐（见下） |
| `max_pages` | 每链最多翻多少页（24 条/页），`12` 一般够 |
| `lookahead_hours` | 只保留多少小时内开始 / 已在进行中的项目 |
| `filters.*` | 筛选阈值，详见[筛选逻辑](#筛选逻辑) |
| `report.top_n` | 报告主表列前多少个 |
| `report.include_all_upcoming` | 报告末尾是否附完整清单 |
| `notify.*` | 企业微信推送，见[推送](#企业微信推送) |

### `chains` 必须和站点对齐

站点左上角有 `ALL / ETH / RH / INK / ARC / BASE / BSC / ELY` 一排链按钮，配置**必须和它一一对应**，否则会静默漏数据。

| 现象 | 原因 | 处理 |
|---|---|---|
| 只抓到 4 条链 | 早期配置漏了 `bsc`、`elysium` | 已补齐，现在抓 6 条 |
| 想「直接查 all」 | `chain=all` 返回 `409 REALTIME_SCOPE_MISMATCH` | `ALL` 只是前端把各链合起来的视图，没有对应接口 |
| `arc` 查不到 | 按钮是 `chain-button-soon`，接口返回 `400 Invalid radar chain` | 未上线，不配 |

所以「拿全部数据」= 把每条真实链都翻一遍，代码里就是这么做的（`fetch_radar.py` 逐链翻页）。
`bsc` / `elysium` 当前是 0 个项目，但链是通的 —— 不要因为暂时为空就从配置里删掉。

## 用法

```bash
# 跑一轮：抓取 → 打分 → 报告 → 推送 → 落盘
python -X utf8 src/run_once.py
```

退出码：`0` 正常 / `1` 抓取失败 / `2` 抓取成功但零项目（多半是会话或代理有问题）。

### 定时运行

Windows 计划任务（每 30 分钟一次，日志写 `logs/task.log`）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -IntervalMinutes 30
# 取消
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Remove
```

不想用计划任务、就想开着终端常驻循环：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_loop.ps1 -IntervalMinutes 30
```

Linux / macOS 用 cron：

```cron
*/30 * * * * cd /path/to/mintgo-monitor && python3 -X utf8 src/run_once.py >> logs/task.log 2>&1
```

> Windows 下也可直接双击 `scripts\run.bat` 跑一轮。

## 输出物

| 路径 | 内容 |
|---|---|
| `data/latest.md` | 最新一份 Markdown 报告 |
| `data/reports/<stamp>.md` | 每轮一份，按时间戳存档 |
| `data/snapshots/<stamp>.json` | 每轮原始抓取结果，便于回溯 |
| `data/latest_raw.json` | 最新一次原始数据 |
| `data/latest_scored.json` | 最新一次打分结果（含每项得分与理由） |
| `data/notify_state.json` | 推送去重状态（自动维护） |
| `logs/` | 每轮日志 |

`data/` 与 `logs/` 都在 `.gitignore` 里，不会进版本库。

## 目录结构

```
mintgo-monitor/
├── config.example.yaml    # 配置模板（复制成 config.yaml 使用）
├── requirements.txt
├── src/
│   ├── fetch_radar.py     # 抓 radar（upcoming mints）+ market snapshot
│   ├── score.py           # 打分与筛选规则
│   ├── report.py          # 生成 Markdown 报告
│   ├── notify.py          # 企业微信推送（纯文本排版 + 去重冷却 + 逐级降级）
│   ├── fmt.py             # 报告与推送共用的数字/时间/链名格式化
│   ├── config_loader.py   # 轻量 YAML 读取（不依赖 pyyaml）
│   └── run_once.py        # 单次完整流程
├── scripts/
│   ├── run.bat            # Windows 一键跑一次
│   ├── run_loop.ps1       # Windows 常驻循环
│   ├── register_task.ps1  # 注册 / 注销 Windows 计划任务
│   ├── preview_notify.py  # 推送排版预览（可 --send 真发）
│   ├── selftest_notify.py # 推送模块自检（纯本地）
│   └── selftest_score.py  # 评分模块自检（纯本地）
└── .github/workflows/     # CI：跑两个自检
```

## 筛选逻辑

核心是「mint 价 ÷ 二级地板」的赔率，叠加真实性与风险扣分：

| 信号 | 影响 |
|---|---|
| `floor / mint_price`（赔率） | 主要得分项，≥ 1.0 才有意义 |
| 免费 mint（priceEth == 0 且有地板） | 直接高分，但要求地板 > 0 且成交 > 0 |
| 1h 成交笔数 | < 5 笔视为无深度，重罚 |
| **1h 成交量** | **低于 `min_volume_1h_eth` 视为刷量，直接判死** |
| creator 的历史项目数 | ≥ 10 视为工厂盘，扣分 |
| creator 钱包年龄 | < 3 天扣分 |
| 进度（sold / supply） | > 95% 视为抢不到，降权 |

### 为什么要看成交量：只数笔数会被刷量骗

真实案例：`ASCIHOOD` 有 359 笔 1h 成交，看起来「深度很好」，一度被判为 🟢 值得打（45 分）。但它 1 小时总成交量只有 **0.000275 ETH**，平均每笔 **$0.0016**：

| 项目 | 地板(ETH) | 1h 笔数 | 1h 成交量(ETH) | 单笔均值(USD) |
|---|---|---|---|---|
| ASCIHOOD | 9.6e-05 | 359 | 0.000275 | **$0.0016** ← 刷量 |
| Cursed-P | 0.09154 | 48 | 0.0189 | $0.81 |
| Hood Bulls | — | 11 | 0.275 | $51 |
| Web Hood | — | 15 | 58.9 | $8086 |

比正常项目低 400 到 500 万倍 —— 典型的用极小金额反复刷笔数。现在两条一起看：

```yaml
filters:
  min_sales_1h: 5            # 笔数门槛
  min_volume_1h_eth: 0.001   # 成交量门槛（≈ $2），防止刷量
```

```
笔数 >= 5 且 成交量 >= 门槛  → 正常，按笔数加分
笔数 >= 5 但 成交量 <  门槛  → 判「疑似刷量」，扣 20 分 + hard_fail
```

加上后 ASCIHOOD 从 45 分掉到 5 分，正确判为「跳过」。设 `min_volume_1h_eth: 0` 可关闭。

### 四档判定

**默认策略：只发布有地板价格的项目**（`require_floor_data: true`）。

| 档位 | 含义 |
|---|---|
| 🟢 `值得打` | 有真实地板、赔率 ≥ 1、深度够、creator 正常 |
| 🟡 `观察` | 有地板，但分数中等 |
| 🔵 `无地板·待看` | **仅当 `require_floor_data: false` 时才出现**，且只收免费 mint |
| ⚪ `跳过` | 触发硬性排除（垃圾名、赔率亏损、深度不足、工厂盘、已售罄、**无地板**） |

**为什么默认只发布有地板的：** 没有地板 = 算不出赔率 = 无法判断值不值得打。对不懂行的人来说，看着一堆「免费」项目去打，本质是盲赌。

> ⚠️ **代价很大**：有地板数据的项目极少。历史 16 份快照统计，占比只有 **0.6% ~ 2.3%**，平均每轮 1~4 个，**经常一个都没有**。所以很多轮次会「无项目可推」，日志里是 `🔕 本轮无可发布项目`，这是正常的，不是坏了。

想恢复「把没地板的也列出来自己看」的模式：

```yaml
filters:
  require_floor_data: false     # 关掉严格模式
  nofloor_max_price_eth: 0      # 且只收免费的
```

### 为什么必须单列「无地板·待看」

**这是一次真实的数据 bug 修复。** 早期版本把「没有地板」当成扣分项，结果：

```
robinhood  77 个 → 值得打 1 个
ethereum   22 个 → 值得打 0 个   ← 全部被埋掉
base        5 个 → 值得打 0 个   ← 全部被埋掉
ink         1 个 → 值得打 0 个   ← 全部被埋掉
```

看起来像「只有 Robinhood 有机会」，实际是**数据管道问题**：

| 事实 | 数字 |
|---|---|
| radar 项目数（即将开抢的新项目） | 178 |
| market-snapshot 地址数（已有二级市场） | 115 |
| **两者重叠** | **只有 2 个，且都在 Robinhood** |

radar 和 market 本就是两个几乎不相交的集合 —— 正在 mint 的新项目天然还没有二级地板。所以「无地板」是**信息缺失**，不该等同于「不值得打」。现在单列一档，把首发条件（价格、限购、开抢时间、阶段、creator 记录）摊给用户自己判断。

### 同时修复的两个 bug

1. **索引缺链**：`build_market_index` 早期只用合约地址做 key，**没带链**。不同 EVM 链上完全可能出现同一地址（相同部署者 nonce、fork 项目），会把 A 链的地板算到 B 链头上。现在 key 是 `(chain, address)`。
2. **配置从未生效**：阈值配在 `filters:` 下面，但早期代码直接读顶层 `cfg.get("min_floor_ratio")` —— 永远 `None`，于是静默回退到硬编码默认值。因为默认值恰好和配置一样，**一直没被发现：改 config 根本不生效**。现在统一走 `_cfg_val(cfg, key, default)`：`filters` 块 → 顶层 → 默认值。

## 企业微信推送

每轮跑完可以把「值得打」的项目推到企业微信群机器人。四条设计原则：

1. **发纯文本，不发 markdown** —— 机器人发 markdown 时，**微信客户端不渲染**，只显示「暂不支持此消息类型」。text 在微信和企业微信两端都能直接看，所以排版靠空格缩进对齐，链接写裸 URL。
2. **不刷屏** —— 同一合约地址在 `cooldown_minutes`（默认 180 分钟）内只推一次；分数比上次推高 `min_score_delta`（默认 10 分）才允许打破冷却。
3. **不打扰** —— 默认只推 `值得打`。**没货就完全不发消息**，日志里记一条 `🔕 本轮无可发布项目`。
4. **不拖垮主流程** —— 推送是旁路：网络抖动、webhook 报错、状态文件损坏都只写日志，抓取和报告照常落盘。

推送长这样（每行都有 emoji + 中文标签，不用记字段顺序）：

```
🚀 MintGo 打新提醒 · 11-10 11:10
🌐 扫描 177 个 ｜ 6 条链（4 条有货） ｜ 值得打 1 个

1. Legend of the Stonks: Founding Legends
   📍 链：Robinhood
   💰 价格：免费 · 地板 0.0028
   ⏰ 开抢：11-11 02:00（14.8 小时后）
   📊 1h 成交：10 笔 ｜ 👤 限购：961 ｜ 🏭 作者项目：2 个
   🎯 评分：50
   🔗 项目网址：https://opensea.io/collection/legend-of-the-stonks-founding-legends
   🐦 推特：https://x.com/LegendOfStonks
   ⛓ 合约地址：https://robinhoodchain.blockscout.com/address/0x44a5...
```

配置：

```yaml
notify:
  enabled: true
  webhook: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<你的key>"
  push_nofloor: true      # 没有「值得打」时也推「无地板·待看」名单
  cooldown_minutes: 180
  min_score_delta: 10
  state_file: "data/notify_state.json"
```

> `config.yaml` 已在 `.gitignore` 中 —— 填了真实 webhook 也不会误提交。

**超长怎么处理**：text 消息正文上限 **2048 字节**（UTF-8，中文/emoji 各占 3–4 字节），所以 `notify.py` 按**字节**截断而不是按字符；项目多了不是一刀切，而是**逐个降级**：`完整链接 → 只留项目网址 → 不带链接`，取第一个放得下的形态；连最精简的都放不下就停下，末尾写「…另有 N 个项目，详见本地报告」。

调试：

```bash
# 只渲染不发（看排版、看字节数）
python -X utf8 scripts/preview_notify.py

# 手动真发一条（不受冷却限制）
python -X utf8 scripts/preview_notify.py --send

# 看极简降级版长什么样
python -X utf8 scripts/preview_notify.py --all --compact
```

## 自检

两个自检脚本纯本地运行，不发网络请求、不开浏览器，CI 里也会跑：

```bash
python -X utf8 scripts/selftest_score.py    # 分档规则、免费过滤、刷量判定、配置读取
python -X utf8 scripts/selftest_notify.py   # 推送渲染、字节截断、降级、去重冷却
```

## 已知限制

- 分数依赖**是否有二级地板数据**：新项目绝大多数没有地板，因此会集中在「观察」档 —— 这是实际情况，不是 bug。
- radar 数据本身有 `stale` 风险（`fetchedAt` 由服务端写入），报告里会带上时间戳。
- 本项目只做**信息聚合**，不签名、不转账、不连钱包。
- 推送去重靠 `data/notify_state.json`，删掉它会让所有项目重新推一遍（调试时有用）。
- 站点接口可能随时变更；抓取失败时看 `logs/` 里当轮的日志和 `data/snapshots/` 落盘结果。

## 免责声明

本项目仅用于个人学习与信息聚合，**不构成任何投资建议**。NFT / 链上资产风险极高，决策与后果由使用者自负。

抓取行为请自行控制频率并遵守 mintgo.fun 的服务条款；如果站点方要求停止，请立即停止使用。

## License

[MIT](LICENSE) © 2026 xcchc
