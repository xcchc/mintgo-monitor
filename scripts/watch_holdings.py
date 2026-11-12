"""持仓监控 —— 盯地板价，到目标倍数就提醒（不自动卖）。

为什么单独做：
  卖出链路（转出 + 授权 + 签单）只在签名环节验证过，没真跑通过。
  所以先做「只提醒不执行」：地板价涨到目标倍数时通知用户，
  由人来决定要不要卖，或者让我手动跑一次卖出流程验证。

监控逻辑：
  · 每个打中的 NFT 记录在 data/holdings.json
  · 每轮查 OpenSea 实时地板价
  · 涨到 alert_multiples 里任一倍数（3x / 5x / 10x）→ 各提醒一次
  · 跌破成本 → 提醒一次（alert_on_loss）
  · 已提醒过的倍数存在 alerts 字段里，不会重复刷屏

用法：
  python -X utf8 scripts/watch_holdings.py          # 检查并推送
  python -X utf8 scripts/watch_holdings.py --show   # 只看不推
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from auto_mint import fresh_stats, load_json, push_wecom, save_json  # noqa: E402
from opensea_key import ensure as ensure_key  # noqa: E402

CST = timezone(timedelta(hours=8))
HOLDINGS = ROOT / "data" / "holdings.json"


def log(msg: str) -> None:
    print(f"[{datetime.now(CST):%H:%M:%S}] {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="只看不发")
    args = ap.parse_args()

    import config_loader

    cfg = config_loader.load(ROOT / "config.yaml")
    w = cfg.get("holdings_watch") or {}
    if not w.get("enabled", True):
        log("holdings_watch 已关闭")
        return 0

    raw_marks = w.get("alert_multiples") or [3, 5, 10]
    # 容错：config_loader 对行内列表 [3, 5, 10] 会原样返回字符串
    if isinstance(raw_marks, str):
        raw_marks = raw_marks.strip().strip("[]").split(",")
    marks = sorted(float(str(x).strip()) for x in raw_marks if str(x).strip())
    alert_loss = bool(w.get("alert_on_loss", True))

    holdings = load_json(HOLDINGS, {})
    if not holdings:
        log("没有持仓，跳过")
        return 0

    key = ensure_key(verbose=False)
    lines: list[str] = []
    changed = False

    for hkey, h in holdings.items():
        slug = h.get("slug") or ""
        cost = float(h.get("cost_eth") or 0) or 1e-12
        name = h.get("name") or slug
        tid = h.get("tokenId") or "?"

        st = fresh_stats(slug, key) if slug else None
        if not st:
            log(f"  {name} #{tid}: 拿不到行情")
            continue

        floor = st.get("floor") or 0.0
        mult = floor / cost if cost else 0
        log(f"  {name} #{tid}: 地板 {floor:g} / 成本 {cost:g} = {mult:.2f}x")

        alerts = h.setdefault("alerts", {})

        # 涨到目标倍数
        for m in marks:
            tag = f"{m:g}x"
            if mult >= m and tag not in alerts:
                alerts[tag] = datetime.now(CST).isoformat()
                changed = True
                profit = (floor - cost) * 1000  # 粗略按 0.001 ETH 展示
                lines.append(
                    f"📈 {name} #{tid} 涨到 {mult:.1f}x\n"
                    f"• 地板 {floor:g} ETH（成本 {cost:g}）\n"
                    f"• 触发 {tag} 提醒\n"
                    f"• OpenSea: https://opensea.io/assets/{h.get('chain','robinhood')}/"
                    f"{h.get('contract','')}/{tid}"
                )

        # 跌破成本
        if alert_loss and floor < cost and "loss" not in alerts:
            alerts["loss"] = datetime.now(CST).isoformat()
            changed = True
            lines.append(
                f"📉 {name} #{tid} 跌破成本\n"
                f"• 地板 {floor:g} ETH < 成本 {cost:g} ETH（{mult:.2f}x）\n"
                f"• 要不要止损？"
            )

    if not lines:
        log("没有触发提醒")
        return 0

    text = "🏦 持仓提醒 · " + datetime.now(CST).strftime("%m-%d %H:%M") + "\n\n" + "\n\n".join(lines)
    print()
    print(text)
    print()

    if args.show:
        log("--show 模式：不推送，也不记录提醒状态（下次还会提醒）")
        return 0

    # 只有推送成功才落盘 —— 失败的话下次会重试，不会漏提醒。
    # 早先版本在 --show 时也保存了 alerts，导致真的该推时反而不推了。
    if push_wecom(text, cfg):
        if changed:
            save_json(HOLDINGS, holdings)
        log("📤 已推送企业微信")
    else:
        log("⚠️ 推送失败，状态未记录，下次重试")
    return 0


if __name__ == "__main__":
    sys.exit(main())
