"""打新提醒：一条消息汇总本轮候选，带「最优买/卖价」与 mint 结束时间。

为什么需要它
------------
光看地板价（最低卖单）会误判 —— 地板是卖方一厢情愿的报价。
真正决定「能不能卖得掉」的是**有没有人挂买单**，以及买卖价差有多大。
所以每个项目固定带：

    · 最优卖单（best ask，即地板）
    · 最优买单（best bid，集合级报价里最高的那条）
    · 真实成交中位价（最近 50 笔，折算成 ETH 等值）
    · mint 结束时间 + 剩余小时数

触发条件（满足任一）
--------------------
1. 报告判定为「值得打 / 观察」
2. 赔率（地板/mint）≥ alert.min_ratio 且 1h 成交笔数 ≥ alert.min_sales_1h

同一项目有冷却（默认 6 小时），避免刷屏；mint 结束前再补一条「即将结束」。

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
MAX_BYTES = 2000  # 企业微信 text 上限 2048 字节，留点余量


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


def byte_len(s: str) -> int:
    return len(s.encode("utf-8"))


# ---------------------------------------------------------------- 市场数据


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


def sale_median(slug: str, key: str, cfg: dict) -> float | None:
    filters = cfg.get("filters") or {}
    eth_usd = float(filters.get("eth_usd_assume", 2000) or 2000)
    prices = recent_sale_prices(slug, key, int(filters.get("sale_sample", 50)), eth_usd)
    return statistics.median(prices) if prices else None


# ---------------------------------------------------------------- 格式化


def fmt_eth(v: float | None) -> str:
    if v is None:
        return "无"
    if v == 0:
        return "FREE"
    if v < 1e-4:
        return f"{v:.8f}".rstrip("0").rstrip(".")
    return f"{v:g}"


def parse_time(raw) -> datetime | None:
    if raw is None:
        return None
    try:
        if str(raw).isdigit():
            n = int(raw)
            return datetime.fromtimestamp(n / (1000 if n > 10**11 else 1), CST)
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(CST)
    except (ValueError, TypeError, OSError):
        return None


def fmt_end(raw) -> tuple[str, float | None]:
    t = parse_time(raw)
    if not t:
        return "未知", None
    left = (t - datetime.now(CST)).total_seconds() / 3600
    tail = f"还剩 {left:.1f} 小时" if left > 0 else "已结束"
    return f"{t:%m-%d %H:%M}（{tail}）", left


def fmt_start(raw) -> str:
    t = parse_time(raw)
    if not t:
        return "—"
    left = (t - datetime.now(CST)).total_seconds() / 60
    tail = "进行中" if left <= 0 else f"还有 {left/60:.1f} 小时"
    return f"{t:%m-%d %H:%M}（{tail}）"


CHAIN_LABEL = {
    "robinhood": "Robinhood",
    "ethereum": "Ethereum",
    "base": "Base",
    "ink": "Ink",
    "bsc": "BSC",
    "elysium": "Elysium",
}


# ---------------------------------------------------------------- 消息拼装


def render_entry(idx: int, e: dict) -> list[str]:
    verdict = e.get("verdict") or ""
    head = {"值得打": "🟢", "观察": "🟡"}.get(verdict, "⚡")
    ratio = e.get("ratio")
    if ratio == float("inf"):
        ratio_txt = "∞"
    elif ratio is None:
        ratio_txt = "—"
    else:
        ratio_txt = f"{ratio:.2f}x"

    ask, bid, med = e.get("ask"), e.get("bid"), e.get("med")
    ask_txt = "无（没有任何卖单）" if ask is None else f"{fmt_eth(ask)} ETH"
    bid_txt = "无（没有任何买单）" if bid is None else f"{fmt_eth(bid)} ETH"
    spread = None
    if ask and bid:
        spread = (ask - bid) / ask * 100
    spread_txt = f"{spread:.1f}%" if spread is not None else "—（缺一侧报价）"

    lines = [
        f"{idx}. {head} {e['name']}",
        f"   📍 链：{CHAIN_LABEL.get(e.get('chain'), e.get('chain') or '—')}",
        f"   💰 mint：{fmt_eth(e.get('mint'))} ETH ｜ 最优卖单 {ask_txt}",
        f"   🟩 最优买单：{bid_txt}",
        f"   📊 成交中位：{fmt_eth(med)} ETH ｜ 价差 {spread_txt} ｜ 1h {e.get('sales_1h') or 0} 笔",
        f"   ⏰ 开抢：{fmt_start(e.get('start_at'))}",
        f"   🏁 mint 结束：{e.get('end_txt')}",
        f"   🎯 赔率 {ratio_txt} ｜ 评分 {round(float(e.get('score') or 0), 1)} ｜ 判定 {verdict or '—'}",
    ]
    if e.get("reason"):
        lines.append(f"   💡 理由：{e['reason']}")
    if e.get("opensea"):
        lines.append(f"   🔗 {e['opensea']}")
    if e.get("explorer"):
        lines.append(f"   ⛓ {e['explorer']}")
    return lines


def build_message(entries: list[dict], stats: dict) -> tuple[str, int]:
    """拼成一条消息；超过字节上限就截断，返回 (文本, 实际纳入条数)。"""
    now = datetime.now(CST)
    header = [
        f"🚀 MintGo 打新提醒 · {now:%m-%d %H:%M}",
        f"🌐 扫描 {stats['scanned']} 个 ｜ {stats['chains_total']} 条链"
        f"（{stats['chains_have']} 条有货） ｜ 本轮提醒 {len(entries)} 个",
        "",
    ]
    body: list[str] = []
    used = 0
    included = 0
    for i, e in enumerate(entries, 1):
        block = render_entry(i, e)
        text = "\n".join(block)
        if used + byte_len(text) + 120 > MAX_BYTES and included > 0:
            break
        body.extend(block)
        body.append("")
        used += byte_len(text) + 1
        included += 1
    if included < len(entries):
        body.append(f"…另有 {len(entries) - included} 个，详见本地报告")
    return "\n".join(header + body).rstrip(), included


# ---------------------------------------------------------------- 主流程


def build_radar_index() -> dict[str, dict]:
    raw = load_json(RAW, {})
    idx: dict[str, dict] = {}
    for _chain, block in (raw.get("radar") or {}).items():
        for it in block.get("items") or []:
            addr = (it.get("contractAddress") or "").lower()
            if addr and addr not in idx:
                idx[addr] = it
    return idx


def scan_stats(raw: dict, scored: list[dict]) -> dict:
    radar = raw.get("radar") or {}
    return {
        "scanned": raw.get("_scanned") or sum(len(b.get("items") or []) for b in radar.values()),
        "chains_total": len(radar) or 0,
        "chains_have": sum(1 for b in radar.values() if (b.get("items") or [])),
        "scored": len(scored),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印不推送")
    ap.add_argument("--force-slug", help="强制对一个 slug 出一版（测试排版）")
    ap.add_argument("--push-test", action="store_true", help="配合 --force-slug：强制真发一条做排版验证")
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
    raw = load_json(RAW, {})
    radar = build_radar_index()
    if not scored:
        log("没有 latest_scored.json，先跑一轮 run_once.py")
        return 1

    key = ensure_key(verbose=False)
    state = load_json(STATE, {})
    now = datetime.now(CST)

    entries: list[dict] = []
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
            ratio_f = float(ratio) if ratio not in (None, "inf") else ratio
        except (TypeError, ValueError):
            ratio_f = None

        qualifies = verdict in ("值得打", "观察") or (
            isinstance(ratio_f, float) and ratio_f >= min_ratio and sales_1h >= min_sales
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
        end_txt, hours_left = fmt_end(item.get("endAt"))
        entries.append({
            "slug": slug,
            "name": s.get("name") or slug,
            "chain": s.get("chain"),
            "mint": s.get("mint_eth"),
            "ask": best_ask(slug, key),
            "bid": best_bid(slug, key),
            "med": sale_median(slug, key, cfg),
            "ratio": ratio_f,
            "score": s.get("score"),
            "verdict": verdict,
            "sales_1h": sales_1h,
            "start_at": item.get("startAt") or s.get("start_at"),
            "end_txt": end_txt,
            "hours_left": hours_left,
            "opensea": s.get("opensea") or item.get("openseaUrl"),
            "explorer": item.get("explorerUrl"),
            "reason": _clip("；".join(s.get("reasons") or []), 90),
        })

    if entries:
        text, included = build_message(entries, scan_stats(raw, scored))
        log(f"提醒 {len(entries)} 个（消息内 {included} 个）")
        if (args.dry_run or args.force_slug) and not args.push_test:
            print("\n" + text + "\n")
        else:
            _push(cfg, text, send_text)
        if not (args.dry_run or args.force_slug):
            for e in entries:
                _record({
                    "at": now.isoformat(), "type": "candidate", "slug": e["slug"],
                    "name": e["name"], "chain": e["chain"], "mint_eth": e["mint"],
                    "best_ask": e["ask"], "best_bid": e["bid"], "sale_median": e["med"],
                    "ratio": e["ratio"], "score": e["score"], "verdict": e["verdict"],
                    "sales_1h": e["sales_1h"], "hours_left": e["hours_left"],
                    "opensea": e["opensea"], "text": text,
                })
                state.setdefault(e["slug"], {})["last_sent_at"] = now.isoformat()
                state[e["slug"]]["last_type"] = "candidate"

    # ---- 即将结束 ----
    sent_end = 0
    for slug, st in list(state.items()):
        if st.get("ending_notified"):
            continue
        rec = _last_record_for(slug)
        if not rec:
            continue
        _, hours_left = fmt_end(rec.get("mint_end") or _hours_to_raw(rec.get("hours_left")))
        if hours_left is None or not (0 < hours_left <= ending_soon_h):
            continue
        text = (
            f"⏰ 打新即将结束 · {rec.get('name')}\n"
            f"📍 链：{CHAIN_LABEL.get(rec.get('chain'), rec.get('chain'))}\n"
            f"💰 mint：{fmt_eth(rec.get('mint_eth'))} ETH ｜ 最优卖单 {fmt_eth(rec.get('best_ask'))}\n"
            f"🟩 最优买单：{fmt_eth(rec.get('best_bid'))}\n"
            f"🏁 {fmt_end(rec.get('mint_end'))[0]}\n"
            f"🔗 {rec.get('opensea') or ''}"
        )
        log(f"即将结束：{rec.get('name')}（剩 {hours_left:.1f}h）")
        if args.dry_run:
            print("\n" + text + "\n")
        else:
            _push(cfg, text, send_text)
            st["ending_notified"] = now.isoformat()
            _record({"at": now.isoformat(), "type": "ending_soon", "slug": slug,
                     "name": rec.get("name"), "hours_left": hours_left, "text": text})
        sent_end += 1

    if not (args.dry_run or args.force_slug):
        save_json(STATE, state)
    log(f"完成：候选 {len(entries)} 条，即将结束 {sent_end} 条")
    return 0


def _hours_to_raw(hours) -> str | None:
    """把「还剩 N 小时」反推回时间串，供 fmt_end 复用（记录里可能没有 mint_end）。"""
    if hours is None:
        return None
    try:
        t = datetime.now(CST) + timedelta(hours=float(hours))
    except (TypeError, ValueError):
        return None
    return t.isoformat()


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


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
