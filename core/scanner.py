#!/usr/bin/env python3
"""
Uniswap V3 Arbitrage Scanner - HIGH PERFORMANCE VERSION

⚡ Zero-Latency Optimizations:
1. Super-Batch Scanning: ONE Multicall per scan cycle
2. Pre-computed pool addresses at startup
3. Pre-encoded calldata (no encoding in hot path)
4. Local-only math (no RPC in calculations)
5. orjson for fast JSON parsing (if available)

Base Mainnet Constants:
- V3 Factory: 0x33128a8fC17869897dcE68Ed026d694621f6FDfD
- Init Code Hash: 0xe34f199b19b2b4f47f68442619d555527d244f78a3297ea89325f843f87b8b54
"""

import os
import time
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

from web3 import Web3
from eth_abi import encode, decode

# Try to import orjson for faster JSON parsing (10x faster than stdlib)
try:
    import orjson
    HAS_ORJSON = True
except ImportError:
    HAS_ORJSON = False

# Import optimization functions from calculator
from .calculator import (
    V3PoolData,
    V3ArbitrageResult,
    find_optimal_amount_in_fast,
    quick_profit_check_fast,
)

# ============================================
# V3 Constants - Load from env or use defaults
# ============================================

V3_FACTORY = os.getenv("V3_FACTORY", "0x33128a8fC17869897dcE68Ed026d694621f6FDfD")
SWAP_ROUTER = os.getenv("SWAP_ROUTER", "0x2626664c2603336E57B271c5C0b26F421741e481")
WETH = os.getenv("WETH", "0x4200000000000000000000000000000000000006")
POOL_INIT_CODE_HASH = os.getenv("POOL_INIT_CODE_HASH", "0xe34f199b19b2b4f47f68442619d555527d244f78a3297ea89325f843f87b8b54")
MULTICALL3 = os.getenv("MULTICALL3", "0xcA11bde05977b3631167028862bE2a173976CA11")

# Fee tiers from env or defaults
FEE_TIERS_STR = os.getenv("FEE_TIERS", "500,3000,10000")
FEE_TIERS = [int(f.strip()) for f in FEE_TIERS_STR.split(",")]
FEE_NAMES = {500: "0.05%", 3000: "0.3%", 10000: "1%", 100: "0.01%"}

# Minimum liquidity threshold
MIN_LIQUIDITY = int(os.getenv("MIN_LIQUIDITY", "1000000000000000"))  # 10^15

# Debug mode
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

# Dynamic amount optimization settings
MIN_BORROW_ETH = float(os.getenv("MIN_BORROW_ETH", "0.01"))
MAX_BORROW_ETH = float(os.getenv("MAX_BORROW_ETH", "20.0"))
AMOUNT_PRECISION_ETH = float(os.getenv("AMOUNT_PRECISION_ETH", "0.001"))

# ============================================
# Pre-computed constants (avoid runtime computation)
# ============================================

# Function selectors (pre-computed bytes)
SLOT0_SELECTOR_BYTES = bytes.fromhex("3850c7bd")
LIQUIDITY_SELECTOR_BYTES = bytes.fromhex("1a686502")

# Pre-compute init code hash bytes
INIT_CODE_HASH_BYTES = bytes.fromhex(POOL_INIT_CODE_HASH[2:])
FACTORY_BYTES = bytes.fromhex(V3_FACTORY[2:])

# Q96 for price calculations (pre-computed)
Q96 = 2 ** 96
Q96_SQUARED = Q96 * Q96

# ============================================
# Data Structures
# ============================================

@dataclass
class V3Pool:
    """V3 Pool information with pre-computed data"""
    address: str
    token0: str
    token1: str
    fee: int
    # Pre-computed for hot path
    address_bytes: bytes = field(default=b'', repr=False)
    # Runtime data
    sqrtPriceX96: int = 0
    tick: int = 0
    liquidity: int = 0
    price_0_to_1: float = 0.0
    price_1_to_0: float = 0.0
    last_update: float = 0
    # Token metadata
    decimals0: int = 18
    decimals1: int = 18


@dataclass
class ArbitrageOpportunity:
    """Arbitrage opportunity between two pools"""
    pool_low: V3Pool
    pool_high: V3Pool
    token_borrow: str
    borrow_amount: int
    expected_profit: int
    flash_fee: int
    net_profit: int
    price_diff_pct: float
    direction: str
    timestamp: float
    is_optimized: bool = True
    swap1_output: int = 0
    swap2_output: int = 0
    price_impact_pct: float = 0.0


