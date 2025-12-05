#!/usr/bin/env python3
"""
套利计算模块 - HIGH PERFORMANCE VERSION (HARDENED)

⚡ Zero-Latency Optimizations:
1. Integer-only arithmetic (no Decimal in hot path)
2. Pre-computed constants
3. Inlined calculations for speed
4. Minimal function call overhead

🛡️ Safety Layers (Base Chain):
1. L1 Data Fee calculation (OVM GasPriceOracle)
2. Quoter verification for tick crossing protection
3. Accurate total cost = L2 Gas + L1 Data Fee

核心公式（Uniswap V3）：
- 基于 sqrtPriceX96 和 liquidity 计算价格影响
- Flash loan fee = pool fee (0.05%, 0.3%, 1%)
"""

import os
from typing import Tuple, Optional, List
from dataclasses import dataclass, field
from web3 import Web3
from eth_abi import encode, decode

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
# Base Chain (OP Stack) - L1 Data Fee Constants
# ============================================

# OVM GasPriceOracle address (same on all OP Stack chains)
OVM_GAS_PRICE_ORACLE = "0x420000000000000000000000000000000000000F"

# Estimated TX data size for a V3 flash swap (bytes)
# startArbitrage(pool, token, amount, swapData) ≈ 4 + 32 + 32 + 32 + 68 = ~168 bytes calldata
# Plus RLP encoding overhead ≈ 500 bytes total
ESTIMATED_TX_DATA_SIZE = 500

# QuoterV2 address on Base
QUOTER_V2_ADDRESS = os.getenv("QUOTER_V2", "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a")

# Slippage tolerance for Quoter verification (0.5% = 50 bps)
SLIPPAGE_TOLERANCE_BPS = int(os.getenv("SLIPPAGE_TOLERANCE_BPS", "50"))

# Minimum profit after L1 fee consideration
MIN_PROFIT_AFTER_L1_FEE = int(os.getenv("MIN_PROFIT_AFTER_L1_FEE", str(int(0.0001 * 10**18))))  # 0.0001 ETH

