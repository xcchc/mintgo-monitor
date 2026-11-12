# 自动打新与自动挂单

这套系统做三件事：**监控 → 判断 → 执行**。本文记录架构决策、踩过的坑、以及哪些部分已验证。

## 一句话架构

```
mintgo.fun  ──抓取──▶  打分模型  ──筛选──▶  OpenSea 官方 API  ──构造交易──▶  BAW / burner 钱包  ──▶  链上
              (radar)      (rule)              (drops/listings)              (签名执行)
```

**核心设计：不逆向任何合约，全部走 OpenSea 官方接口拿现成的交易数据。**

---

## 为什么这样设计（三条关键决策）

### 决策一：不装 MetaMask，用程序化钱包

最初的想法是让用户装 MetaMask 去 OpenSea 手动挂单。问题是：

- NFT 在币安托管的钱包里，**用户拿不到私钥，连不上 OpenSea**
- MetaMask 只是一个签名 UI，本质是「用私钥签 EIP-712」

既然服务端也能签，就不需要浏览器插件。我们直接生成一个钱包，用 `eth-account` 签名。

**代价**：私钥落在磁盘上。所以定位是「专用热钱包（burner）」，只放打新和 gas 的小钱。

### 决策二：不逆向 SeaDrop，直接用 OpenSea 的 drop 接口

最早的思路是逆向 ERC721SeaDrop 合约：解析 EIP-1167 代理 → 拿实现合约 ABI → 找 `mintPublic` → 手工拼 calldata → 还得找出 SeaDrop 合约地址。

这条路能走通，但很脆弱：函数签名会变、SeaDrop 地址各链不同、参数顺序容易错。

后来发现 OpenSea 有：

```
POST /api/v2/drops/{slug}/mint
body: { "minter": "0x...", "quantity": 1 }
```

它直接返回**完整可执行的交易**：

```json
{
  "to": "0x00005ea00ac477b1030ce78506496e8c2de24bf5",
  "data": "0x161ac21f0000...",
  "value": "50000000000000",
  "chain": "robinhood",
  "stage_type": "public_sale"
}
```

同理，挂单用：

```
POST /api/v2/listings/actions
body: { "address": "0x...", "items": [{chain, contract, token_id, quantity, price}] }
```

返回两个 step：

```json
{
  "steps": [
    { "itemApprovalAction": { "transaction": {to, data, value} } },      // setApprovalForAll
    { "createListingsAction": { "signatureRequest": { "message": "{...Seaport EIP-712...}" } } }
  ]
}
```

签完再 `POST /api/v2/orders/robinhood/seaport/listings`（body：`parameters` / `protocol_address` / `signature`）。

**这条路把所有合约细节都交给了 OpenSea，我们只负责签名和执行。**

### 决策三：BAW 只用来打新，挂单必须用 burner

实测发现币安 BAW 的 `sign-message` **不支持签 Seaport 的 OrderComponents**：

```
baw sign-message preview --signType EIP712 --message '<Seaport typed data>'
→ { "code": 953004, "message": "We currently don't support signing this type of message." }
```

所以：

| 环节 | 用什么 | 原因 |
|---|---|---|
| mint | BAW `contract-call` | 有模拟和风控检查，且私钥不落地 |
| transfer NFT | BAW `contract-call` | 同上 |
| approval | burner 钱包 | NFT 必须在签名方名下 |
| 签卖单 | burner 钱包 | BAW 签不了 Seaport 类型 |
| 提交挂单 | OpenSea API | 官方接口 |

---

## 完整流程图

