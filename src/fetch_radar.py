"""抓取 mintgo.fun 的 radar（upcoming mints）与 market snapshot。

为什么必须用浏览器：
  /api/view/* 校验 same-origin realtime client，curl 带 cookie 也返回
  403 "Same-origin realtime view required"；client id 还必须取自页面
  sessionStorage['mintgo-realtime-client-id']。所以这里用 Playwright 打开
  真实页面，再在页面内 fetch 同源接口。

另外：本站对默认 headless UA 直接 403 "403 Forbidden / nginx/1.24.0"，
所以必须伪装成真实 Chrome UA。
"""
from __future__ import annotations

import json
import time
from typing import Any

from playwright.sync_api import sync_playwright

PAGE_URL = "https://mintgo.fun/"

# 页面里执行的抓取脚本：先翻完 radar（走 nextCursor），再拉各链 market snapshot
JS_COLLECT = r"""
async (opts) => {
  const { chains, maxPages } = opts;
  const out = { radar: {}, market: {}, client: '', errors: [] };

  const cid = sessionStorage.getItem('mintgo-realtime-client-id');
  out.client = cid || '';

  const jget = async (url) => {
    const r = await fetch(url, { credentials: 'same-origin', cache: 'no-store' });
    const text = await r.text();
    if (!r.ok) throw new Error('HTTP ' + r.status + ': ' + text.slice(0, 200));
    return JSON.parse(text);
  };

  for (const chain of chains) {
    let cursor = null;
    const items = [];
    let meta = null;
    for (let p = 0; p < maxPages; p++) {
      const q = new URLSearchParams({ client: cid, chain, limit: '24' });
      if (cursor) q.set('cursor', cursor);
      let j;
      try {
        j = await jget('/api/view/radar?' + q.toString());
      } catch (e) {
        out.errors.push(chain + ' radar page ' + p + ': ' + String(e).slice(0, 160));
        break;
      }
      if (!meta) meta = { chainName: j.chainName, fetchedAt: j.fetchedAt, updatedAt: j.updatedAt, latestBlock: j.latestBlock };
      const pageItems = j.items || [];
      items.push(...pageItems);
      const page = j.page || {};
      if (!page.hasMore || !page.nextCursor) break;
      cursor = page.nextCursor;
      if (pageItems.length === 0) break;
    }
    out.radar[chain] = { meta, items };
  }

  for (const chain of chains) {
    try {
      const j = await jget('/api/market-snapshot?chain=' + encodeURIComponent(chain));
      out.market[chain] = j;
    } catch (e) {
      out.errors.push(chain + ' market: ' + String(e).slice(0, 160));
      out.market[chain] = null;
    }
  }
  return out;
}
"""


def collect(cfg: dict) -> dict[str, Any]:
    """启动无头 Chrome，抓取全部数据并返回原始 dict。"""
    proxy = (cfg.get("proxy") or "").strip()
    chains = cfg.get("chains") or ["robinhood", "ethereum", "ink", "base"]
    max_pages = int(cfg.get("max_pages") or 12)

    args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
    if proxy:
        args.append(f"--proxy-server={proxy}")

    launch_kwargs: dict[str, Any] = {
        "headless": True,
        "args": args,
    }
    chrome_path = (cfg.get("chrome_path") or "").strip()
    if chrome_path:
        launch_kwargs["executable_path"] = chrome_path

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        ctx = browser.new_context(
            viewport={"width": 1600, "height": 1000},
            locale="en-US",
            timezone_id="America/New_York",
            user_agent=cfg.get("user_agent") or None,
        )
        page = ctx.new_page()
        try:
            page.goto(PAGE_URL, timeout=90_000, wait_until="domcontentloaded")
            # 等 realtime client id 落地 + 会话建立
            page.wait_for_function(
                "() => !!sessionStorage.getItem('mintgo-realtime-client-id')",
                timeout=30_000,
            )
            time.sleep(3)
            data = page.evaluate(JS_COLLECT, {"chains": chains, "maxPages": max_pages})
        finally:
            browser.close()

    data["collectedAt"] = int(time.time() * 1000)
    return data


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config_loader

    cfg = config_loader.load(Path(__file__).resolve().parent.parent / "config.yaml")
    result = collect(cfg)
    print(json.dumps({k: (v if k != "radar" else {c: len(x["items"]) for c, x in v.items()})
                      for k, v in result.items() if k != "market"}, ensure_ascii=False, indent=1))