# OVM GasPriceOracle ABI (minimal)
OVM_ORACLE_ABI = [
    {
        "inputs": [{"name": "_data", "type": "bytes"}],
        "name": "getL1Fee",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "l1BaseFee",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "overhead",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "scalar",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# QuoterV2 ABI (minimal for quoteExactInputSingle)
QUOTER_V2_ABI = [
    {
        "inputs": [{
            "components": [
                {"name": "tokenIn", "type": "address"},
                {"name": "tokenOut", "type": "address"},
                {"name": "amountIn", "type": "uint256"},
                {"name": "fee", "type": "uint24"},
                {"name": "sqrtPriceLimitX96", "type": "uint160"}
            ],
            "name": "params",
            "type": "tuple"
        }],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "sqrtPriceX96After", "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate", "type": "uint256"}
        ],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]


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


# ============================================
# 🛡️ Pitfall 1: L1 Data Fee Calculator (Base Chain)
# ============================================

class L1FeeCalculator:
    """
    Calculate L1 Data Fee for Base Chain (OP Stack).
    
    ⚠️ CRITICAL: On Base, total cost = L2 Gas + L1 Data Fee
    The L1 Data Fee can be 10x the L2 execution fee!
    
    Formula: L1Fee = L1BaseFee * (txDataGas + overhead) * scalar / 1e6
    """
    
    def __init__(self, w3: Web3):
        self.w3 = w3
        self._oracle_contract = None
        self._cached_l1_base_fee: Optional[int] = None
        self._cached_overhead: Optional[int] = None
        self._cached_scalar: Optional[int] = None
        self._cache_time: float = 0
        self._cache_ttl: float = 2.0  # Refresh every 2 seconds
        
    @property
    def oracle(self):
        """Lazy load OVM GasPriceOracle contract."""
        if self._oracle_contract is None:
            self._oracle_contract = self.w3.eth.contract(
                address=self.w3.to_checksum_address(OVM_GAS_PRICE_ORACLE),
                abi=OVM_ORACLE_ABI
            )
        return self._oracle_contract
    
    def _refresh_cache(self):
        """Refresh cached L1 fee parameters."""
        import time
        now = time.time()
        
        if self._cached_l1_base_fee is not None and now - self._cache_time < self._cache_ttl:
            return
        
        try:
            self._cached_l1_base_fee = self.oracle.functions.l1BaseFee().call()
            self._cached_overhead = self.oracle.functions.overhead().call()
            self._cached_scalar = self.oracle.functions.scalar().call()
            self._cache_time = now
        except Exception:
            # Fallback to conservative estimates
            self._cached_l1_base_fee = self.w3.to_wei(30, 'gwei')  # ~30 gwei L1
            self._cached_overhead = 2100
            self._cached_scalar = 1000000
    
    def get_l1_fee_for_tx_data(self, tx_data: bytes) -> int:
        """
        Get L1 Data Fee for specific transaction data.
        
        Args:
            tx_data: Raw transaction calldata bytes
            
        Returns:
            L1 fee in wei
        """
        try:
            return self.oracle.functions.getL1Fee(tx_data).call()
        except Exception:
            return self.estimate_l1_fee(len(tx_data))
    
    def estimate_l1_fee(self, data_size: int = ESTIMATED_TX_DATA_SIZE) -> int:
        """
        Estimate L1 fee based on data size.
        
        ⚡ Fast estimation without RPC call to getL1Fee.
        
        Formula: L1Fee = L1BaseFee * (16*zeroBytes + 4*nonZeroBytes + overhead) * scalar / 1e6
        Simplified: Assume 50% zero bytes for calldata
        """
        self._refresh_cache()
        
        # Gas per byte: 4 gas for zero, 16 gas for non-zero
        # Assume 50% mix = 10 gas per byte average
        data_gas = data_size * 10
        
        # Add overhead
        total_l1_gas = data_gas + (self._cached_overhead or 2100)
        
        # Apply scalar (usually 1e6 = 1.0x)
        scalar = self._cached_scalar or 1000000
        l1_base_fee = self._cached_l1_base_fee or self.w3.to_wei(30, 'gwei')
        
        l1_fee = (l1_base_fee * total_l1_gas * scalar) // 1000000
        
        return l1_fee
    
    def get_total_tx_cost(
        self,
        l2_gas_used: int,
        l2_gas_price: int,
        tx_data_size: int = ESTIMATED_TX_DATA_SIZE
    ) -> Tuple[int, int, int]:
        """
        Calculate total transaction cost including L1 fee.
        
        Returns:
            (total_cost, l2_cost, l1_fee)
        """
        l2_cost = l2_gas_used * l2_gas_price
        l1_fee = self.estimate_l1_fee(tx_data_size)
        total_cost = l2_cost + l1_fee
        
        return total_cost, l2_cost, l1_fee


# ============================================
# 🛡️ Pitfall 2: Quoter Verification (Tick Crossing)
# ============================================

@dataclass
class QuoterResult:
    """Result from Quoter verification."""
    success: bool
    amount_out: int = 0
    sqrt_price_after: int = 0
    ticks_crossed: int = 0
    gas_estimate: int = 0
    error: Optional[str] = None


class QuoterVerifier:
    """
    Verify swap outputs using Uniswap V3 QuoterV2.
    
    ⚠️ CRITICAL: Local math assumes infinite liquidity at current tick.
    Large trades cross ticks, causing different execution prices.
    ALWAYS verify with Quoter before execution!
    """
    
    def __init__(self, w3: Web3, quoter_address: str = QUOTER_V2_ADDRESS):
        self.w3 = w3
        self.quoter_address = self.w3.to_checksum_address(quoter_address)
        self._quoter_contract = None
    
    @property
    def quoter(self):
        """Lazy load QuoterV2 contract."""
        if self._quoter_contract is None:
            self._quoter_contract = self.w3.eth.contract(
                address=self.quoter_address,
                abi=QUOTER_V2_ABI
            )
        return self._quoter_contract
    
    def quote_exact_input_single(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        fee: int,
        sqrt_price_limit: int = 0
    ) -> QuoterResult:
        """
        Get exact output amount from Quoter.
        
        This is the TRUTH - it accounts for tick crossing.
        
        Args:
            token_in: Input token address
            token_out: Output token address
            amount_in: Input amount in wei
            fee: Pool fee tier (500, 3000, 10000)
            sqrt_price_limit: Price limit (0 = no limit)
            
        Returns:
            QuoterResult with real output amount
        """
        try:
            params = (
                self.w3.to_checksum_address(token_in),
                self.w3.to_checksum_address(token_out),
                amount_in,
                fee,
                sqrt_price_limit
            )
            
            # Quoter uses eth_call (no state change)
            result = self.quoter.functions.quoteExactInputSingle(params).call()
            
            return QuoterResult(
                success=True,
                amount_out=result[0],
                sqrt_price_after=result[1],
                ticks_crossed=result[2],
                gas_estimate=result[3]
            )
            
        except Exception as e:
            return QuoterResult(
                success=False,
                error=str(e)
            )
    
    def verify_arb_path(
        self,
        token_borrow: str,
        token_target: str,
        amount_borrow: int,
        fee_pool1: int,
        fee_pool2: int
    ) -> Tuple[bool, int, int, str]:
        """
        Verify complete arbitrage path with Quoter.
        
        Path: borrow -> swap1 -> swap2 -> repay
        
        Returns:
            (is_profitable, net_profit, amount_out_final, error_msg)
        """
        # Swap 1: borrow token -> target token
        quote1 = self.quote_exact_input_single(
            token_in=token_borrow,
            token_out=token_target,
            amount_in=amount_borrow,
            fee=fee_pool1
        )
        
        if not quote1.success:
            return False, 0, 0, f"Swap1 quote failed: {quote1.error}"
        
        if quote1.amount_out <= 0:
            return False, 0, 0, "Swap1 output is zero"
        
        # Swap 2: target token -> borrow token
        quote2 = self.quote_exact_input_single(
            token_in=token_target,
            token_out=token_borrow,
            amount_in=quote1.amount_out,
            fee=fee_pool2
        )
        
        if not quote2.success:
            return False, 0, 0, f"Swap2 quote failed: {quote2.error}"
        
        # Calculate profit
        flash_fee = (amount_borrow * fee_pool1) // FEE_DENOMINATOR
        repay_amount = amount_borrow + flash_fee
        net_profit = quote2.amount_out - repay_amount
        
        return net_profit > 0, net_profit, quote2.amount_out, ""
    
    def calculate_min_amount_out(
        self,
        quoted_amount: int,
        slippage_bps: int = SLIPPAGE_TOLERANCE_BPS
    ) -> int:
        """
        Calculate minimum acceptable output with slippage tolerance.
        
        ⚡ JIT Protection: Never send amountOutMinimum = 0
        
        Args:
            quoted_amount: Amount from Quoter
            slippage_bps: Slippage tolerance in basis points (50 = 0.5%)
            
        Returns:
            Minimum acceptable output amount
        """
        # min_out = quoted * (10000 - slippage) / 10000
        return (quoted_amount * (10000 - slippage_bps)) // 10000


# ============================================
# Combined Profit Calculator with Safety Checks
# ============================================

@dataclass
class SafeArbitrageResult:
    """Complete arbitrage result with all safety checks."""
    profitable: bool
    net_profit: int                    # After all fees
    gross_profit: int                  # Before gas
    amount_in: int
    amount_out_swap1: int              # From Quoter
    amount_out_swap2: int              # From Quoter
    min_amount_out_swap1: int          # With slippage protection
    min_amount_out_swap2: int          # With slippage protection
    flash_fee: int
    l2_gas_cost: int
    l1_data_fee: int
    total_gas_cost: int
    ticks_crossed_swap1: int = 0
    ticks_crossed_swap2: int = 0
    quoter_verified: bool = False
    error: Optional[str] = None


def calculate_safe_profit(
    w3: Web3,
    pool_borrow: V3PoolData,
    pool_swap: V3PoolData,
    amount_in: int,
    token_borrow: str,
    token_target: str,
    l2_gas_price: int,
    l2_gas_estimate: int = 350000,
    l1_fee_calculator: Optional[L1FeeCalculator] = None,
    quoter_verifier: Optional[QuoterVerifier] = None,
    skip_quoter: bool = False
) -> SafeArbitrageResult:
    """
    Calculate arbitrage profit with ALL safety checks.
    
    🛡️ Safety Layers:
    1. L1 Data Fee included in cost
    2. Quoter verification for tick crossing
    3. Slippage protection values calculated
    
    Args:
        w3: Web3 instance
        pool_borrow: Pool to flash borrow from
        pool_swap: Pool to swap back
        amount_in: Borrow amount
        token_borrow: Token being borrowed
        token_target: Target token for swap
        l2_gas_price: Current L2 gas price
        l2_gas_estimate: Estimated gas usage
        l1_fee_calculator: L1 fee calculator (or creates new)
        quoter_verifier: Quoter verifier (or creates new)
        skip_quoter: Skip Quoter verification (for fast scanning)
        
    Returns:
        SafeArbitrageResult with all details
    """
    # Initialize helpers
    if l1_fee_calculator is None:
        l1_fee_calculator = L1FeeCalculator(w3)
    if quoter_verifier is None:
        quoter_verifier = QuoterVerifier(w3)
    
    # Calculate gas costs (L2 + L1)
    total_gas_cost, l2_cost, l1_fee = l1_fee_calculator.get_total_tx_cost(
        l2_gas_used=l2_gas_estimate,
        l2_gas_price=l2_gas_price
    )
    
    # Flash loan fee
    flash_fee = (amount_in * pool_borrow.fee) // FEE_DENOMINATOR
    
    # Quick local math check first (fast filter)
    local_result = calculate_v3_arb_profit_fast(
        amount_in=amount_in,
        pool_borrow=pool_borrow,
        pool_swap=pool_swap,
        borrow_token_is_token0=(token_borrow.lower() == pool_borrow.token0.lower())
    )
    
    # If local math shows no profit, skip Quoter
    if not local_result.profitable and local_result.profit < -total_gas_cost:
        return SafeArbitrageResult(
            profitable=False,
            net_profit=local_result.profit - total_gas_cost,
            gross_profit=local_result.profit,
            amount_in=amount_in,
            amount_out_swap1=local_result.amount_out_swap1,
            amount_out_swap2=local_result.amount_out_swap2,
            min_amount_out_swap1=0,
            min_amount_out_swap2=0,
            flash_fee=flash_fee,
            l2_gas_cost=l2_cost,
            l1_data_fee=l1_fee,
            total_gas_cost=total_gas_cost,
            quoter_verified=False,
            error="Local math shows no profit"
        )
    
    # If skip_quoter, return local result with gas costs
    if skip_quoter:
        net_profit = local_result.profit - total_gas_cost
        return SafeArbitrageResult(
            profitable=net_profit > MIN_PROFIT_AFTER_L1_FEE,
            net_profit=net_profit,
            gross_profit=local_result.profit,
            amount_in=amount_in,
            amount_out_swap1=local_result.amount_out_swap1,
            amount_out_swap2=local_result.amount_out_swap2,
            min_amount_out_swap1=quoter_verifier.calculate_min_amount_out(local_result.amount_out_swap1),
            min_amount_out_swap2=quoter_verifier.calculate_min_amount_out(local_result.amount_out_swap2),
            flash_fee=flash_fee,
            l2_gas_cost=l2_cost,
            l1_data_fee=l1_fee,
            total_gas_cost=total_gas_cost,
            quoter_verified=False
        )
    
    # ⚠️ CRITICAL: Verify with Quoter (Pitfall 2)
    quote1 = quoter_verifier.quote_exact_input_single(
        token_in=token_borrow,
        token_out=token_target,
        amount_in=amount_in,
        fee=pool_borrow.fee
    )
    
    if not quote1.success:
        return SafeArbitrageResult(
            profitable=False,
            net_profit=0,
            gross_profit=0,
            amount_in=amount_in,
            amount_out_swap1=0,
            amount_out_swap2=0,
            min_amount_out_swap1=0,
            min_amount_out_swap2=0,
            flash_fee=flash_fee,
            l2_gas_cost=l2_cost,
            l1_data_fee=l1_fee,
            total_gas_cost=total_gas_cost,
            quoter_verified=False,
            error=f"Quoter swap1 failed: {quote1.error}"
        )
    
    quote2 = quoter_verifier.quote_exact_input_single(
        token_in=token_target,
        token_out=token_borrow,
        amount_in=quote1.amount_out,
        fee=pool_swap.fee
    )
    
    if not quote2.success:
        return SafeArbitrageResult(
            profitable=False,
            net_profit=0,
            gross_profit=0,
            amount_in=amount_in,
            amount_out_swap1=quote1.amount_out,
            amount_out_swap2=0,
            min_amount_out_swap1=0,
            min_amount_out_swap2=0,
            flash_fee=flash_fee,
            l2_gas_cost=l2_cost,
            l1_data_fee=l1_fee,
            total_gas_cost=total_gas_cost,
            ticks_crossed_swap1=quote1.ticks_crossed,
            quoter_verified=False,
            error=f"Quoter swap2 failed: {quote2.error}"
        )
    
    # Calculate verified profit
    repay_amount = amount_in + flash_fee
    gross_profit = quote2.amount_out - repay_amount
    net_profit = gross_profit - total_gas_cost
    
    # Calculate slippage-protected minimums (Pitfall 3)
    min_out_swap1 = quoter_verifier.calculate_min_amount_out(quote1.amount_out)
    min_out_swap2 = quoter_verifier.calculate_min_amount_out(quote2.amount_out)
    
    return SafeArbitrageResult(
        profitable=net_profit > MIN_PROFIT_AFTER_L1_FEE,
        net_profit=net_profit,
        gross_profit=gross_profit,
        amount_in=amount_in,
        amount_out_swap1=quote1.amount_out,
        amount_out_swap2=quote2.amount_out,
        min_amount_out_swap1=min_out_swap1,
        min_amount_out_swap2=min_out_swap2,
        flash_fee=flash_fee,
        l2_gas_cost=l2_cost,
        l1_data_fee=l1_fee,
        total_gas_cost=total_gas_cost,
        ticks_crossed_swap1=quote1.ticks_crossed,
        ticks_crossed_swap2=quote2.ticks_crossed,
        quoter_verified=True
    )
