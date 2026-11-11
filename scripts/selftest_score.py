"""评分模块自检：分档规则、无地板过滤、配置读取。

用法：python -X utf8 scripts/selftest_score.py
不需要网络。

重点盯三件事：
  1. 默认策略 require_floor_data=true：无地板项目一律跳过（只发布有地板价格的）
  2. 关掉该开关后：无地板 + 免费 → 待看；无地板 + 付费 → 跳过（不能盲付）
  3. 配置里的阈值必须真的被读到（历史 bug：filters 块从来没生效过）
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config_loader  # noqa: E402
from score import Scored, _cfg_val, build_market_index, score_all, score_item  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  ✅ {msg}")
    else:
        print(f"  ❌ {msg}")
        FAILED.append(msg)


def item(name: str, price: float, chain: str = "robinhood", contract: str = "0xabc") -> dict:
    """造一个 radar item。startAt 设成 1 小时后，保证落在时间窗内。"""
    return {
        "displayName": name,
        "chain": chain,
        "contractAddress": contract,
        "priceEth": str(price),
        "startAt": int(time.time() * 1000) + 3_600_000,
        "endAt": int(time.time() * 1000) + 7_200_000,
        "maxPerWallet": 5,
        "firstStageLabel": "Public",
        "status": "upcoming",
        "soldOut": False,
    }


def main() -> int:
    now_ms = int(time.time() * 1000)
    cfg = config_loader.load(ROOT / "config.yaml")

    print("[1] 配置里的阈值真的被读到")
    for key, expect in (
        ("min_floor_ratio", 1.0),
        ("min_sales_1h", 5),
        ("late_progress_pct", 95.0),
        ("nofloor_max_price_eth", 0),
    ):
        got = _cfg_val(cfg, key, "MISSING")
        check(got != "MISSING", f"{key} 能读到（{got}）")
        check(float(got) == float(expect), f"  {key} = {expect}")

    print("[1b] _cfg_val 的优先级：filters 块 > 顶层 > 默认值")
    check(_cfg_val({"filters": {"k": 7}, "k": 9}, "k", 0) == 7, "filters 块优先")
    check(_cfg_val({"k": 9}, "k", 0) == 9, "没有 filters 时读顶层")
    check(_cfg_val({}, "k", 42) == 42, "都没有时用默认值")

    print("[2] 默认策略：只发布有地板数据的项目")
    s = score_item(item("Free NoFloor", 0), None, cfg, now_ms)
    check(s is not None, "能打分")
    # require_floor_data 默认 true，所以无地板一律跳过（即使免费）
    require_floor = bool(_cfg_val(cfg, "require_floor_data", True))
    if require_floor:
        check(s.verdict == "跳过", f"无地板+免费 在严格模式下被判为「{s.verdict}」")
        check(any("无地板价" in r for r in s.reasons), "理由里写清了「无地板价」")
    else:
        check(s.verdict == "无地板·待看", f"宽松模式下被判为「{s.verdict}」")

    print("[2b] 严格模式下付费无地板也跳过")
    s2 = score_item(item("Paid NoFloor", 0.004), None, cfg, now_ms)
    check(s2.verdict == "跳过", f"被判为「{s2.verdict}」")

    print("[2c] 把 require_floor_data 关掉，恢复「无地板·待看」（且只收免费）")
    loose = dict(cfg)
    loose["filters"] = dict(cfg.get("filters") or {})
    loose["filters"]["require_floor_data"] = False
    s6 = score_item(item("Free NoFloor", 0), None, loose, now_ms)
    check(s6.verdict == "无地板·待看", "免费无地板 → 无地板·待看")
    s7 = score_item(item("Paid NoFloor", 0.004), None, loose, now_ms)
    check(s7.verdict == "跳过", "付费无地板 → 仍跳过")
    check(any("要付费" in r for r in s7.reasons), "理由里写清了「无地板且要付费」")

    print("[3] 阈值可放宽：nofloor_max_price_eth 抬高后低价项目能进来")
    loose2 = dict(loose)
    loose2["filters"] = dict(loose["filters"])
    loose2["filters"]["nofloor_max_price_eth"] = 0.001
    s3 = score_item(item("Cheap NoFloor", 0.0005), None, loose2, now_ms)
    check(s3.verdict == "无地板·待看", "0.0005 ETH 在阈值 0.001 下被保留")
    s4 = score_item(item("Still Expensive", 0.01), None, loose2, now_ms)
    check(s4.verdict == "跳过", "0.01 ETH 仍被排除")

    print("[5] 有地板 + 免费 → 走赔率逻辑（不被无地板分支截走）")
    # 注意两件事：
    #  1. windowTxCount 要放在 "1h" 窗口：MarketRow.sales_1h 读的是 tx["1h"]
    #  2. 必须同时给 windowVolumeEth —— 只有笔数没有成交量会被刷量规则拦掉
    market = {
        "robinhood": {
            "runners": {
                "1d": [
                    {
                        "contractAddress": "0xfree",
                        "name": "Free WithFloor",
                        "floorPriceEth": "0.002",
                        "windowTxCount": 30,
                        "windowVolumeEth": "0.06",
                        "deployerNftProjectCount": 1,
                    }
                ],
                "1h": [
                    {
                        "contractAddress": "0xfree",
                        "name": "Free WithFloor",
                        "windowTxCount": 30,
                        "windowVolumeEth": "0.06",
                    }
                ],
            }
        }
    }
    idx = build_market_index(market)
    check(("robinhood", "0xfree") in idx, "索引 key 是 (链, 地址)")

    it = item("Free WithFloor", 0, contract="0xfree")
    s5 = score_item(it, idx[("robinhood", "0xfree")], cfg, now_ms)
    check(s5.verdict == "值得打", f"免费+有地板+深度够 → 「{s5.verdict}」")
    check(s5.ratio == "inf", "赔率标记为 inf（免费）")

    print("[6] 跨链同地址不串味（索引 bug 回归测试）")
    cross = {
        "ethereum": {"runners": {"1d": [{"contractAddress": "0xsame", "floorPriceEth": "0.5", "windowTxCount": 50}]}},
        "base": {"runners": {"1d": [{"contractAddress": "0xsame", "floorPriceEth": "0.0001", "windowTxCount": 50}]}},
    }
    cidx = build_market_index(cross)
    check(len(cidx) == 2, "两个链的同名地址存成两条，不互相覆盖")
    check(cidx[("ethereum", "0xsame")].floor == 0.5, "Ethereum 的地板是 0.5")
    check(cidx[("base", "0xsame")].floor == 0.0001, "Base 的地板是 0.0001，没被污染")

    print("[7] 严格模式：全量跑一遍，所有项目要么有地板、要么被跳过")
    data = {
        "radar": {
            "robinhood": {
                "items": [
                    item("F1", 0, contract="0x1"),
                    item("P1", 0.01, contract="0x2"),
                    item("F2", 0, contract="0x3"),
                ]
            }
        },
        "market": {},
    }
    out = score_all(data, cfg)
    nf_strict = [s for s in out if s.verdict == "无地板·待看"]
    check(len(nf_strict) == 0, f"严格模式下没有任何「无地板·待看」（实际 {len(nf_strict)}）")
    check(all(s.verdict == "跳过" for s in out), "全部无地板 → 全部跳过")

    print("[7b] 宽松模式：只保留免费的无地板项目")
    out2 = score_all(data, loose)
    nf2 = [s for s in out2 if s.verdict == "无地板·待看"]
    check(len(nf2) == 2, f"3 个项目里 2 个免费进待看（实际 {len(nf2)}）")
    check(all(s.mint_eth == 0 for s in nf2), "待看档里没有任何付费项目")
    skipped_paid = [s for s in out2 if s.verdict == "跳过" and s.mint_eth > 0]
    check(len(skipped_paid) == 1, "付费那个进了跳过")

    print("[8] 有地板数据的项目不会被无地板规则误伤")
    # 造一个「有地板但深度不足」的：应该进「跳过」，理由里不该出现「无地板价」
    thin = {
        "robinhood": {
            "runners": {
                "1d": [{"contractAddress": "0xthin", "floorPriceEth": "0.001", "windowTxCount": 1}],
                "1h": [{"contractAddress": "0xthin", "windowTxCount": 1}],
            }
        }
    }
    tidx = build_market_index(thin)
    st = score_item(item("Thin Floor", 0, contract="0xthin"), tidx[("robinhood", "0xthin")], cfg, now_ms)
    check(st.floor_eth is not None, "确实拿到了地板")
    check(
        not any("无地板价" in r for r in st.reasons),
        "有地板时不该出现「无地板价」理由",
    )

    print("[9] 刷量识别：笔数多但成交量太小 → 必须拦掉")
    # 复刻 ASCIHOOD 的真实形态：359 笔 / 0.000275 ETH / 地板 9.6e-05
    wash = {
        "robinhood": {
            "runners": {
                "1d": [{"contractAddress": "0xwash", "floorPriceEth": "0.000096",
                        "windowTxCount": 2217, "windowVolumeEth": "0.001337"}],
                "1h": [{"contractAddress": "0xwash", "windowTxCount": 359,
                        "windowVolumeEth": "0.000275"}],
            }
        }
    }
    widx = build_market_index(wash)
    wrow = widx[("robinhood", "0xwash")]
    check(wrow.sales_1h == 359, f"笔数读到 359（实际 {wrow.sales_1h}）")
    check(abs(wrow.volume_1h - 0.000275) < 1e-12, "成交量读到 0.000275 ETH")

    ws = score_item(item("Wash", 0, contract="0xwash"), wrow, cfg, now_ms)
    check(ws.verdict == "跳过", f"刷量项目被判为「{ws.verdict}」")
    check(any("疑似刷量" in r for r in ws.reasons), "理由里点明「疑似刷量」")

    print("[9b] 正常项目不受影响：笔数少但成交量大 → 通过")
    healthy = {
        "robinhood": {
            "runners": {
                "1d": [{"contractAddress": "0xgood", "floorPriceEth": "0.005",
                        "windowTxCount": 48, "windowVolumeEth": "0.01887",
                        "deployerNftProjectCount": 2}],
                "1h": [{"contractAddress": "0xgood", "windowTxCount": 48,
                        "windowVolumeEth": "0.01887"}],
            }
        }
    }
    hidx = build_market_index(healthy)
    hrow = hidx[("robinhood", "0xgood")]
    check(hrow.volume_1h > 0.001, f"成交量 {hrow.volume_1h} ETH 超过门槛")
    hs = score_item(item("Good", 0.001, contract="0xgood"), hrow, cfg, now_ms)
    check(hs.verdict != "跳过" or not any("刷量" in r for r in hs.reasons),
          f"正常项目未被误判为刷量（判定 {hs.verdict}）")

    print("[9c] 门槛可调：设 0 就关闭这项检查")
    off = dict(cfg)
    off["filters"] = dict(cfg.get("filters") or {})
    off["filters"]["min_volume_1h_eth"] = 0
    ws2 = score_item(item("Wash", 0, contract="0xwash"), wrow, off, now_ms)
    check(not any("疑似刷量" in r for r in ws2.reasons), "门槛设 0 后不再判刷量")

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for f in FAILED:
            print("  -", f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
