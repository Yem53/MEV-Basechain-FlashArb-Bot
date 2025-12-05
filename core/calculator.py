#!/usr/bin/env python3
"""
套利计算模块 - HIGH PERFORMANCE VERSION

⚡ Zero-Latency Optimizations:
1. Integer-only arithmetic (no Decimal in hot path)
2. Pre-computed constants
3. Inlined calculations for speed
4. Minimal function call overhead

核心公式（Uniswap V3）：
- 基于 sqrtPriceX96 和 liquidity 计算价格影响
- Flash loan fee = pool fee (0.05%, 0.3%, 1%)
"""

import os
from typing import Tuple, Optional, List
from dataclasses import dataclass

# ============================================
# Pre-computed Constants (avoid runtime computation)
# ============================================

# Q96 constants
Q96 = 2 ** 96
Q96_SQUARED = 2 ** 192
Q96_FLOAT = float(Q96)

# Fee denominators (pre-computed)
FEE_DENOMINATOR = 1_000_000

# Golden ratio for search (pre-computed)
PHI = 1.618033988749895
RESPHI = 0.381966011250105  # 2 - PHI

# Load config
MAX_BORROW_ETH = float(os.getenv("MAX_BORROW_ETH", "20.0"))
MAX_BORROW_WEI = int(MAX_BORROW_ETH * 10**18)


# ============================================
# Data Structures
# ============================================

@dataclass
class V3PoolData:
    """V3 pool data for local calculations"""
    address: str
    token0: str
    token1: str
    fee: int                   # 500, 3000, 10000
    sqrtPriceX96: int          # Current sqrt price * 2^96
    liquidity: int             # Current tick liquidity
    decimals0: int = 18
    decimals1: int = 18


@dataclass
class V3ArbitrageResult:
    """V3 arbitrage calculation result"""
    profitable: bool
    profit: int               # Net profit (wei)
    amount_in: int            # Input amount (wei)
    amount_out_swap1: int     # First swap output
    amount_out_swap2: int     # Second swap output
    flash_fee: int            # Flash loan fee
    total_fees: int           # Total fees
    price_impact_pct: float   # Price impact percentage


# ============================================
# FAST Integer Math Functions
# ============================================

def sqrt_price_x96_to_price(
    sqrtPriceX96: int,
    decimals0: int = 18,
    decimals1: int = 18
) -> float:
    """
    Convert sqrtPriceX96 to price using FAST integer math.
    
    ⚡ No Decimal, pure integer/float operations.
    """
    if sqrtPriceX96 == 0:
        return 0.0
    
    # price = (sqrtPriceX96 / 2^96)^2 * 10^(dec0-dec1)
    # = sqrtPriceX96^2 * 10^(dec0-dec1) / 2^192
    
    price_squared = sqrtPriceX96 * sqrtPriceX96
    decimal_adj = 10 ** (decimals0 - decimals1)
    
    return (price_squared * decimal_adj) / Q96_SQUARED


def get_v3_amount_out_fast(
    amount_in: int,
    sqrtPriceX96: int,
    liquidity: int,
    fee: int,
    zero_for_one: bool
) -> Tuple[int, int]:
    """
    Calculate V3 swap output using FAST integer math.
    
    ⚡ Optimized for speed - no Decimal, minimal operations.
    
    V3 Formula (simplified for current tick):
    - For token0 -> token1: dy = L * (sqrt_P_old - sqrt_P_new)
    - For token1 -> token0: dx = L * (1/sqrt_P_new - 1/sqrt_P_old)
    """
    if amount_in <= 0 or liquidity <= 0 or sqrtPriceX96 <= 0:
        return 0, 0
    
    # Calculate fee (integer division)
    fee_amount = (amount_in * fee) // FEE_DENOMINATOR
    amount_after_fee = amount_in - fee_amount
    
    # Convert sqrtPriceX96 to float for calculation (faster than Decimal)
    sqrt_price = sqrtPriceX96 / Q96_FLOAT
    L = float(liquidity)
    dx = float(amount_after_fee)
    
    try:
        if zero_for_one:
            # token0 -> token1
            # sqrt_price_new = L * sqrt_price / (L + dx * sqrt_price)
            denominator = L + dx * sqrt_price
            if denominator <= 0:
                return 0, fee_amount
            
            sqrt_price_new = L * sqrt_price / denominator
            
            # dy = L * (sqrt_price - sqrt_price_new)
            dy = L * (sqrt_price - sqrt_price_new)
            amount_out = int(dy)
        else:
            # token1 -> token0
            # sqrt_price_new = sqrt_price + dy / L
            sqrt_price_new = sqrt_price + dx / L
            
            if sqrt_price_new <= 0:
                return 0, fee_amount
            
            # dx_out = L * (1/sqrt_price - 1/sqrt_price_new)
            dx_out = L * (1.0 / sqrt_price - 1.0 / sqrt_price_new)
            amount_out = int(dx_out)
        
        return max(0, amount_out), fee_amount
        
    except (ZeroDivisionError, OverflowError):
        return 0, fee_amount


