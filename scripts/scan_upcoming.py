"""把未开抢项目（在仓位上限内的）逐个解析 slug → 查实时地板 → 算赔率。"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from auto_mint import fresh_stats, os_get  # noqa: E402
from opensea_key import ensure  # noqa: E402

CST = timezone(timedelta(hours=8))
CAP = 0.0005
CHAIN_OK = {"robinhood", "ethereum", "base", "bsc", "polygon", "arbitrum"}


def slug_of(chain: str, addr: str, key: str) -> str | None:
    code, d = os_get(f"/chain/{chain}/contract/{addr}", key, timeout=20)
    if code == 200 and isinstance(d, dict):
        return d.get("collection")
    return None


def main() -> None:
    now = datetime.now(timezone.utc)
    raw = json.loads((ROOT / "data" / "latest_raw.json").read_text(encoding="utf-8"))
    key = ensure(verbose=False)

    rows = []
    for ch, blk in (raw.get("radar") or {}).items():
        if ch not in CHAIN_OK:
            continue
        for it in blk.get("items") or []:
            start = it.get("startAt") or 0
            if not start:
                continue
            t = datetime.fromtimestamp(start / 1000, timezone.utc)
            if t < now:
                continue
            price = float(it.get("priceEth") or 0)
            if price > CAP:
                continue
            addr = (it.get("contractAddress") or "").lower()
            rows.append((t, ch, it.get("displayName") or "?", price, addr))

    rows.sort()
    print(f"未开抢 + 价格在 {CAP:g} ETH 以内的项目：{len(rows)} 个\n")
    print(f"{'开抢(北京)':<13}{'剩余':>6}  {'项目':<24}{'mint':>10}{'地板':>11}{'赔率':>8}{'成交':>7}  判定")
    print("-" * 96)

    good = []
    for t, ch, name, price, addr in rows[:20]:
        hours = (t - now).total_seconds() / 3600
        if hours > 12:
            continue
        slug = slug_of(ch, addr, key)
        st = fresh_stats(slug, key) if slug else None
        if not st:
            print(f"{t.astimezone(CST):%m-%d %H:%M}  {hours:5.1f}h  {name[:22]:<24}{price:>10.2g}{'—':>11}{'—':>8}{'—':>7}  无市场数据")
            continue
        floor = st["floor"]
        if price == 0 and floor:
            ratio_s, ratio_v = "∞", float("inf")
        elif floor and price:
            ratio_v = floor / price
            ratio_s = f"{ratio_v:.2f}x"
        else:
            ratio_s, ratio_v = "—", 0.0
        verdict = "✅ 值得等" if (ratio_v >= 1 and st["sales"] >= 5) else "❌"
        if verdict.startswith("✅"):
            good.append((t, name, price, floor, ratio_v))
        print(f"{t.astimezone(CST):%m-%d %H:%M}  {hours:5.1f}h  {name[:22]:<24}{price:>10.2g}"
              f"{(floor or 0):>11.2g}{ratio_s:>8}{st['sales']:>7}  {verdict}")

    print()
    if good:
        t, name, price, floor, r = good[0]
        print(f"👉 最近一个值得等的：{name}")
        print(f"   {t.astimezone(CST):%m-%d %H:%M}（{(t-now).total_seconds()/3600:.1f} 小时后）")
        print(f"   mint {price:g} → 地板 {floor:g}，赔率 {r:.2f}x" if price else f"   免费 → 地板 {floor:g}")
    else:
        print("👉 接下来 12 小时内没有值得等的项目")


if __name__ == "__main__":
    main()
