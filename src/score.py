"""对 radar 项目打分，给出「值得打 / 观察 / 无地板·待看 / 跳过」。

核心思路：NFT mint 能不能打，本质是一道赔率题 ——
    赔率 = 二级地板价 / mint 价
赔率 < 1 意味着铸出来就浮亏，只有一个例外：免费 mint 且有真实地板。

但赔率必须建立在「这个地板是真的」之上，所以叠加真实性扣分：
  · 1 小时成交笔数太少 → 地板是挂单幻觉
  · creator 发过一堆项目 → 工厂盘，做完就跑
  · creator 钱包太新 → 一次性地址
  · 进度 > 95% → 抢不到，机会窗口已关

重要：radar（即将开抢的新项目）和 market-snapshot（已有二级市场的项目）是
两个几乎不相交的集合 —— 实测 178 个 radar 项目只有 2 个在 market 里有记录。
所以绝大多数项目「没有地板」不是因为它差，而是因为它还没有二级市场。
把这种情况当成扣分项会把几乎所有项目埋掉（早期版本就是这个毛病，
导致只剩 Robinhood 一个项目「值得打」，误以为别的链没机会）。
因此这里单列一档「无地板·待看」：信息缺失，交给用户自己判断。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

WEI = 10**18


@dataclass
class MarketRow:
    """某项目在 market snapshot 里的表现，按时间窗聚合。"""

    name: str = ""
    floors: dict[str, float] = field(default_factory=dict)
    vol: dict[str, float] = field(default_factory=dict)
    tx: dict[str, int] = field(default_factory=dict)
    change_1d: float | None = None
    deployer_projects: int | None = None
    deployer_age: str | None = None
    twitter: str | None = None
    opensea: str | None = None
    chain: str = ""

    @property
    def floor(self) -> float | None:
        for w in ("1d", "1h", "15m", "5m"):
            v = self.floors.get(w)
            if v:
                return v
        return None

    @property
    def sales_1h(self) -> int:
        return int(self.tx.get("1h") or 0)

    @property
    def volume_1h(self) -> float:
        """1 小时成交量（ETH）。

        为什么要它：光看成交**笔数**会被刷量骗。实测 ASCIHOOD 有 359 笔成交，
        但总成交量只有 0.000275 ETH（平均每笔 $0.0016），而正常项目单笔是
        $0.66~$8000 —— 这是典型的用极小金额反复刷笔数。
        """
        v = self.vol.get("1h")
        return float(v) if v is not None else 0.0

    @property
    def avg_trade_1h(self) -> float:
        """1 小时平均单笔成交额（ETH）。用于识别「笔数很多但金额是尘埃」的刷量。"""
        n = self.sales_1h
        return (self.volume_1h / n) if n else 0.0


def build_market_index(market: dict[str, Any]) -> dict[tuple[str, str], MarketRow]:
    """把各链 market-snapshot 摊平成 {(链, 合约地址小写): MarketRow}。

    为什么要带链：不同 EVM 链上完全可能出现同一个合约地址（部署者用同样的
    nonce、或者 fork 的项目）。早期版本只用地址做 key，跨链撞地址时会把
    A 链的地板算到 B 链的项目头上。虽然当前数据里还没撞上，但这是隐患。
    """
    idx: dict[tuple[str, str], MarketRow] = {}
    for chain, snap in (market or {}).items():
        if not snap:
            continue
        for window, rows in (snap.get("runners") or {}).items():
            for row in rows or []:
                addr = (row.get("contractAddress") or "").lower()
                if not addr:
                    continue
                key = (chain, addr)
                mr = idx.get(key)
                if mr is None:
                    mr = MarketRow(
                        name=row.get("name") or "",
                        change_1d=row.get("windowFloorPriceChange"),
                        deployer_projects=row.get("deployerNftProjectCount"),
                        deployer_age=row.get("deployerCreatedAgo"),
                        twitter=row.get("twitterUrl"),
                        opensea=row.get("openSeaUrl"),
                        chain=chain,
                    )
                    idx[key] = mr
                if row.get("floorPriceEth"):
                    mr.floors[window] = float(row["floorPriceEth"])
                if row.get("windowVolumeEth") is not None:
                    mr.vol[window] = float(row["windowVolumeEth"])
                if row.get("windowTxCount") is not None:
                    mr.tx[window] = int(row["windowTxCount"])
                if row.get("deployerNftProjectCount") is not None:
                    mr.deployer_projects = int(row["deployerNftProjectCount"])
                if row.get("deployerCreatedAgo"):
                    mr.deployer_age = row["deployerCreatedAgo"]
    return idx


def _age_to_days(age: str | None) -> float | None:
    """把 mintgo 的 '14 hours ago' / '3 days ago' / '2 months ago' 换成天数。"""
    if not age:
        return None
    parts = age.lower().replace("ago", "").split()
    if len(parts) < 2:
        return None
    try:
        n = float(parts[0])
    except ValueError:
        return None
    unit = parts[1].rstrip("s")
    factor = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30, "year": 365}.get(unit)
    if factor is None:
        return None
    return n * factor


@dataclass
class Scored:
    name: str
    chain: str
    contract: str
    score: float
    verdict: str
    reasons: list[str]
    mint_eth: float
    floor_eth: float | None
    ratio: float | None
    sales_1h: int
    progress_pct: float | None
    start_at: int | None
    max_per_wallet: int | None
    stage_label: str
    status: str
    deployer_projects: int | None
    deployer_age: str | None
    twitter: str | None
    opensea: str | None
    explorer: str | None
    sale_median_eth: float | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _progress_pct(item: dict) -> float | None:
    """radar API 只给 supply / soldOut，不给已铸数量。

    所以：
      · soldOut=true  → 100%（机会已关）
      · 其余情况      → None（进度未知，报告里显示 —）
    真正的已铸数量要从项目详情接口拿，逐个项目调用成本太高，这里不做。
    """
    if item.get("soldOut"):
        return 100.0
    return None


# 明显是测试/垃圾合约的项目名（radar 里混进大量 deploy 测试）
_NOISE_NAMES = {
    "dnd", "els", "scott", "mccall", "vine", "wif", "testc", "trytestsv",
    "bblo-test", "dang", "lpcg", "vfbvfbvfb", "试", "test", "testing",
}


def _name_quality(name: str) -> int:
    """项目名质量：返回 0 正常 / -X 扣分。"""
    n = (name or "").strip()
    if not n:
        return -20
    # 纯地址当名字，基本是未配置的裸合约
    if re.fullmatch(r"0x[0-9a-fA-F]{40}", n):
        return -25
    if n.lower() in _NOISE_NAMES:
        return -25
    # 全小写无空格且很短，多半是随手部署的测试
    if len(n) <= 6 and n.islower() and " " not in n:
        return -12
    return 0


def _cfg_val(cfg: dict, key: str, default):
    """读阈值，优先 filters 块，其次顶层，最后默认值。

    历史坑：config.yaml 里的阈值配在 `filters:` 下面，但早期代码直接
    cfg.get("min_floor_ratio") 读顶层 —— 永远是 None，于是静默回退到
    硬编码默认值。因为默认值恰好和配置一样，一直没被发现：**改 config 不生效**。
    现在统一走这个函数，两个位置都认。
    """
    filters = cfg.get("filters") or {}
    v = filters.get(key)
    if v is None:
        v = cfg.get(key)
    return default if v is None else v


def score_item(item: dict, market: MarketRow | None, cfg: dict, now_ms: int) -> Scored | None:
    """给单个 radar 项目打分。返回 None 表示按时间窗过滤掉了。"""
    lookahead_h = float(cfg.get("lookahead_hours") or 48)
    start_at = item.get("startAt") or None
    if start_at:
        hours_ahead = (start_at - now_ms) / 3_600_000
        if hours_ahead > lookahead_h:
            return None
        # 结束太久的也丢掉（endAt 早于现在）
        end_at = item.get("endAt") or 0
        if end_at and end_at < now_ms:
            return None

    price_eth = float(item.get("priceEth") or 0)
    floor = market.floor if market else None

    # 成交中位只作为**展示用参考**，不参与评分。
    # 评分口径坚持用挂单地板（mintgo market-snapshot 的 floorPriceEth）：
    # 它是「现在市场上最便宜的卖方报价」，用于发现「铸造价 vs 二级报价」的落差。
    # 真实的盈亏必须用成交价另算（见 scripts/backtest.py 与利润复盘）。
    sale_med: float | None = None
    raw_sm = item.get("saleMedianEth")
    if raw_sm is not None:
        try:
            sale_med = float(raw_sm)
        except (TypeError, ValueError):
            sale_med = None
        if sale_med is not None and sale_med <= 0:
            sale_med = None
    price_ref = floor
    ref_label = "地板"

    sales_1h = market.sales_1h if market else 0
    # 1 小时成交量门槛（ETH）。低于这个值即使笔数很多也视为刷量。
    # 0.001 ETH ≈ $2，足够过滤掉「每笔 $0.0016」这种尘埃成交。
    min_volume_1h = float(_cfg_val(cfg, "min_volume_1h_eth", 0.001))
    deployer_projects = market.deployer_projects if market else None
    deployer_age_days = _age_to_days(market.deployer_age if market else None)
    progress = _progress_pct(item)

    score = 0.0
    reasons: list[str] = []

    name = item.get("displayName") or item.get("name") or ""
    name_penalty = _name_quality(name)
    if name_penalty < 0:
        score += name_penalty
        reasons.append(f"项目名不可信（{name!r}），疑似部署测试")

    # ---- 1. 赔率 ----
    ratio = None
    if price_ref and price_eth > 0:
        ratio = price_ref / price_eth
        if ratio >= 2:
            score += 40
            reasons.append(f"赔率 {ratio:.2f}x（{ref_label} {price_ref:g} / mint {price_eth:g}）")
        elif ratio >= 1:
            score += 25
            reasons.append(f"赔率 {ratio:.2f}x（{ref_label}口径）")
        elif ratio >= 0.5:
            score += 8
            reasons.append(f"赔率偏低 {ratio:.2f}x（{ref_label}口径）")
        else:
            score -= 25
            reasons.append(f"赔率亏损 {ratio:.2f}x（{ref_label} {price_ref:g} 低于 mint {price_eth:g}）")
    elif price_eth == 0 and price_ref:
        ratio = float("inf")
        score += 35
        reasons.append(f"免费 mint，{ref_label} {price_ref:g}")
    elif price_eth == 0:
        score += 12
        reasons.append("免费 mint（暂无地板）")
    else:
        score -= 10
        reasons.append(f"付费 {price_eth:g} 且无地板数据")

    # ---- 2. 二级深度 ----
    # 关键改进：不能只看成交笔数，还要看成交量。
    # 刷量项目会用极小金额反复成交，把笔数刷得很好看（实测 ASCIHOOD：
    # 359 笔但总成交量仅 0.000275 ETH，平均每笔 $0.0016）。
    vol_1h = market.volume_1h if market else 0.0
    avg_1h = market.avg_trade_1h if market else 0.0
    vol_ok = vol_1h >= min_volume_1h

    if market is None:
        score -= 15
        reasons.append("market 无记录（24h 内零成交）")
    elif sales_1h >= 20 and vol_ok:
        score += 20
        reasons.append(f"1h 成交 {sales_1h} 笔 / {vol_1h:g} ETH，深度好")
    elif sales_1h >= 5 and vol_ok:
        score += 10
        reasons.append(f"1h 成交 {sales_1h} 笔 / {vol_1h:g} ETH")
    elif sales_1h >= 5 and not vol_ok:
        # 笔数够但成交量太小 —— 典型的刷量特征，重罚
        score -= 20
        reasons.append(
            f"疑似刷量：1h 有 {sales_1h} 笔成交，但总量仅 {vol_1h:g} ETH"
            f"（平均每笔 {avg_1h:.2g} ETH，门槛 {min_volume_1h:g}）"
        )
    else:
        score -= 12
        reasons.append(f"1h 仅 {sales_1h} 笔，深度不足")

    # ---- 3. creator 风险 ----
    if deployer_projects is not None:
        if deployer_projects >= 20:
            score -= 25
            reasons.append(f"creator 已发 {deployer_projects} 个项目（工厂盘）")
        elif deployer_projects >= 10:
            score -= 15
            reasons.append(f"creator 已发 {deployer_projects} 个项目")
        elif deployer_projects <= 3:
            score += 5
            reasons.append(f"creator 项目数 {deployer_projects}，正常")
    if deployer_age_days is not None:
        if deployer_age_days < 1:
            score -= 20
            reasons.append(f"creator 钱包 {market.deployer_age}（全新地址）")
        elif deployer_age_days < 3:
            score -= 10
            reasons.append(f"creator 钱包很新：{market.deployer_age}")

    # ---- 4. 进度 ----
    late = float(_cfg_val(cfg, "late_progress_pct", 95))
    if progress is not None:
        if progress >= late:
            score -= 15
            reasons.append(f"进度 {progress}%，基本打不到")
        elif 20 <= progress <= 80:
            score += 8
            reasons.append(f"进度 {progress}%，窗口正合适")
        elif progress < 20:
            score += 3
            reasons.append(f"进度 {progress}%，刚开")

    # ---- 5. 地板趋势 ----
    if market and market.change_1d is not None:
        chg = float(market.change_1d)
        if chg <= -0.5:
            score -= 15
            reasons.append(f"24h 地板跌 {chg*100:.0f}%")
        elif chg >= 0.2:
            score += 5
            reasons.append(f"24h 地板涨 {chg*100:.0f}%")

    # ---- 判定 ----
    min_ratio = float(_cfg_val(cfg, "min_floor_ratio", 1.0))
    min_sales = int(_cfg_val(cfg, "min_sales_1h", 5))

    # 「没有地板数据」不等于「不值得打」——它只是信息缺失。
    # radar（即将开抢的新项目）和 market-snapshot（已有二级市场的项目）本就是
    # 两个几乎不相交的集合：实测 178 个 radar 项目里只有 2 个在 market 里有记录。
    #
    # 默认策略（require_floor_data=true）：**只发布有地板价格的项目**。
    # 理由：没有地板 = 算不出赔率 = 无法判断值不值得打，对新手而言等于盲赌。
    # 代价：覆盖率极低。历史 16 份快照统计，有地板的项目只占 0.6%~2.3%，
    #       平均每轮 1~4 个，经常是 0 个。这不是 bug，是数据本身的客观情况。
    # 想恢复「列出来自己看」的模式：config.yaml 里把 require_floor_data 设为 false。
    require_floor = bool(_cfg_val(cfg, "require_floor_data", True))
    no_floor_data = floor is None

    # 无地板但允许展示时，只收免费 mint（赔率算不出却要付钱 = 盲付）
    nofloor_max_price = float(_cfg_val(cfg, "nofloor_max_price_eth", 0.0))

    # 成交量门槛：笔数够但量太小 = 刷量，直接判失败。
    # 不与 sales_1h < min_sales 重复（那种情况已经失败）。
    wash_trading = (
        market is not None
        and sales_1h >= min_sales
        and (market.volume_1h < min_volume_1h)
    )

    hard_fail = (
        name_penalty < 0
        or (ratio is not None and ratio != float("inf") and ratio < min_ratio)
        or (market is not None and sales_1h < min_sales)
        or wash_trading
        or (deployer_projects is not None and deployer_projects >= 20)
        or (progress is not None and progress >= late)
    )

    if no_floor_data:
        if require_floor:
            # 只发布有地板的：没地板一律跳过，并在理由里说清楚为什么
            verdict = "跳过"
            if not hard_fail:
                reasons.append("无地板价，算不出赔率（当前配置只发布有地板数据的项目）")
            else:
                reasons.append("无地板价，算不出赔率")
        elif hard_fail:
            verdict = "跳过"
        elif price_eth > nofloor_max_price:
            verdict = "跳过"
            reasons.append(
                f"无地板且要付费 {price_eth:g} ETH —— 赔率算不出、钱照付，按配置跳过"
            )
        else:
            verdict = "无地板·待看"
    elif score >= 45 and not hard_fail:
        verdict = "值得打"
    elif score >= 20:
        verdict = "观察"
    else:
        verdict = "跳过"

    return Scored(
        name=name or "?",
        chain=item.get("chain") or "",
        contract=item.get("contractAddress") or "",
        score=round(score, 1),
        verdict=verdict,
        reasons=reasons,
        mint_eth=price_eth,
        floor_eth=floor,
        sale_median_eth=sale_med,
        ratio=(None if ratio is None else (round(ratio, 3) if ratio != float("inf") else "inf")),
        sales_1h=sales_1h,
        progress_pct=progress,
        start_at=start_at,
        max_per_wallet=item.get("maxPerWallet"),
        stage_label=item.get("firstStageLabel") or "",
        status=item.get("status") or "",
        deployer_projects=deployer_projects,
        deployer_age=market.deployer_age if market else None,
        twitter=item.get("xUrl") or (market.twitter if market else None),
        opensea=item.get("openseaUrl") or (market.opensea if market else None),
        explorer=item.get("explorerUrl"),
    )


def score_all(data: dict, cfg: dict) -> list[Scored]:
    """对全部 radar 项目打分，按分数降序返回。"""
    now_ms = int(time.time() * 1000)
    market_idx = build_market_index(data.get("market") or {})
    out: list[Scored] = []
    for chain, block in (data.get("radar") or {}).items():
        for item in block.get("items") or []:
            addr = (item.get("contractAddress") or "").lower()
            # 索引 key 带链，避免跨链同地址错配
            s = score_item(item, market_idx.get((chain, addr)), cfg, now_ms)
            if s is not None:
                out.append(s)
    out.sort(key=lambda x: x.score, reverse=True)
    return out
