"""OpenSea API 工具：自动申请 / 保存 / 验证即时 API key。

背景：OpenSea 提供「Instant API key (for agents)」—— 一个 POST 就能拿免费 key，
不需要注册、不需要钱包、不需要人工审核。有效期 7 天，过期后自动重新申请即可。

这个模块负责：
  · 申请新 key（POST /api/v2/auth/keys）
  · 存到 data/opensea_key.json（已 gitignore）
  · 检查是否快过期，快过期就自动换新
  · 提供带 X-API-KEY 的请求 helper

用法：
  python -X utf8 scripts/opensea_key.py ensure     # 确保有可用 key（快过期就换新）
  python -X utf8 scripts/opensea_key.py show       # 看当前 key 状态
  python -X utf8 scripts/opensea_key.py renew      # 强制换新
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY_FILE = ROOT / "data" / "opensea_key.json"
API = "https://api.opensea.io/api/v2"
# 距离过期不足这个时长就换新（留出调试余量）
RENEW_BEFORE = timedelta(hours=12)

# OpenSea 走 Cloudflare，不带浏览器 UA 会直接 403 Error 1010
# （"blocked access based on your browser's signature"）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _headers(key: str | None = None) -> dict:
    h = {
        "Accept": "application/json",
        "User-Agent": UA,
        "Origin": "https://opensea.io",
        "Referer": "https://opensea.io/",
    }
    if key:
        h["X-API-KEY"] = key
    return h


def _now() -> datetime:
    return datetime.now(timezone.utc)


def request_key() -> dict:
    """POST /api/v2/auth/keys 拿一个免费 key。"""
    req = urllib.request.Request(
        f"{API}/auth/keys",
        data=b"",
        headers=_headers(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def load() -> dict | None:
    try:
        return json.loads(KEY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def save(data: dict) -> None:
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _expires_at(rec: dict) -> datetime | None:
    raw = rec.get("expires_at")
    if not raw:
        return None
    try:
        # 形如 2026-11-18T13:51:18.899268423Z —— 截到微秒精度再解析
        s = raw.replace("Z", "+00:00")
        if "." in s:
            head, _, tail = s.partition(".")
            frac = "".join(ch for ch in tail if ch.isdigit())[:6]
            tz = "+00:00" if tail.endswith("Z") or "+" not in tail else tail[tail.index("+"):]
            s = f"{head}.{frac}{tz}" if frac else f"{head}{tz}"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def is_usable(rec: dict | None) -> bool:
    if not rec or not rec.get("api_key"):
        return False
    exp = _expires_at(rec)
    if exp is None:
        return True
    return exp - _now() > RENEW_BEFORE


def ensure(verbose: bool = True) -> str:
    """拿到一个可用 key；需要时自动申请。返回 api_key。"""
    rec = load()
    if is_usable(rec):
        if verbose:
            exp = _expires_at(rec)
            left = (exp - _now()) if exp else None
            print(f"沿用现有 key（{rec.get('name')}）"
                  + (f"，还剩 {left.total_seconds()/3600:.1f} 小时" if left else ""))
        return rec["api_key"]

    if verbose:
        print("没有可用 key 或即将过期，正在申请新的…")
    fresh = request_key()
    save(fresh)
    if verbose:
        print(f"已申请：{fresh.get('name')}  有效期至 {fresh.get('expires_at')}")
    return fresh["api_key"]


def api_get(path: str, key: str | None = None, timeout: int = 30) -> tuple[int, dict | list | str]:
    """带认证的 GET。path 形如 /chains 或 /collections/xxx。"""
    k = key or ensure(verbose=False)
    req = urllib.request.Request(f"{API}{path}", headers=_headers(k), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            try:
                return r.status, json.loads(body)
            except json.JSONDecodeError:
                return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:400]
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"

    if cmd == "ensure":
        ensure()
        return 0

    if cmd == "renew":
        rec = request_key()
        save(rec)
        print(f"已换新：{rec.get('name')}  有效期至 {rec.get('expires_at')}")
        return 0

    # show
    rec = load()
    if not rec:
        print("当前没有 key。运行 ensure 获取。")
        return 1
    exp = _expires_at(rec)
    print(f"name     : {rec.get('name')}")
    print(f"key      : {rec.get('api_key')[:8]}…（已隐藏，共 {len(rec.get('api_key',''))} 字符）")
    print(f"expires  : {rec.get('expires_at')}")
    if exp:
        left = exp - _now()
        print(f"剩余     : {left.total_seconds()/3600:.1f} 小时")
    print(f"ratelim  : {rec.get('rate_limits')}")
    print(f"可用     : {is_usable(rec)}")

    print()
    print("=== 连通性测试：GET /chains ===")
    code, data = api_get("/chains")
    print(f"HTTP {code}")
    if isinstance(data, dict):
        chains = data.get("chains") or []
        print(f"支持 {len(chains)} 条链")
        for c in chains:
            if "robinhood" in json.dumps(c).lower():
                print(f"  ✅ Robinhood: {c}")
    else:
        print(str(data)[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())
