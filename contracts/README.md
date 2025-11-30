# FlashBot 智能合约

## 概述

`FlashBot` 是一个模块化、Gas 优化的闪电贷套利合约，专为 Uniswap V2 及其分叉（PancakeSwap、SushiSwap、Baseswap 等）设计。

## 文件结构

```
contracts/
├── FlashBot.sol              # 主合约
├── interfaces/
│   └── ISwaps.sol            # 接口定义
├── libraries/
│   └── SafeERC20.sol         # 安全 ERC20 操作库
└── README.md                 # 本文件
```

## 核心功能

### 1. 闪电贷套利

通过 Uniswap V2 的闪电兑换功能，无需预先持有资金即可执行套利：

1. 从配对合约借入代币（触发闪电贷）
2. 在目标路由器上执行套利交易
3. 偿还借款 + 0.3% 手续费
4. 保留利润

### 2. 安全特性

- **所有者权限控制**：关键函数仅所有者可调用
- **回调验证**：验证闪电贷回调来源
- **SafeERC20**：处理非标准 ERC20（如 USDT）
- **利润检查**：确保交易有利可图

### 3. Gas 优化

- **无状态存储路由器/代币**：通过 calldata 传递，节省 SLOAD
- **unchecked 数学运算**：在安全场景使用 unchecked
- **预授权机制**：部署后一次性授权，避免交易中授权消耗
- **自定义错误**：比 require 字符串更省 gas

## 部署步骤

### 1. 部署合约

```bash
# 使用 Foundry
forge create contracts/FlashBot.sol:FlashBot \
  --rpc-url $RPC_URL \
  --private-key $PRIVATE_KEY
```

### 2. 预授权路由器

部署后，调用 `approveRouter` 或 `batchApproveRouters` 预授权常用的代币/路由器组合：

```solidity
// 示例：授权 WETH 给 Uniswap V2 Router
flashBot.approveRouter(WETH_ADDRESS, UNISWAP_V2_ROUTER);

// 批量授权
address[] memory tokens = [WETH, USDC, USDT];
address[] memory routers = [UNISWAP_ROUTER, UNISWAP_ROUTER, UNISWAP_ROUTER];
flashBot.batchApproveRouters(tokens, routers);
```

### 3. 执行套利

从 Python 后端调用 `startArbitrage`：

```python
# 编码 userData
user_data = encode(['address', 'address[]'], [
    target_router,
    trade_path  # [WETH, USDC, WETH]
])

# 调用合约
tx = flash_bot.functions.startArbitrage(
    token_borrow,      # 借入的代币
    amount,            # 借入数量
    pair_address,      # V2 配对地址
    user_data          # 编码的交易数据
).build_transaction({...})
```

## 函数说明

### 入口函数

| 函数 | 描述 |
|------|------|
| `startArbitrage` | 启动闪电贷套利 |

### 回调函数

| 函数 | 描述 |
|------|------|
| `uniswapV2Call` | Uniswap V2 / SushiSwap 回调 |
| `pancakeCall` | PancakeSwap 回调 |

### 管理函数

| 函数 | 描述 |
|------|------|
| `approveRouter` | 预授权单个代币/路由器 |
| `batchApproveRouters` | 批量预授权 |
| `setMinProfitThreshold` | 设置最小利润阈值 |
| `transferOwnership` | 转移所有权 |

### 提取函数

| 函数 | 描述 |
|------|------|
| `withdrawToken` | 提取 ERC20 代币 |
| `withdrawETH` | 提取 ETH |
| `batchWithdrawTokens` | 批量提取代币 |

### 查询函数

| 函数 | 描述 |
|------|------|
| `getTokenBalance` | 查询代币余额 |
| `getETHBalance` | 查询 ETH 余额 |
| `getRouterAllowance` | 查询路由器授权额度 |

## 费率说明

- **Uniswap V2 / SushiSwap**: 0.3%
- **PancakeSwap**: 0.25%（部分池子）

偿还公式：
```
amountOwed = amountBorrowed * 1000 / 997 + 1  // 0.3% 费率
amountOwed = amountBorrowed * 10000 / 9975 + 1  // 0.25% 费率
```

## 安全注意事项

1. **私钥安全**：永远不要在代码中硬编码私钥
2. **测试网先行**：先在测试网验证逻辑
3. **Gas 限制**：设置合理的 gas limit 避免 gas 耗尽
4. **MEV 保护**：考虑使用 Flashbots 或私有交易池
5. **紧急提取**：确保 `withdraw` 函数可用于紧急情况

## 未来扩展

合约预留了以下扩展接口：

- **Uniswap V3**: `uniswapV3SwapCallback`
- **Aave 闪电贷**: `executeOperation`
- **Balancer 闪电贷**: 可添加

## License

MIT


