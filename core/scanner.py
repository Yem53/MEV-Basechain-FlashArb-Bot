#!/usr/bin/env python3
"""
Uniswap V3 Arbitrage Scanner

Pure V3 implementation - no V2/Solidly legacy code.

Features:
- Discover V3 pools via deterministic address computation
- Fetch slot0 (sqrtPriceX96) and liquidity via Multicall
- Convert sqrtPriceX96 to human-readable prices
- Find cross-pool arbitrage opportunities

Base Mainnet Constants:
- V3 Factory: 0x33128a8fC17869897dcE68Ed026d694621f6FDfD
- Init Code Hash: 0xe34f199b19b2b4f47f68442619d555527d244f78a3297ea89325f843f87b8b54
"""

import time
from decimal import Decimal, getcontext
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

from web3 import Web3
from eth_abi import encode, decode

# High precision for price calculations
getcontext().prec = 78

# ============================================
# V3 Constants - Base Mainnet
# ============================================

V3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
SWAP_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
WETH = "0x4200000000000000000000000000000000000006"
POOL_INIT_CODE_HASH = "0xe34f199b19b2b4f47f68442619d555527d244f78a3297ea89325f843f87b8b54"

# Fee tiers: 500 (0.05%), 3000 (0.3%), 10000 (1%)
FEE_TIERS = [500, 3000, 10000]
FEE_NAMES = {500: "0.05%", 3000: "0.3%", 10000: "1%"}

# Minimum liquidity threshold
MIN_LIQUIDITY = 10**15  # ~0.001 units

# Function selectors
SLOT0_SELECTOR = "0x3850c7bd"
LIQUIDITY_SELECTOR = "0x1a686502"

# ============================================
# Data Structures
# ============================================

@dataclass
class V3Pool:
    """V3 Pool information"""
    address: str
    token0: str
    token1: str
    fee: int
    sqrtPriceX96: int = 0
    tick: int = 0
    liquidity: int = 0
    price_0_to_1: float = 0.0
    price_1_to_0: float = 0.0
    last_update: float = 0


@dataclass
class ArbitrageOpportunity:
    """Arbitrage opportunity between two pools"""
    pool_low: V3Pool      # Lower price pool
    pool_high: V3Pool     # Higher price pool
    token_borrow: str
    borrow_amount: int
    expected_profit: int
    flash_fee: int
    net_profit: int
    price_diff_pct: float
    direction: str
    timestamp: float


@dataclass
class ScanResult:
    """Scan cycle result"""
    opportunities: List[ArbitrageOpportunity] = field(default_factory=list)
    pools_scanned: int = 0
    pools_active: int = 0
    time_network_ms: float = 0.0
    time_calc_ms: float = 0.0


# ============================================
# V3 Price Math
# ============================================

def sqrt_price_x96_to_price(
    sqrtPriceX96: int, 
    decimals0: int = 18, 
    decimals1: int = 18
) -> Tuple[float, float]:
    """
    Convert sqrtPriceX96 to human-readable price.
    
    V3 Price Formula:
    price = (sqrtPriceX96 / 2^96)^2
    
    Args:
        sqrtPriceX96: Price from slot0
        decimals0: Token0 decimals
        decimals1: Token1 decimals
    
    Returns:
        (price_0_to_1, price_1_to_0)
    """
    if sqrtPriceX96 == 0:
        return 0.0, 0.0
    
    try:
        Q96 = Decimal(2 ** 96)
        sqrt_price = Decimal(sqrtPriceX96) / Q96
        raw_price = sqrt_price ** 2
        
        # Adjust for decimal difference
        decimal_adj = Decimal(10 ** (decimals0 - decimals1))
        price_0_to_1 = float(raw_price * decimal_adj)
        price_1_to_0 = 1.0 / price_0_to_1 if price_0_to_1 > 0 else 0.0
        
        return price_0_to_1, price_1_to_0
    except Exception:
        return 0.0, 0.0


def compute_pool_address(
    token0: str, 
    token1: str, 
    fee: int,
    factory: str = V3_FACTORY
) -> str:
    """
    Compute V3 pool address deterministically using CREATE2.
    
    Args:
        token0: First token (will be sorted)
        token1: Second token (will be sorted)
        fee: Fee tier
        factory: Factory address
    
    Returns:
        Pool address
    """
    # Sort tokens
    if token0.lower() > token1.lower():
        token0, token1 = token1, token0
    
    # Encode salt
    salt = Web3.keccak(encode(
        ['address', 'address', 'uint24'],
        [Web3.to_checksum_address(token0), Web3.to_checksum_address(token1), fee]
    ))
    
    # Compute CREATE2 address
    init_hash = bytes.fromhex(POOL_INIT_CODE_HASH[2:])
    factory_bytes = bytes.fromhex(factory[2:])
    
    create2_input = b'\xff' + factory_bytes + salt + init_hash
    pool_hash = Web3.keccak(create2_input)[-20:]
    
    return Web3.to_checksum_address(pool_hash.hex())


# ============================================
# V3 Scanner Class
# ============================================

