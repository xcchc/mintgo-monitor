"""自动挂单卖出 —— 把 burner 钱包里的 NFT 挂到 OpenSea。

完整链路（全部由 OpenSea 官方接口驱动，不逆向任何合约）：

  ① POST /api/v2/listings/actions
       返回两个 step：
         step1 itemApprovalAction.transaction    → setApprovalForAll 交易
         step2 createListingsAction.signatureRequest.message → Seaport EIP-712
  ② burner 钱包发送 step1 的交易（要 gas）
  ③ burner 钱包对 step2 的 typed data 签名
  ④ POST /api/v2/orders/robinhood/seaport/listings
       body { parameters, protocol_address, signature } → 挂单成功

为什么必须用 burner 钱包：
  币安 BAW 的 `sign-message` 不支持 Seaport 的 OrderComponents 类型
  （实测返回 953004 "We currently don't support signing this type of message"），
  所以只有自己持私钥的钱包才能签卖单。

用法：
  python -X utf8 scripts/auto_list.py              # 预检（默认，不广播）
  python -X utf8 scripts/auto_list.py --live       # 真实挂单

前提：
  · burner 钱包里有 NFT
  · burner 钱包有少量 ETH 付 approval 的 gas（约 0.00005 ETH/笔）
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from auto_mint import os_get, os_post, load_json, save_json  # noqa: E402
from burner_wallet import _acct  # noqa: E402
from opensea_key import UA, ensure as ensure_key  # noqa: E402

RPC = "https://rpc.mainnet.chain.robinhood.com"
SEAPORT_PROTOCOL = "seaport"
CHAIN = "robinhood"
CHAIN_ID = 4663
LIST_LOG = ROOT / "data" / "list_log.json"
CST = timezone(timedelta(hours=8))

ZERO = "0x0000000000000000000000000000000000000000"


def log(msg: str) -> None:
    print(f"[{datetime.now(CST):%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- RPC


def rpc(method: str, params: list, timeout: int = 30):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        RPC, data=body,
        headers={"Content-Type": "application/json", "User-Agent": UA,
                 "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read().decode())
    if "error" in j:
        raise RuntimeError(f"RPC {method} 失败: {j['error']}")
    return j.get("result")


def send_tx(acct, to: str, data: str, value: int = 0, live: bool = False) -> str | None:
    """用 burner 钱包发一笔交易。live=False 时只打印不广播。"""
    # eth-account 校验 to 的 EIP-55 校验和，OpenSea 返回的是全小写地址，必须先规范化
    from eth_utils import to_checksum_address

    to = to_checksum_address(to)
    if data and not data.startswith("0x"):
        data = "0x" + data

    if not live:
        log(f"      [dry-run] 将发送交易 to={to[:12]}… value={value} data={data[:20]}…")
        return None

    nonce = int(rpc("eth_getTransactionCount", [acct.address, "pending"]), 16)
    gas_price = int(rpc("eth_gasPrice", []), 16)
    # 预估 gas，失败就退到保守值
    try:
        est = rpc("eth_estimateGas", [{"from": acct.address, "to": to,
                                       "data": data, "value": hex(value)}])
        gas = int(int(est, 16) * 1.25)
    except Exception:  # noqa: BLE001
        gas = 120_000

    tx = {
        "nonce": nonce,
        "to": to,
        "value": value,
        "data": data,
        "gas": gas,
        "maxFeePerGas": int(gas_price * 1.5),
        "maxPriorityFeePerGas": gas_price,
        "chainId": CHAIN_ID,
        "type": 2,
    }
    signed = acct.sign_transaction(tx)
    raw = signed.raw_transaction.hex()
    if not raw.startswith("0x"):
        raw = "0x" + raw
    h = rpc("eth_sendRawTransaction", [raw])
    log(f"      已广播 txHash={h}")
    return h


# ---------------------------------------------------------------- 业务


def owned_nfts(address: str) -> list[dict]:
    """burner 钱包持有的 NFT。"""
    code, data = os_get(f"/chain/{CHAIN}/account/{address}/nfts?limit=50", ensure_key(verbose=False))
    if code != 200 or not isinstance(data, dict):
        log(f"  查 NFT 失败 HTTP {code}: {str(data)[:200]}")
        return []
    return data.get("nfts") or []


def floor_price(slug: str) -> float | None:
    """collection 当前地板价（ETH）。"""
    key = ensure_key(verbose=False)
    code, data = os_get(f"/collections/{slug}/stats", key)
    if code == 200 and isinstance(data, dict):
        for k in ("floor_price", "floorPrice"):
            v = (data.get("total") or {}).get(k) if isinstance(data.get("total"), dict) else data.get(k)
            if v:
                try:
                    return float(v)
                except Exception:
                    pass
    # 退路：看最便宜的挂单
    code, data = os_get(f"/listings/collection/{slug}/best?limit=1", key)
    if code == 200 and isinstance(data, dict):
        for it in data.get("listings") or []:
            p = (it.get("price") or {}).get("current") or {}
            v = p.get("value")
            if v:
                try:
                    return float(v) / (10 ** int(p.get("decimals") or 18))
                except Exception:
                    pass
    return None


def list_one(nft: dict, cfg: dict, live: bool, price_override: float | None = None) -> dict:
    acct = _acct()
    if not acct:
        return {"ok": False, "reason": "没有 burner 钱包"}

    auto = cfg.get("autolist") or {}
    target_mult = float(auto.get("target_multiple", 2.0))
    undercut = float(auto.get("undercut_pct", 2.0)) / 100.0
    min_price = float(auto.get("min_price_eth", 0.0001))

    contract = nft.get("contract") or ""
    token_id = nft.get("identifier") or nft.get("token_id") or ""
    # collection 字段有两种形态：字符串 slug（v2 实际返回）或 {slug: ...} 对象
    coll = nft.get("collection")
    slug = (coll.get("slug") if isinstance(coll, dict) else coll) or nft.get("collection_slug") or ""
    name = nft.get("name") or f"{slug}#{token_id}"

    rec = {"name": name, "slug": slug, "contract": contract, "tokenId": str(token_id),
           "at": datetime.now(CST).isoformat(), "ok": False, "reason": "", "price": None}

    if not contract or not token_id:
        rec["reason"] = "NFT 信息不全"
        return rec

    # 1. 地板价
    fl = floor_price(slug) if slug else None
    if fl is None:
        rec["reason"] = "拿不到地板价"
        return rec
    rec["floor"] = fl
    log(f"  {name}: 地板 {fl:g} ETH")

    if fl < min_price:
        rec["reason"] = f"地板 {fl:g} 低于最低挂单价 {min_price:g}，不值得付 gas"
        return rec

    # 2. 定价：地板价下浮一点，求快成交；--price 可钉死价格（避免自己的挂单变成新地板后自我压价）
    if price_override:
        price = round(float(price_override), 9)
        log(f"    挂单价 {price:g} ETH（--price 指定）")
    else:
        price = round(fl * (1 - undercut), 9)
        log(f"    挂单价 {price:g} ETH（地板下浮 {undercut*100:.1f}%）")
    if price <= 0:
        rec["reason"] = "算出的挂单价无效"
        return rec
    rec["price"] = price

    # 3. 向 OpenSea 要挂单动作
    key = ensure_key(verbose=False)
    payload = {
        "address": acct.address,
        "items": [{
            "chain": CHAIN,
            "contract": contract,
            "token_id": str(token_id),
            "quantity": 1,
            "price": {"currency": ZERO, "amount": str(price)},
        }],
    }
    code, data = os_post("/listings/actions", payload, key)
    if code != 200 or not isinstance(data, dict):
        rec["reason"] = f"取挂单动作失败 HTTP {code}: {str(data)[:200]}"
        return rec

    steps = data.get("steps") or []
    approval_tx = None
    sig_req = None
    for st in steps:
        if "itemApprovalAction" in st:
            approval_tx = (st["itemApprovalAction"] or {}).get("transaction")
        if "createListingsAction" in st:
            sig_req = (st["createListingsAction"] or {}).get("signatureRequest")

    # 4. 授权
    if approval_tx:
        to = (approval_tx.get("to") or {}).get("value") or approval_tx.get("to")
        d = approval_tx.get("data") or ""
        v = int(approval_tx.get("value") or 0)
        log(f"    需要授权 Seaport（to={str(to)[:12]}…）")
        try:
            h = send_tx(acct, to, d, v, live=live)
            rec["approvalTx"] = h
            if live:
                # 等一个块，让授权生效
                import time
                time.sleep(6)
        except Exception as e:  # noqa: BLE001
            rec["reason"] = f"授权交易失败: {e}"
            return rec
    else:
        log("    已授权过，跳过")

    if not sig_req:
        rec["reason"] = "OpenSea 没返回待签数据"
        return rec

    # 5. 签名
    msg_raw = sig_req.get("message") or ""
    try:
        typed = json.loads(msg_raw) if isinstance(msg_raw, str) else msg_raw
    except Exception as e:  # noqa: BLE001
        rec["reason"] = f"解析待签数据失败: {e}"
        return rec

    from eth_account.messages import encode_typed_data

    try:
        signable = encode_typed_data(full_message=typed)
        signed = acct.sign_message(signable)
        signature = signed.signature.hex()
        if not signature.startswith("0x"):
            signature = "0x" + signature
        log(f"    已签名 {signature[:18]}…")
    except Exception as e:  # noqa: BLE001
        rec["reason"] = f"签名失败: {e}"
        return rec

    # 6. 提交挂单
    protocol_address = ((typed.get("domain") or {}).get("verifyingContract")) or ""
    submit = {
        "parameters": typed.get("message"),
        "protocol_address": protocol_address,
        "signature": signature,
    }
    if not live:
        rec["reason"] = f"预检通过（dry-run，将挂 {price:g} ETH）"
        rec["signature"] = signature[:20] + "…"
        return rec

    code, resp = os_post(f"/orders/{CHAIN}/{SEAPORT_PROTOCOL}/listings", submit, key)
    if code in (200, 201):
        rec["ok"] = True
        rec["reason"] = f"已挂单 {price:g} ETH"
        rec["orderHash"] = (resp.get("order") or {}).get("order_hash") if isinstance(resp, dict) else None
        log(f"    ✅ 挂单成功")
    else:
        rec["reason"] = f"提交挂单失败 HTTP {code}: {str(resp)[:250]}"
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--token-id", default=None, help="只挂指定 tokenId（例如 1372）")
    ap.add_argument("--price", type=float, default=None, help="钉死挂单价（ETH），不给则按地板下浮")
    args = ap.parse_args()

    import config_loader

    cfg = config_loader.load(ROOT / "config.yaml")
    auto = cfg.get("autolist") or {}
    if not auto.get("enabled", False):
        log("⚠️ autolist.enabled = false，跳过")
        return 0

    acct = _acct()
    if not acct:
        log("没有 burner 钱包（先跑 scripts/burner_wallet.py create）")
        return 1
    log(f"burner 钱包：{acct.address}")

    nfts = owned_nfts(acct.address)
    log(f"持有 NFT：{len(nfts)} 个")
    if args.token_id:
        want = str(args.token_id)
        nfts = [n for n in nfts if str(n.get("identifier") or n.get("token_id") or "") == want]
        log(f"按 tokenId 过滤后：{len(nfts)} 个")
    if not nfts:
        return 0

    results = []
    for nft in nfts[: args.limit]:
        log(f"处理：{nft.get('name') or nft.get('identifier')}")
        results.append(list_one(nft, cfg, args.live, price_override=args.price))

    hist = load_json(LIST_LOG, [])
    hist.extend(results)
    save_json(LIST_LOG, hist[-500:])

    ok = [r for r in results if r.get("ok")]
    log(f"完成：成功挂单 {len(ok)}/{len(results)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
