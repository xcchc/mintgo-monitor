"""报告与通知共用的格式化工具。

放在这里是为了让 report.py（Markdown 报告）和 notify.py（企业微信推送）
用同一套数字/时间/链名口径，避免两边显示不一致。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))

CHAIN_LABEL = {
    "robinhood": "Robinhood",
    "ethereum": "Ethereum",
    "ink": "Ink",
    "base": "Base",
    "bsc": "BSC",
    "elysium": "Elysium",
    "arc": "Arc",
}

VERDICT_ICON = {
    "值得打": "🟢",
    "观察": "🟡",
    "无地板·待看": "🔵",   # 信息缺失，不是差评；交给用户自己判断
    "跳过": "⚪",
}


def fmt_eth(v) -> str:
    """价格展示：None → —，0 → FREE，inf → ∞。"""
    if v is None:
        return "—"
    if v == 0:
        return "FREE"
    if isinstance(v, str) and v == "inf":
        return "∞"
    return f"{float(v):g}"


def fmt_ratio(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, str) and v == "inf":
        return "∞（免费）"
    return f"{float(v):.2f}x"


def fmt_start(ts: int | None, now_ms: int) -> str:
    """开抢时间 + 相对倒计时；已过开始时间显示「进行中」。"""
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts / 1000, CST)
    delta_min = (ts - now_ms) / 60000
    if delta_min < 0:
        return f"{dt:%m-%d %H:%M}（进行中）"
    if delta_min < 60:
        return f"{dt:%H:%M}（{delta_min:.0f} 分钟后）"
    if delta_min < 1440:
        return f"{dt:%m-%d %H:%M}（{delta_min/60:.1f} 小时后）"
    return f"{dt:%m-%d %H:%M}（{delta_min/1440:.1f} 天后）"


def fmt_countdown(ts: int | None, now_ms: int) -> str:
    """只要相对时间，给推送用（挤字数）。"""
    if not ts:
        return "—"
    delta_min = (ts - now_ms) / 60000
    if delta_min < 0:
        return "进行中"
    if delta_min < 60:
        return f"{delta_min:.0f} 分钟后"
    if delta_min < 1440:
        return f"{delta_min/60:.1f} 小时后"
    return f"{delta_min/1440:.1f} 天后"


def is_started(ts: int | None, now_ms: int) -> int:
    """给排序用：已开抢返回 1，未开抢返回 0（未开抢的排前面）。

    没有开始时间的按「未开抢」处理，因为它至少还是未来的事。
    """
    if not ts:
        return 0
    return 1 if ts <= now_ms else 0
