"""通知预览 / dry-run 工具。

用法：
  python -X utf8 scripts/preview_notify.py            # 用最近一次落盘数据渲染，不发送
  python -X utf8 scripts/preview_notify.py --send     # 真发一条到企业微信
  python -X utf8 scripts/preview_notify.py --all      # 连「观察」档也带上（试排版用）

注意：不加 --send 时完全不碰网络，也不会改 notify_state.json。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config_loader  # noqa: E402
import notify  # noqa: E402
from score import Scored  # noqa: E402


def load_scored() -> list[Scored]:
    raw = json.loads((ROOT / "data" / "latest_scored.json").read_text(encoding="utf-8"))
    return [Scored(**r) for r in raw]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="真的推送到企业微信")
    ap.add_argument("--all", action="store_true", help="包含「观察」档，便于检查排版")
    ap.add_argument("--compact", action="store_true", help="渲染极简降级版")
    args = ap.parse_args()

    cfg = config_loader.load(ROOT / "config.yaml")
    items = load_scored()
    raw = json.loads((ROOT / "data" / "latest_raw.json").read_text(encoding="utf-8"))
    data = {"radar": raw.get("radar") or {}}

    picked = [s for s in items if s.verdict == "值得打"]
    if args.all:
        picked += [s for s in items if s.verdict == "观察"][:3]

    # 没有「值得打」时，也展示「无地板·待看」名单（与线上推送逻辑一致）
    nofloor = [s for s in items if s.verdict == "无地板·待看"]
    if not picked and nofloor:
        picked = []  # render_text 会自己处理 nofloor 部分

    if not picked and not nofloor:
        print("当前数据里没有可推送的项目（值得打 0 个，无地板待看 0 个）。")
        return 0

    now_ms = int(time.time() * 1000)
    content = (
        notify.render_compact(picked, now_ms)
        if args.compact
        else notify.render_text(picked, items, data, cfg, now_ms)
    )
    print(content)
    print()
    print("=" * 60)
    print(
        f"项目数 {len(picked)} · {len(content)} 字符 · "
        f"{len(content.encode('utf-8'))} 字节（text 上限 2048）"
    )

    if args.send:
        webhook = ((cfg.get("notify") or {}).get("webhook") or "").strip()
        if not webhook:
            print("!! config.yaml 里 notify.webhook 为空")
            return 1
        resp = notify.send_text(webhook, content)
        print("发送结果:", resp)
        return 0 if resp.get("errcode") == 0 else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
