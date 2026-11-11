"""notify 模块的自检：排版、字节上限、冷却逻辑。

用法：python -X utf8 scripts/selftest_notify.py
不需要网络，不发消息。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import notify  # noqa: E402
from score import Scored  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  ✅ {msg}")
    else:
        print(f"  ❌ {msg}")
        FAILED.append(msg)


def fake(i: int, score: float = 50.0, verdict: str = "值得打") -> Scored:
    return Scored(
        name=f"测试项目 TEST PROJECT NUMBER {i} WITH A VERY LONG NAME",
        chain="robinhood",
        contract=f"0x{i:040x}",
        score=score,
        verdict=verdict,
        reasons=["赔率 3.20x（地板 0.01 / mint 0.0031）", "1h 成交 128 笔，深度好", "creator 项目数 2，正常"],
        mint_eth=0.0031,
        floor_eth=0.00992,
        ratio=3.2,
        sales_1h=128,
        progress_pct=None,
        start_at=int(time.time() * 1000) + 3_600_000 * 5,
        max_per_wallet=3,
        stage_label="Stage 1",
        status="upcoming",
        deployer_projects=2,
        deployer_age="3 months ago",
        twitter="https://x.com/example",
        opensea="https://opensea.io/collection/example",
        explorer="https://robinhoodchain.blockscout.com/address/0xabc",
    )


def main() -> int:
    now_ms = int(time.time() * 1000)
    data = {"radar": {"robinhood": {"items": [{}] * 50}, "ethereum": {"items": [{}] * 30}}}

    print("[1] 单项目排版")
    txt = notify.render_text([fake(1)], [fake(1)], data, {}, now_ms)
    check("MintGo 打新提醒" in txt, "标题存在")
    check("3.20x" in txt, "赔率正确渲染")
    check("开抢" in txt, "时间行存在")
    check("**" not in txt and "<font" not in txt and "](http" not in txt, "没有任何 markdown 语法残留")
    check("https://opensea.io/collection/example" in txt, "链接是裸 URL（微信可点）")

    print("[1b] 每行带 emoji + 中文标签")
    for tag in ("📍 链：", "💰 价格：", "⏰ 开抢：", "📊 1h 成交：", "🎯 评分：", "🔗 项目网址："):
        check(tag in txt, f"有「{tag}」")
    check("仅供参考" not in txt and "不构成投资建议" not in txt, "没有免责声明行（按要求去掉）")

    print("[2] 多项目不超字节上限")
    many = [fake(i) for i in range(1, 31)]
    txt2 = notify.render_text(many, many, data, {}, now_ms)
    size = len(txt2.encode("utf-8"))
    check(size <= 2048, f"字节数 {size} ≤ 2048（text 上限）")
    check(size > 1500, f"确实用到了长度（{size}）")

    print("[2b] 分级降级：链接不被切成半截，也不因项目多就全丢")
    for n in (1, 3, 5, 15):
        grp = [fake(i) for i in range(1, n + 1)]
        body = notify.render_text(grp, grp, data, {}, now_ms)
        check(len(body.encode("utf-8")) <= 2048, f"{n} 个项目 {len(body.encode())} 字节 ≤ 2048")
        check(body.count("🐦 推特") >= 1, f"  {n} 个项目仍保留了完整链接")
        # 任何一条链接行都必须是完整 URL，不能被截断成半截
        bad = [
            ln for ln in body.split("\n")
            if any(ln.strip().startswith(p) for p in ("🔗 项目网址：", "🐦 推特：", "⛓ 合约地址："))
            and not ln.rstrip().endswith(("example", "0xabc", "/"))
        ]
        check(not bad, f"  {n} 个项目没有残缺链接行")
    big = [fake(i) for i in range(1, 16)]
    bigbody = notify.render_text(big, big, data, {}, now_ms)
    check("另有" in bigbody, "项目过多时给出「另有 N 个」提示")

    print("[3] 极简降级版")
    compact = notify.render_compact(many, now_ms)
    csize = len(compact.encode("utf-8"))
    check(csize <= 2048, f"降级版 {csize} 字节 ≤ 2048")
    check("\n1." in compact, "降级版仍带编号")

    print("[4] 截断不切坏多字节字符")
    truncated = notify._truncate_bytes("中文😀" * 2000, 100)
    check(len(truncated.encode("utf-8")) <= 100, "截断后字节数达标")
    check("�" not in truncated, "没有出现乱码替换符")

    print("[5] 冷却与涨分重推")
    now = time.time()
    cfg = {"notify": {"cooldown_minutes": 180, "min_score_delta": 10}}
    s = fake(7, score=50.0)
    picked, st = notify.select_candidates([s], {}, cfg, now)
    check(len(picked) == 1, "首次推送")
    picked2, st2 = notify.select_candidates([s], st, cfg, now + 60)
    check(len(picked2) == 0, "冷却期内不重推")
    picked3, st3 = notify.select_candidates([fake(7, score=65.0)], st, cfg, now + 60)
    check(len(picked3) == 1, "涨 15 分可打破冷却")
    picked4, _ = notify.select_candidates([s], st, cfg, now + 181 * 60)
    check(len(picked4) == 1, "冷却期过后可重推")

    print("[6] 非「值得打」不入选")
    picked5, _ = notify.select_candidates([fake(9, verdict="观察")], {}, cfg, now)
    check(len(picked5) == 0, "观察档被过滤")

    print("[6b] 多链都要显示出来（防止「只有 RH」错觉复发）")
    # 造 3 条链，Robinhood 数量远多于其他链——旧逻辑会让 RH 吃光名额
    mixed = []
    for i in range(40):
        s = fake(i + 1)
        s.chain = "robinhood"
        s.verdict = "无地板·待看"
        s.floor_eth = None
        s.ratio = None
        mixed.append(s)
    for i, ch in enumerate(["ethereum", "base", "ink"], start=100):
        s = fake(i)
        s.chain = ch
        s.verdict = "无地板·待看"
        s.floor_eth = None
        s.ratio = None
        s.name = f"{ch} 上的项目"
        mixed.append(s)

    body_mixed = notify.render_text([], mixed, data, {}, now_ms)
    check(len(body_mixed.encode("utf-8")) <= 2048, f"混合数据 {len(body_mixed.encode())} 字节 ≤ 2048")
    check("Ethereum" in body_mixed, "Ethereum 出现在推送里")
    check("Base" in body_mixed, "Base 出现在推送里")
    check("Ink" in body_mixed, "Ink 出现在推送里")
    check("Robinhood" in body_mixed, "Robinhood 也在")
    # 每条链至少要有一条具体项目，而不只是标题
    for ch_label, probe in (("Ethereum", "ethereum 上的项目"), ("Base", "base 上的项目"), ("Ink", "ink 上的项目")):
        check(probe in body_mixed, f"{ch_label} 至少列出一个具体项目")

    print("[6c] 无地板列表不混入垃圾名")
    junk = fake(999)
    junk.name = "..."
    junk.verdict = "无地板·待看"
    junk.floor_eth = None
    junk.ratio = None
    body_junk = notify.render_text([], [junk] + mixed, data, {}, now_ms)
    check("· ..." not in body_junk, "名字为 '...' 的垃圾项被过滤")

    print("[7] 空理由/缺字段不崩")
    bare = Scored(
        name="Bare", chain="base", contract="0xdead", score=45.0, verdict="值得打",
        reasons=[], mint_eth=0.0, floor_eth=None, ratio=None, sales_1h=0,
        progress_pct=None, start_at=None, max_per_wallet=None, stage_label="", status="",
        deployer_projects=None, deployer_age=None, twitter=None, opensea=None, explorer=None,
    )
    txt3 = notify.render_text([bare], [bare], {}, {}, now_ms)
    check("Bare" in txt3, "极简数据也能渲染")
    check("时间待定" in txt3, "无时间时给出兜底文案")

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
