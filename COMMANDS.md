# 🎮 FlashArb V3 - 命令指令集

> 所有可用的脚本、命令和用法汇总

---

## 📋 目录

1. [主程序](#1-主程序)
2. [部署与修复](#2-部署与修复)
3. [资金管理](#3-资金管理)
4. [市场扫描](#4-市场扫描)
5. [测试与诊断](#5-测试与诊断)
6. [环境变量配置](#6-环境变量配置)

---

## 1. 主程序

### 🚀 启动套利机器人

```bash
# 基础启动
python main.py

# 干运行模式（不执行真实交易）
# 在 .env 中设置 DRY_RUN=true
python main.py

# 生产模式
# 在 .env 中设置 DRY_RUN=false
python main.py
```

**配置项 (.env):**
```env
DRY_RUN=true                    # true=模拟, false=真实交易
DEBUG_MODE=false                # 详细日志
SCAN_INTERVAL=1.0               # 扫描间隔(秒)
MIN_PROFIT_ETH=0.001            # 最小利润(ETH)
MAX_GAS_GWEI=10                 # 最大Gas价格
SNIPER_MODE_ENABLED=true        # 激进Gas策略
```

---

## 2. 部署与修复

### 📦 部署新合约

```bash
python scripts/deploy.py
```

**功能:**
- 编译 `FlashBotV3.sol`
- 部署到 Base Mainnet
- 自动授权 SwapRouter
- 保存部署信息到 `deployments.json`

**前提条件:**
```env
PRIVATE_KEY=0x你的私钥
RPC_URL=https://mainnet.base.org
```

---

### 🔧 修复部署状态

```bash
python scripts/fix_deployment.py
```

**功能:**
- 重新编译获取 ABI
- 连接已部署的合约
- 执行缺失的 `approveRouter` 调用
- 更新 `deployments.json`

**使用场景:**
- 部署中断后恢复
- 添加新的路由器授权

---

## 3. 资金管理

### 💰 注资到合约

```bash
python scripts/fund_contract.py
```

**交互式流程:**
```
1. 检测钱包中的 WETH 余额
2. 如果 WETH > 0.002 ETH:
   → 询问是否直接转移 WETH
3. 如果 WETH 不足:
   → 包装 ETH → 转移 WETH
4. 确认后执行
```

**配置项 (.env):**
```env
FLASHBOT_ADDRESS=0x你的合约地址
FUND_AMOUNT_ETH=0.002           # 默认注资金额
MIN_WETH_THRESHOLD_ETH=0.002    # WETH检测阈值
```

---

### 💸 从合约提取资金

```bash
python scripts/withdraw.py
```

**交互式流程:**
```
1. 显示合约 WETH 和 ETH 余额
2. 显示 Owner 和 Contract 地址（安全确认）
3. 询问提取确认
4. 提取 WETH（如果有）
5. 提取 ETH（如果有）
```

**安全检查:**
- 显示地址确认
- 需要手动输入 `y` 确认
- 使用 Owner 权限验证

---

## 4. 市场扫描

### 🔍 市场扫描器 (生成配置)

```bash
# 基础运行
python scripts/market_screener.py

# 自定义过滤
python scripts/market_screener.py --min-liquidity 100000 --min-spread 1.0

# 生成配置文件
python scripts/market_screener.py --top 15 --output config/target_tokens.py

# 包含风险代币
python scripts/market_screener.py --include-caution
```

**参数说明:**

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--min-liquidity` | 50000 | 最小流动性 ($) |
| `--min-volume` | 10000 | 最小24h交易量 ($) |
| `--min-spread` | 0.5 | 最小价差 (%) |
| `--top` | 10 | 输出代币数量 |
| `--output` | None | 配置文件输出路径 |
| `--include-caution` | False | 包含 CAUTION 级别代币 |

**输出示例:**
```python
TARGET_TOKENS = [
    # BRETT | Spread: 1.23% | Liq: $2.50M
    {
        "symbol": "BRETT",
        "address": "0x532f27101965dd16442E59d40670FaF5eBB142E4",
        "decimals": 18,  # TODO: Verify Decimals
        "fee_tiers": [500, 3000, 10000],
        "min_profit": 0.0005,
    },
]
```

---

## 5. 测试与诊断

### 🧪 闪电贷测试

```bash
python scripts/test_flash.py
```

**测试内容:**
- 验证合约部署和配置
- 检查路由器授权状态
- 诊断闪电贷执行环境
- 解释测试限制

**前提:**
- Anvil fork 运行在 `http://127.0.0.1:8545`
- 或连接到真实 RPC

---

### 🧠 扫描器/计算器测试

```bash
python scripts/test_brain.py
```

**测试内容:**
1. **单元测试 (Calculator):** AMM 数学公式验证
2. **集成测试 (Multicall):** 批量储备数据获取
3. **逻辑测试 (Scanner):** 套利利润计算模拟

---

### 🌐 网络连接测试

```bash
python test_network.py
```

**测试内容:**
- RPC 连接状态
- 区块同步检查
- Gas 价格获取

---

## 6. 环境变量配置

### 📝 完整 .env 模板

```env
# ========================================
# 网络配置
# ========================================
RPC_URL=https://mainnet.base.org
CHAIN_ID=8453
PRIVATE_KEY=0x你的私钥
RPC_TIMEOUT=30

# ========================================
# 合约地址
# ========================================
FLASHBOT_ADDRESS=0x你的FlashBot合约地址
WETH=0x4200000000000000000000000000000000000006
V3_FACTORY=0x33128a8fC17869897dcE68Ed026d694621f6FDfD
SWAP_ROUTER=0x2626664c2603336E57B271c5C0b26F421741e481
MULTICALL3=0xcA11bde05977b3631167028862bE2a173976CA11

# ========================================
# 套利参数
# ========================================
MIN_PROFIT_ETH=0.001
MIN_BORROW_ETH=0.01
MAX_BORROW_ETH=20.0
AMOUNT_PRECISION_ETH=0.001

# ========================================
# Gas 配置
# ========================================
MAX_GAS_GWEI=10
GAS_LIMIT=500000
SNIPER_MODE_ENABLED=true
SNIPER_MODE_MULTIPLIER=1.2

# ========================================
# 扫描配置
# ========================================
SCAN_INTERVAL=1.0
FEE_TIERS=500,3000,10000
FLASH_FEE_TIER=500

# ========================================
# 运行模式
# ========================================
DRY_RUN=true
DEBUG_MODE=false
LATENCY_PROFILING=true
SHADOW_MODE_ENABLED=true
SHADOW_SPREAD_THRESHOLD=0.005

# ========================================
# 流动性过滤
# ========================================
MIN_LIQUIDITY=1000000000000000
MIN_LIQUIDITY_ETH=0.5

# ========================================
# 资金管理
# ========================================
FUND_AMOUNT_ETH=0.002
MIN_WETH_THRESHOLD_ETH=0.002
```

---

## 🔄 快速命令速查

### 日常操作

```bash
# 1. 检查市场机会
python scripts/market_screener.py --min-spread 0.5

# 2. 生成新的目标代币配置
python scripts/market_screener.py --top 10 --output config/target_tokens.py

# 3. 注资到合约
python scripts/fund_contract.py

# 4. 干运行测试
# 设置 DRY_RUN=true
python main.py

# 5. 正式运行
# 设置 DRY_RUN=false
python main.py

# 6. 提取利润
python scripts/withdraw.py
```

### 故障排查

```bash
# 测试网络连接
python test_network.py

# 测试合约状态
python scripts/test_flash.py

# 测试扫描逻辑
python scripts/test_brain.py

# 修复部署问题
python scripts/fix_deployment.py
```

### 本地开发

```bash
# 启动 Anvil fork
anvil --fork-url https://mainnet.base.org --port 8545

# 运行测试
python scripts/test_brain.py

# 部署到 fork
python scripts/deploy.py
```

---

## 📁 项目结构

```
FlashArb V3/
├── main.py                      # 🚀 主程序入口
├── .env                         # ⚙️ 配置文件
├── deployments.json             # 📋 部署记录
├── requirements.txt             # 📦 依赖列表
│
├── core/                        # 🧠 核心模块
│   ├── calculator.py           #    利润计算
│   ├── scanner.py              #    机会扫描
│   ├── executor.py             #    交易执行
│   ├── multicall.py            #    批量调用
│   └── network.py              #    网络管理
│
├── contracts/                   # 📜 Solidity 合约
│   └── FlashBotV3.sol          #    闪电贷合约
│
├── scripts/                     # 🔧 工具脚本
│   ├── deploy.py               #    部署合约
│   ├── fix_deployment.py       #    修复部署
│   ├── fund_contract.py        #    注资
│   ├── withdraw.py             #    提取资金
│   ├── market_screener.py      #    市场扫描
│   ├── test_flash.py           #    闪电贷测试
│   └── test_brain.py           #    逻辑测试
│
├── config/                      # 📂 配置文件
│   └── chains.json             #    链配置
│
├── abis/                        # 📄 ABI 文件
│   ├── erc20.json
│   ├── swap_router.json
│   └── uniswap_v3_pool.json
│
├── logs/                        # 📊 日志
│   └── trade_history.csv
│
└── COMMANDS.md                  # 📖 本文档
```

---

## ⚡ 单行命令汇总

```bash
# 主程序
python main.py

# 部署
python scripts/deploy.py

# 修复部署
python scripts/fix_deployment.py

# 注资
python scripts/fund_contract.py

# 提取
python scripts/withdraw.py

# 市场扫描 (默认)
python scripts/market_screener.py

# 市场扫描 (生成配置)
python scripts/market_screener.py --top 10 --output config/target_tokens.py

# 市场扫描 (严格筛选)
python scripts/market_screener.py --min-liquidity 100000 --min-spread 1.0

# 市场扫描 (包含风险代币)
python scripts/market_screener.py --include-caution

# 闪电贷测试
python scripts/test_flash.py

# 逻辑测试
python scripts/test_brain.py

# 网络测试
python test_network.py
```

---

**📅 最后更新:** 2024-12-05

**📌 备注:** 所有命令需要在项目根目录 `E:\PythonProject\MEV套利\` 下执行