@dataclass
class ScanResult:
    """Scan cycle result"""
    opportunities: List[ArbitrageOpportunity] = field(default_factory=list)
    pools_scanned: int = 0
    pools_active: int = 0
    time_network_ms: float = 0.0
    time_calc_ms: float = 0.0


# ============================================
# Optimized Price Math (Integer-only)
# ============================================

def sqrt_price_x96_to_price_fast(
    sqrtPriceX96: int,
    decimals0: int = 18,
    decimals1: int = 18
) -> Tuple[float, float]:
    """
    Convert sqrtPriceX96 to price using fast integer math.
    
    Optimized: No Decimal, pure integer/float operations.
    """
    if sqrtPriceX96 == 0:
        return 0.0, 0.0
    
    # price = (sqrtPriceX96 / 2^96)^2 = sqrtPriceX96^2 / 2^192
    # We compute this as: sqrtPriceX96^2 * 10^(dec0-dec1) / 2^192
    
    price_squared = sqrtPriceX96 * sqrtPriceX96
    
    # Adjust for decimals and Q192
    # price_0_to_1 = price_squared * 10^(dec0-dec1) / 2^192
    decimal_adj = 10 ** (decimals0 - decimals1)
    
    # Use float division for final result
    price_0_to_1 = (price_squared * decimal_adj) / Q96_SQUARED
    price_1_to_0 = 1.0 / price_0_to_1 if price_0_to_1 > 0 else 0.0
    
    return price_0_to_1, price_1_to_0


def compute_pool_address_fast(
    token0: str,
    token1: str,
    fee: int
) -> str:
    """
    Compute V3 pool address using optimized CREATE2.
    
    Pre-sorts tokens and uses cached factory/init bytes.
    """
    # Sort tokens (lowercase comparison)
    t0_lower = token0.lower()
    t1_lower = token1.lower()
    
    if t0_lower > t1_lower:
        token0, token1 = token1, token0
    
    # Encode salt using pre-imported encode
    salt = Web3.keccak(encode(
        ['address', 'address', 'uint24'],
        [Web3.to_checksum_address(token0), Web3.to_checksum_address(token1), fee]
    ))
    
    # Compute CREATE2 address using cached bytes
    create2_input = b'\xff' + FACTORY_BYTES + salt + INIT_CODE_HASH_BYTES
    pool_hash = Web3.keccak(create2_input)[-20:]
    
    return Web3.to_checksum_address(pool_hash.hex())


# ============================================
# Pre-computed Multicall Data
# ============================================

class MulticallBatch:
    """
    Pre-computed multicall batch for zero-latency scanning.
    
    All calldata is pre-encoded at startup.
    """
    
    def __init__(self, pools: List[V3Pool]):
        self.pools = pools
        self.call_count = len(pools) * 2  # slot0 + liquidity per pool
        
        # Pre-encode all calls once
        self.encoded_calls: List[Tuple[str, bool, bytes]] = []
        for pool in pools:
            addr = Web3.to_checksum_address(pool.address)
            # slot0 call
            self.encoded_calls.append((addr, True, SLOT0_SELECTOR_BYTES))
            # liquidity call
            self.encoded_calls.append((addr, True, LIQUIDITY_SELECTOR_BYTES))
    
    def get_calls(self) -> List[Tuple[str, bool, bytes]]:
        """Return pre-encoded calls (no computation needed)."""
        return self.encoded_calls


# ============================================
# V3 Scanner Class - OPTIMIZED
# ============================================

