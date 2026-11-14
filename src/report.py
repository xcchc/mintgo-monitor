"""把打分结果渲染成 Markdown 报告。"""
from __future__ import annotations

import time
from datetime import datetime

from fmt import CHAIN_LABEL, CST, VERDICT_ICON, fmt_eth as _fmt_eth, fmt_ratio as _fmt_ratio, fmt_start as _fmt_start
from score import Scored


def _link(url: str | None, label: str) -> str:
    return f"[{label}]({url})" if url else "—"


def render(scored: list[Scored], data: dict, cfg: dict) -> str:
    now_ms = int(time.time() * 1000)
    now = datetime.now(CST)
    top_n = int((cfg.get("report") or {}).get("top_n") or 20)

    lines: list[str] = []
    lines.append(f"# MintGo 监控报告 · {now:%Y-%m-%d %H:%M} (UTC+8)")
    lines.append("")

    # ---- 概况 ----
    radar = data.get("radar") or {}
    total_radar = sum(len(b.get("items") or []) for b in radar.values())
    per_chain = "、".join(
        f"{CHAIN_LABEL.get(c, c)} {len(b.get('items') or [])}" for c, b in radar.items()
    )
    counts = {"值得打": 0, "观察": 0, "无地板·待看": 0, "跳过": 0}
    for s in scored:
        counts[s.verdict] = counts.get(s.verdict, 0) + 1

    lines.append("## 概况")
    lines.append("")
    lines.append(f"- 抓取项目：**{total_radar}** 个（{per_chain}）")
    lines.append(f"- 时间窗内：**{len(scored)}** 个")
    lines.append(
        f"- 判定分布：🟢 值得打 **{counts['值得打']}** · "
        f"🟡 观察 **{counts['观察']}** · "
        f"🔵 无地板·待看 **{counts['无地板·待看']}** · "
        f"⚪ 跳过 **{counts['跳过']}**"
    )
    if counts["无地板·待看"]:
        lines.append(
            "  - 🔵 的含义：这些项目**还没有二级地板数据**（radar 里的新项目天然没有），"
            "所以算不出赔率。这不是差评，只是信息缺失——要不要打请自己判断。"
        )
    errs = data.get("errors") or []
    if errs:
        lines.append(f"- ⚠️ 抓取告警 {len(errs)} 条：")
        for e in errs[:5]:
            lines.append(f"  - `{e}`")
    lines.append("")

    # ---- 值得打 ----
    good = [s for s in scored if s.verdict == "值得打"]
    lines.append("## 🟢 值得打")
    lines.append("")
    if not good:
        lines.append("_本轮没有满足条件的项目。_")
        lines.append("")
    else:
        lines.append("| 项目 | 链 | mint | 地板 | 成交中位 | 赔率 | 1h成交 | 进度 | 开抢 | 上限 | 链接 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for s in good[:top_n]:
            links = " ".join(
                x for x in (_link(s.opensea, "OS"), _link(s.twitter, "X"), _link(s.explorer, "链")) if x != "—"
            ) or "—"
            lines.append(
                f"| **{s.name}** | {CHAIN_LABEL.get(s.chain, s.chain)} | {_fmt_eth(s.mint_eth)} "
                f"| {_fmt_eth(s.floor_eth)} | {_fmt_eth(s.sale_median_eth)} | {_fmt_ratio(s.ratio)} | {s.sales_1h} "
                f"| {s.progress_pct if s.progress_pct is not None else '—'}% "
                f"| {_fmt_start(s.start_at, now_ms)} | {s.max_per_wallet or '—'} | {links} |"
            )
        lines.append("")
        lines.append("<details><summary>判定理由</summary>")
        lines.append("")
        for s in good[:top_n]:
            lines.append(f"**{s.name}**（{s.score} 分）")
            for r in s.reasons:
                lines.append(f"- {r}")
            lines.append("")
        lines.append("</details>")
        lines.append("")

    # ---- 观察 ----
    watch = [s for s in scored if s.verdict == "观察"]
    lines.append(f"## 🟡 观察（{len(watch)} 个，列前 {min(top_n, len(watch))}）")
    lines.append("")
    if not watch:
        lines.append("_无。_")
        lines.append("")
    else:
        lines.append("| 项目 | 链 | mint | 地板 | 成交中位 | 赔率 | 1h成交 | 进度 | 开抢 | 分 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for s in watch[:top_n]:
            lines.append(
                f"| {s.name} | {CHAIN_LABEL.get(s.chain, s.chain)} | {_fmt_eth(s.mint_eth)} "
                f"| {_fmt_eth(s.floor_eth)} | {_fmt_eth(s.sale_median_eth)} | {_fmt_ratio(s.ratio)} | {s.sales_1h} "
                f"| {s.progress_pct if s.progress_pct is not None else '—'}% "
                f"| {_fmt_start(s.start_at, now_ms)} | {s.score} |"
            )
        lines.append("")

    # ---- 无地板·待看 ----
    nofloor = [s for s in scored if s.verdict == "无地板·待看"]
    lines.append(f"## 🔵 无地板·待看（{len(nofloor)} 个，列前 {min(top_n, len(nofloor))}）")
    lines.append("")
    if not nofloor:
        lines.append("_无。_")
        lines.append("")
    else:
        lines.append("> 这些项目没有二级地板数据，**算不出赔率**，所以无法判断值不值得打。")
        lines.append("> 列出来是给你自己看首发条件的（价格、限购、开抢时间、creator 记录）。")
        lines.append("")
        lines.append("| 项目 | 链 | mint | 开抢 | 上限 | 阶段 | creator | 链接 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for s in nofloor[:top_n]:
            links = " ".join(
                x for x in (_link(s.opensea, "OS"), _link(s.twitter, "X"), _link(s.explorer, "链")) if x != "—"
            ) or "—"
            creator = f"{s.deployer_projects} 个" if s.deployer_projects is not None else "—"
            lines.append(
                f"| {s.name} | {CHAIN_LABEL.get(s.chain, s.chain)} | {_fmt_eth(s.mint_eth)} "
                f"| {_fmt_start(s.start_at, now_ms)} | {s.max_per_wallet or '—'} "
                f"| {s.stage_label or '—'} | {creator} | {links} |"
            )
        lines.append("")

    # ---- 全量清单 ----
    if (cfg.get("report") or {}).get("include_all_upcoming"):
        lines.append("<details><summary>完整清单（按分数）</summary>")
        lines.append("")
        lines.append("| 项目 | 链 | mint | 地板 | 成交中位 | 1h成交 | 判定 | 分 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for s in scored:
            lines.append(
                f"| {s.name} | {CHAIN_LABEL.get(s.chain, s.chain)} | {_fmt_eth(s.mint_eth)} "
                f"| {_fmt_eth(s.floor_eth)} | {_fmt_eth(s.sale_median_eth)} | {s.sales_1h} "
                f"| {VERDICT_ICON.get(s.verdict,'')} {s.verdict} | {s.score} |"
            )
        lines.append("")
        lines.append("</details>")
        lines.append("")

    # ---- 免责 ----
    lines.append("---")
    lines.append("")
    lines.append(
        "> 口径：**赔率 = 挂单地板 / mint 价**（用于发现落差）；**成交中位** 来自 OpenSea 真实成交记录，"
        "仅供参考、不参与评分。真实盈亏请看 `scripts/backtest.py` 的历史回测，那里一律按成交价算。"
    )
    lines.append("")
    lines.append(
        "> 数据源：mintgo.fun `/api/view/radar` + `/api/market-snapshot`。"
        "本报告只做信息聚合，不构成投资建议；签名与转账请一律在自己的钱包完成。"
    )
    lines.append("")
    return "\n".join(lines)