```
① 监控（已有）
   box 每 30 分钟抓 mintgo.fun radar + market
   → data/latest_scored.json

② 判断（auto_mint.py）
   筛 verdict == "值得打"
   → GET /drops/{slug}  拿【实时】价格/阶段/限购
   → 安全闸：public_sale？价格 ≤ 上限？今日次数 ≤ 上限？

③ 打新（auto_mint.py --live）
   → POST /drops/{slug}/mint  → {to, data, value}
   → baw contract-call preview  四项检查
        · simulationErrorDetail 为空
        · balanceChanges 扣款符合预期
        · 必须出现 Erc721 +1（说明真能拿到 NFT）
        · authorityChanges / risks 为空
   → baw contract-call execute --requestId

④ 转移（待实现：scripts/transfer_nft.py）
   → baw contract-call 调 safeTransferFrom
   → 把 NFT 从 BAW 钱包转到 burner 钱包

⑤ 挂单（auto_list.py --live）
   → GET /chain/robinhood/account/{burner}/nfts   看持有什么
   → GET /collections/{slug}/stats                拿地板价
   → 定价：地板 ×（1 − undercut）
   → POST /listings/actions                      拿 approval 交易 + 待签数据
   → burner 发 approval 交易（要 gas）
   → burner 签 EIP-712
   → POST /orders/robinhood/seaport/listings     挂单成功
```

---

## 已验证 / 未验证

诚实标注每一部分的状态，避免误以为全都能跑。

| 环节 | 状态 | 验证方式 |
|---|---|---|
| mintgo 抓取 + 打分 | ✅ 已验证 | 每 30 分钟真实跑，历史多轮 |
| OpenSea 即时 API key | ✅ 已验证 | `POST /auth/keys` 返回 201 |
| `GET /drops/{slug}` | ✅ 已验证 | 返回真实价格、阶段、限购 |
| `POST /drops/{slug}/mint` | ✅ 已验证 | 返回完整 to/data/value |
| BAW preview 打新交易 | ✅ 已验证 | 模拟显示收到 ERC721，tokenId 真实 |
| **BAW 真实广播 mint** | ❌ **未验证** | 需要真钱，等有合适项目 |
| `POST /listings/actions` | ✅ 已验证 | 返回 approval + Seaport typed data |
| burner 签 Seaport EIP-712 | ✅ 已验证 | 签名成功生成 |
| **burner 真发 approval** | ❌ **未验证** | burner 还没 NFT 和 gas |
| **提交挂单** | ❌ **未验证** | 等有 NFT |
| **NFT 从 BAW 转 burner** | ❌ **未实现** | 需要打到一个 NFT 才有意义 |

**结论：所有「读取 + 构造 + 签名」环节都验证过；「广播」环节因为涉及真钱，尚未做端到端测试。**

---

## 安全设计

### 分层安全闸

自动打新要连过五关，任何一关不过就不发交易：

```python
# 1. 阶段必须是公开销售（白名单打不了）
if stage_type != "public_sale": reject

# 2. 价格不能超上限
if price > max_price_eth: reject

# 3. 今日次数不能超
if today_count >= max_per_day: reject

# 4. BAW 模拟必须通过
if simulationErrorDetail: reject
if authorityChanges: reject      # 要授权 = 警惕
if risks.riskDetails: reject

# 5. 模拟必须真的带来 NFT
if not any(balanceChanges tokenType == "Erc721" and change > 0): reject
```

第 5 条是实践里加的：模拟「成功」不代表拿到了东西，可能只是扣了钱但没 mint 出 NFT（售罄、合约异常等）。

### 默认 dry-run

```bash
python scripts/auto_mint.py          # 只预检，不广播
python scripts/auto_mint.py --live   # 才真正执行
```

配置里还有一层总开关：

```yaml
automint:
  enabled: false      # 默认关，要显式打开
```

### 私钥处理

- BAW 钱包：私钥在币安，本地不碰
- burner 钱包：明文存 `data/burner_wallet.key`，已 gitignore
- `wallet_admin.py` 会**拒绝**任何传入疑似私钥的参数（退出码 2）

---

## 踩过的坑（给后来者省时间）

### 1. 价格不能信缓存

radar 缓存里 ASCIHOOD `priceWei = 0`（免费），但 OpenSea `/drops/{slug}` 实时显示
`price = 50000000000000`（0.00005 ETH）。

**缓存会过期，动钱之前必须用实时接口重新确认。**

### 2. `eth_getCode` 返回空可能是 403 而不是「没部署」

