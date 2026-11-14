"""打新提醒：带「最优买单 / 最优卖单」+ mint 结束时间，落盘并推送。

为什么需要它
------------
光看地板价（最低卖单）会误判 —— 地板是卖方一厢情愿的报价。
真正决定「能不能卖得掉」的是**有没有人挂买单**，以及买卖价差有多大。
所以这条提醒强制带上：

    · 最优卖单（best ask，即地板）
    · 最优买单（best bid，集合级报价里最高的那条）
    · 真实成交中位价（最近 50 笔，折算成 ETH 等值）
    · mint 结束时间 + 剩余小时数

触发条件（满足任一）
--------------------
1. 报告判定为「值得打 / 观察」
2. 赔率（地板/mint）≥ alert.min_ratio 且 1h 成交笔数 ≥ alert.min_sales_1h

同类提醒有冷却（默认 6 小时），避免刷屏。

用法
----
    python -X utf8 scripts/alert_mint.py            # 正常跑（会推送）
    python -X utf8 scripts/alert_mint.py --dry-run  # 只打印不推送
    python -X utf8 scripts/alert_mint.py --force-slug mihaoo   # 强制对一个项目出一版（测试）
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from auto_list import os_get  # noqa: E402
from auto_mint import recent_sale_prices  # noqa: E402
from opensea_key import ensure as ensure_key  # noqa: E402

RAW = ROOT / "data" / "latest_raw.json"
SCORED = ROOT / "data" / "latest_scored.json"
STATE = ROOT / "data" / "alert_state.json"
LOG_FILE = ROOT / "data" / "mint_alerts.jsonl"
CST = timezone(timedelta(hours=8))


def log(msg: str) -> None:
    print(f"[{datetime.now(CST):%H:%M:%S}] {msg}", flush=True)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def best_ask(slug: str, key: str) -> float | None:
    code, d = os_get(f"/listings/collection/{slug}/best?limit=1", key)
    if code != 200 or not isinstance(d, dict):
        return None
    for it in d.get("listings") or []:
        p = (it.get("price") or {}).get("current") or {}
        v = p.get("value")
        if v:
            try:
                return int(v) / (10 ** int(p.get("decimals") or 18))
            except (TypeError, ValueError):
                continue
    return None


def best_bid(slug: str, key: str) -> float | None:
    """集合级最优买单。没有买单返回 None —— 这本身就是最重要的信号。"""
    code, d = os_get(f"/offers/collection/{slug}?limit=50", key)
    if code != 200 or not isinstance(d, dict):
        return None
    best = None
    for it in d.get("offers") or []:
        p = (it.get("price") or {}).get("current") or {}
        v = p.get("value")
        if not v:
            continue
        try:
            price = int(v) / (10 ** int(p.get("decimals") or 18))
        except (TypeError, ValueError):
            continue
        if price > 0 and (best is None or price > best):
            best = price
    return best


def fmt_eth(v: float | None) -> str:
    if v is None:
        return "无"
    if v == 0:
        return "FREE"
    if v < 1e-4:
        return f"{v:.8f}".rstrip("0").rstrip(".")
    return f"{v:g}"


def end_info(end_iso: str | None) -> tuple[str, float | None]:
    """把 endAt 转成 (人话, 剩余小时)。endAt 可能是 ISO 或毫秒时间戳。"""
    if not end_iso:
        return "未知", None
    try:
        if str(end_iso).isdigit():
            t = datetime.fromtimestamp(int(end_iso) / (1000 if len(str(end_iso)) > 10 else 1), CST)
        else:
            t = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00")).astimezone(CST)
    except (ValueError, TypeError, OSError):
        return str(end_iso), None
    left = (t - datetime.now(CST)).total_seconds() / 3600
    return f"{t:%m-%d %H:%M}（{'还剩 %.1f 小时' % left if left > 0 else '已结束'}）", left


def build_radar_index() -> dict[str, dict]:
    raw = load_json(RAW, {})
    idx: dict[str, dict] = {}
    for chain, block in (raw.get("radar") or {}).items():
        for it in block.get("items") or []:
            addr = (it.get("contractAddress") or "").lower()
            if addr and addr not in idx:
                idx[addr] = it
    return idx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印不推送")
    ap.add_argument("--force-slug", help="强制对一个 slug 出一版（测试排版）")
    args = ap.parse_args()

    import config_loader
    from notify import send_text

    cfg = config_loader.load(ROOT / "config.yaml")
    alert_cfg = cfg.get("alert") or {}
    if not alert_cfg.get("enabled", True) and not args.force_slug:
        log("alert.enabled = false，跳过")
        return 0
    min_ratio = float(alert_cfg.get("min_ratio", 2.0))
    min_sales = int(alert_cfg.get("min_sales_1h", 5))
    cooldown = int(alert_cfg.get("cooldown_minutes", 360))
    ending_soon_h = float(alert_cfg.get("ending_soon_minutes", 60)) / 60.0

    scored = load_json(SCORED, [])
    radar = build_radar_index()
    if not scored:
        log("没有 latest_scored.json，先跑一轮 run_once.py")
        return 1

    key = ensure_key(verbose=False)
    state = load_json(STATE, {})
    now = datetime.now(CST)
    sent = 0

    # ---- 1. 候选提醒 ----
    for s in scored:
        slug = _slug_of(s)
        if not slug:
            continue
        if args.force_slug and slug != args.force_slug:
            continue
        verdict = s.get("verdict")
        ratio = s.get("ratio")
        sales_1h = s.get("sales_1h") or 0
        try:
            ratio_f = float(ratio) if ratio not in (None, "inf") else None
        except (TypeError, ValueError):
            ratio_f = None

        qualifies = verdict in ("值得打", "观察") or (
            ratio_f is not None and ratio_f >= min_ratio and sales_1h >= min_sales
        )
        if not qualifies and not args.force_slug:
            continue

        last = state.get(slug, {}).get("last_sent_at")
        if last and not args.force_slug:
            try:
                age = (now - datetime.fromisoformat(last)).total_seconds() / 60
                if age < cooldown:
                    continue
            except ValueError:
                pass

        item = radar.get((s.get("contract") or "").lower()) or {}
        ask = best_ask(slug, key)
        bid = best_bid(slug, key)
        med = _median_sale(slug, key, cfg)
        end_txt, hours_left = end_info(item.get("endAt"))

        spread = None
        if ask and bid:
            spread = (ask - bid) / ask * 100
        text = _render(
            name=s.get("name") or slug,
            chain=s.get("chain") or "",
            mint=s.get("mint_eth"),
            ask=ask,
            bid=bid,
            med=med,
            spread=spread,
            ratio=ratio_f,
            score=s.get("score"),
            verdict=verdict,
            sales_1h=sales_1h,
            end_txt=end_txt,
            hours_left=hours_left,
            opensea=s.get("opensea") or item.get("openseaUrl"),
        )
        log(f"提醒：{s.get('name')}（ask={fmt_eth(ask)} bid={fmt_eth(bid)} 剩余={hours_left}）")
        if args.dry_run or args.force_slug:
            print("\n" + text + "\n")
        else:
            _push(cfg, text, send_text)
        sent += 1
        if args.dry_run or args.force_slug:
            continue  # 预览模式不落库、不占冷却，避免影响真实提醒
        _record({
            "at": now.isoformat(), "type": "candidate", "slug": slug,
            "name": s.get("name"), "chain": s.get("chain"),
            "mint_eth": s.get("mint_eth"), "best_ask": ask, "best_bid": bid,
            "sale_median": med, "ratio": ratio_f, "score": s.get("score"),
            "verdict": verdict, "sales_1h": sales_1h,
            "mint_end": item.get("endAt"), "hours_left": hours_left,
            "opensea": s.get("opensea") or item.get("openseaUrl"),
            "text": text,  # 存下原文，便于核对推送内容（含 emoji）
        })
        state.setdefault(slug, {})["last_sent_at"] = now.isoformat()
        state[slug]["last_type"] = "candidate"

    # ---- 2. 即将结束提醒 ----
    for slug, st in list(state.items()):
        if st.get("ending_notified"):
            continue
        rec = _last_record_for(slug)
        if not rec:
            continue
        _, hours_left = end_info(rec.get("mint_end"))
        if hours_left is None or not (0 < hours_left <= ending_soon_h):
            continue
        text = (
            f"⏰ 打新即将结束 · {rec.get('name')}\n"
            f"• 链：{rec.get('chain')}\n"
            f"• mint 价：{fmt_eth(rec.get('mint_eth'))} ETH\n"
            f"• 当前最优卖单：{fmt_eth(rec.get('best_ask'))}\n"
            f"• 当前最优买单：{fmt_eth(rec.get('best_bid'))}\n"
            f"• {end_info(rec.get('mint_end'))[0]}\n"
            f"• OpenSea：{rec.get('opensea') or ''}"
        )
        log(f"即将结束：{rec.get('name')}（剩 {hours_left:.1f}h）")
        if args.dry_run:
            print("\n" + text + "\n")
        else:
            _push(cfg, text, send_text)
        st["ending_notified"] = now.isoformat()
        _record({"at": now.isoformat(), "type": "ending_soon", "slug": slug,
                 "name": rec.get("name"), "hours_left": hours_left, "text": text})
        sent += 1

    save_json(STATE, state)
    log(f"完成：发出 {sent} 条提醒")
    return 0


def _median_sale(slug: str, key: str, cfg: dict) -> float | None:
    filters = cfg.get("filters") or {}
    eth_usd = float(filters.get("eth_usd_assume", 2000) or 2000)
    prices = recent_sale_prices(slug, key, int(filters.get("sale_sample", 50)), eth_usd)
    return statistics.median(prices) if prices else None


def _render(**k) -> str:
    spread = k["spread"]
    if k["ask"] is None:
        ask_txt = "无（没有任何卖单）"
    else:
        ask_txt = f"{fmt_eth(k['ask'])} ETH"
    if k["bid"] is None:
        bid_txt = "无（没有任何买单）"
    else:
        bid_txt = f"{fmt_eth(k['bid'])} ETH"

    # 赔率优先用报告里的（地板口径）；报告没算出来就用刚查到的卖单现算
    ratio = k["ratio"]
    if ratio is None and k["ask"] and k["mint"]:
        try:
            ratio = float(k["ask"]) / float(k["mint"])
        except (TypeError, ValueError, ZeroDivisionError):
            ratio = None
    if ratio == float("inf"):
        ratio_txt = "∞"
    elif ratio is None:
        ratio_txt = "—"
    else:
        ratio_txt = f"{ratio:.2f}x"

    spread_txt = f"{spread:.1f}%" if spread is not None else "无法计算（缺一侧报价）"
    return (
        f"🎯 打新提醒 · {k['name']}\n"
        f"• 链：{k['chain']}\n"
        f"• mint 价：{fmt_eth(k['mint'])} ETH\n"
        f"• 最优卖单：{ask_txt}（地板）\n"
        f"• 最优买单：{bid_txt}\n"
        f"• 买卖价差：{spread_txt}\n"
        f"• 真实成交中位：{fmt_eth(k['med'])} ETH\n"
        f"• 赔率（地板/mint）：{ratio_txt}\n"
        f"• 1h 成交：{k['sales_1h']} 笔 · 评分 {k['score']} · 判定 {k['verdict']}\n"
        f"• mint 结束：{k['end_txt']}\n"
        f"• OpenSea：{k['opensea'] or ''}"
    )


def _push(cfg: dict, text: str, send_text) -> None:
    webhook = ((cfg.get("notify") or {}).get("webhook") or "").strip()
    if not webhook:
        log("⚠️ 没配 notify.webhook，只落盘不推送")
        return
    try:
        resp = send_text(webhook, text)
        if (resp or {}).get("errcode") not in (0, None):
            log(f"推送返回异常：{resp}")
    except Exception as e:  # noqa: BLE001
        log(f"推送失败：{e}")


def _record(rec: dict) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _last_record_for(slug: str) -> dict | None:
    if not LOG_FILE.exists():
        return None
    last = None
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("slug") == slug:
            last = r
    return last


def _slug_of(s: dict) -> str | None:
    url = s.get("opensea") or ""
    marker = "/collection/"
    if marker in url:
        return url.split(marker, 1)[1].split("?", 1)[0].split("#", 1)[0].strip("/") or None
    return s.get("slug")


if __name__ == "__main__":
    raise SystemExit(main())
