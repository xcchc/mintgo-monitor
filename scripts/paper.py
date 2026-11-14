"""信号 → 成交 → 回测：把「当时的判断」和「后来的真实成交」分开记录。

设计
----
1. **record**：每轮抓完，把候选项目的**信号快照**写进 `data/paper_signals.jsonl`
   （mint 价、挂单地板、赔率、分数、判定、1h 成交……）。同一个项目每小时只记一条。
   这一步不发任何请求，零成本。

2. **settle**：对已经过了 N 小时（默认 24h）的信号，去 OpenSea 拉它**信号之后**的
   真实成交，算出「如果当时按 mint 价打 1 个、按后续成交中位价卖」的 ROI，
   写进 `data/paper_fills.jsonl`。这一步才消耗 OpenSea 读额度。

3. **report**：按判定分档统计（值得打 / 观察 / 跳过），输出胜率、中位 ROI。
   这就是「用赔率当信号、用成交价验证」的闭环。

用法
----
    python -X utf8 scripts/paper.py record          # 记录本轮信号（建议每轮跑）
    python -X utf8 scripts/paper.py settle          # 结算 24h 前的信号（每天跑一次）
    python -X utf8 scripts/paper.py report          # 输出统计
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from auto_list import os_get  # noqa: E402
from opensea_key import ensure as ensure_key  # noqa: E402

SIGNALS = ROOT / "data" / "paper_signals.jsonl"
FILLS = ROOT / "data" / "paper_fills.jsonl"
SCORED = ROOT / "data" / "latest_scored.json"
OUT = ROOT / "data" / "reports"
CST = timezone(timedelta(hours=8))
GAS_ETH = 0.000004
FEE = 0.99
STABLE = {"USDG", "USDC", "USDT", "DAI", "FDUSD"}


def log(msg: str) -> None:
    print(f"[{datetime.now(CST):%H:%M:%S}] {msg}", flush=True)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def append_jsonl(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- record


def cmd_record(args) -> int:
    if not SCORED.exists():
        log("没有 latest_scored.json，先跑一轮 src\\run_once.py")
        return 1
    scored = json.loads(SCORED.read_text(encoding="utf-8"))
    now = datetime.now(CST)
    hour_key = now.strftime("%Y-%m-%dT%H")
    seen = {(r.get("slug"), r.get("hour")) for r in read_jsonl(SIGNALS)}

    rows = []
    for s in scored:
        slug = _slug_of(s)
        if not slug:
            continue
        if (slug, hour_key) in seen:
            continue
        rows.append({
            "ts": now.isoformat(),
            "ts_epoch": int(now.timestamp()),
            "hour": hour_key,
            "slug": slug,
            "name": s.get("name"),
            "chain": s.get("chain"),
            "contract": (s.get("contract") or "").lower(),
            "mint_eth": s.get("mint_eth"),
            "floor_eth": s.get("floor_eth"),
            "ratio": s.get("ratio"),
            "score": s.get("score"),
            "verdict": s.get("verdict"),
            "sales_1h": s.get("sales_1h"),
            "sale_median_eth": s.get("sale_median_eth"),
        })
    append_jsonl(SIGNALS, rows)
    log(f"记录信号 {len(rows)} 条（本轮 {len(scored)} 个项目，同项目每小时只记一次）")
    return 0


def _slug_of(s: dict) -> str | None:
    url = s.get("opensea") or ""
    marker = "/collection/"
    if marker in url:
        return url.split(marker, 1)[1].split("?", 1)[0].split("#", 1)[0].strip("/") or None
    return s.get("slug")


# ---------------------------------------------------------------- settle


def sale_prices_between(slug: str, key: str, t0_epoch: int, t1_epoch: int,
                        eth_usd: float, pages: int = 6) -> tuple[list[float], bool, bool]:
    """拉取 [t0, t1] 窗口内的成交价。

    返回 (价格列表, 是否回溯到 t0, 是否有成功响应)。
    第三项很重要：拉取失败（404/限流/集合被下架）不能当成「没人买」，
    否则回测会出现假阴性。失败时调用方应保留待结算状态，下次重试。
    """
    out: list[float] = []
    cursor = ""
    reached = False
    ok = False
    for _ in range(pages):
        path = f"/events/collection/{slug}?event_type=sale&limit=50"
        if cursor:
            path += f"&next={cursor}"
        code, d = os_get(path, key)
        if code == 429:
            time.sleep(8)
            continue
        if code != 200 or not isinstance(d, dict):
            break
        ok = True
        oldest = None
        for e in d.get("asset_events") or []:
            pay = e.get("payment") or {}
            q, ts = pay.get("quantity"), e.get("event_timestamp")
            if not q or not ts:
                continue
            t = int(ts)
            oldest = t if oldest is None else min(oldest, t)
            if not (t0_epoch <= t <= t1_epoch):
                continue
            sym = (pay.get("symbol") or "").upper()
            dec = int(pay.get("decimals") or 18)
            try:
                raw = int(q) / (10 ** dec)
            except (TypeError, ValueError):
                continue
            if sym in ("ETH", "WETH") and dec == 18:
                out.append(raw)
            elif sym in STABLE:
                out.append(raw / eth_usd)
        if oldest is not None and oldest < t0_epoch:
            reached = True
            break
        cursor = d.get("next") or ""
        if not cursor:
            reached = True  # 没有更多历史了
            break
    return out, reached, ok


def cmd_settle(args) -> int:
    signals = read_jsonl(SIGNALS)
    fills = read_jsonl(FILLS)
    done = {(f.get("slug"), f.get("ts_epoch"), f.get("window_h")) for f in fills}
    now = int(time.time())
    pending = [
        s for s in signals
        if (s.get("slug"), s.get("ts_epoch"), args.window) not in done
        and now - int(s.get("ts_epoch") or 0) >= args.window * 3600 + 600
        and (s.get("mint_eth") or 0) > 0
    ]
    pending.sort(key=lambda s: s["ts_epoch"])
    if args.limit:
        pending = pending[: args.limit]
    log(f"待结算 {len(pending)} 条（窗口 {args.window}h）")
    if not pending:
        return 0

    key = ensure_key(verbose=False)
    rows = []
    for i, s in enumerate(pending, 1):
        t0 = int(s["ts_epoch"])
        prices, reached, ok = sale_prices_between(
            s["slug"], key, t0, t0 + args.window * 3600, args.eth_usd
        )
        if not ok:
            # 拉取失败不等于没人买：保留待结算状态，下次重试
            log(f"  {s['slug']}: 拉取失败，跳过（下次重试）")
            continue
        med = statistics.median(prices) if prices else None
        cost = float(s["mint_eth"]) + GAS_ETH
        roi = ((med * FEE - cost) / cost) if med else None
        rows.append({
            "slug": s["slug"], "name": s.get("name"), "chain": s.get("chain"),
            "ts_epoch": t0, "window_h": args.window,
            "verdict": s.get("verdict"), "score": s.get("score"), "ratio": s.get("ratio"),
            "mint_eth": s["mint_eth"], "floor_eth": s.get("floor_eth"),
            "n_sales": len(prices), "median_sale": med,
            "roi": roi,
            "reached_window_start": reached,
            "settled_at": datetime.now(CST).isoformat(),
        })
        if i % 10 == 0:
            log(f"  {i}/{len(pending)}")
        time.sleep(args.min_interval)
    append_jsonl(FILLS, rows)
    log(f"写入 {len(rows)} 条结算记录")
    return 0


# ---------------------------------------------------------------- report


def cmd_report(args) -> int:
    fills = read_jsonl(FILLS)
    if not fills:
        log("还没有结算数据，先跑 settle")
        return 1
    rows = [f for f in fills if f.get("roi") is not None]
    lines = ["# 信号 → 成交 回测", ""]
    lines.append(f"> 样本：{len(fills)} 条信号结算，其中 {len(rows)} 条有成交可算 ROI"
                 f"（窗口 {args.window}h，按真实成交中位价卖出，扣 1% 平台费与固定 gas）")
    lines.append("")
    lines.append("## 按判定分档")
    lines.append("")
    lines.append("| 判定 | 样本 | 有成交 | 胜率 | 中位 ROI | 平均 ROI |")
    lines.append("|---|---|---|---|---|---|")
    for verdict in ("值得打", "观察", "跳过", "无地板·待看"):
        group = [r for r in rows if r.get("verdict") == verdict]
        if not group:
            continue
        win = sum(1 for r in group if r["roi"] > 0)
        lines.append(
            f"| {verdict} | {sum(1 for f in fills if f.get('verdict')==verdict)} | {len(group)} "
            f"| {win}/{len(group)} = {win/len(group)*100:.0f}% "
            f"| {statistics.median(r['roi'] for r in group)*100:+.0f}% "
            f"| {statistics.mean(r['roi'] for r in group)*100:+.0f}% |"
        )
    lines.append("")
    lines.append("## 明细")
    lines.append("")
    lines.append("| 项目 | 判定 | 赔率(地板) | mint | 中位成交 | ROI | 成交数 | 信号时间 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda x: x["roi"], reverse=True)[: args.top]:
        ts = datetime.fromtimestamp(r["ts_epoch"], CST)
        lines.append(
            f"| {str(r.get('name'))[:24]} | {r.get('verdict')} | {_fmt(r.get('ratio'))} "
            f"| {r['mint_eth']:g} | {r['median_sale']:g} | {r['roi']*100:+.0f}% "
            f"| {r['n_sales']} | {ts:%m-%d %H:%M} |"
        )
    md = "\n".join(lines) + "\n"
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"paper-{datetime.now(CST):%Y%m%d-%H%M%S}.md"
    path.write_text(md, encoding="utf-8")
    log(f"报告写入 {path}")
    print(md)
    return 0


def _fmt(v) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return "∞" if f == float("inf") else f"{f:.2f}x"


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_rec = sub.add_parser("record", help="记录本轮信号（零 API 成本）")
    p_rec.set_defaults(func=cmd_record)

    p_set = sub.add_parser("settle", help="结算 N 小时前的信号")
    p_set.add_argument("--window", type=int, default=24, help="结算窗口（小时）")
    p_set.add_argument("--limit", type=int, default=0)
    p_set.add_argument("--eth-usd", type=float, default=2000.0)
    p_set.add_argument("--min-interval", type=float, default=0.8)
    p_set.set_defaults(func=cmd_settle)

    p_rep = sub.add_parser("report", help="输出统计")
    p_rep.add_argument("--window", type=int, default=24)
    p_rep.add_argument("--top", type=int, default=40)
    p_rep.set_defaults(func=cmd_report)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