一开始用 `urllib` 直连 Robinhood RPC 没带 User-Agent，全部返回 403，
然后 `eth_getCode` 拿到空字符串，误判成「SeaDrop 合约没部署」。

加上浏览器 UA 后一切正常，SeaDrop 合约有 42164 字节代码。

**RPC 请求必须带 User-Agent。**

### 3. OpenSea 走 Cloudflare，同样要 UA

不带 UA 会返回：

```
403 Error 1010: The site owner has blocked access based on your browser's signature.
```

### 4. PowerShell 改 YAML 会写入 BOM

```powershell
Set-Content -Encoding UTF8   # 会写 \ufeff
```

之后 `config_loader` 第一行解析成 `'\ufeff# mintgo-monitor 配置'` 直接炸。

**修法**：读取用 `encoding="utf-8-sig"`，写入用 Python 而非 PowerShell。

### 5. 成交笔数会被刷量骗

ASCIHOOD 有 359 笔 1h 成交，看着「深度好」，实则总成交额只有 0.000275 ETH
（平均每笔 $0.0016）。正常项目单笔是 $0.66 ~ $8000。

**只看笔数会被骗，必须同时看成交量。** 现在已经加了 `min_volume_1h_eth` 门槛。

### 6. `baw` 是 npm shim，Python subprocess 调不到

Windows 上 `shutil.which("baw")` 才能解析到 `baw.CMD`。

---

## 用法

### 一次性准备

```bash
# 1. 生成 burner 钱包（只放小钱）
python -X utf8 scripts/burner_wallet.py create
python -X utf8 scripts/burner_wallet.py export   # 抄到密码管理器！

# 2. 确认 BAW 已登录
python -X utf8 scripts/wallet_admin.py status

# 3. 确认 OpenSea key 可用（会自动申请）
python -X utf8 scripts/opensea_key.py ensure
```

### 日常

```bash
# 看有没有值得打的
python -X utf8 src/run_once.py

# 预检（不发交易）
python -X utf8 scripts/auto_mint.py

# 真实打新（改 config.yaml 的 automint.enabled = true 之后）
python -X utf8 scripts/auto_mint.py --live

# 挂单卖出
python -X utf8 scripts/auto_list.py
python -X utf8 scripts/auto_list.py --live
```

### 配置

```yaml
automint:
  enabled: false          # 总开关
  max_price_eth: 0        # 单笔上限（0 = 只打完全免费的）
  max_per_day: 5          # 每日次数上限
  quantity: 1             # 每次 mint 几个

autolist:
  enabled: false
  target_multiple: 2.0    # 地板达到成本几倍才卖
  undercut_pct: 2.0       # 比地板低几个百分点（求快成交）
  min_price_eth: 0.0001   # 低于此价不挂（不值得付 gas）
```

---

## 成本参考

Robinhood Chain 上：

| 操作 | gas | 成本 |
|---|---|---|
| mint（SeaDrop） | ~150,000 | 约 0.000004 ETH（$0.008） |
| setApprovalForAll | ~50,000 | 约 0.000001 ETH（$0.003） |
| 挂单签名 | 0（不上链） | 免费 |

**所以一整套「打新 → 转出 → 授权 → 挂单」的链上成本约 $0.02。**

---

## 已知限制

1. **免费且有地板数据的项目极罕见** —— 历史统计只占 0.6%~2.3%，多数轮次为 0
2. **白名单阶段（`signed_presale`）无法自动化** —— 需要 OpenSea 给钱包单独签名
3. **burner 钱包必须自己备份私钥** —— 丢了无法找回
4. **OpenSea 免费 key 7 天过期** —— `opensea_key.py` 会在到期前 12 小时自动换新
5. **成交依赖市场流动性** —— 挂单 ≠ 卖出，地板价低时可能长期无买家

---

## 依赖

```
Python 3.10+
eth-account      # 生成钱包、签 EIP-712
requests         # （可选，目前用 urllib）
Node.js + baw    # npm i -g @binance/agentic-wallet
```