class V3Scanner:
    """
    Pure Uniswap V3 Arbitrage Scanner
    
    Discovers pools, fetches prices, finds opportunities.
    """
    
    def __init__(
        self,
        w3: Web3,
        target_tokens: List[Dict],
        fee_tiers: List[int] = None,
        min_liquidity: int = MIN_LIQUIDITY
    ):
        """
        Initialize V3 Scanner.
        
        Args:
            w3: Web3 instance
            target_tokens: List of {"symbol": str, "address": str, "decimals": int}
            fee_tiers: Fee tiers to scan (default: [500, 3000, 10000])
            min_liquidity: Minimum liquidity threshold
        """
        self.w3 = w3
        self.target_tokens = target_tokens
        self.fee_tiers = fee_tiers or FEE_TIERS
        self.min_liquidity = min_liquidity
        
        # Pool cache
        self.pools: Dict[str, V3Pool] = {}
        
        # Token decimals cache
        self.decimals: Dict[str, int] = {WETH.lower(): 18}
        for token in target_tokens:
            self.decimals[token["address"].lower()] = token.get("decimals", 18)
        
        # Multicall3 address (standard across chains)
        self.multicall_address = "0xcA11bde05977b3631167028862bE2a173976CA11"
        
        # Stats
        self.scan_count = 0
    
    def discover_pools(self, base_token: str = WETH) -> List[V3Pool]:
        """
        Discover V3 pools for all target tokens.
        
        Args:
            base_token: Base token for pairs (default: WETH)
        
        Returns:
            List of discovered pools
        """
        discovered = []
        
        print(f"\n🔍 Discovering V3 pools...")
        
        for token_config in self.target_tokens:
            token = token_config["address"]
            symbol = token_config.get("symbol", "???")
            decimals = token_config.get("decimals", 18)
            
            for fee in self.fee_tiers:
                try:
                    pool_address = compute_pool_address(base_token, token, fee)
                    
                    # Check if pool exists (has code)
                    code = self.w3.eth.get_code(pool_address)
                    if len(code) <= 2:
                        continue
                    
                    # Determine token0/token1 order
                    if base_token.lower() < token.lower():
                        t0, t1 = base_token, token
                    else:
                        t0, t1 = token, base_token
                    
                    pool = V3Pool(
                        address=pool_address,
                        token0=self.w3.to_checksum_address(t0),
                        token1=self.w3.to_checksum_address(t1),
                        fee=fee
                    )
                    
                    discovered.append(pool)
                    self.pools[pool_address.lower()] = pool
                    
                    print(f"  ✅ [{symbol}] {FEE_NAMES[fee]}: {pool_address[:16]}...")
                    
                except Exception as e:
                    print(f"  ⚠️ [{symbol}] {FEE_NAMES.get(fee, str(fee))}: {e}")
        
        print(f"\n📊 Discovered {len(discovered)} V3 pools")
        return discovered
    
    def update_pool_data(self) -> Tuple[bool, float, int]:
        """
        Batch update all pool data via Multicall.
        
        Returns:
            (success, network_time_ms, pools_updated)
        """
        if not self.pools:
            return False, 0.0, 0
        
        pool_list = list(self.pools.values())
        
        # Build multicall requests: slot0 + liquidity per pool
        calls = []
        for pool in pool_list:
            addr = Web3.to_checksum_address(pool.address)
            calls.append({
                "target": addr,
                "callData": SLOT0_SELECTOR
            })
            calls.append({
                "target": addr,
                "callData": LIQUIDITY_SELECTOR
            })
        
        try:
            t_start = time.time()
            
            # Multicall3 aggregate3
            multicall = self.w3.eth.contract(
                address=self.multicall_address,
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
            
            formatted_calls = [
                (c["target"], True, bytes.fromhex(c["callData"][2:]))
                for c in calls
            ]
            
            results = multicall.functions.aggregate3(formatted_calls).call()
            
            t_end = time.time()
            network_ms = (t_end - t_start) * 1000
            
            # Parse results
            success_count = 0
            now = time.time()
            
            for i, pool in enumerate(pool_list):
                try:
                    slot0_result = results[i * 2]
                    liquidity_result = results[i * 2 + 1]
                    
                    # Parse slot0
                    if slot0_result[0] and len(slot0_result[1]) >= 64:
                        decoded = decode(
                            ['uint160', 'int24', 'uint16', 'uint16', 'uint16', 'uint8', 'bool'],
                            slot0_result[1]
                        )
                        pool.sqrtPriceX96 = decoded[0]
                        pool.tick = decoded[1]
                        
                        # Get decimals
                        dec0 = self.decimals.get(pool.token0.lower(), 18)
                        dec1 = self.decimals.get(pool.token1.lower(), 18)
                        
                        pool.price_0_to_1, pool.price_1_to_0 = sqrt_price_x96_to_price(
                            pool.sqrtPriceX96, dec0, dec1
                        )
                    
                    # Parse liquidity
                    if liquidity_result[0] and len(liquidity_result[1]) >= 32:
                        pool.liquidity = decode(['uint128'], liquidity_result[1])[0]
                    
                    pool.last_update = now
                    success_count += 1
                    
                except Exception:
                    pass
            
            return success_count > 0, network_ms, success_count
            
        except Exception as e:
            print(f"[ERROR] Multicall failed: {e}")
            return False, 0.0, 0
    
    def find_opportunities(
        self,
        min_profit_wei: int = 0,
        borrow_amount: int = 10**18  # 1 ETH default
    ) -> List[ArbitrageOpportunity]:
        """
        Find arbitrage opportunities between V3 pools.
        
        Args:
            min_profit_wei: Minimum profit threshold
            borrow_amount: Amount to borrow for arbitrage
        
        Returns:
            List of opportunities
        """
        opportunities = []
        
        # Group pools by token pair
        pair_pools: Dict[Tuple[str, str], List[V3Pool]] = {}
        
        for pool in self.pools.values():
            if pool.liquidity < self.min_liquidity:
                continue
            if pool.sqrtPriceX96 == 0:
                continue
            
            key = (pool.token0.lower(), pool.token1.lower())
            if key not in pair_pools:
                pair_pools[key] = []
            pair_pools[key].append(pool)
        
        # Compare pools of same pair but different fees
        for (t0, t1), pools in pair_pools.items():
            if len(pools) < 2:
                continue
            
            for i in range(len(pools)):
                for j in range(i + 1, len(pools)):
                    opp = self._check_opportunity(
                        pools[i], pools[j], borrow_amount, min_profit_wei
                    )
                    if opp:
                        opportunities.append(opp)
        
        return opportunities
    
    def _check_opportunity(
        self,
        pool_a: V3Pool,
        pool_b: V3Pool,
        borrow_amount: int,
        min_profit: int
    ) -> Optional[ArbitrageOpportunity]:
        """Check for arbitrage between two pools."""
        try:
            price_a = pool_a.price_0_to_1
            price_b = pool_b.price_0_to_1
            
            if price_a <= 0 or price_b <= 0:
                return None
            
            # Determine high/low
            if price_a > price_b:
                pool_high, pool_low = pool_a, pool_b
                diff = (price_a - price_b) / price_b
            else:
                pool_high, pool_low = pool_b, pool_a
                diff = (price_b - price_a) / price_a
            
            diff_pct = diff * 100
            
            # Calculate flash loan fee (same as pool fee)
            # Use lower fee pool for flash loan
            flash_pool = pool_low if pool_low.fee <= pool_high.fee else pool_high
            flash_fee_rate = flash_pool.fee / 1_000_000
            flash_fee = int(borrow_amount * flash_fee_rate)
            
            # Calculate swap fees
            swap_fee = int(borrow_amount * (pool_high.fee / 1_000_000))
            
            # Expected profit (simplified)
            gross_profit = int(borrow_amount * diff)
            net_profit = gross_profit - flash_fee - swap_fee
            
            if net_profit < min_profit:
                return None
            
            direction = f"{FEE_NAMES[pool_low.fee]} → {FEE_NAMES[pool_high.fee]}"
            
            return ArbitrageOpportunity(
                pool_low=pool_low,
                pool_high=pool_high,
                token_borrow=WETH,
                borrow_amount=borrow_amount,
                expected_profit=gross_profit,
                flash_fee=flash_fee,
                net_profit=net_profit,
                price_diff_pct=diff_pct,
                direction=direction,
                timestamp=time.time()
            )
            
        except Exception:
            return None
    
    def scan(self) -> ScanResult:
        """
        Execute one scan cycle.
        
        Returns:
            ScanResult with opportunities and metrics
        """
        t_start = time.time()
        
        # Update pool data
        success, network_ms, updated = self.update_pool_data()
        
        if not success:
            return ScanResult(
                pools_scanned=len(self.pools),
                time_network_ms=network_ms
            )
        
        # Find opportunities
        t_calc_start = time.time()
        opportunities = self.find_opportunities()
        calc_ms = (time.time() - t_calc_start) * 1000
        
        # Count active pools
        active = sum(1 for p in self.pools.values() if p.liquidity >= self.min_liquidity)
        
        self.scan_count += 1
        
        return ScanResult(
            opportunities=opportunities,
            pools_scanned=len(self.pools),
            pools_active=active,
            time_network_ms=network_ms,
            time_calc_ms=calc_ms
        )
    
    def get_pool_prices(self) -> Dict[str, Dict]:
        """Get current pool prices."""
        prices = {}
        
        for addr, pool in self.pools.items():
            if pool.sqrtPriceX96 == 0:
                continue
            
            prices[pool.address] = {
                "fee": pool.fee,
                "fee_name": FEE_NAMES.get(pool.fee, str(pool.fee)),
                "liquidity": pool.liquidity,
                "sqrtPriceX96": pool.sqrtPriceX96,
                "price_0_to_1": pool.price_0_to_1,
                "price_1_to_0": pool.price_1_to_0,
            }
        
        return prices

