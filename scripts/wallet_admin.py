"""钱包管理入口 —— 把你说的「命令」翻译成安全的 baw 调用。

设计原则（都是为了「钱包安全」）：

1. **私钥永远不碰**。BAW 的私钥由币安托管，签名在币安侧完成。
   本脚本只发命令，从不接收、不存储、不打印任何私钥/助记词。
   如果你把私钥贴进来，脚本会直接拒绝并提示。

2. **默认只读**。查询类命令（余额/地址/历史/额度）随时可用。
   任何会动钱的命令（send / swap / revoke / call）**必须显式加 --yes**，
   否则只打印「将要做什么」，不执行。

3. **网络自动走代理**。BAW 的 Node 进程默认直连会被 DNS 污染打不通
   （错误是 REQUEST_TIMEOUT，IP 会乱跳到 31.13.x.x 这种）。这里统一注入
   NODE_USE_ENV_PROXY=1 + 代理环境变量，不用每次手动设。

4. **全量留痕**。每个命令都写 logs/wallet.log，含时间、命令、结果。
   出问题时能回溯到底执行了什么。

用法：
  python -X utf8 scripts/wallet_admin.py status
  python -X utf8 scripts/wallet_admin.py balance
  python -X utf8 scripts/wallet_admin.py address
  python -X utf8 scripts/wallet_admin.py history --limit 10
  python -X utf8 scripts/wallet_admin.py quota
  python -X utf8 scripts/wallet_admin.py settings
  python -X utf8 scripts/wallet_admin.py approvals

  # 会动钱的，必须 --yes
  python -X utf8 scripts/wallet_admin.py send --token 0xEeee... --amount 0.001 \
      --to 0xabc... --chain 56 --yes
  python -X utf8 scripts/wallet_admin.py swap --from-token 0xEeee... \
      --to-token 0x55d3... --amount 0.01 --chain 56 --yes
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGFILE = ROOT / "logs" / "wallet.log"
PROXY = os.environ.get("MINTGO_PROXY") or "http://127.0.0.1:7990"

# 只读命令：任何时候都能跑，不动钱
READ_ONLY = {
    "status", "balance", "address", "history", "quota", "settings", "approvals",
    "chains", "gas", "txlock", "agent-id",
}
# 会动钱的命令：必须 --yes
WRITE = {"send", "swap", "revoke", "call", "cancel", "speedup"}

# 明显是私钥/助记词的输入，直接拒绝（防止误粘贴进日志或命令行）
_SECRET_HINTS = ("private", "privkey", "secret", "mnemonic", "seed", "0x" + "a" * 64)


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        LOGFILE.parent.mkdir(parents=True, exist_ok=True)
        with LOGFILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _env() -> dict:
    """给 baw 子进程准备环境：强制走代理，否则 DNS 污染会超时。"""
    env = dict(os.environ)
    env["NODE_USE_ENV_PROXY"] = "1"
    if PROXY:
        env["HTTPS_PROXY"] = PROXY
        env["HTTP_PROXY"] = PROXY
        env["ALL_PROXY"] = PROXY
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def run_baw(args: list[str], *, quiet: bool = False) -> tuple[int, str, str]:
    """执行 baw 命令，返回 (退出码, stdout, stderr)。

    Windows 上一个坑：baw 是 npm 装的 shim（baw / baw.cmd / baw.ps1），
    subprocess 直接调 "baw" 会报找不到。这里用 shutil.which 解析成绝对路径，
    再交给 subprocess（.cmd 由 Windows 自己处理）。
    """
    import shutil

    exe = shutil.which("baw") or shutil.which("baw.cmd")
    if not exe:
        return 127, "", "找不到 baw 命令。先装：npm install -g @binance/agentic-wallet"

    cmd = [exe, *args]
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=_env(), timeout=120,
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except FileNotFoundError:
        return 127, "", f"无法执行：{exe}"
    except subprocess.TimeoutExpired:
        return 124, "", "baw 超时（120s）。可能是网络问题，检查代理是否在跑。"


def _explain_common_error(stderr: str, stdout: str) -> str | None:
    """把常见错误翻译成人话，减少「为什么不动」的困惑。

    注意：只在**确实失败**时调用（见 cmd_read）。早期版本无条件调用，
    导致成功的响应里因为含 devMode / quotaLeft 等字段名而被误判成
    「需要开发者模式」「额度不足」。
    """
    blob = (stderr + stdout).lower()

    # CLI 参数错误（比如 --limit 不存在）不该套业务提示，直接让用户看原文
    if "unknown option" in blob or "error: missing" in blob or "error: required" in blob:
        return None

    if (
        "session_expired" in blob
        or "not_logged_in" in blob
        or "not logged in" in blob
        or "please log in first" in blob
        or "10003000" in blob
    ):
        return (
            "钱包未登录。执行：baw auth signin → 用币安 App 扫码 → baw auth verify\n"
            "  （这一步只能你本人做，我无法代劳。）"
        )
    if "request_timeout" in blob or "connect timeout" in blob:
        return (
            f"网络超时。确认代理 {PROXY} 在跑；或设环境变量 MINTGO_PROXY 指向正确代理。"
        )
    if "351804" in blob or "devmode_disabled" in blob:
        return (
            "需要开发者模式。币安 App → Agentic Wallet 管理页 → Settings → 打开 Developer Mode。"
        )
    # 额度相关只在明确报错时提示，避免 tag 到 quotaLeft 这种正常字段
    if "quota_exceeded" in blob or "exceeds the limit" in blob or "daily limit" in blob:
        return "触发了每日额度上限。先跑 quota 查看剩余额度。"
    return None


def guard_secret(args: argparse.Namespace) -> None:
    """防止把私钥当参数传进来。"""
    blob = " ".join(str(v) for v in vars(args).values() if v is not None).lower()
    for hint in _SECRET_HINTS:
        if hint in blob:
            log("🚫 检测到疑似私钥/助记词的输入，已拒绝执行。")
            log("   本工具不需要私钥：BAW 的钱包由币安托管，签名在币安侧完成。")
            log("   请勿在任何命令、聊天、截图里传递私钥。")
            sys.exit(2)


# ---------------------------------------------------------------- 子命令


def cmd_read(name: str, extra: list[str]) -> int:
    log(f"查询：{name} {' '.join(extra)}".rstrip())
    code, out, err = run_baw([*extra, "--json"])

    # 先判断这次到底成功没有 —— 只有真的失败才给排查提示。
    # 否则会误报：成功的响应里也含 devMode / quotaLeft 这些字段名，
    # 早先版本因此把「查询成功」提示成「需要开发者模式」「额度不足」。
    ok = code == 0
    try:
        parsed = json.loads(out)
        if parsed.get("success") is False:
            ok = False
    except Exception:
        pass

    if out.strip():
        try:
            print(json.dumps(json.loads(out), ensure_ascii=False, indent=2))
        except json.JSONDecodeError:
            print(out)
    if err.strip():
        print(err, file=sys.stderr)

    if not ok:
        hint = _explain_common_error(err, out)
        if hint:
            log(f"💡 {hint}")
    return code


def cmd_send(a: argparse.Namespace) -> int:
    if not a.yes:
        log("⚠️ 这是会动钱的命令，但没加 --yes，只做预演：")
        log(f"   将发送 {a.amount} 个代币 {a.token}")
        log(f"   到      {a.to}")
        log(f"   链      {a.chain}")
        log(f"   gas档位 {a.gas_level}")
        log("   确认无误后，在命令末尾加 --yes 才会真正执行。")
        return 0
    log(f"🔴 执行发送：{a.amount} {a.token} → {a.to}（链 {a.chain}）")
    args = [
        "wallet", "send",
        "--amount", str(a.amount),
        "--recipient", a.to,
        "--tokenAddress", a.token,
        "--binanceChainId", str(a.chain),
        "--gasLevel", a.gas_level,
        "--json",
    ]
    code, out, err = run_baw(args)
    print(out or err)
    hint = _explain_common_error(err, out)
    if hint:
        log(f"💡 {hint}")
    log(f"发送结果退出码：{code}")
    return code


def cmd_swap(a: argparse.Namespace) -> int:
    if not a.yes:
        log("⚠️ 这是会动钱的命令，但没加 --yes，只做预演：")
        log(f"   将把 {a.amount} 的 {a.from_token}")
        log(f"   换成 {a.to_token}")
        log(f"   链   {a.chain}")
        log(f"   滑点 {a.slippage}")
        log("   确认无误后，在命令末尾加 --yes 才会真正执行。")
        return 0
    log(f"🔴 执行兑换：{a.amount} {a.from_token} → {a.to_token}（链 {a.chain}）")
    args = [
        "market-order", "swap",
        "--fromToken", a.from_token,
        "--toToken", a.to_token,
        "--amount", str(a.amount),
        "--binanceChainId", str(a.chain),
        "--slippage", str(a.slippage),
        "--json",
    ]
    code, out, err = run_baw(args)
    print(out or err)
    hint = _explain_common_error(err, out)
    if hint:
        log(f"💡 {hint}")
    log(f"兑换结果退出码：{code}")
    return code


def main() -> int:
    ap = argparse.ArgumentParser(
        description="钱包管理入口（BAW 托管私钥，默认只读）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    # ---- 只读 ----
    sub.add_parser("status", help="登录状态")
    sub.add_parser("address", help="各链钱包地址")
    sub.add_parser("quota", help="今日剩余交易额度")
    sub.add_parser("settings", help="钱包设置（含会话有效期 / 开发者模式）")
    sub.add_parser("chains", help="支持哪些链")
    sub.add_parser("approvals", help="已授权的合约（查有没有危险的无限授权）")
    sub.add_parser("agent-id", help="Agent ID 状态")

    p_bal = sub.add_parser("balance", help="各链余额")
    p_bal.add_argument("--chain", help="只看某条链（binanceChainId，如 56）")

    p_hist = sub.add_parser("history", help="交易历史")
    p_hist.add_argument("--limit", type=int, default=10)
    p_hist.add_argument("--chain", help="只看某条链")

    p_gas = sub.add_parser("gas", help="gas 价格")
    p_gas.add_argument("--chain", default="56")

    p_lock = sub.add_parser("txlock", help="交易锁状态")
    p_lock.add_argument("--chain", default="56")

    # ---- 会动钱 ----
    p_send = sub.add_parser("send", help="转账（需 --yes）")
    p_send.add_argument("--token", required=True, help="代币合约地址")
    p_send.add_argument("--amount", required=True, help="数量")
    p_send.add_argument("--to", required=True, help="收款地址")
    p_send.add_argument("--chain", required=True, help="binanceChainId，如 56=BSC")
    p_send.add_argument("--gas-level", default="MEDIUM", choices=["LOW", "MEDIUM", "HIGH"])
    p_send.add_argument("--yes", action="store_true", help="确认执行")

    p_swap = sub.add_parser("swap", help="兑换（需 --yes）")
    p_swap.add_argument("--from-token", required=True)
    p_swap.add_argument("--to-token", required=True)
    p_swap.add_argument("--amount", required=True)
    p_swap.add_argument("--chain", required=True)
    p_swap.add_argument("--slippage", default="auto")
    p_swap.add_argument("--yes", action="store_true", help="确认执行")

    a = ap.parse_args()
    guard_secret(a)

    # 把子命令映射到 baw 的真实路径
    mapping = {
        "status": ["wallet", "status"],
        "address": ["wallet", "address"],
        "quota": ["wallet", "left-quota"],
        "settings": ["wallet", "settings"],
        "chains": ["wallet", "chains"],
        "approvals": ["approvals", "list"],
        "agent-id": ["wallet", "agent-id"],
    }

    if a.cmd in mapping:
        return cmd_read(a.cmd, mapping[a.cmd])

    if a.cmd == "balance":
        extra = ["wallet", "balance"]
        if getattr(a, "chain", None):
            extra += ["--binanceChainId", str(a.chain)]
        return cmd_read("balance", extra)

    if a.cmd == "history":
        # tx-history 的分页参数是 --size，不是 --limit
        extra = ["wallet", "tx-history", "--size", str(a.limit)]
        if getattr(a, "chain", None):
            extra += ["--binanceChainId", str(a.chain)]
        return cmd_read("history", extra)

    if a.cmd == "gas":
        return cmd_read("gas", ["wallet", "gas-price", "--binanceChainId", str(a.chain)])

    if a.cmd == "txlock":
        return cmd_read("txlock", ["wallet", "tx-lock", "--binanceChainId", str(a.chain)])

    if a.cmd == "send":
        return cmd_send(a)

    if a.cmd == "swap":
        return cmd_swap(a)

    log(f"未知命令：{a.cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
