"""BAW 扫码登录助手 —— 自动续期二维码，直到登录成功。

为什么要这个：`baw auth signin` 出的二维码只有约 5 分钟有效期。
如果靠人手把链接转给别人再回来扫码，经常还没来得及扫就过期了
（实测连续两次 AUTH_REJECTED）。

这个脚本做三件事：
  1. 调 `baw auth signin --json` 拿 urlForWeb
  2. 把它渲染成二维码图片，固定写到 data/wallet_qr.png
  3. 调 `baw auth verify` 阻塞等待扫码；过期了自动重新出码，
     图片原地覆盖 —— 你只需要盯着一张图，什么时候扫都行

用法：
  python -X utf8 scripts/baw_login.py                # 默认守 20 分钟
  python -X utf8 scripts/baw_login.py --minutes 30

完成后：
  data/wallet_qr.png   二维码图片（登录成功后不再更新）
  logs/baw_login.log   过程日志，含每轮链接和过期时间
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QR_PATH = ROOT / "data" / "wallet_qr.png"
LOG_PATH = ROOT / "logs" / "baw_login.log"
PROXY = os.environ.get("MINTGO_PROXY") or "http://127.0.0.1:7990"
CST = timezone(timedelta(hours=8))


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def env() -> dict:
    e = dict(os.environ)
    e["NODE_USE_ENV_PROXY"] = "1"
    if PROXY:
        e["HTTPS_PROXY"] = PROXY
        e["HTTP_PROXY"] = PROXY
        e["ALL_PROXY"] = PROXY
    return e


def baw_exe() -> str | None:
    return shutil.which("baw") or shutil.which("baw.cmd")


def run(args: list[str], timeout: int = 60) -> tuple[int, str]:
    exe = baw_exe()
    if not exe:
        return 127, "找不到 baw"
    try:
        p = subprocess.run(
            [exe, *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env(), timeout=timeout,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as e:  # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


def make_qr(url: str) -> bool:
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M

        qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M,
                           box_size=10, border=3)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        QR_PATH.parent.mkdir(parents=True, exist_ok=True)
        img.save(QR_PATH)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"⚠️ 生成二维码图片失败：{e}（仍然可以用链接扫码）")
        return False


def check_logged_in() -> bool:
    code, out = run(["wallet", "status", "--json"], timeout=40)
    try:
        return json.loads(out).get("data", {}).get("status") == "CONNECTED"
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=20, help="总共守多久（分钟）")
    args = ap.parse_args()
    deadline = time.time() + args.minutes * 60

    log("=" * 56)
    log(f"开始登录助手，最长守 {args.minutes} 分钟")
    log(f"二维码会写到：{QR_PATH}")
    log("每轮过期后自动换新码，图片原地覆盖 —— 盯住这一张图扫就行")
    log("=" * 56)

    round_no = 0
    while time.time() < deadline:
        round_no += 1

        if check_logged_in():
            log("✅ 检测到已登录")
            return 0

        code, out = run(["auth", "signin", "--json"], timeout=60)
        try:
            j = json.loads(out[out.index("{"):])
        except Exception:
            log(f"第 {round_no} 轮：signin 解析失败 → {out[:200]}")
            time.sleep(10)
            continue

        if not j.get("success"):
            log(f"第 {round_no} 轮：signin 失败 → {out[:200]}")
            time.sleep(15)
            continue

        d = j["data"]
        url = d.get("urlForWeb", "")
        qid = d.get("qrCodeId", "")
        pair = d.get("pairingCode", "")
        try:
            exp_dt = datetime.fromtimestamp(int(d["expireAt"]) / 1000, CST)
            exp = exp_dt.strftime("%H:%M:%S")
        except Exception:
            exp = "?"

        ok = make_qr(url)
        log(f"第 {round_no} 轮出码：配对码 {pair} ｜ 过期 {exp} ｜ 图片 {'已更新' if ok else '失败'}")
        log(f"          链接：{url}")

        # 阻塞等扫码；过期会返回 AUTH_REJECTED，外层继续下一轮
        vcode, vout = run(["auth", "verify", "--qrCodeId", qid, "--json"], timeout=400)
        try:
            vj = json.loads(vout[vout.index("{"):])
        except Exception:
            vj = {}

        if vj.get("success"):
            log("✅ 扫码成功，登录完成")
            if check_logged_in():
                log("✅ 已确认 CONNECTED")
                return 0

        err = (vj.get("error") or {}).get("name", "")
        log(f"第 {round_no} 轮未完成：{err or vout[:120]}")

        if time.time() >= deadline:
            break
        time.sleep(2)

    log(f"⏰ 守了 {args.minutes} 分钟仍未登录成功，退出。可以重新运行本脚本。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