def calculate_v3_arb_profit_fast(
    amount_in: int,
    pool_borrow: V3PoolData,
    pool_swap: V3PoolData,
    borrow_token_is_token0: bool = True
) -> V3ArbitrageResult:
    """
    Calculate V3 arbitrage profit using FAST math.
    
    ⚡ Inlined calculations, minimal function calls.
    
    Path:
    1. Flash borrow from pool_borrow
    2. Swap in pool_borrow (borrowed -> other)
    3. Swap in pool_swap (other -> borrowed)
    4. Repay flash loan + fee
    5. Profit = remaining
    """
    if amount_in <= 0:
        return V3ArbitrageResult(
            profitable=False, profit=0, amount_in=0,
            amount_out_swap1=0, amount_out_swap2=0,
            flash_fee=0, total_fees=0, price_impact_pct=0.0
        )
    
    # Flash loan fee (integer math)
    flash_fee = (amount_in * pool_borrow.fee) // FEE_DENOMINATOR
    
    # Swap 1: borrowed token -> other token
    swap1_out, swap1_fee = get_v3_amount_out_fast(
        amount_in=amount_in,
        sqrtPriceX96=pool_borrow.sqrtPriceX96,
        liquidity=pool_borrow.liquidity,
        fee=pool_borrow.fee,
        zero_for_one=borrow_token_is_token0
    )
    
    if swap1_out <= 0:
        return V3ArbitrageResult(
            profitable=False, profit=0, amount_in=amount_in,
            amount_out_swap1=0, amount_out_swap2=0,
            flash_fee=flash_fee, total_fees=flash_fee + swap1_fee,
            price_impact_pct=100.0
        )
    
    # Swap 2: other token -> borrowed token
    swap2_out, swap2_fee = get_v3_amount_out_fast(
        amount_in=swap1_out,
        sqrtPriceX96=pool_swap.sqrtPriceX96,
        liquidity=pool_swap.liquidity,
        fee=pool_swap.fee,
        zero_for_one=not borrow_token_is_token0
    )
    
    # Calculate profit
    repay_amount = amount_in + flash_fee
    profit = swap2_out - repay_amount
    profitable = profit > 0
    
    # Price impact (simplified)
    total_fees = flash_fee + swap1_fee + swap2_fee
    price_impact = max(0.0, (amount_in - swap2_out) / amount_in * 100) if amount_in > 0 else 0.0
    
    return V3ArbitrageResult(
        profitable=profitable,
        profit=profit,
        amount_in=amount_in,
        amount_out_swap1=swap1_out,
        amount_out_swap2=swap2_out,
        flash_fee=flash_fee,
        total_fees=total_fees,
        price_impact_pct=price_impact
    )


