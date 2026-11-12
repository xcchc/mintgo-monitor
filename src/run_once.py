"""单次完整流程：抓取 → 打分 → 报告 → 落盘。

退出码：
  0  正常
  1  抓取失败
  2  抓取成功但零项目（多半是会话/代理问题）
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config_loader  # noqa: E402
from fetch_radar import collect  # noqa: E402
from score import score_all  # noqa: E402
from report import render  # noqa: E402
from notify import push  # noqa: E402


def apply_demand_filter(scored: list, cfg: dict, log) -> int:
    """用真实成交价 + 买单数复核「值得打 / 观察」，不达标就降级成跳过。

    为什么要有这一层：挂单地板只是**卖方要价**。实测 mihaoo 地板 0.000396，
    但 35 笔真实成交全在 0.000097~0.0001（等于 mint 价），买单 0 个 ——
    按挂单价算出的 3.96x 赔率没有任何人付过钱。

    只对已判为 值得打/观察 的少数项目发请求（每轮 0~4 个），不会拖慢抓取。
    """
    filters = cfg.get("filters") or {}
    if not filters.get("demand_check_report", True):
        return 0

    sys.path.insert(0, str(ROOT / "scripts"))
    from auto_mint import premium_gate, slug_from_scored  # noqa: PLC0415
    from opensea_key import ensure  # noqa: PLC0415

    targets = [s for s in scored if s.verdict in ("值得打", "观察")]
    if not targets:
        return 0

    key = ensure(verbose=False)
    downgraded = 0
    for s in targets:
        slug = slug_from_scored(s.as_dict())
        if not slug:
            continue
        try:
            ok, why, info = premium_gate(slug, s.mint_eth, cfg, key)
        except Exception as e:  # noqa: BLE001
            log(f"  需求复核失败（{s.name}）：{e}")
            continue
        s.reasons.append(f"需求复核：{why}")
        s.demand = info
        if not ok:
            s.verdict = "跳过"
            downgraded += 1
            log(f"  降级为跳过：{s.name} —— {why}")
    return downgraded


def main() -> int:
    cfg_path = ROOT / "config.yaml"
    if not cfg_path.exists():
        print("未找到 config.yaml —— 请先复制配置模板：")
        print("  Windows: copy config.example.yaml config.yaml")
        print("  Linux  : cp config.example.yaml config.yaml")
        return 1

    # 首次运行时目录可能还不存在，先建好，避免写入阶段才报错
    for rel in ("logs", "data/snapshots", "data/reports"):
        (ROOT / rel).mkdir(parents=True, exist_ok=True)

    cfg = config_loader.load(cfg_path)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = ROOT / "logs" / f"{stamp}.log"

    def log(msg: str) -> None:
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        print(line)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    t0 = time.time()
    log("开始抓取 …")
    try:
        data = collect(cfg)
    except Exception:
        log("抓取失败：\n" + traceback.format_exc())
        return 1

    total = sum(len(b.get("items") or []) for b in (data.get("radar") or {}).values())
    log(f"抓取完成：{total} 个项目，耗时 {time.time()-t0:.1f}s")
    for err in data.get("errors") or []:
        log(f"  告警: {err}")

    if total == 0:
        log("零项目，疑似会话或代理异常，放弃本轮")
        (ROOT / "data" / "snapshots" / f"{stamp}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return 2

    scored = score_all(data, cfg)
    log(f"打分完成：{len(scored)} 个进入时间窗")

    # 真实需求复核：成交中位价 + 买单数，把「挂单赔率好看但没人买」的降级
    downgraded = apply_demand_filter(scored, cfg, log)
    if downgraded:
        log(f"需求复核降级 {downgraded} 个（挂单价不等于成交价）")

    md = render(scored, data, cfg)

    # 落盘
    snap_path = ROOT / "data" / "snapshots" / f"{stamp}.json"
    raw_path = ROOT / "data" / "latest_raw.json"
    rep_path = ROOT / "data" / "reports" / f"{stamp}.md"
    latest_md = ROOT / "data" / "latest.md"
    scored_path = ROOT / "data" / "latest_scored.json"

    payload = {
        "collectedAt": data.get("collectedAt"),
        "radar": {c: {"meta": b.get("meta"), "items": b.get("items")} for c, b in (data.get("radar") or {}).items()},
        "market": data.get("market"),
        "errors": data.get("errors"),
    }
    snap_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    rep_path.write_text(md, encoding="utf-8")
    latest_md.write_text(md, encoding="utf-8")
    scored_path.write_text(
        json.dumps([s.as_dict() for s in scored], ensure_ascii=False, indent=1), encoding="utf-8"
    )

    good = [s for s in scored if s.verdict == "值得打"]
    log(f"报告已生成：{rep_path}")
    if good:
        log(f"🟢 值得打 {len(good)} 个：" + "、".join(s.name for s in good[:10]))
    else:
        log("🟢 本轮无「值得打」项目")

    # 企业微信推送（旁路，失败不影响本轮结果）
    push(scored, data, cfg, ROOT, log=log)

    log(f"总耗时 {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
