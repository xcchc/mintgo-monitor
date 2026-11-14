"""自动打新（auto mint）—— 用 OpenSea 官方接口构造交易，BAW 执行。

设计要点（都是踩过坑总结的）：

1. **价格以 OpenSea 实时接口为准，不信缓存**
   实测：radar 缓存里 ASCIHOOD 的 priceWei=0（免费），但 OpenSea `/drops/{slug}`
   显示实际价格 50000000000000 wei（0.00005 ETH）。缓存会过期，交易不能拿过期
   数据去签。

2. **不要自己拼 calldata**
   OpenSea 的 `POST /api/v2/drops/{slug}/mint` 直接返回 to/data/value。
   早期我尝试逆向 SeaDrop 地址 + ABI 手工拼 calldata，既复杂又容易错。

3. **必须走 BAW 的 preview 做四项检查**
   模拟能确认「扣多少钱、拿到几个 NFT」，是最后一道防线。

4. **默认 dry-run**
   不加 --live 只做预检和模拟，绝不广播。

用法：
  python -X utf8 scripts/auto_mint.py                 # 预检（默认，不发交易）
  python -X utf8 scripts/auto_mint.py --live          # 真实执行
  python -X utf8 scripts/auto_mint.py --slug xxx --live   # 只处理指定项目

安全闸（全部满足才会真正发交易）：
  · verdict == "值得打"
  · stage_type == "public_sale"
  · price <= max_price_eth
  · 今日已执行次数 < max_per_day
  · BAW 模拟：无报错、扣款符合预期、拿到 NFT、无授权变更、无风险标记
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, date, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from opensea_key import UA, ensure as ensure_key  # noqa: E402

API = "https://api.opensea.io/api/v2"
CST = timezone(timedelta(hours=8))
LOG_FILE = ROOT / "data" / "mint_log.json"
STATE_FILE = ROOT / "data" / "mint_state.json"

# 链名 → BAW 的 binanceChainId
CHAIN_MAP = {
    "robinhood": "4663",
    "ethereum": "1",
    "base": "8453",
    "bsc": "56",
    "polygon": "137",
    "arbitrum": "42161",
}
# 代理：优先读环境变量 MINTGO_PROXY，其次用本机常见端口。境外主机可设空字符串直连。
PROXY = os.environ.get("MINTGO_PROXY", "http://127.0.0.1:7990")


# ---------------------------------------------------------------- 基础设施


def log(msg: str) -> None:
    print(f"[{datetime.now(CST):%H:%M:%S}] {msg}", flush=True)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def os_get(path: str, key: str, timeout: int = 30):
    req = urllib.request.Request(
        f"{API}{path}",
        headers={"Accept": "application/json", "User-Agent": UA, "X-API-KEY": key},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            try:
                return r.status, json.loads(body)
            except json.JSONDecodeError:
                return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:400]
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def os_post(path: str, payload: dict, key: str, timeout: int = 45):
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": UA,
            "Origin": "https://opensea.io",
            "Referer": "https://opensea.io/",
            "X-API-KEY": key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            try:
                return r.status, json.loads(body)
            except json.JSONDecodeError:
                return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:600]
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def baw_exe() -> str | None:
    return shutil.which("baw") or shutil.which("baw.cmd")


def baw_env() -> dict:
    e = dict(__import__("os").environ)
    e["NODE_USE_ENV_PROXY"] = "1"
    e["HTTPS_PROXY"] = PROXY
    e["HTTP_PROXY"] = PROXY
    e["ALL_PROXY"] = PROXY
    return e


def baw_run(args: list[str], timeout: int = 180) -> tuple[int, str]:
    exe = baw_exe()
    if not exe:
        return 127, "找不到 baw"
    try:
        p = subprocess.run(
            [exe, *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=baw_env(), timeout=timeout,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "baw 超时"
    except Exception as e:  # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


def pick_json(text: str) -> dict | None:
    """baw 有时会在 JSON 前后夹杂日志，抓第一个完整 JSON 对象。"""
    i = text.find("{")
    if i < 0:
        return None
    try:
        return json.loads(text[i:])
    except Exception:
        # 尝试截到最后一个右括号
        j = text.rfind("}")
        if j > i:
            try:
                return json.loads(text[i:j + 1])
            except Exception:
                return None
    return None


# ---------------------------------------------------------------- 业务逻辑


def slug_from_scored(s: dict) -> str | None:
    """从 opensea 链接、slug 字段或 name 推断 collection slug。"""
    # watchlist 里直接存了 slug
    if s.get("slug"):
        return s["slug"]
    url = s.get("opensea") or ""
    m = re.search(r"/collection/([^/?#]+)", url)
    if m:
        return m.group(1)
    return None


def get_state() -> dict:
    st = load_json(STATE_FILE, {})
    today = date.today().isoformat()
    if st.get("date") != today:
        st = {"date": today, "count": 0, "spent_eth": 0.0, "items": []}
    return st


def today_count() -> int:
    return int(get_state().get("count") or 0)


def today_spent() -> float:
    return float(get_state().get("spent_eth") or 0.0)


def bump_state(slug: str, address: str, price_eth: float = 0.0) -> None:
    st = get_state()
    st["count"] = int(st.get("count") or 0) + 1
    st["spent_eth"] = float(st.get("spent_eth") or 0.0) + float(price_eth)
    st.setdefault("items", []).append({
        "slug": slug, "address": address, "price_eth": price_eth,
        "at": datetime.now(CST).isoformat(),
    })
    save_json(STATE_FILE, st)


NOTIFY_STATE = ROOT / "data" / "automint_notified.json"
WATCH_FILE = ROOT / "data" / "watchlist.json"
HOLDINGS_FILE = ROOT / "data" / "holdings.json"


def holdings_add(chain: str, contract: str, token_id: str, slug: str,
                 name: str, mint_price_eth: float, cost_eth: float) -> str:
    """把打中的 NFT 登记进持仓表，交给 watch_holdings.py 监控地板价。"""
    h = load_json(HOLDINGS_FILE, {})
    key = f"{chain}:{contract.lower()}:{token_id}"
    h[key] = {
        "slug": slug, "name": name, "chain": chain,
        "contract": contract.lower(), "tokenId": str(token_id),
        "mint_price_eth": mint_price_eth,
        "cost_eth": cost_eth,
        "minted_at": datetime.now(CST).isoformat(),
        "alerts": {},
    }
    save_json(HOLDINGS_FILE, h)
    return key


def watch_add(slug: str, name: str, chain: str, price_eth: float, start_iso: str | None) -> None:
    """把「等着公开阶段」的项目单独记下来。

    **为什么必须有这个**：候选原来只从 `latest_scored.json` 里取，也就是依赖
    mintgo 的 radar。但项目一旦开抢，radar 里就找不到了（被移除或翻页沉底），
    于是等到公开阶段真的开时，它已经不在候选里，永远不会被尝试。

    实测事故：MIMORA 赔率 2.33x、公开阶段 02:30 开，00:32 之后就从 radar 消失，
    到点完全没去尝试，白白错过。
    """
    w = load_json(WATCH_FILE, {})
    w[slug] = {
        "slug": slug, "name": name, "chain": chain,
        "price_eth": price_eth, "start": start_iso,
        "added": datetime.now(CST).isoformat(),
    }
    save_json(WATCH_FILE, w)


def watch_list() -> dict:
    return load_json(WATCH_FILE, {})


def watch_prune() -> None:
    """清掉已经过期很久的，避免无限堆积。"""
    w = load_json(WATCH_FILE, {})
    now = datetime.now(timezone.utc)
    keep = {}
    for slug, it in (w or {}).items():
        raw = it.get("start")
        t = _parse_time(raw) if raw else None
        if t is None:
            continue
        hours_past = (now - t).total_seconds() / 3600
        if hours_past < 6:      # 开始后 6 小时内还留着（可能仍在 mint）
            keep[slug] = it
    save_json(WATCH_FILE, keep)


def already_notified(key: str) -> bool:
    """同一天同一个项目只提醒一次，避免每 15 分钟刷屏。"""
    st = load_json(NOTIFY_STATE, {})
    return st.get(key) == date.today().isoformat()


def mark_notified(key: str) -> None:
    st = load_json(NOTIFY_STATE, {})
    st[key] = date.today().isoformat()
    # 只保留今天的记录，避免文件无限膨胀
    today = date.today().isoformat()
    st = {k: v for k, v in st.items() if v == today}
    save_json(NOTIFY_STATE, st)


def push_wecom(text: str, cfg: dict) -> bool:
    """推企业微信。失败只记日志，不影响主流程。"""
    webhook = (cfg.get("notify") or {}).get("webhook") or ""
    if not webhook:
        return False
    payload = json.dumps({"msgtype": "text", "text": {"content": text}}).encode()
    req = urllib.request.Request(
        webhook, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode()).get("errcode") == 0
    except Exception:  # noqa: BLE001
        return False


def wallet_address() -> str:
    """BAW 钱包地址（mint 从它出）。"""
    code, out = baw_run(["wallet", "address", "--json"], timeout=60)
    j = pick_json(out) or {}
    for a in (j.get("data") or {}).get("addresses") or []:
        if a.get("binanceChainId") == "4663":
            return a["address"]
    return ""


def _parse_time(raw: str | None):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def find_public_stage(drops: dict) -> tuple[dict | None, str]:
    """在全部阶段里找 public_sale 阶段。

    返回 (当前可用的公开阶段 | None, 说明文字)。

    **这是踩过的坑**：早期只看 `active_stage`，遇到「白名单阶段正在跑、
    但公开阶段 2 小时后开始」的项目就直接判死。实测 MIMORA 和 JPEGZ
    都是这种 —— 它们有 public_sale 阶段，只是还没到时间。
    OpenSea 的报错也印证了：`The public stage opens at <时间>, and any
    wallet can mint in it then.`
    """
    stages = drops.get("stages") or []
    now = datetime.now(timezone.utc)

    publics = [st for st in stages if (st.get("stage_type") or "") == "public_sale"]

    # 1. 有没有正在进行的公开阶段
    for st in publics:
        s_t = _parse_time(st.get("start_time"))
        e_t = _parse_time(st.get("end_time"))
        if s_t and e_t and s_t <= now <= e_t:
            return st, "公开阶段进行中"

    # 2. 有没有即将开始的
    upcoming = []
    for st in publics:
        s_t = _parse_time(st.get("start_time"))
        if s_t and s_t > now:
            upcoming.append((s_t, st))
    if upcoming:
        s_t, st = min(upcoming, key=lambda x: x[0])
        mins = (s_t - now).total_seconds() / 60
        return None, (
            f"公开阶段 {s_t.astimezone(CST):%m-%d %H:%M} 开始"
            f"（还有 {mins:.0f} 分钟，价格 {int(st.get('price') or 0)/1e18:g} ETH）"
        )

    # 3. 全是白名单/已结束
    return None, "没有公开销售阶段（只能白名单，程序打不了）"


def fresh_stats(slug: str, key: str) -> dict | None:
    """从 OpenSea 拉实时市场数据。

    **为什么必须实时**：radar 里的 market 快照会随项目老化而消失，
    导致「明明有地板却算成无地板」。实测 MIMORA：
      radar   → 地板 None、判定 -25 分「付费且无地板」
      OpenSea → 地板 7.8e-05 ETH、3259 笔成交、323 持有人
    mint 价 1.2e-05，实时赔率 6.5x —— 好机会被陈旧数据判死了。

    返回 {floor, sales, volume, owners} 或 None。
    """
    code, d = os_get(f"/collections/{slug}/stats", key, timeout=20)
    if code != 200 or not isinstance(d, dict):
        return None
    total = d.get("total") or {}
    try:
        return {
            "floor": float(total["floor_price"]) if total.get("floor_price") else None,
            "sales": int(total.get("sales") or 0),
            "volume": float(total.get("volume") or 0),
            "owners": int(total.get("num_owners") or 0),
        }
    except Exception:  # noqa: BLE001
        return None


STABLE_SYMBOLS = {"USDG", "USDC", "USDT", "DAI", "FDUSD"}


def recent_sale_prices(slug: str, key: str, sample: int = 50, eth_usd: float = 2000.0) -> list[float]:
    """最近的真实成交价（不是挂单价），统一折算成 ETH 等值。

    **为什么不能用地板价当赔率**：实测 mihaoo 挂单地板 0.000396，
    但 35 笔真实成交全部在 0.000097~0.0001（等于 mint 价）。
    按挂单价算出来的 3.96x 从来没被任何人付过钱。

    **为什么要折算币种**：Robinhood 链上大量成交其实用 USDG（6 位小数稳定币）
    结算。直接拿 payment.quantity 当 ETH 会得到「0.09 ETH」这种荒谬价格 ——
    实际是 0.09 USDG。这里只接受 ETH/WETH 与常见稳定币，其余币种丢弃。
    """
    code, d = os_get(
        f"/events/collection/{slug}?event_type=sale&limit={max(1, min(sample, 50))}",
        key, timeout=20,
    )
    if code != 200 or not isinstance(d, dict):
        return []
    out: list[float] = []
    for e in d.get("asset_events") or []:
        pay = e.get("payment") or {}
        q = pay.get("quantity")
        if not q:
            continue
        try:
            sym = (pay.get("symbol") or "").upper()
            dec = int(pay.get("decimals") or 18)
            raw = int(q) / (10 ** dec)
        except Exception:  # noqa: BLE001
            continue
        if sym in ("ETH", "WETH") and dec == 18:
            out.append(raw)
        elif sym in STABLE_SYMBOLS:
            out.append(raw / eth_usd)
        # 其他代币计价：无法可靠折算，丢弃
    return out


def open_offer_count(slug: str, key: str) -> int | None:
    """集合级买单数量。挂单价可以随手标，买单要真金白银。None 表示查询失败。"""
    code, d = os_get(f"/offers/collection/{slug}?limit=50", key, timeout=20)
    if code != 200 or not isinstance(d, dict):
        return None
    return len(d.get("offers") or [])


def premium_gate(slug: str, price_eth: float, cfg: dict, key: str) -> tuple[bool, str, dict]:
    """真实需求复核：成交中位价 + 买单数量。

    拦两类陷阱：
      1. 挂单地板虚高 —— 地板是「最低报价」，成交价才是市场
      2. 全场没有买单 —— 没人出价说明需求为零，挂上去就是排队
    """
    filters = cfg.get("filters") or {}
    need = float(filters.get("min_sale_premium", 1.1))
    sample = int(filters.get("sale_sample", 50))
    require_offers = bool(filters.get("require_offers", True))

    prices = recent_sale_prices(slug, key, sample, float(filters.get("eth_usd_assume", 2000) or 2000))
    info: dict = {"sales_sampled": len(prices), "median_sale": None, "offers": None}
    if not prices:
        return False, "最近没有真实成交记录，挂单价没有成交支撑", info

    prices.sort()
    n = len(prices)
    med = prices[n // 2] if n % 2 else (prices[n // 2 - 1] + prices[n // 2]) / 2
    info["median_sale"] = med

    if price_eth > 0 and med < price_eth * need:
        return False, (
            f"成交中位价 {med:g} ETH 低于 mint 价 {price_eth:g} × {need:g}"
            f"（挂单价不等于成交价，无溢价市场）"
        ), info

    cnt = open_offer_count(slug, key)
    info["offers"] = cnt
    if require_offers and price_eth > 0 and cnt == 0:
        return False, "集合买单为 0，没有真实买家出价", info
    if price_eth == 0 and (cnt or 0) == 0:
        return False, "免费但既无买单也无成交，打了卖不掉", info

    return True, (
        f"成交中位价 {med:g} ETH（{n} 笔样本），买单 "
        f"{cnt if cnt is not None else '未知'} 个"
    ), info


def realtime_gate(slug: str, price_eth: float, cfg: dict, key: str) -> tuple[bool, str, dict]:
    """打之前用实时数据再过一遍门槛（临门一脚复核，不信任任何缓存）。"""
    filters = cfg.get("filters") or {}
    min_ratio = float(filters.get("min_floor_ratio", 1.0))
    min_sales = int(filters.get("min_sales_1h", 5))
    require_floor = bool(filters.get("require_floor_data", True))

    st = fresh_stats(slug, key)
    if st is None:
        return False, "拿不到实时市场数据，不敢打", {}

    floor = st["floor"]
    info = {"floor": floor, "sales": st["sales"], "owners": st["owners"]}

    if price_eth == 0:
        if floor:
            base = f"免费 + 实时地板 {floor:g} ETH（赔率 ∞）"
        elif require_floor:
            return False, "免费但实时无地板（当前配置要求有地板）", info
        elif st["sales"] < min_sales:
            return False, f"免费但 24h 仅 {st['sales']} 笔成交", info
        else:
            base = f"免费 + 24h {st['sales']} 笔成交"
        ok_pm, why_pm, pinfo = premium_gate(slug, price_eth, cfg, key)
        info.update(pinfo)
        if not ok_pm:
            return False, f"{base}，但{why_pm}", info
        return True, f"{base}；{why_pm}", info

    if not floor:
        return False, f"付费 {price_eth:g} ETH 但实时无地板，算不出赔率", info

    ratio = floor / price_eth
    info["ratio"] = ratio
    if ratio < min_ratio:
        return False, f"实时赔率 {ratio:.2f}x 低于门槛 {min_ratio:g}x", info
    if st["sales"] < min_sales:
        return False, f"实时赔率 {ratio:.2f}x 但 24h 仅 {st['sales']} 笔成交", info

    # 挂单赔率只是「卖方要价」，还要看真实成交和买单
    ok_pm, why_pm, pinfo = premium_gate(slug, price_eth, cfg, key)
    info.update(pinfo)
    if not ok_pm:
        return False, f"挂单赔率 {ratio:.2f}x，但{why_pm}", info
    return True, (
        f"挂单赔率 {ratio:.2f}x（地板 {floor:g} / mint {price_eth:g}）；{why_pm}"
    ), info


def evaluate(s: dict, drops: dict, cfg: dict) -> tuple[bool, str]:
    """返回 (现在能否打, 原因)。"""
    auto = cfg.get("automint") or {}
    max_price = float(auto.get("max_price_eth", 0))
    qty = int(auto.get("quantity", 1) or 1)

    if not drops:
        return False, "OpenSea 没有该 drop 的信息"
    if not drops.get("is_minting"):
        return False, "当前不在 mint 期"

    stage, note = find_public_stage(drops)
    if stage is None:
        return False, note

    price_wei = int(stage.get("price") or 0)
    price_eth = price_wei / 1e18
    if price_eth > max_price + 1e-12:
        return False, f"价格 {price_eth:g} ETH 超过上限 {max_price:g} ETH"

    mpw = stage.get("max_per_wallet")
    if mpw is not None:
        try:
            if qty > int(mpw):
                return False, f"限购 {mpw}，请求 {qty} 超了"
        except Exception:
            pass

    return True, f"可打：{price_eth:g} ETH × {qty}（{stage.get('label') or 'public'}）"


def preview_tx(chain_id: str, frm: str, to: str, value_wei: int, data: str) -> tuple[bool, dict, str]:
    """BAW 模拟。返回 (通过?, 模拟结果, requestId 或错误信息)。"""
    code, out = baw_run([
        "contract-call", "preview",
        "--binanceChainId", chain_id,
        "--from", frm,
        "--to", to,
        "--value", hex(value_wei),
        "--inputData", data,
        "--json",
    ], timeout=200)

    j = pick_json(out)
    if not j or not j.get("success"):
        return False, {}, f"预览失败: {out[:300]}"

    d = j.get("data") or {}
    sim = d.get("simulationResult") or {}
    rid = d.get("requestId") or ""

    if sim.get("simulationErrorDetail"):
        return False, d, f"模拟报错: {sim['simulationErrorDetail']}"
    if (sim.get("authorityChanges") or []):
        return False, d, "存在授权变更，警惕"
    if ((d.get("risks") or {}).get("riskDetails") or []):
        return False, d, "风控有标记"

    return True, d, rid


def execute(request_id: str) -> tuple[bool, dict]:
    code, out = baw_run(["contract-call", "execute", "--requestId", request_id, "--json"], timeout=240)
    j = pick_json(out) or {}
    ok = bool(j.get("success"))
    return ok, j.get("data") or {"raw": out[:300]}


def handle_one(s: dict, key: str, cfg: dict, live: bool) -> dict:
    """处理单个项目，返回结果记录。"""
    slug = slug_from_scored(s)
    name = s.get("name") or "?"
    rec = {"name": name, "slug": slug, "at": datetime.now(CST).isoformat(),
           "live": live, "ok": False, "reason": "", "txHash": None}

    if not slug:
        rec["reason"] = "拿不到 collection slug"
        return rec

    chain = (s.get("chain") or "").lower()
    chain_id = CHAIN_MAP.get(chain)
    if not chain_id:
        rec["reason"] = f"链 {chain} 不在支持列表"
        return rec

    # 1. 拿实时 drop 信息（唯一权威来源）
    code, drops = os_get(f"/drops/{slug}", key)
    if code != 200 or not isinstance(drops, dict):
        rec["reason"] = f"查询 drop 失败 HTTP {code}"
        return rec

    # 2. 判定
    ok, why = evaluate(s, drops, cfg)
    rec["reason"] = why
    log(f"  {name}: {why}")
    if not ok:
        # 如果是「等公开阶段」，记进 watchlist —— 之后 radar 里可能就找不到它了
        if "公开阶段" in why:
            stages = drops.get("stages") or []
            now = datetime.now(timezone.utc)
            nxt = None
            for st in stages:
                if (st.get("stage_type") or "") != "public_sale":
                    continue
                s_t = _parse_time(st.get("start_time"))
                e_t = _parse_time(st.get("end_time"))
                if s_t and e_t and s_t <= now <= e_t:
                    nxt = st
                    break
                if s_t and s_t > now and nxt is None:
                    nxt = st
            if nxt:
                watch_add(
                    slug, name, chain,
                    int(nxt.get("price") or 0) / 1e18,
                    nxt.get("start_time"),
                )
                log(f"    已加入 watchlist（{nxt.get('start_time')}）")
        return rec

    # 2.5 临门一脚：用实时市场数据复核（不信任 radar 缓存）
    stage, _ = find_public_stage(drops)
    if stage is None:
        rec["reason"] = "阶段判断异常"
        return rec
    price_eth = int(stage.get("price") or 0) / 1e18
    ok_rt, why_rt, stats = realtime_gate(slug, price_eth, cfg, key)
    rec["realtime"] = stats
    log(f"    实时复核：{why_rt}")
    if not ok_rt:
        rec["reason"] = why_rt
        return rec

    # 3. 构造交易
    qty = int((cfg.get("automint") or {}).get("quantity", 1) or 1)
    minter = wallet_address()
    if not minter:
        rec["reason"] = "拿不到 BAW 钱包地址"
        return rec

    code, tx = os_post(f"/drops/{slug}/mint", {"minter": minter, "quantity": qty}, key)
    if code != 200 or not isinstance(tx, dict) or not tx.get("to"):
        rec["reason"] = f"构造 mint 交易失败 HTTP {code}: {str(tx)[:200]}"
        return rec

    value_wei = int(tx.get("value") or 0)
    rec.update({"to": tx["to"], "valueWei": value_wei, "minter": minter})

    # 4. 模拟
    log(f"    模拟中（to={tx['to'][:10]}… value={value_wei} wei）…")
    ok, sim, rid = preview_tx(chain_id, minter, tx["to"], value_wei, tx["data"])
    if not ok:
        rec["reason"] = rid
        return rec

    # 检查模拟是否真的带来了 NFT
    got_nft = any(
        b.get("tokenType") == "Erc721" and int(b.get("change") or 0) > 0
        for b in (sim.get("simulationResult") or {}).get("balanceChanges") or []
    )
    if not got_nft:
        rec["reason"] = "模拟没显示收到 NFT，可能已售罄或合约异常"
        return rec

    rec["simulated"] = True
    log("    ✅ 模拟通过（会收到 NFT）")

    if not live:
        rec["reason"] = "预检通过（dry-run，未广播）"
        rec["requestId"] = rid
        return rec

    # 5. 执行
    price_eth = value_wei / 1e18
    log("    🚀 广播交易…")
    ok, res = execute(rid)
    rec["ok"] = ok
    rec["priceEth"] = price_eth
    rec["txHash"] = res.get("txHash")
    rec["reason"] = "已广播" if ok else f"执行失败: {str(res)[:200]}"
    if ok:
        bump_state(slug, rec.get("to") or "", price_eth)
        log(f"    ✅ txHash={rec['txHash']}")

        # 登记持仓 → 交给 watch_holdings.py 盯地板价
        token_id = None
        nft_contract = ""
        for b in (sim.get("simulationResult") or {}).get("balanceChanges") or []:
            if b.get("tokenType") == "Erc721" and int(b.get("change") or 0) > 0:
                token_id = b.get("tokenId")
                nft_contract = b.get("contractAddress") or ""
                break
        if token_id:
            # tokenId 是 0x 十六进制，转十进制
            try:
                tid = str(int(token_id, 16)) if str(token_id).startswith("0x") else str(token_id)
            except Exception:
                tid = str(token_id)
            hkey = holdings_add(chain, nft_contract, tid, slug, name,
                                price_eth, price_eth + 0.0000024)
            rec["holding"] = hkey
            log(f"    📌 已纳入持仓监控：{slug} #{tid}")
    return rec


def build_report(results: list[dict], cfg: dict, live: bool) -> str:
    """生成企业微信汇报文本（纯 text，微信端才显示得出来）。"""
    auto = cfg.get("automint") or {}
    ok = [r for r in results if r.get("ok")]
    tried = [r for r in results if r.get("simulated")]
    waiting = [r for r in results if "公开阶段" in (r.get("reason") or "")]
    rejected = [r for r in results
                if not r.get("simulated") and not r.get("ok")
                and "公开阶段" not in (r.get("reason") or "")]

    lines = []
    head = "🟢 打新汇报" if live else "🔍 打新预检"
    lines.append(f"{head} · {datetime.now(CST):%m-%d %H:%M}")

    if ok:
        lines.append("")
        lines.append(f"✅ 已打成 {len(ok)} 个")
        for r in ok:
            lines.append(f"• {r.get('name')}")
            lines.append(f"  价格 {r.get('priceEth', 0):g} ETH")
            if r.get("txHash"):
                lines.append(f"  tx {r['txHash'][:18]}…")
    elif tried and not live:
        lines.append("")
        lines.append(f"🎯 有 {len(tried)} 个可打（dry-run，未广播）")
        for r in tried:
            lines.append(f"• {r.get('name')}")
            lines.append(f"  价格 {r.get('priceEth', r.get('valueWei', 0) / 1e18 if r.get('valueWei') else 0):g} ETH")

    if waiting:
        lines.append("")
        lines.append(f"⏳ 等待公开阶段（{len(waiting)} 个）")
        for r in waiting:
            lines.append(f"• {r.get('name')}")
            lines.append(f"  {r.get('reason')}")

    if rejected:
        lines.append("")
        lines.append("⏭ 未通过：")
        for r in rejected[:6]:
            lines.append(f"• {r.get('name')}：{r.get('reason', '?')}")

    used = today_count()
    spent = today_spent()
    lines.append("")
    lines.append("📊 今日仓位")
    lines.append(f"• 已用 {used}/{auto.get('max_per_day', '?')} 笔")
    lines.append(f"• 已花 {spent:g}/{auto.get('max_daily_spend_eth', '?')} ETH")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="真实执行（默认只预检）")
    ap.add_argument("--slug", help="只处理指定 collection slug")
    ap.add_argument("--limit", type=int, default=3, help="本轮最多处理几个")
    ap.add_argument("--no-push", action="store_true", help="不推企业微信")
    args = ap.parse_args()

    import config_loader

    cfg = config_loader.load(ROOT / "config.yaml")
    auto = cfg.get("automint") or {}
    if not auto.get("enabled", False):
        log("⚠️ automint.enabled = false，跳过。要启用请改 config.yaml")
        return 0

    max_per_day = int(auto.get("max_per_day", 3) or 3)
    max_spend = float(auto.get("max_daily_spend_eth", 0.0015) or 0)
    used, spent = today_count(), today_spent()
    log(f"今日已用 {used}/{max_per_day} 笔，已花 {spent:g}/{max_spend:g} ETH")

    watch_prune()

    # 后面反查 slug 要用 key，所以先初始化
    key = ensure_key(verbose=False)

    # 候选池来源（三个都要，缺一会漏机会）：
    #   ① radar 判定「值得打」的 —— 传统来源
    #   ② watchlist —— 已开抢但从 radar 消失的
    #   ③ radar 里【未开抢 + 价格在上限内】的 —— 关键补充！
    #      这些在 radar 里往往被判「跳过」（因为 radar 的 market 快照没它们的地板），
    #      但 OpenSea 实时是有地板的。实测 mihaoo：radar 判跳过，
    #      实时赔率却是 4.00x、1261 笔成交 —— 不补这一条就完全看不到。
    cands: list[dict] = []
    seen: set[str] = set()

    scored = load_json(ROOT / "data" / "latest_scored.json", [])
    for s in scored:
        if s.get("verdict") != "值得打":
            continue
        slug = slug_from_scored(s)
        if slug and slug not in seen:
            seen.add(slug)
            cands.append(s)

    for slug, it in (watch_list() or {}).items():
        if slug in seen:
            continue
        seen.add(slug)
        cands.append({
            "name": it.get("name") or slug, "chain": it.get("chain") or "",
            "slug": slug, "verdict": "值得打", "_from_watchlist": True,
        })

    # ③ radar 里未开抢、价格在上限内的
    cap = float(auto.get("max_price_eth", 0.0005))
    raw = load_json(ROOT / "data" / "latest_raw.json", {})
    now_ms = int(time.time() * 1000)
    for ch, blk in (raw.get("radar") or {}).items():
        if ch not in CHAIN_MAP:
            continue
        for it in blk.get("items") or []:
            start = it.get("startAt") or 0
            if not start or start > now_ms + 3 * 3600_000:
                continue          # 只补未来 3 小时内的，避免一次查太多
            price = float(it.get("priceEth") or 0)
            if price > cap:
                continue
            addr = (it.get("contractAddress") or "").lower()
            if not addr:
                continue
            slug = (it.get("opensea") or "")
            m = re.search(r"/collection/([^/?#]+)", slug)
            key_slug = m.group(1) if m else None
            if not key_slug:
                # 用合约地址反查 slug
                code, cd = os_get(f"/chain/{ch}/contract/{addr}", key, timeout=15)
                if code == 200 and isinstance(cd, dict):
                    key_slug = cd.get("collection")
            if not key_slug or key_slug in seen:
                continue
            seen.add(key_slug)
            cands.append({
                "name": it.get("displayName") or "?", "chain": ch,
                "slug": key_slug, "verdict": "值得打", "_from_radar_scan": True,
            })

    if args.slug:
        cands = [s for s in cands if (slug_from_scored(s) or "") == args.slug]
    n_w = sum(1 for s in cands if s.get("_from_watchlist"))
    n_s = sum(1 for s in cands if s.get("_from_radar_scan"))
    log(f"候选：{len(cands)} 个（评分 {len(cands) - n_w - n_s} + watchlist {n_w} + 扫描 {n_s}）")
    if not cands:
        return 0

    key = ensure_key(verbose=False)
    results = []
    for s in cands[: args.limit]:
        if args.live:
            if today_count() >= max_per_day:
                log(f"⛔ 已到每日笔数上限 {max_per_day}，停止")
                break
            if today_spent() >= max_spend:
                log(f"⛔ 已到每日花费上限 {max_spend:g} ETH，停止")
                break
        log(f"处理：{s.get('name')}（{s.get('chain')}）")
        r = handle_one(s, key, cfg, args.live)
        results.append(r)

    # 落盘
    hist = load_json(LOG_FILE, [])
    hist.extend(results)
    save_json(LOG_FILE, hist[-500:])

    done = [r for r in results if r.get("ok")]
    log(f"本轮完成：执行成功 {len(done)} 个 / 共 {len(results)} 个")

    # 汇报策略：
    #   · 有可打/已打 → 一定推
    #   · 只有「等待公开阶段」→ 每个项目每天只推一次（否则每 15 分钟刷屏）
    #   · 其他情况（价格超限、无公开阶段等）→ 不推，避免噪音
    if not args.no_push:
        actionable = any(r.get("simulated") or r.get("ok") for r in results)
        fresh_waiting = [
            r for r in results
            if "公开阶段" in (r.get("reason") or "")
            and not already_notified(f"wait:{r.get('slug')}")
        ]
        if actionable or fresh_waiting:
            text = build_report(results, cfg, args.live)
            if push_wecom(text, cfg):
                log("📤 已推送企业微信")
                for r in fresh_waiting:
                    mark_notified(f"wait:{r.get('slug')}")
            else:
                log("⚠️ 企业微信推送失败")

    return 0


if __name__ == "__main__":
    sys.exit(main())
