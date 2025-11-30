#!/usr/bin/env python3
"""
套利计算模块

功能：
- 实现 Uniswap V2 AMM 数学公式
- 计算最优借入金额
- 计算套利利润

核心公式（Uniswap V2）：
- 输出金额 = (输入金额 * 997 * 储备Out) / (储备In * 1000 + 输入金额 * 997)
- 手续费 = 0.3%（997/1000）
- 闪电贷手续费 = 0.3%

使用示例：
    profit = calculate_arb_profit(
        borrow_amount=1e18,  # 1 WETH
        pair0_reserves=(reserve0_in, reserve0_out),
        pair1_reserves=(reserve1_in, reserve1_out),
        is_pair0_borrow=True
    )
"""

from typing import Tuple, Optional, List
from dataclasses import dataclass
from decimal import Decimal, getcontext

# 设置高精度小数计算
getcontext().prec = 78  # 足够处理 uint256


# ============================================
# 常量定义
# ============================================

# Uniswap V2 手续费因子（0.3% 手续费）
FEE_NUMERATOR = 997
FEE_DENOMINATOR = 1000

# 闪电贷手续费因子（0.3%）
FLASH_LOAN_FEE_NUMERATOR = 1000
FLASH_LOAN_FEE_DENOMINATOR = 997  # 需要还款 = 借款 * 1000 / 997


@dataclass
class ArbitrageResult:
    """套利计算结果"""
    profitable: bool           # 是否有利可图
    profit: int               # 净利润（wei）
    borrow_amount: int        # 借入金额（wei）
    repay_amount: int         # 还款金额（wei）
    swap1_output: int         # 第一次 swap 输出
    swap2_output: int         # 第二次 swap 输出
    price_diff_bps: float     # 价格差异（基点）


@dataclass
class PairReserves:
    """配对储备数据"""
    address: str              # 配对地址
    token0: str               # Token0 地址
    token1: str               # Token1 地址
    reserve0: int             # Token0 储备
    reserve1: int             # Token1 储备
    dex_name: str             # DEX 名称


# ============================================
# 核心数学函数
# ============================================

def get_amount_out(
    amount_in: int,
    reserve_in: int,
    reserve_out: int
) -> int:
    """
    计算 Uniswap V2 swap 的实际输出金额（考虑滑点）
    
    ⚠️ 重要：此函数计算的是 **实际输出金额**，不是现货价格！
    
    公式：output = (input * 997 * reserveOut) / (reserveIn * 1000 + input * 997)
    
    滑点说明：
    - 当 amount_in 相对于 reserve_in 较大时，输出会显著减少
    - 这就是"价格影响"或"滑点"
    - 例如：在 10 ETH 池中交易 5 ETH，滑点约 33%
    
    为什么不能用现货价格（reserveOut / reserveIn）：
    - 现货价格假设交易量为 0，忽略了价格影响
    - 实际交易会消耗流动性，导致价格变化
    - 使用现货价格会高估利润，导致亏损交易
    
    参数：
        amount_in: 输入金额（wei）
        reserve_in: 输入代币的储备量
        reserve_out: 输出代币的储备量
        
    返回：
        实际输出金额（wei），已考虑 0.3% 手续费和滑点
    """
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    
    # 使用整数运算避免精度问题
    # 公式分解：
    # amount_in_with_fee = amount_in * 997 (扣除 0.3% 手续费)
    # numerator = amount_in_with_fee * reserve_out
    # denominator = reserve_in * 1000 + amount_in_with_fee (流动性调整)
    amount_in_with_fee = amount_in * FEE_NUMERATOR
    numerator = amount_in_with_fee * reserve_out
    denominator = reserve_in * FEE_DENOMINATOR + amount_in_with_fee
    
    return numerator // denominator


