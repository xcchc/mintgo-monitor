"""企业微信机器人推送。

为什么用 text 而不是 markdown：
  机器人发 markdown 消息时，**微信客户端不渲染**，只显示一行
  「暂不支持此消息类型，点击前往企业微信查看」。而 text 在微信、企业微信
  两端都能直接看。所以这里一律发 text，靠纯文本对齐排版。

text 消息的约束：
  · content 最长 2048 字节（UTF-8，中文/emoji 各占 3-4 字节），比 markdown 的
    4096 短一半，所以必须按字节切，按字符切会被 45002 拒掉。
  · 不支持任何语法：粗体、链接、颜色全部按原样显示，所以只写裸 URL。

推送策略（避免把监控跑成骚扰）：
  · 每轮只推「值得打」
  · 同一项目设冷却时间，冷却期内不重复推；分数明显上涨可以打破冷却
  · 状态文件坏了就当空的处理，绝不让推送逻辑拖垮主流程
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import fmt
from score import Scored

# text 消息上限 2048 字节，留 48 字节余量
MAX_BYTES = 2000
# 万一来消息超长被拒，降级到只剩名字+赔率的极简版，压在这个长度内
MAX_BYTES_COMPACT = 1800


def _byte_len(s: str) -> int:
    return len(s.encode("utf-8"))


def _truncate_bytes(s: str, limit: int) -> str:
    """按 UTF-8 字节截断，不切坏多字节字符。"""
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    return b[: max(0, limit - 3)].decode("utf-8", errors="ignore") + "…"


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _chain_label(c: str) -> str:
    return fmt.CHAIN_LABEL.get(c, c or "—")


def summarize_reason(s: Scored) -> list[str]:
    """挑出推送里值得看的理由，并去掉和上方数据行重复的信息。

    成交笔数、地板价、赔率都已经在数据行里显示过了，理由里再说一遍纯属占屏。
    """
    picked = [r for r in s.reasons if any(k in r for k in ("赔率", "免费 mint", "成交", "工厂", "钱包"))]
    order = {"赔率": 0, "免费 mint": 0, "成交": 1}
    picked.sort(key=lambda r: next((v for k, v in order.items() if k in r), 2))

    out: list[str] = []
    for r in picked:
        # 纯成交笔数 / 纯地板价这类，数据行已覆盖
        if r.startswith("1h 成交") or r.startswith("1h 仅"):
            continue
        if r.startswith("免费 mint，地板") or r == "免费 mint（暂无地板）":
            continue
        if r.startswith("赔率") and len(r) < 24:
            continue
        out.append(r)
    return out[:2]


# ---------------------------------------------------------------- 去重/冷却状态


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass  # 状态存不下不影响推送本身


def select_candidates(
    scored: list[Scored], state: dict, cfg: dict, now_ts: float
) -> tuple[list[Scored], dict]:
    """挑出该推的项目，并返回更新后的 state。

    冷却规则：同一 contract 在 cooldown_minutes 内只推一次；
    或者分数比上次推的高 min_score_delta 才再推。
    """
    notify_cfg = cfg.get("notify") or {}
    cooldown_s = float(notify_cfg.get("cooldown_minutes") or 180) * 60
    min_delta = float(notify_cfg.get("min_score_delta") or 10)
    last = state.get("pushed") or {}

    picked: list[Scored] = []
    new_state = {"pushed": dict(last), "lastRunTs": now_ts}

    for s in scored:
        if s.verdict != "值得打":
            continue
        rec = last.get(s.contract.lower()) if s.contract else None
        if rec:
            age = now_ts - float(rec.get("ts") or 0)
            score_up = s.score - float(rec.get("score") or 0) >= min_delta
            if age < cooldown_s and not score_up:
                continue
        picked.append(s)
        if s.contract:
            new_state["pushed"][s.contract.lower()] = {"ts": now_ts, "score": s.score, "name": s.name}

    # 清掉太久远的记录，防止文件无限增长
    horizon = now_ts - 7 * 86400
    new_state["pushed"] = {
        k: v for k, v in new_state["pushed"].items() if float(v.get("ts") or 0) > horizon
    }
    return picked, new_state


# ---------------------------------------------------------------- 渲染


def _price_line(s: Scored) -> str:
    """价格 / 地板 / 赔率压成一行，避免同义重复。"""
    parts: list[str] = []
    if s.mint_eth == 0:
        parts.append("免费")
        if s.floor_eth:
            parts.append(f"地板 {fmt.fmt_eth(s.floor_eth)}")
    else:
        parts.append(f"{fmt.fmt_eth(s.mint_eth)} ETH")
        if s.floor_eth:
            parts.append(f"地板 {fmt.fmt_eth(s.floor_eth)}")
            if s.ratio is not None:
                parts.append(f"赔率 {fmt.fmt_ratio(s.ratio)}")
        else:
            parts.append("无地板")
    return " · ".join(parts)


def _time_line(s: Scored, now_ms: int) -> str:
    if not s.start_at:
        return "⏰ 开抢：时间待定"
    dt = datetime.fromtimestamp(s.start_at / 1000, fmt.CST)
    return f"⏰ 开抢：{dt:%m-%d %H:%M}（{fmt.fmt_countdown(s.start_at, now_ms)}）"


def _quota_line(s: Scored) -> str:
    """成交深度 + 限购，合成一行。"""
    parts = [f"📊 1h 成交：{s.sales_1h} 笔"]
    if s.max_per_wallet:
        parts.append(f"👤 限购：{s.max_per_wallet}")
    if s.deployer_projects is not None:
        parts.append(f"🏭 作者项目：{s.deployer_projects} 个")
    return " ｜ ".join(parts)


def _link_lines(s: Scored) -> list[str]:
    """每条链接单独一行，并写清是什么链接。"""
    out: list[str] = []
    if s.opensea:
        out.append(f"🔗 项目网址：{s.opensea}")
    if s.twitter:
        out.append(f"🐦 推特：{s.twitter}")
    if s.explorer:
        out.append(f"⛓ 合约地址：{s.explorer}")
    return out


def _project_block(s: Scored, now_ms: int, link_mode: str) -> list[str]:
    """渲染一个项目。link_mode 控制带几条链接，用来在字节预算内保住排版完整。

    full  = 项目网址 + 推特 + 合约
    thin  = 只留项目网址
    none  = 不带链接（连 thin 都放不下时）
    """
    lines = [f"   📍 链：{_chain_label(s.chain)}"]
    lines.append(f"   💰 价格：{_price_line(s)}")
    lines.append(f"   {_time_line(s, now_ms)}")
    lines.append(f"   {_quota_line(s)}")
    lines.append(f"   🎯 评分：{s.score:g}")
    reasons = summarize_reason(s)
    if reasons:
        lines.append("   💡 理由：" + "；".join(_clip(r, 32) for r in reasons))
    if link_mode == "full":
        lines.extend("   " + x for x in _link_lines(s))
    elif link_mode == "thin":
        if s.opensea:
            lines.append(f"   🔗 项目网址：{s.opensea}")
        elif s.explorer:
            lines.append(f"   ⛓ 合约地址：{s.explorer}")
    return lines


def render_text(
    picked: list[Scored], all_scored: list[Scored], data: dict, cfg: dict, now_ms: int
) -> str:
    """纯文本排版。手机窄屏也能对齐，不用任何 markdown 语法。

    每行前面都带 emoji + 中文标签，扫一眼就知道这行是什么，不用记字段顺序。

    字节预算：企业微信 text 上限 2048 字节，一个项目带 3 条链接约 500 字节，
    项目一多就爆。这里的策略是**逐个降级**而不是一刀切：
      每个项目依次尝试 完整链接 → 只留项目网址 → 不带链接，
      取第一个放得下的形态；连最精简的都放不下时停止追加，
      末尾写一句「另有 N 个项目」指向本地报告。
    这样既不硬截断（会把链接切成半截），也不会因为项目多就把所有链接全丢掉。
    """
    now = datetime.now(fmt.CST)
    radar = data.get("radar") or {}
    total_radar = sum(len(b.get("items") or []) for b in radar.values())
    n_watch = sum(1 for s in all_scored if s.verdict == "观察")
    n_nofloor = sum(1 for s in all_scored if s.verdict == "无地板·待看")
    # 抓取范围：有项目的链数 / 总链数。BSC、Elysium 目前常为 0，
    # 但它们是配置里实际在抓的链，只报「有货的链」会让人以为漏抓了。
    chains_with_items = [c for c, b in radar.items() if (b.get("items") or [])]
    n_chains_total = len(radar)

    lines: list[str] = []
    lines.append(f"🚀 MintGo 打新提醒 · {now:%m-%d %H:%M}")
    summary: list[str] = []
    if total_radar:
        summary.append(f"扫描 {total_radar} 个")
    if n_chains_total:
        if len(chains_with_items) == n_chains_total:
            summary.append(f"{n_chains_total} 条链")
        else:
            summary.append(f"{n_chains_total} 条链（{len(chains_with_items)} 条有货）")
    summary.append(f"值得打 {len(picked)} 个")
    if n_watch:
        summary.append(f"观察 {n_watch} 个")
    if n_nofloor:
        summary.append(f"无地板待看 {n_nofloor} 个")
    lines.append("🌐 " + " ｜ ".join(summary))
    lines.append("")

    FOOTER_RESERVE = 60  # 预留给「另有 N 个项目」那行
    omitted = 0

    for i, s in enumerate(picked, 1):
        placed = False
        for mode in ("full", "thin", "none"):
            block = [f"{i}. {s.name}", *_project_block(s, now_ms, mode)]
            if i < len(picked):
                block.append("")
            trial = "\n".join(lines + block)
            if _byte_len(trial) + FOOTER_RESERVE <= MAX_BYTES:
                lines.extend(block)
                placed = True
                break
        if not placed:
            omitted = len(picked) - (i - 1)
            break

    if omitted:
        lines.append(f"…另有 {omitted} 个项目，详见本地报告")

    # ---- 无地板·待看（简要列表）----
    # 这些项目算不出赔率，但用户明确要求看到其他链的项目，不能只在报告里躺着。
    # 推送空间有限，这里只给「名字 + 价格 + 开抢」，一行一个。
    #
    # 排序讲究：**优先还没开抢的**（还能做决策），已进行中的排后面；
    # 同类里按开抢时间升序。否则「进行中」的项目会把名额全占满，
    # 用户就看不到 Ethereum / Base / Ink 上真正还能提前准备的机会。
    nofloor = [
        s for s in all_scored
        if s.verdict == "无地板·待看" and (s.name or "").strip() and s.name.strip() != "..."
    ]
    if nofloor:
        # 分组：按链聚在一起，链内按「未开抢优先 + 开抢时间升序」
        def _sort_key(s):
            return (fmt.is_started(s.start_at, now_ms), s.start_at or 9_999_999_999_999)

        by_chain: dict[str, list[Scored]] = {}
        for s in sorted(nofloor, key=_sort_key):
            by_chain.setdefault(s.chain, []).append(s)
        chain_order = sorted(
            by_chain, key=lambda c: min(_sort_key(s) for s in by_chain[c])
        )

        # 名额分配：**先给每条链保底 2 个**，剩下的名额再按链轮转填。
        # 否则 Robinhood 60+ 个项目会把名额吃光，Ethereum / Base / Ink 一个都露不出来
        # —— 这正是之前"只有 RH 值得打"错觉的来源。
        MAX_LINES = 15
        PER_CHAIN_MIN = 2

        quota: dict[str, int] = {c: min(PER_CHAIN_MIN, len(by_chain[c])) for c in chain_order}
        remaining = MAX_LINES - sum(quota.values())
        # 轮转补足
        while remaining > 0:
            progressed = False
            for c in chain_order:
                if remaining <= 0:
                    break
                if quota[c] < len(by_chain[c]):
                    quota[c] += 1
                    remaining -= 1
                    progressed = True
            if not progressed:
                break

        lines.append("")
        lines.append(f"🔵 无地板·待看 {len(nofloor)} 个（算不出赔率，仅供自己判断）")

        shown = 0
        for chain in chain_order:
            group = by_chain[chain]
            head = f"  ▸ {_chain_label(chain)}（{len(group)} 个，列 {quota[chain]}）"
            if _byte_len("\n".join(lines + [head])) + 100 > MAX_BYTES:
                break
            lines.append(head)
            for s in group[: quota[chain]]:
                price = "免费" if s.mint_eth == 0 else f"{fmt.fmt_eth(s.mint_eth)} ETH"
                when = fmt.fmt_countdown(s.start_at, now_ms)
                line = f"    · {_clip(s.name, 24)} ｜ {price} ｜ {when}"
                if _byte_len("\n".join(lines + [line])) + 60 <= MAX_BYTES:
                    lines.append(line)
                    shown += 1
                else:
                    break
        if len(nofloor) > shown:
            lines.append(f"    …另 {len(nofloor) - shown} 个见本地报告")

    body = "\n".join(lines).rstrip()
    if _byte_len(body) > MAX_BYTES:  # 兜底，理论上不会走到
        body = _truncate_bytes(body, MAX_BYTES)
    return body


def render_compact(picked: list[Scored], now_ms: int) -> str:
    """极简版：只在超长降级时用。"""
    lines = ["🚀 MintGo 打新提醒"]
    for i, s in enumerate(picked, 1):
        lines.append(
            f"{i}. {s.name}（{_chain_label(s.chain)}）"
            f" {fmt.fmt_ratio(s.ratio)} · {fmt.fmt_countdown(s.start_at, now_ms)}"
        )
    body = "\n".join(lines)
    return _truncate_bytes(body, MAX_BYTES_COMPACT)


# ---------------------------------------------------------------- 发送


def _post_webhook(webhook: str, payload: dict) -> dict:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=raw,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"errcode": -1, "errmsg": text[:200]}


def send_text(webhook: str, content: str) -> dict:
    return _post_webhook(webhook, {"msgtype": "text", "text": {"content": content}})


def send_markdown(webhook: str, content: str) -> dict:
    """保留给企业微信内部群用；微信端看不了 markdown，默认不走这里。"""
    return _post_webhook(webhook, {"msgtype": "markdown", "markdown": {"content": content}})


def push(
    scored: list[Scored], data: dict, cfg: dict, root: Path, log=print
) -> int:
    """按配置决定是否推送。返回推送条数（0 表示没推）。

    绝不抛异常：推送失败只写日志，主流程照常收尾。
    """
    notify_cfg = cfg.get("notify") or {}
    if not notify_cfg.get("enabled", False):
        return 0

    webhook = (notify_cfg.get("webhook") or "").strip()
    if not webhook:
        log("⚠️ notify.enabled 为真但 webhook 为空，跳过推送")
        return 0

    try:
        now_ts = time.time()
        now_ms = int(now_ts * 1000)
        state_path = root / (notify_cfg.get("state_file") or "data/notify_state.json")
        state = _load_state(state_path)

        picked, new_state = select_candidates(scored, state, cfg, now_ts)

        nofloor = [s for s in scored if s.verdict == "无地板·待看"]
        nofloor_cfg = notify_cfg.get("push_nofloor", True)

        # 什么时候发：
        #   · 有新的「值得打」→ 一定发
        #   · require_floor_data=true 时：无地板项目已被全部排除，
        #     nofloor 恒为空，等于「有值得打才发」。
        #     这是刻意的 —— 只发布有地板价格的项目，没货就安静，
        #     而不是塞一堆算不出赔率的项目把消息填满。
        #   · 开了 push_nofloor 且有「无地板·待看」→ 也发
        #   · 都没有 → 不发
        if not picked and not (nofloor_cfg and nofloor):
            log("🔕 本轮无可发布项目（无「值得打」，也没有「无地板·待看」），跳过推送")
            _save_state(state_path, new_state)
            return 0

        if not picked:
            log(f"ℹ️ 本轮无新增「值得打」，但推送「无地板·待看」{len(nofloor)} 个")

        content = render_text(picked, scored, data, cfg, now_ms)
        resp = send_text(webhook, content)
        if resp.get("errcode") == 0:
            parts = []
            if picked:
                parts.append(f"{len(picked)} 个「值得打」")
            if nofloor:
                parts.append(f"{len(nofloor)} 个「无地板·待看」")
            log("📤 已推送 " + " + ".join(parts))
            _save_state(state_path, new_state)
            return len(picked) or len(nofloor)

        log(f"⚠️ 推送失败：{resp}")
        # 45002 = 内容超长，降级到极简版再试一次
        if resp.get("errcode") == 45002:
            resp2 = send_text(webhook, render_compact(picked or nofloor, now_ms))
            if resp2.get("errcode") == 0:
                log("📤 已降级为极简版推送")
                _save_state(state_path, new_state)
                return len(picked) or len(nofloor)
            log(f"⚠️ 极简版推送也失败：{resp2}")
        return 0
    except Exception as e:  # noqa: BLE001 —— 推送是旁路，不能拖垮主流程
        log(f"⚠️ 推送异常（已忽略）：{e}")
        return 0
