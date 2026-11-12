"""专用热钱包（burner wallet）管理。

为什么需要它：
  币安 BAW 钱包的私钥在币安手上，我们无法用它签名挂单到 OpenSea。
  要卖 NFT，必须有一个「自己持有私钥」的钱包；而 MetaMask 只是签名的 UI，
  我们直接用 eth-account 签名即可，不需要浏览器插件。

安全定位：
  · 这是**专用热钱包**，只放打新和 gas 用的小额资金，不碰主资产
  · 私钥以明文存在 data/burner_wallet.key（已在 .gitignore）
  · 唯一凭证，丢了无法找回 —— 必须自己备份

用法：
  python -X utf8 scripts/burner_wallet.py create    # 生成新钱包
  python -X utf8 scripts/burner_wallet.py show      # 看地址和余额相关
  python -X utf8 scripts/burner_wallet.py export    # 显示私钥（谨慎）
  python -X utf8 scripts/burner_wallet.py import <私钥>   # 导入已有钱包
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY_FILE = ROOT / "data" / "burner_wallet.key"
META_FILE = ROOT / "data" / "burner_wallet.json"


def _acct(path: Path = KEY_FILE):
    from eth_account import Account

    if not path.exists():
        return None
    pk = path.read_text(encoding="utf-8").strip()
    if not pk:
        return None
    if not pk.startswith("0x"):
        pk = "0x" + pk
    return Account.from_key(pk)


def save_wallet(private_key: str) -> dict:
    from eth_account import Account

    acct = Account.from_key(private_key)
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(private_key if private_key.startswith("0x") else "0x" + private_key,
                        encoding="utf-8")
    meta = {"address": acct.address, "type": "burner", "chain": "evm"}
    META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def create() -> int:
    from eth_account import Account

    if KEY_FILE.exists():
        acct = _acct()
        print(f"已有钱包，未覆盖。地址：{acct.address if acct else '?'}")
        print("如需换新，先删除 data/burner_wallet.key 和 data/burner_wallet.json")
        return 1

    acct = Account.create()
    meta = save_wallet(acct.key.hex())
    print("=" * 60)
    print("  已生成专用热钱包")
    print("=" * 60)
    print(f"  地址   : {meta['address']}")
    print(f"  私钥存 : {KEY_FILE}")
    print(f"  元信息 : {META_FILE}")
    print()
    print("  ⚠️ 这个私钥是唯一凭证，丢了里面的 NFT 就永久消失。")
    print("     请运行以下命令查看并抄到密码管理器：")
    print("       python -X utf8 scripts\\burner_wallet.py export")
    print("=" * 60)
    return 0


def show() -> int:
    acct = _acct()
    if not acct:
        print("还没有钱包。运行 create 生成。")
        return 1
    print(f"地址 : {acct.address}")
    print(f"文件 : {KEY_FILE}")
    print()
    print("别名（各链同地址）：")
    print("  Robinhood / Ethereum / Base / BSC / Polygon / Arbitrum 都是这个地址")
    return 0


def export() -> int:
    acct = _acct()
    if not acct:
        print("还没有钱包。")
        return 1
    pk = KEY_FILE.read_text(encoding="utf-8").strip()
    print("=" * 60)
    print("  私钥（复制到密码管理器后，建议关掉这个窗口）")
    print("=" * 60)
    print()
    print(f"  {pk}")
    print()
    print("=" * 60)
    print(f"  地址：{acct.address}")
    print("  导入 MetaMask 时选「导入账户 → 私钥」，粘贴上面那串")
    print("=" * 60)
    return 0


def do_import(pk: str) -> int:
    if not pk.startswith("0x"):
        pk = "0x" + pk
    meta = save_wallet(pk)
    print(f"已导入。地址：{meta['address']}")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "create":
        return create()
    if cmd == "show":
        return show()
    if cmd == "export":
        return export()
    if cmd == "import":
        if len(sys.argv) < 3:
            print("用法: burner_wallet.py import <私钥>")
            return 1
        return do_import(sys.argv[2])
    print(f"未知命令: {cmd}")
    print("可用: create / show / export / import <私钥>")
    return 1


if __name__ == "__main__":
    sys.exit(main())