def get_amount_in(
    amount_out: int,
    reserve_in: int,
    reserve_out: int
) -> int:
    """
    计算获得指定输出所需的输入金额
    
    公式：input = (reserveIn * amountOut * 1000) / ((reserveOut - amountOut) * 997) + 1
    
    参数：
        amount_out: 期望的输出金额（wei）
        reserve_in: 输入代币的储备量
        reserve_out: 输出代币的储备量
        
    返回：
        所需输入金额（wei）
    """
    if amount_out <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    
    if amount_out >= reserve_out:
        return 0  # 无法提取超过储备的金额
    
    numerator = reserve_in * amount_out * FEE_DENOMINATOR
    denominator = (reserve_out - amount_out) * FEE_NUMERATOR
    
    return numerator // denominator + 1


def get_flash_loan_repayment(borrow_amount: int) -> int:
    """
    计算闪电贷还款金额
    
    公式：repayment = borrow * 1000 / 997 + 1
    
    参数：
        borrow_amount: 借入金额（wei）
        
    返回：
        还款金额（wei）
    """
    return (borrow_amount * FLASH_LOAN_FEE_NUMERATOR) // FLASH_LOAN_FEE_DENOMINATOR + 1


def get_price_ratio(reserve_in: int, reserve_out: int) -> float:
    """
    计算价格比率（输出/输入）
    
    参数：
        reserve_in: 输入代币储备
        reserve_out: 输出代币储备
        
    返回：
        价格比率
    """
    if reserve_in == 0:
        return 0.0
    return reserve_out / reserve_in


def get_price_diff_bps(price1: float, price2: float) -> float:
    """
    计算两个价格之间的差异（基点）
    
    参数：
        price1: 价格1
        price2: 价格2
        
    返回：
        差异（基点，1 bp = 0.01%）
    """
    if price1 == 0 or price2 == 0:
        return 0.0
    
    diff = abs(price1 - price2) / min(price1, price2)
    return diff * 10000  # 转换为基点


# ============================================
# 套利利润计算
# ============================================

def calculate_arb_profit(
    borrow_amount: int,
    pair0_reserves: Tuple[int, int],
    pair1_reserves: Tuple[int, int],
    borrow_is_token0: bool = True
) -> ArbitrageResult:
    """
    计算两个配对之间的套利利润（考虑滑点）
    
    ⚠️ 重要：此函数使用 get_amount_out() 计算实际输出，完全考虑滑点！
    
    套利路径：
    1. 从 Pair0 借入代币 A（闪电贷）
    2. 在 Pair0 用 A 换 B（使用 get_amount_out 计算实际输出）
    3. 在 Pair1 用 B 换回 A（使用 get_amount_out 计算实际输出）
    4. 偿还闪电贷（A + 0.3% 手续费）
    5. 剩余的 A 就是利润
    
    滑点处理：
    - 每次 swap 都使用 Uniswap V2 AMM 公式计算实际输出
    - 公式：output = (input * 997 * reserveOut) / (reserveIn * 1000 + input * 997)
    - 不使用现货价格（reserveOut / reserveIn），因为那会忽略滑点
    
    参数：
        borrow_amount: 借入金额（wei）
        pair0_reserves: Pair0 的储备 (reserveA, reserveB)
        pair1_reserves: Pair1 的储备 (reserveA, reserveB)
        borrow_is_token0: 借入的是否是 token0
        
    返回：
        ArbitrageResult 结果对象
    """
    if borrow_amount <= 0:
        return ArbitrageResult(
            profitable=False,
            profit=0,
            borrow_amount=0,
            repay_amount=0,
            swap1_output=0,
            swap2_output=0,
            price_diff_bps=0.0
        )
    
    # 确定储备方向
    if borrow_is_token0:
        reserve0_in, reserve0_out = pair0_reserves
        reserve1_in, reserve1_out = pair1_reserves
    else:
        reserve0_out, reserve0_in = pair0_reserves
        reserve1_out, reserve1_in = pair1_reserves
    
    # 计算价格差异
    price0 = get_price_ratio(reserve0_in, reserve0_out)
    price1 = get_price_ratio(reserve1_out, reserve1_in)  # 反向
    price_diff_bps = get_price_diff_bps(price0, price1)
    
    # 步骤 1: 在 Pair0 swap A -> B
    # 借入 A，换成 B
    swap1_output = get_amount_out(borrow_amount, reserve0_in, reserve0_out)
    
    if swap1_output <= 0:
        return ArbitrageResult(
            profitable=False,
            profit=0,
            borrow_amount=borrow_amount,
            repay_amount=0,
            swap1_output=0,
            swap2_output=0,
            price_diff_bps=price_diff_bps
        )
    
    # 步骤 2: 在 Pair1 swap B -> A
    # 用 B 换回 A
    swap2_output = get_amount_out(swap1_output, reserve1_out, reserve1_in)
    
    # 步骤 3: 计算还款金额
    repay_amount = get_flash_loan_repayment(borrow_amount)
    
    # 步骤 4: 计算利润
    if swap2_output > repay_amount:
        profit = swap2_output - repay_amount
        profitable = True
    else:
        profit = swap2_output - repay_amount  # 负数表示亏损
        profitable = False
    
    return ArbitrageResult(
        profitable=profitable,
        profit=profit,
        borrow_amount=borrow_amount,
        repay_amount=repay_amount,
        swap1_output=swap1_output,
        swap2_output=swap2_output,
        price_diff_bps=price_diff_bps
    )