def find_optimal_amount_in_fast(
    pool_low: V3PoolData,
    pool_high: V3PoolData,
    min_amount: int = 10**16,
    max_amount: int = None,
    precision: int = 10**15,
    borrow_token_is_token0: bool = True,
    max_iterations: int = 30  # Reduced for speed
) -> Tuple[int, int, V3ArbitrageResult]:
    """
    Find optimal borrow amount using FAST Golden Section Search.
    
    ⚡ Optimizations:
    - Reduced iterations (30 vs 50)
    - Integer arithmetic where possible
    - Inlined profit calculation
    - Early termination on convergence
    """
    # Set defaults
    if max_amount is None:
        max_amount = MAX_BORROW_WEI
    
    # Safety: don't exceed pool liquidity
    max_safe = min(pool_low.liquidity // 10, pool_high.liquidity // 10)
    if max_safe > 0 and max_safe < max_amount:
        max_amount = max_safe
    
    if min_amount >= max_amount:
        min_amount = max(max_amount // 10, 10**15)
    
    # Golden section search
    low = min_amount
    high = max_amount
    
    # Initial points
    x1 = int(high - RESPHI * (high - low))
    x2 = int(low + RESPHI * (high - low))
    
    # Calculate initial profits
    result1 = calculate_v3_arb_profit_fast(x1, pool_low, pool_high, borrow_token_is_token0)
    result2 = calculate_v3_arb_profit_fast(x2, pool_low, pool_high, borrow_token_is_token0)
    f1, f2 = result1.profit, result2.profit
    
    # Track best
    if f1 > f2:
        best_amount, best_result, best_profit = x1, result1, f1
    else:
        best_amount, best_result, best_profit = x2, result2, f2
    
    # Iterate
    for _ in range(max_iterations):
        if (high - low) <= precision:
            break
        
        if f1 < f2:
            low = x1
            x1 = x2
            f1 = f2
            x2 = int(low + RESPHI * (high - low))
            result2 = calculate_v3_arb_profit_fast(x2, pool_low, pool_high, borrow_token_is_token0)
            f2 = result2.profit
            
            if f2 > best_profit:
                best_amount, best_result, best_profit = x2, result2, f2
        else:
            high = x2
            x2 = x1
            f2 = f1
            x1 = int(high - RESPHI * (high - low))
            result1 = calculate_v3_arb_profit_fast(x1, pool_low, pool_high, borrow_token_is_token0)
            f1 = result1.profit
            
            if f1 > best_profit:
                best_amount, best_result, best_profit = x1, result1, f1
    
    return best_amount, best_profit, best_result


def quick_profit_check_fast(
    pool_a: V3PoolData,
    pool_b: V3PoolData
) -> Tuple[bool, float]:
    """
    Quick check if arbitrage is possible.
    
    ⚡ Minimal computation for fast filtering.
    """
    # Fast price calculation
    if pool_a.sqrtPriceX96 == 0 or pool_b.sqrtPriceX96 == 0:
        return False, 0.0
    
    price_a = sqrt_price_x96_to_price(
        pool_a.sqrtPriceX96, pool_a.decimals0, pool_a.decimals1
    )
    price_b = sqrt_price_x96_to_price(
        pool_b.sqrtPriceX96, pool_b.decimals0, pool_b.decimals1
    )
    
    if price_a <= 0 or price_b <= 0:
        return False, 0.0
    
    # Price difference
    if price_a > price_b:
        diff_pct = (price_a - price_b) / price_b * 100
    else:
        diff_pct = (price_b - price_a) / price_a * 100
    
    # Need more than combined fees to profit
    min_fee_pct = (pool_a.fee + pool_b.fee) / 10000  # Convert to percentage
    
    return diff_pct > min_fee_pct * 1.5, diff_pct


# ============================================
# Legacy exports (for compatibility)
# ============================================

# Alias fast functions as default
find_optimal_amount_in = find_optimal_amount_in_fast
quick_profit_check = quick_profit_check_fast
get_v3_amount_out = get_v3_amount_out_fast
calculate_v3_arb_profit = calculate_v3_arb_profit_fast


# ============================================
# V2 Functions (kept for compatibility)
# ============================================

@dataclass
class ArbitrageResult:
    """V2 arbitrage result (legacy)"""
    profitable: bool
    profit: int
    borrow_amount: int
    repay_amount: int
    swap1_output: int
    swap2_output: int
    price_diff_bps: float


def get_amount_out(
    amount_in: int,
    reserve_in: int,
    reserve_out: int
) -> int:
    """V2 swap output calculation."""
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    
    amount_in_with_fee = amount_in * 997
    numerator = amount_in_with_fee * reserve_out
    denominator = reserve_in * 1000 + amount_in_with_fee
    
    return numerator // denominator


def estimate_gas_cost(
    gas_price_gwei: float = 0.01,
    flash_swap_gas: int = 250000
) -> int:
    """Estimate gas cost in wei."""
    gas_price_wei = int(gas_price_gwei * 10**9)
    return gas_price_wei * flash_swap_gas