class V3Scanner:
    """
    High-Performance Uniswap V3 Arbitrage Scanner
    
    ⚡ Optimizations:
    - Pre-computed pool addresses at startup
    - Pre-encoded Multicall calldata
    - Single RPC call per scan cycle
    - Local-only profit calculations
    """
    
    def __init__(
        self,
        w3: Web3,
        target_tokens: List[Dict],
        fee_tiers: List[int] = None,
        min_liquidity: int = MIN_LIQUIDITY
    ):
        self.w3 = w3
        self.target_tokens = target_tokens
        self.fee_tiers = fee_tiers or FEE_TIERS
        self.min_liquidity = min_liquidity
        
        # Pool storage
        self.pools: Dict[str, V3Pool] = {}
        self.pool_list: List[V3Pool] = []  # Ordered list for fast iteration
        
        # Token decimals cache
        self.decimals: Dict[str, int] = {WETH.lower(): 18}
        for token in target_tokens:
            self.decimals[token["address"].lower()] = token.get("decimals", 18)
        
        # Pre-computed Multicall batch (set after discovery)
        self._multicall_batch: Optional[MulticallBatch] = None
        
        # Multicall3 contract (cached)
        self._multicall_contract = None
        
        # Stats
        self.scan_count = 0
    
    def _get_multicall_contract(self):
        """Get or create cached Multicall3 contract."""
        if self._multicall_contract is None:
            self._multicall_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(MULTICALL3),
                abi=[{
                    "inputs": [{"components": [
                        {"name": "target", "type": "address"},
                        {"name": "allowFailure", "type": "bool"},
                        {"name": "callData", "type": "bytes"}
                    ], "name": "calls", "type": "tuple[]"}],
                    "name": "aggregate3",
                    "outputs": [{"components": [
                        {"name": "success", "type": "bool"},
                        {"name": "returnData", "type": "bytes"}
                    ], "type": "tuple[]"}],
                    "stateMutability": "view",
                    "type": "function"
                }]
            )
        return self._multicall_contract
    
    def discover_pools(self, base_token: str = WETH) -> List[V3Pool]:
        """
        Discover V3 pools and pre-compute all addresses.
        
        This runs ONCE at startup. All pool addresses are computed
        deterministically without RPC calls.
        """
        discovered = []
        check_addresses = []
        
        print(f"\n🔍 Pre-computing V3 pool addresses...")
        
        # Phase 1: Compute all possible pool addresses (no RPC)
        for token_config in self.target_tokens:
            token = token_config["address"]
            symbol = token_config.get("symbol", "???")
            decimals = token_config.get("decimals", 18)
            
            for fee in self.fee_tiers:
                pool_address = compute_pool_address_fast(base_token, token, fee)
                
                # Determine token0/token1 order
                if base_token.lower() < token.lower():
                    t0, t1 = base_token, token
                    dec0, dec1 = 18, decimals
                else:
                    t0, t1 = token, base_token
                    dec0, dec1 = decimals, 18
                
                pool = V3Pool(
                    address=pool_address,
                    token0=Web3.to_checksum_address(t0),
                    token1=Web3.to_checksum_address(t1),
                    fee=fee,
                    address_bytes=bytes.fromhex(pool_address[2:]),
                    decimals0=dec0,
                    decimals1=dec1
                )
                
                check_addresses.append((pool, symbol))
        
        print(f"   📊 Computed {len(check_addresses)} potential pools")
        
        # Phase 2: Batch check which pools exist (single Multicall)
        print(f"   🔗 Verifying pool existence...")
        
        # Build code check calls
        code_calls = []
        for pool, _ in check_addresses:
            code_calls.append((
                Web3.to_checksum_address(pool.address),
                True,
                bytes()  # Empty calldata = check for code
            ))
        
        # Use eth_getCode in batch via Multicall
        # Actually, we need to check code existence differently
        # Let's do a simple slot0 call - if it fails, pool doesn't exist
        
        slot0_calls = [
            (Web3.to_checksum_address(p.address), True, SLOT0_SELECTOR_BYTES)
            for p, _ in check_addresses
        ]
        
        try:
            multicall = self._get_multicall_contract()
            results = multicall.functions.aggregate3(slot0_calls).call()
            
            for i, ((pool, symbol), result) in enumerate(zip(check_addresses, results)):
                if result[0] and len(result[1]) >= 64:
                    # Pool exists and has valid slot0
                    discovered.append(pool)
                    self.pools[pool.address.lower()] = pool
                    print(f"  ✅ [{symbol}] {FEE_NAMES[pool.fee]}: {pool.address[:16]}...")
        
        except Exception as e:
            print(f"  ⚠️ Batch verification failed: {e}")
            # Fallback: check individually
            for pool, symbol in check_addresses:
                try:
                    code = self.w3.eth.get_code(pool.address)
                    if len(code) > 2:
                        discovered.append(pool)
                        self.pools[pool.address.lower()] = pool
                        print(f"  ✅ [{symbol}] {FEE_NAMES[pool.fee]}: {pool.address[:16]}...")
                except:
                    pass
        
        # Phase 3: Create ordered pool list and pre-compute Multicall batch
        self.pool_list = list(self.pools.values())
        self._multicall_batch = MulticallBatch(self.pool_list)
        
        print(f"\n📊 Discovered {len(discovered)} V3 pools")
        print(f"   ⚡ Multicall batch pre-encoded ({self._multicall_batch.call_count} calls)")
        
        return discovered
    
    def update_pool_data(self) -> Tuple[bool, float, int]:
        """
        Super-Batch update: ONE Multicall for ALL pools.
        
        ⚡ Uses pre-encoded calldata - zero encoding overhead.
        """
        if not self._multicall_batch or not self.pool_list:
            return False, 0.0, 0
        
        try:
            t_start = time.time()
            
            # Execute pre-encoded Multicall
            multicall = self._get_multicall_contract()
            results = multicall.functions.aggregate3(
                self._multicall_batch.get_calls()
            ).call()
            
            network_ms = (time.time() - t_start) * 1000
            
            # Parse results (optimized loop)
            success_count = 0
            now = time.time()
            
            for i, pool in enumerate(self.pool_list):
                idx = i * 2
                slot0_result = results[idx]
                liquidity_result = results[idx + 1]
                
                # Parse slot0 (inline for speed)
                if slot0_result[0] and len(slot0_result[1]) >= 64:
                    try:
                        decoded = decode(
                            ['uint160', 'int24', 'uint16', 'uint16', 'uint16', 'uint8', 'bool'],
                            slot0_result[1]
                        )
                        pool.sqrtPriceX96 = decoded[0]
                        pool.tick = decoded[1]
                        
                        # Fast price calculation
                        pool.price_0_to_1, pool.price_1_to_0 = sqrt_price_x96_to_price_fast(
                            pool.sqrtPriceX96, pool.decimals0, pool.decimals1
                        )
                        success_count += 1
                    except:
                        pass
                
                # Parse liquidity (inline for speed)
                if liquidity_result[0] and len(liquidity_result[1]) >= 32:
                    try:
                        pool.liquidity = decode(['uint128'], liquidity_result[1])[0]
                    except:
                        pass
                
                pool.last_update = now
            
            return success_count > 0, network_ms, success_count
            
        except Exception as e:
            if DEBUG_MODE:
                print(f"[ERROR] Multicall failed: {e}")
            return False, 0.0, 0
    
    def find_opportunities(
        self,
        min_profit_wei: int = 0
    ) -> List[ArbitrageOpportunity]:
        """
        Find arbitrage opportunities using local-only calculations.
        
        ⚡ No RPC calls - uses cached pool data.
        """
        opportunities = []
        
        # Group pools by token pair (optimized)
        pair_pools: Dict[Tuple[str, str], List[V3Pool]] = {}
        
        for pool in self.pool_list:
            if pool.liquidity < self.min_liquidity:
                continue
            if pool.sqrtPriceX96 == 0:
                continue
            
            key = (pool.token0.lower(), pool.token1.lower())
            if key not in pair_pools:
                pair_pools[key] = []
            pair_pools[key].append(pool)
        
        # Compare pools of same pair (optimized nested loop)
        min_amount = int(MIN_BORROW_ETH * 10**18)
        max_amount = int(MAX_BORROW_ETH * 10**18)
        precision = int(AMOUNT_PRECISION_ETH * 10**18)
        
        weth_lower = WETH.lower()
        
        for pools in pair_pools.values():
            n = len(pools)
            if n < 2:
                continue
            
            for i in range(n):
                pool_a = pools[i]
                for j in range(i + 1, n):
                    pool_b = pools[j]
                    
                    opp = self._check_opportunity_fast(
                        pool_a, pool_b, min_profit_wei,
                        min_amount, max_amount, precision, weth_lower
                    )
                    if opp:
                        opportunities.append(opp)
        
        # Sort by profit (descending)
        opportunities.sort(key=lambda x: x.net_profit, reverse=True)
        
        return opportunities
    
    def _check_opportunity_fast(
        self,
        pool_a: V3Pool,
        pool_b: V3Pool,
        min_profit: int,
        min_amount: int,
        max_amount: int,
        precision: int,
        weth_lower: str
    ) -> Optional[ArbitrageOpportunity]:
        """
        Fast opportunity check using local math only.
        
        ⚡ Zero RPC calls - pure computation.
        """
        # Quick price check
        price_a = pool_a.price_0_to_1
        price_b = pool_b.price_0_to_1
        
        if price_a <= 0 or price_b <= 0:
            return None
        
        # Calculate price difference
        if price_a > price_b:
            diff_pct = (price_a - price_b) / price_b * 100
            pool_low, pool_high = pool_b, pool_a
        else:
            diff_pct = (price_b - price_a) / price_a * 100
            pool_low, pool_high = pool_a, pool_b
        
        # Quick filter: need at least 2x fees to be profitable
        min_fee_pct = (pool_a.fee + pool_b.fee) / 10000  # Convert to percentage
        if diff_pct < min_fee_pct * 1.5:
            return None
        
        # Convert to V3PoolData for calculator
        pool_data_low = V3PoolData(
            address=pool_low.address,
            token0=pool_low.token0,
            token1=pool_low.token1,
            fee=pool_low.fee,
            sqrtPriceX96=pool_low.sqrtPriceX96,
            liquidity=pool_low.liquidity,
            decimals0=pool_low.decimals0,
            decimals1=pool_low.decimals1
        )
        
        pool_data_high = V3PoolData(
            address=pool_high.address,
            token0=pool_high.token0,
            token1=pool_high.token1,
            fee=pool_high.fee,
            sqrtPriceX96=pool_high.sqrtPriceX96,
            liquidity=pool_high.liquidity,
            decimals0=pool_high.decimals0,
            decimals1=pool_high.decimals1
        )
        
        # Determine borrow direction
        borrow_token_is_token0 = (pool_low.token0.lower() == weth_lower)
        
        # Run fast optimization
        best_amount, max_profit, result = find_optimal_amount_in_fast(
            pool_low=pool_data_low,
            pool_high=pool_data_high,
            min_amount=min_amount,
            max_amount=max_amount,
            precision=precision,
            borrow_token_is_token0=borrow_token_is_token0
        )
        
        if max_profit < min_profit:
            return None
        
        direction = f"{FEE_NAMES[pool_low.fee]} → {FEE_NAMES[pool_high.fee]}"
        
        return ArbitrageOpportunity(
            pool_low=pool_low,
            pool_high=pool_high,
            token_borrow=WETH,
            borrow_amount=best_amount,
            expected_profit=result.amount_out_swap2 if result else 0,
            flash_fee=result.flash_fee if result else 0,
            net_profit=max_profit,
            price_diff_pct=diff_pct,
            direction=direction,
            timestamp=time.time(),
            is_optimized=True,
            swap1_output=result.amount_out_swap1 if result else 0,
            swap2_output=result.amount_out_swap2 if result else 0,
            price_impact_pct=result.price_impact_pct if result else 0.0
        )
    
    def scan(
        self,
        min_profit_wei: int = 0,
        use_optimization: bool = True  # Kept for compatibility
    ) -> ScanResult:
        """
        Execute one scan cycle.
        
        ⚡ Performance: 1 RPC call + local calculations only.
        """
        # Single network request for ALL pool data
        success, network_ms, updated = self.update_pool_data()
        
        if not success:
            return ScanResult(
                pools_scanned=len(self.pool_list),
                time_network_ms=network_ms
            )
        
        # Local-only calculations
        t_calc_start = time.time()
        opportunities = self.find_opportunities(min_profit_wei)
        calc_ms = (time.time() - t_calc_start) * 1000
        
        # Count active pools
        active = sum(1 for p in self.pool_list if p.liquidity >= self.min_liquidity)
        
        self.scan_count += 1
        
        if DEBUG_MODE and opportunities:
            print(f"\n📊 Found {len(opportunities)} opportunities:")
            for i, opp in enumerate(opportunities[:3]):
                print(f"  [{i+1}] {opp.direction}")
                print(f"      Amount: {opp.borrow_amount/1e18:.4f} ETH")
                print(f"      Net Profit: {opp.net_profit/1e18:.6f} ETH")
        
        return ScanResult(
            opportunities=opportunities,
            pools_scanned=len(self.pool_list),
            pools_active=active,
            time_network_ms=network_ms,
            time_calc_ms=calc_ms
        )
    
    def get_pool_prices(self) -> Dict[str, Dict]:
        """Get current pool prices."""
        return {
            pool.address: {
                "fee": pool.fee,
                "fee_name": FEE_NAMES.get(pool.fee, str(pool.fee)),
                "liquidity": pool.liquidity,
                "sqrtPriceX96": pool.sqrtPriceX96,
                "price_0_to_1": pool.price_0_to_1,
                "price_1_to_0": pool.price_1_to_0,
            }
            for pool in self.pool_list
            if pool.sqrtPriceX96 > 0
        }