def calculate_arb_profit_reverse(
    borrow_amount: int,
    pair0_reserves: Tuple[int, int],
    pair1_reserves: Tuple[int, int],
    borrow_is_token0: bool = True
) -> ArbitrageResult:
    """
    计算反向套利利润（先在 Pair1 swap，再在 Pair0 swap）
    
    参数：
        borrow_amount: 借入金额（wei）
        pair0_reserves: Pair0 的储备 (reserveA, reserveB)
        pair1_reserves: Pair1 的储备 (reserveA, reserveB)
        borrow_is_token0: 借入的是否是 token0
        
    返回：
        ArbitrageResult 结果对象
    """
    # 交换 pair0 和 pair1 的位置
    return calculate_arb_profit(
        borrow_amount=borrow_amount,
        pair0_reserves=pair1_reserves,
        pair1_reserves=pair0_reserves,
        borrow_is_token0=borrow_is_token0
    )


# ============================================
# 最优借入金额搜索
# ============================================

def find_optimal_borrow_amount(
    pair0_reserves: Tuple[int, int],
    pair1_reserves: Tuple[int, int],
    borrow_is_token0: bool = True,
    min_amount: int = 10**15,       # 0.001 ETH
    max_amount: int = 100 * 10**18, # 100 ETH
    precision: int = 10**15         # 搜索精度
) -> Tuple[int, ArbitrageResult]:
    """
    使用二分搜索找到最优借入金额
    
    参数：
        pair0_reserves: Pair0 的储备
        pair1_reserves: Pair1 的储备
        borrow_is_token0: 借入的是否是 token0
        min_amount: 最小借入金额
        max_amount: 最大借入金额
        precision: 搜索精度
        
    返回：
        (最优借入金额, 套利结果)
    """
    best_amount = 0
    best_result = ArbitrageResult(
        profitable=False,
        profit=0,
        borrow_amount=0,
        repay_amount=0,
        swap1_output=0,
        swap2_output=0,
        price_diff_bps=0.0
    )
    
    # 使用黄金分割搜索
    phi = 1.618033988749895
    
    low = min_amount
    high = max_amount
    
    while high - low > precision:
        # 两个测试点
        mid1 = int(high - (high - low) / phi)
        mid2 = int(low + (high - low) / phi)
        
        result1 = calculate_arb_profit(mid1, pair0_reserves, pair1_reserves, borrow_is_token0)
        result2 = calculate_arb_profit(mid2, pair0_reserves, pair1_reserves, borrow_is_token0)
        
        if result1.profit > result2.profit:
            high = mid2
            if result1.profit > best_result.profit:
                best_amount = mid1
                best_result = result1
        else:
            low = mid1
            if result2.profit > best_result.profit:
                best_amount = mid2
                best_result = result2
    
    # 检查边界值
    for test_amount in [min_amount, max_amount, (low + high) // 2]:
        result = calculate_arb_profit(test_amount, pair0_reserves, pair1_reserves, borrow_is_token0)
        if result.profit > best_result.profit:
            best_amount = test_amount
            best_result = result
    
    return best_amount, best_result


def find_optimal_borrow_fixed_steps(
    pair0_reserves: Tuple[int, int],
    pair1_reserves: Tuple[int, int],
    borrow_is_token0: bool = True,
    test_amounts: Optional[List[int]] = None
) -> Tuple[int, ArbitrageResult]:
    """
    使用固定步长测试找到最优借入金额（更快但精度较低）
    
    参数：
        pair0_reserves: Pair0 的储备
        pair1_reserves: Pair1 的储备
        borrow_is_token0: 借入的是否是 token0
        test_amounts: 测试金额列表（默认为 0.1, 0.5, 1, 5, 10, 50, 100 ETH）
        
    返回：
        (最优借入金额, 套利结果)
    """
    if test_amounts is None:
        # 默认测试金额：从 0.01 ETH 到 100 ETH
        test_amounts = [
            10**16,     # 0.01 ETH
            5 * 10**16, # 0.05 ETH
            10**17,     # 0.1 ETH
            5 * 10**17, # 0.5 ETH
            10**18,     # 1 ETH
            5 * 10**18, # 5 ETH
            10 * 10**18,  # 10 ETH
            50 * 10**18,  # 50 ETH
            100 * 10**18, # 100 ETH
        ]
    
    best_amount = 0
    best_result = ArbitrageResult(
        profitable=False,
        profit=0,
        borrow_amount=0,
        repay_amount=0,
        swap1_output=0,
        swap2_output=0,
        price_diff_bps=0.0
    )
    
    for amount in test_amounts:
        result = calculate_arb_profit(amount, pair0_reserves, pair1_reserves, borrow_is_token0)
        if result.profit > best_result.profit:
            best_amount = amount
            best_result = result
    
    return best_amount, best_result


def check_both_directions(
    pair0_reserves: Tuple[int, int],
    pair1_reserves: Tuple[int, int],
    borrow_is_token0: bool = True,
    test_amounts: Optional[List[int]] = None
) -> Tuple[str, int, ArbitrageResult]:
    """
    检查两个方向的套利机会
    
    参数：
        pair0_reserves: Pair0 的储备
        pair1_reserves: Pair1 的储备
        borrow_is_token0: 借入的是否是 token0
        test_amounts: 测试金额列表
        
    返回：
        (方向, 最优借入金额, 套利结果)
        方向: "forward" 或 "reverse"
    """
    # 正向：Pair0 swap A->B, Pair1 swap B->A
    fwd_amount, fwd_result = find_optimal_borrow_fixed_steps(
        pair0_reserves, pair1_reserves, borrow_is_token0, test_amounts
    )
    
    # 反向：Pair1 swap A->B, Pair0 swap B->A
    rev_amount, rev_result = find_optimal_borrow_fixed_steps(
        pair1_reserves, pair0_reserves, borrow_is_token0, test_amounts
    )
    
    if fwd_result.profit >= rev_result.profit:
        return "forward", fwd_amount, fwd_result
    else:
        return "reverse", rev_amount, rev_result


# ============================================
# Gas 成本估算
# ============================================

def estimate_gas_cost(
    gas_price_gwei: float = 0.01,  # Base 的 gas 价格很低
    flash_swap_gas: int = 250000   # 估计的 gas 消耗
) -> int:
    """
    估算执行套利的 Gas 成本（以 wei 为单位）
    
    参数：
        gas_price_gwei: Gas 价格（Gwei）
        flash_swap_gas: 预估的 gas 消耗量
        
    返回：
        Gas 成本（wei）
    """
    gas_price_wei = int(gas_price_gwei * 10**9)
    return gas_price_wei * flash_swap_gas


def is_profitable_after_gas(
    arb_result: ArbitrageResult,
    gas_cost_wei: int,
    min_profit_wei: int = 0
) -> Tuple[bool, int]:
    """
    检查扣除 Gas 成本后是否有利可图
    
    参数：
        arb_result: 套利计算结果
        gas_cost_wei: Gas 成本（wei）
        min_profit_wei: 最小利润要求（wei）
        
    返回：
        (是否有利可图, 净利润)
    """
    if not arb_result.profitable:
        return False, arb_result.profit - gas_cost_wei
    
    net_profit = arb_result.profit - gas_cost_wei
    return net_profit > min_profit_wei, net_profit


# ============================================
# 测试代码
# ============================================

if __name__ == "__main__":
    from decimal import Decimal
    
    print("=" * 60)
    print("套利计算器测试")
    print("=" * 60)
    
    # 模拟两个配对的储备
    # Pair0: WETH/USDC on BaseSwap
    # 假设 1 WETH = 3000 USDC
    pair0_reserves = (
        2_274_170_525_766_754_739,  # ~2.27 WETH (18 decimals)
        6_805_892_347               # ~6805 USDC (6 decimals)
    )
    
    # Pair1: WETH/USDC on Uniswap V2
    # 假设价格略有不同
    pair1_reserves = (
        62_001_035_300_825_768,     # ~0.062 WETH
        185_423_228                  # ~185 USDC
    )
    
    print("\n配对储备:")
    print(f"Pair0 (BaseSwap):")
    print(f"  WETH: {pair0_reserves[0] / 10**18:.4f}")
    print(f"  USDC: {pair0_reserves[1] / 10**6:.2f}")
    print(f"  价格: {(pair0_reserves[1] / 10**6) / (pair0_reserves[0] / 10**18):.2f} USDC/WETH")
    
    print(f"\nPair1 (Uniswap V2):")
    print(f"  WETH: {pair1_reserves[0] / 10**18:.4f}")
    print(f"  USDC: {pair1_reserves[1] / 10**6:.2f}")
    print(f"  价格: {(pair1_reserves[1] / 10**6) / (pair1_reserves[0] / 10**18):.2f} USDC/WETH")
    
    # 测试固定金额
    print("\n测试固定借入金额 (1 ETH):")
    result = calculate_arb_profit(
        borrow_amount=10**18,  # 1 ETH
        pair0_reserves=pair0_reserves,
        pair1_reserves=pair1_reserves,
        borrow_is_token0=True
    )
    
    print(f"  借入: {result.borrow_amount / 10**18:.4f} WETH")
    print(f"  Swap1 输出: {result.swap1_output / 10**6:.4f} USDC")
    print(f"  Swap2 输出: {result.swap2_output / 10**18:.6f} WETH")
    print(f"  还款: {result.repay_amount / 10**18:.6f} WETH")
    print(f"  利润: {result.profit / 10**18:.6f} WETH")
    print(f"  有利可图: {result.profitable}")
    print(f"  价格差异: {result.price_diff_bps:.2f} bps")
    
    # 搜索最优金额
    print("\n搜索最优借入金额...")
    direction, opt_amount, opt_result = check_both_directions(
        pair0_reserves=pair0_reserves,
        pair1_reserves=pair1_reserves,
        borrow_is_token0=True
    )
    
    print(f"  方向: {direction}")
    print(f"  最优借入: {opt_amount / 10**18:.4f} WETH")
    print(f"  最大利润: {opt_result.profit / 10**18:.6f} WETH")
    
    # Gas 成本分析
    gas_cost = estimate_gas_cost(gas_price_gwei=0.01)
    is_profitable, net_profit = is_profitable_after_gas(opt_result, gas_cost)
    
    print(f"\nGas 成本分析:")
    print(f"  预估 Gas 成本: {gas_cost / 10**18:.8f} ETH")
    print(f"  净利润: {net_profit / 10**18:.6f} WETH")
    print(f"  扣除 Gas 后有利可图: {is_profitable}")
    
    print("\n测试完成!")

