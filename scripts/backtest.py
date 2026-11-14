"""回测：用历史快照 + OpenSea 真实成交，检验「打新然后卖掉」到底赚不赚钱。

思路
----
每轮抓取都会把当时的 mint 价、阶段、地板写进 data/snapshots/*.json。
本脚本取每个 collection 的**首次出现时间 T0**，再去 OpenSea 拉它之后的真实成交
（/events/collection/{slug}?event_type=sale），据此回答一个问题：

    假设在 T0 那一刻按 mint 价打 1 个，之后按「后续成交中位价」卖出，赚还是亏？

注意（这会写在报告里）
----
1. 只覆盖 2026-11-10 ~ 11-12 两天，样本期极短，结论只能当参考。
2. 「后续成交价」是市场里别人成交的价格，不等于你一定卖得掉。
3. 不考虑 mint 失败、并发抢购、版税/平台费之外的成本。
4. 只统计 public_sale（白名单阶段程序打不了）。

用法
----
    python -X utf8 scripts/backtest.py                  # 全量回测
    python -X utf8 scripts/backtest.py --limit 30       # 只跑前 30 个（试水）
    python -X utf8 scripts/backtest.py --min-interval 0.3
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

SNAP = ROOT / "data" / "snapshots"
OUT = ROOT / "data" / "reports"
CST = timezone(timedelta(hours=8))
GAS_ETH = 0.000004  # 一笔 mint + 一笔转账的粗略 gas
FEE = 0.99          # OpenSea 平台费 1%
STABLE = {"USDG", "USDC", "USDT", "DAI", "FDUSD"}
ETH_USD = 2000.0    # 把稳定币成交折算成 ETH 等值的固定汇率（两边同乘，ROI 比例不受影响）


def log(msg: str) -> None:
    print(f"[{datetime.now(CST):%H:%M:%S}] {msg}", flush=True)


def load_snapshots() -> list[tuple[datetime, dict]]:
    out = []
    for f in sorted(SNAP.glob("*.json")):
        try:
            t = datetime.strptime(f.stem, "%Y%m%d-%H%M%S").replace(tzinfo=CST)
        except ValueError:
            continue
        out.append((t, json.loads(f.read_text(encoding="utf-8"))))
    return out


def first_sightings(snaps) -> dict[str, dict]:
    """每个 collection 第一次出现时的状态。"""
    seen: dict[str, dict] = {}
    for ts, data in snaps:
        for chain, block in (data.get("radar") or {}).items():
            for it in block.get("items") or []:
                slug = it.get("openSeaSlug")
                if not slug or slug in seen:
                    continue
                try:
                    price = int(it.get("priceWei") or 0) / 1e18
                except (TypeError, ValueError):
                    price = 0.0
                seen[slug] = {
                    "slug": slug,
                    "name": it.get("displayName") or it.get("name") or slug,
                    "chain": chain,
                    "contract": (it.get("contractAddress") or "").lower(),
                    "stage": it.get("firstStageType") or "",
                    "mint_eth": price,
                    "t0": ts,
                    "soldOut": bool(it.get("soldOut")),
                }
    return seen


def os_get_retry(path: str, key: str, tries: int = 5):
    """带退避的 GET：OpenSea 读额度 600/h，短时间密集请求会返回 429。"""
    code, data = 0, None
    for i in range(tries):
        code, data = os_get(path, key)
        if code == 429:
            wait = 6 * (i + 1)
            log(f"  429 限流，等 {wait}s 重试（{path[:60]}）")
            time.sleep(wait)
            continue
        return code, data
    return code, data


def sales_after(slug: str, key: str, t0: datetime, pages: int = 2) -> tuple[list[tuple[datetime, float]], dict, dict]:
    """拉取该 collection 的成交事件，只保留 T0 之后的成交，统一折算成 ETH 等值。

    坑：Robinhood 链上大量成交是用 **USDG（6 位小数的稳定币）** 结算的，
    直接拿 payment.quantity 当 ETH 会得出 0.09「ETH」这种荒谬价格。
    这里把稳定币按固定汇率折成 ETH 等值，其他币种单独计数并排除。
    """
    out: list[tuple[datetime, float]] = []
    skipped: dict[str, int] = {}
    converted: dict[str, int] = {}
    cursor = ""
    for _ in range(pages):
        path = f"/events/collection/{slug}?event_type=sale&limit=50"
        if cursor:
            path += f"&next={cursor}"
        code, d = os_get_retry(path, key)
        if code != 200 or not isinstance(d, dict):
            break
        for e in d.get("asset_events") or []:
            pay = e.get("payment") or {}
            q = pay.get("quantity")
            ts = e.get("event_timestamp")
            if not q or not ts:
                continue
            try:
                t = datetime.fromtimestamp(int(ts), CST)
                sym = (pay.get("symbol") or "").upper()
                dec = int(pay.get("decimals") or 18)
                price = int(q) / (10 ** dec)
            except (TypeError, ValueError):
                continue
            if t < t0:
                continue
            if sym in ("ETH", "WETH") and dec == 18:
                out.append((t, price))
            elif sym in STABLE:
                out.append((t, price / ETH_USD))
                converted[sym] = converted.get(sym, 0) + 1
            else:
                skipped[sym or "?"] = skipped.get(sym or "?", 0) + 1
        cursor = d.get("next") or ""
        if not cursor:
            break
    return out, skipped, converted


def historical_verdicts(snaps, cfg) -> dict[str, dict]:
    """用当时的快照重放一遍打分，取出被判为「值得打 / 观察」的项目。

    这比「全部项目」更贴近实际：我们的筛选每轮只放行极少数项目，
    真正要检验的是**这些被挑出来的项目后来赚钱了吗**。
    """
    from score import score_all  # noqa: PLC0415
    from auto_mint import slug_from_scored  # noqa: PLC0415

    out: dict[str, dict] = {}
    for ts, data in snaps:
        try:
            scored = score_all(data, cfg)
        except Exception:  # noqa: BLE001
            continue
        for s in scored:
            if s.verdict not in ("值得打", "观察"):
                continue
            slug = slug_from_scored(s.as_dict())
            if not slug or slug in out:
                continue
            out[slug] = {
                "slug": slug,
                "name": s.name,
                "chain": s.chain,
                "contract": s.contract.lower(),
                "stage": "public_sale",
                "verdict": s.verdict,
                "score": s.score,
                "mint_eth": float(s.mint_eth or 0),
                "floor_eth": float(s.floor_eth or 0),
                "t0": ts,
            }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个项目")
    ap.add_argument("--mode", choices=("verdicts", "all"), default="verdicts",
                    help="verdicts=只回测当时被判为值得打/观察的项目（默认）；all=全部付费项目")
    ap.add_argument("--min-interval", type=float, default=0.8, help="两次请求最小间隔（秒）")
    args = ap.parse_args()

    snaps = load_snapshots()
    if not snaps:
        log("没有快照，先跑几轮 src\\run_once.py")
        return 1
    log(f"快照 {len(snaps)} 份：{snaps[0][0]:%m-%d %H:%M} → {snaps[-1][0]:%m-%d %H:%M}")

    if args.mode == "verdicts":
        import config_loader  # noqa: PLC0415
        cfg = config_loader.load(ROOT / "config.yaml")
        universe = list(historical_verdicts(snaps, cfg).values())
        universe.sort(key=lambda v: v["t0"])
    else:
        universe = [v for v in first_sightings(snaps).values()
                    if v["stage"] == "public_sale" and v["mint_eth"] > 0]
        universe.sort(key=lambda v: v["t0"])
    if args.limit:
        universe = universe[: args.limit]
    log(f"{args.mode} 模式：{len(universe)} 个项目，开始拉真实成交 …")

    key = ensure_key(verbose=False)
    rows: list[dict] = []
    for i, u in enumerate(universe, 1):
        skipped: dict = {}
        converted: dict = {}
        try:
            sales, skipped, converted = sales_after(u["slug"], key, u["t0"])
        except Exception as e:  # noqa: BLE001
            log(f"  {u['slug']}: 拉取失败 {e}")
            sales = []
        prices = sorted(p for _, p in sales)
        row = dict(u)
        row["n_sales"] = len(prices)
        row["skipped_ccy"] = skipped
        row["converted_ccy"] = converted
        row["median_sale"] = statistics.median(prices) if prices else None
        row["max_sale"] = max(prices) if prices else None
        if prices and u["mint_eth"] > 0:
            cost = u["mint_eth"] + GAS_ETH
            row["roi_median"] = (row["median_sale"] * FEE - cost) / cost
            row["roi_max"] = (row["max_sale"] * FEE - cost) / cost
        else:
            row["roi_median"] = None
            row["roi_max"] = None
        rows.append(row)
        if i % 20 == 0:
            log(f"  {i}/{len(universe)}")
        time.sleep(args.min_interval)

    scored = [r for r in rows if r["roi_median"] is not None]
    log(f"有后续成交且有 mint 价的项目：{len(scored)} / {len(rows)}")

    md = render(rows, scored, snaps)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"backtest-{datetime.now(CST):%Y%m%d-%H%M%S}.md"
    path.write_text(md, encoding="utf-8")
    log(f"报告已写入 {path}")

    if scored:
        med = statistics.median(r["roi_median"] for r in scored)
        win = sum(1 for r in scored if r["roi_median"] > 0)
        log(f"中位 ROI {med*100:+.1f}% · 盈利占比 {win}/{len(scored)} = {win/len(scored)*100:.0f}%")
    return 0


def render(rows: list[dict], scored: list[dict], snaps) -> str:
    lines = ["# 打新回测报告", ""]
    lines.append(f"> 数据来源：{len(snaps)} 份历史快照（{snaps[0][0]:%Y-%m-%d %H:%M} → {snaps[-1][0]:%Y-%m-%d %H:%M}）"
                 f" + OpenSea 真实成交事件；只含 `public_sale` 项目。")
    lines.append("")
    lines.append("口径：每个 collection 取**首次出现**时的 mint 价 T0，统计 T0 之后的真实成交；"
                 "假设按 mint 价打 1 个、按后续成交中位价卖出，扣 1% 平台费与固定 gas。")
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 项目数（public_sale） | {len(rows)} |")
    lines.append(f"| 有后续成交记录 | {sum(1 for r in rows if r['n_sales'])} |")
    lines.append(f"| 可算 ROI（有成交 + 有价格） | {len(scored)} |")
    if scored:
        med = statistics.median(r["roi_median"] for r in scored)
        win = sum(1 for r in scored if r["roi_median"] > 0)
        lines.append(f"| **中位 ROI** | **{med*100:+.1f}%** |")
        lines.append(f"| 盈利项目占比 | {win}/{len(scored)} = {win/len(scored)*100:.0f}% |")
        lines.append(f"| 平均 ROI | {statistics.mean(r['roi_median'] for r in scored)*100:+.1f}% |")
    skipped_total: dict[str, int] = {}
    converted_total: dict[str, int] = {}
    for r in rows:
        for k, v in (r.get("skipped_ccy") or {}).items():
            skipped_total[k] = skipped_total.get(k, 0) + v
        for k, v in (r.get("converted_ccy") or {}).items():
            converted_total[k] = converted_total.get(k, 0) + v
    if converted_total:
        lines.append(f"| 稳定币成交（按 {ETH_USD:g} USD/ETH 折算） | {converted_total} |")
    if skipped_total:
        lines.append(f"| 其他币种成交（已排除） | {skipped_total} |")
    lines.append("")

    lines.append("## 明细（按中位 ROI 排序）")
    lines.append("")
    has_verdict = any("verdict" in r for r in rows)
    if has_verdict:
        lines.append("| 项目 | 链 | 当时的判定 | 分 | mint(ETH) | T0 | 后续成交数 | 中位成交 | 中位ROI |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
    else:
        lines.append("| 项目 | 链 | mint(ETH) | T0 | 后续成交数 | 中位成交 | 最高成交 | 中位ROI |")
        lines.append("|---|---|---|---|---|---|---|---|")
    for r in sorted(scored, key=lambda x: x["roi_median"], reverse=True):
        if has_verdict:
            lines.append(
                f"| {r['name'][:26]} | {r['chain']} | {r.get('verdict','')} | {r.get('score',0):g} "
                f"| {r['mint_eth']:g} | {r['t0']:%m-%d %H:%M} | {r['n_sales']} "
                f"| {r['median_sale']:g} | {r['roi_median']*100:+.0f}% |"
            )
        else:
            lines.append(
                f"| {r['name'][:26]} | {r['chain']} | {r['mint_eth']:g} | {r['t0']:%m-%d %H:%M} "
                f"| {r['n_sales']} | {r['median_sale']:g} | {r['max_sale']:g} | {r['roi_median']*100:+.0f}% |"
            )
    no_sales = [r for r in rows if not r["n_sales"]]
    if no_sales:
        lines.append("")
        lines.append(f"## 没有任何后续成交的项目（{len(no_sales)} 个，打进去等于砸手里）")
        lines.append("")
        lines.append("| 项目 | 链 | mint(ETH) | T0 |")
        lines.append("|---|---|---|---|")
        for r in no_sales[:40]:
            lines.append(f"| {r['name'][:28]} | {r['chain']} | {r['mint_eth']:g} | {r['t0']:%m-%d %H:%M} |")

    lines.append("")
    lines.append("## 这份回测的局限")
    lines.append("")
    lines.append("- 样本期只有两天，覆盖的是那两天在售的项目，不能代表长期")
    lines.append("- 「后续成交中位价」是别人成交的价格，不代表你一定卖得掉（尤其流动性差的盘子）")
    lines.append("- 不含 mint 失败、抢购并发、版税、跨链成本")
    lines.append("- 免费 mint（mint 价 0）算不出 ROI，未纳入")
    lines.append(f"- **稳定币成交按固定 {ETH_USD:g} USD/ETH 折算**：Robinhood 链上大量成交用 USDG（6 位小数）结算，"
                 "直接当 ETH 会得出 0.09「ETH」这种荒谬价格。折算率是常数，ROI 比例不受影响，绝对值需按此理解")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
