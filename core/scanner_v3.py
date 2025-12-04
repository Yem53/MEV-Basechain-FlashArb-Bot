#!/usr/bin/env python3
"""
Uniswap V3 原生套利扫描器

功能：
- 发现 V3 池（通过工厂或地址计算）
- 使用 Multicall 批量获取 slot0 和 liquidity
- 从 sqrtPriceX96 计算实际价格
- 寻找跨池套利机会

Base Mainnet 常量：
- V3 Factory: 0x33128a8fC17869897dcE68Ed026d694621f6FDfD
- V3 Router: 0x2626664c2603336E57B271c5C0b26F421741e481
- WETH: 0x4200000000000000000000000000000000000006
"""

import os
import sys
import time
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple, NamedTuple
from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from web3 import Web3
from eth_abi import encode, decode

# 设置高精度
getcontext().prec = 78

# 添加项目根目录到路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from core.multicall import Multicall

# ============================================
# V3 常量 - Base Mainnet
# ============================================

V3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
V3_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
WETH_ADDRESS = "0x4200000000000000000000000000000000000006"

# V3 池 INIT_CODE_HASH (Base Mainnet)
POOL_INIT_CODE_HASH = "0xe34f199b19b2b4f47f68442619d555527d244f78a3297ea89325f843f87b8b54"

# V3 费率层级
V3_FEE_TIERS = [100, 500, 3000, 10000]  # 0.01%, 0.05%, 0.3%, 1%

# 费率名称映射
FEE_TIER_NAMES = {
    100: "0.01%",
    500: "0.05%",
    3000: "0.3%",
    10000: "1%"
}

# 最小流动性阈值
MIN_LIQUIDITY = 10**15  # 约 0.001 单位

# slot0 函数选择器
SLOT0_SELECTOR = "0x3850c7bd"
# liquidity 函数选择器
LIQUIDITY_SELECTOR = "0x1a686502"
# token0 函数选择器
TOKEN0_SELECTOR = "0x0dfe1681"
# token1 函数选择器
TOKEN1_SELECTOR = "0xd21220a7"
# fee 函数选择器
FEE_SELECTOR = "0xddca3f43"

# ============================================
# 数据结构
# ============================================

@dataclass
class V3PoolInfo:
    """V3 池信息"""
    address: str
    token0: str
    token1: str
    fee: int
    sqrtPriceX96: int = 0
    tick: int = 0
    liquidity: int = 0
    price_0_to_1: float = 0.0  # token0 -> token1 的价格
    price_1_to_0: float = 0.0  # token1 -> token0 的价格
    last_update: float = 0


@dataclass
class V3ArbitrageOpportunity:
    """V3 套利机会"""
    pool_a: V3PoolInfo
    pool_b: V3PoolInfo
    token_borrow: str
    borrow_amount: int
    expected_profit: int
    profit_after_fee: int
    price_diff_percent: float
    flash_fee: int  # V3 闪电贷费用
    direction: str
    timestamp: float


@dataclass
class V3ScanResult:
    """V3 扫描结果"""
    opportunities: List[V3ArbitrageOpportunity] = field(default_factory=list)
    pools_scanned: int = 0
    pools_with_liquidity: int = 0
    time_network_ms: float = 0.0
    time_calc_ms: float = 0.0
    time_total_ms: float = 0.0


# ============================================
# V3 价格计算
# ============================================

def sqrt_price_x96_to_price(sqrtPriceX96: int, decimals0: int = 18, decimals1: int = 18) -> Tuple[float, float]:
    """
    将 sqrtPriceX96 转换为可读价格
    
    V3 价格公式：
    price = (sqrtPriceX96 / 2^96)^2
    
    参数：
        sqrtPriceX96: slot0 返回的价格平方根
        decimals0: token0 的小数位
        decimals1: token1 的小数位
    
    返回：
        (price_0_to_1, price_1_to_0)
    """
    if sqrtPriceX96 == 0:
        return 0.0, 0.0
    
    try:
        # 使用 Decimal 进行高精度计算
        Q96 = Decimal(2 ** 96)
        sqrt_price = Decimal(sqrtPriceX96) / Q96
        
        # price = sqrt_price^2
        raw_price = sqrt_price ** 2
        
        # 调整小数位差异
        decimal_adjustment = Decimal(10 ** (decimals0 - decimals1))
        
        price_0_to_1 = float(raw_price * decimal_adjustment)
        price_1_to_0 = 1.0 / price_0_to_1 if price_0_to_1 > 0 else 0.0
        
        return price_0_to_1, price_1_to_0
        
    except Exception:
        return 0.0, 0.0


def compute_v3_pool_address(token0: str, token1: str, fee: int, factory: str = V3_FACTORY) -> str:
    """
    计算 V3 池地址 (CREATE2)
    
    参数：
        token0: 排序后的 token0 地址
        token1: 排序后的 token1 地址
        fee: 费率
        factory: 工厂地址
    
    返回：
        池地址
    """
    # 确保地址排序正确
    if token0.lower() > token1.lower():
        token0, token1 = token1, token0
    
    # 编码 salt
    salt = Web3.keccak(encode(
        ['address', 'address', 'uint24'],
        [Web3.to_checksum_address(token0), Web3.to_checksum_address(token1), fee]
    ))
    
    # 计算 CREATE2 地址
    init_code_hash = bytes.fromhex(POOL_INIT_CODE_HASH[2:])
    
    create2_input = b'\xff' + bytes.fromhex(factory[2:]) + salt + init_code_hash
    pool_address = Web3.keccak(create2_input)[-20:]
    
    return Web3.to_checksum_address(pool_address.hex())


# ============================================
# V3 扫描器类
# ============================================

class V3ArbitrageScanner:
    """
    Uniswap V3 原生套利扫描器
    
    使用 Multicall 批量获取 V3 池数据，寻找套利机会。
    """
    
    def __init__(
        self,
        w3: Web3,
        target_tokens: List[Dict] = None,
        fee_tiers: List[int] = None,
        min_liquidity: int = MIN_LIQUIDITY
    ):
        """
        初始化 V3 扫描器
        
        参数：
            w3: Web3 实例
            target_tokens: 目标代币列表 [{"symbol": "...", "address": "...", "decimals": 18}, ...]
            fee_tiers: 要扫描的费率层级
            min_liquidity: 最小流动性要求
        """
        self.w3 = w3
        self.multicall = Multicall(w3)
        self.target_tokens = target_tokens or []
        self.fee_tiers = fee_tiers or V3_FEE_TIERS
        self.min_liquidity = min_liquidity
        
        # 池信息缓存
        self.pools: Dict[str, V3PoolInfo] = {}
        
        # 统计
        self.scan_count = 0
        self.opportunity_count = 0
    
    def discover_pools(self, base_token: str = WETH_ADDRESS) -> List[V3PoolInfo]:
        """
        发现所有目标代币与基础代币的 V3 池
        
        参数：
            base_token: 基础代币地址（默认 WETH）
        
        返回：
            发现的池列表
        """
        discovered_pools = []
        
        for token_config in self.target_tokens:
            token_address = token_config["address"]
            symbol = token_config.get("symbol", "UNKNOWN")
            decimals = token_config.get("decimals", 18)
            
            for fee in self.fee_tiers:
                try:
                    # 计算池地址
                    pool_address = compute_v3_pool_address(base_token, token_address, fee)
                    
                    # 检查池是否存在（通过代码长度）
                    code = self.w3.eth.get_code(pool_address)
                    if len(code) <= 2:  # 空地址或 "0x"
                        continue
                    
                    # 确定 token0 和 token1 的顺序
                    if base_token.lower() < token_address.lower():
                        t0, t1 = base_token, token_address
                    else:
                        t0, t1 = token_address, base_token
                    
                    pool_info = V3PoolInfo(
                        address=pool_address,
                        token0=self.w3.to_checksum_address(t0),
                        token1=self.w3.to_checksum_address(t1),
                        fee=fee
                    )
                    
                    discovered_pools.append(pool_info)
                    self.pools[pool_address.lower()] = pool_info
                    
                    print(f"  ✅ [{symbol}] V3 {FEE_TIER_NAMES[fee]} 池: {pool_address[:10]}...")
                    
                except Exception as e:
                    print(f"  ⚠️ 发现池错误 [{symbol}] {FEE_TIER_NAMES.get(fee, str(fee))}: {e}")
        
        return discovered_pools
    
    def update_pool_data(self) -> Tuple[bool, float, int]:
        """
        批量更新所有池的 slot0 和 liquidity 数据
        
        返回：
            (成功, 网络耗时ms, 成功更新的池数)
        """
        if not self.pools:
            return False, 0.0, 0
        
        pool_addresses = list(self.pools.keys())
        
        # 构建 Multicall 请求
        calls = []
        for addr in pool_addresses:
            # slot0 调用
            calls.append((addr, SLOT0_SELECTOR))
            # liquidity 调用
            calls.append((addr, LIQUIDITY_SELECTOR))
        
        try:
            t_start = time.time()
            results = self.multicall.aggregate(calls)
            t_end = time.time()
            network_time_ms = (t_end - t_start) * 1000
            
            success_count = 0
            now = time.time()
            
            # 解析结果（每个池有 2 个调用）
            for i, addr in enumerate(pool_addresses):
                try:
                    slot0_result = results[i * 2]
                    liquidity_result = results[i * 2 + 1]
                    
                    pool = self.pools[addr]
                    
                    # 解析 slot0
                    if slot0_result[0] and len(slot0_result[1]) >= 64:
                        decoded = decode(
                            ['uint160', 'int24', 'uint16', 'uint16', 'uint16', 'uint8', 'bool'],
                            slot0_result[1]
                        )
                        pool.sqrtPriceX96 = decoded[0]
                        pool.tick = decoded[1]
                        
                        # 计算价格
                        # 需要知道代币小数位，这里假设都是 18
                        pool.price_0_to_1, pool.price_1_to_0 = sqrt_price_x96_to_price(
                            pool.sqrtPriceX96
                        )
                    
                    # 解析 liquidity
                    if liquidity_result[0] and len(liquidity_result[1]) >= 32:
                        pool.liquidity = decode(['uint128'], liquidity_result[1])[0]
                    
                    pool.last_update = now
                    success_count += 1
                    
                except Exception:
                    pass
            
            return success_count > 0, network_time_ms, success_count
            
        except Exception as e:
            print(f"[WARN] V3 Multicall 失败: {e}")
            return False, 0.0, 0
    
    def find_opportunities(
        self,
        min_profit_wei: int = 0,
        flash_fee_tier: int = 500  # 使用 0.05% 池作为闪电贷源
    ) -> List[V3ArbitrageOpportunity]:
        """
        在 V3 池之间寻找套利机会
        
        参数：
            min_profit_wei: 最小利润要求（wei）
            flash_fee_tier: 闪电贷使用的费率层级
        
        返回：
            套利机会列表
        """
        opportunities = []
        
        # 按代币对分组池
        token_pair_pools: Dict[Tuple[str, str], List[V3PoolInfo]] = {}
        
        for pool in self.pools.values():
            if pool.liquidity < self.min_liquidity:
                continue
            if pool.sqrtPriceX96 == 0:
                continue
            
            key = (pool.token0.lower(), pool.token1.lower())
            if key not in token_pair_pools:
                token_pair_pools[key] = []
            token_pair_pools[key].append(pool)
        
        # 比较同一代币对的不同费率池
        for (t0, t1), pools in token_pair_pools.items():
            if len(pools) < 2:
                continue
            
            for i in range(len(pools)):
                for j in range(i + 1, len(pools)):
                    pool_a = pools[i]
                    pool_b = pools[j]
                    
                    opp = self._check_opportunity(pool_a, pool_b, flash_fee_tier, min_profit_wei)
                    if opp:
                        opportunities.append(opp)
        
        return opportunities
    
    def _check_opportunity(
        self,
        pool_a: V3PoolInfo,
        pool_b: V3PoolInfo,
        flash_fee_tier: int,
        min_profit_wei: int
    ) -> Optional[V3ArbitrageOpportunity]:
        """
        检查两个池之间的套利机会
        """
        try:
            # 获取价格
            price_a = pool_a.price_0_to_1
            price_b = pool_b.price_0_to_1
            
            if price_a <= 0 or price_b <= 0:
                return None
            
            # 计算价差
            if price_a > price_b:
                high_pool, low_pool = pool_a, pool_b
                price_diff = (price_a - price_b) / price_b
            else:
                high_pool, low_pool = pool_b, pool_a
                price_diff = (price_b - price_a) / price_a
            
            price_diff_percent = price_diff * 100
            
            # 计算闪电贷费用
            # V3 闪电贷费用 = amount * fee / 1e6
            # 使用 0.05% 池时费用最低
            flash_fee_rate = flash_fee_tier / 1_000_000  # 例如 500 -> 0.0005
            
            # 简化利润估算（实际需要更复杂的 AMM 数学）
            # 这里假设借入 1 ETH
            borrow_amount = 10**18  # 1 ETH
            
            # 预期利润 = 借入金额 * 价差 - 闪电贷费用 - 交换费用
            swap_fee_a = pool_a.fee / 1_000_000
            swap_fee_b = pool_b.fee / 1_000_000
            total_fee = flash_fee_rate + swap_fee_a + swap_fee_b
            
            gross_profit = int(borrow_amount * price_diff)
            flash_fee = int(borrow_amount * flash_fee_rate)
            net_profit = gross_profit - flash_fee - int(borrow_amount * (swap_fee_a + swap_fee_b))
            
            if net_profit < min_profit_wei:
                return None
            
            # 确定方向
            direction = f"{FEE_TIER_NAMES[low_pool.fee]} -> {FEE_TIER_NAMES[high_pool.fee]}"
            
            return V3ArbitrageOpportunity(
                pool_a=low_pool,
                pool_b=high_pool,
                token_borrow=WETH_ADDRESS,
                borrow_amount=borrow_amount,
                expected_profit=gross_profit,
                profit_after_fee=net_profit,
                price_diff_percent=price_diff_percent,
                flash_fee=flash_fee,
                direction=direction,
                timestamp=time.time()
            )
            
        except Exception:
            return None
    
    def scan(self) -> V3ScanResult:
        """
        执行一次完整扫描
        
        返回：
            V3ScanResult 包含机会和性能指标
        """
        t_start = time.time()
        
        # 更新池数据
        success, network_time_ms, pools_updated = self.update_pool_data()
        
        if not success:
            return V3ScanResult(
                pools_scanned=len(self.pools),
                pools_with_liquidity=0,
                time_network_ms=network_time_ms,
                time_total_ms=(time.time() - t_start) * 1000
            )
        
        # 寻找机会
        t_calc_start = time.time()
        opportunities = self.find_opportunities()
        t_calc_end = time.time()
        calc_time_ms = (t_calc_end - t_calc_start) * 1000
        
        # 统计有流动性的池
        pools_with_liquidity = sum(
            1 for p in self.pools.values() 
            if p.liquidity >= self.min_liquidity
        )
        
        self.scan_count += 1
        self.opportunity_count += len(opportunities)
        
        return V3ScanResult(
            opportunities=opportunities,
            pools_scanned=len(self.pools),
            pools_with_liquidity=pools_with_liquidity,
            time_network_ms=network_time_ms,
            time_calc_ms=calc_time_ms,
            time_total_ms=(time.time() - t_start) * 1000
        )
    
    def get_pool_prices(self) -> Dict[str, Dict]:
        """
        获取所有池的当前价格
        """
        prices = {}
        
        for addr, pool in self.pools.items():
            if pool.sqrtPriceX96 == 0:
                continue
            
            prices[pool.address] = {
                "fee": pool.fee,
                "fee_name": FEE_TIER_NAMES.get(pool.fee, str(pool.fee)),
                "token0": pool.token0,
                "token1": pool.token1,
                "sqrtPriceX96": pool.sqrtPriceX96,
                "tick": pool.tick,
                "liquidity": pool.liquidity,
                "price_0_to_1": pool.price_0_to_1,
                "price_1_to_0": pool.price_1_to_0,
            }
        
        return prices


# ============================================
# 测试代码
# ============================================

if __name__ == "__main__":
    load_dotenv(PROJECT_ROOT / ".env")
    
    rpc_url = os.getenv("RPC_URL", "http://127.0.0.1:8545")
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    
    if not w3.is_connected():
        print("无法连接到网络")
        sys.exit(1)
    
    print(f"已连接到网络，链 ID: {w3.eth.chain_id}")
    
    # 测试代币
    test_tokens = [
        {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
    ]
    
    scanner = V3ArbitrageScanner(w3, target_tokens=test_tokens)
    
    print("\n发现 V3 池...")
    pools = scanner.discover_pools()
    print(f"发现 {len(pools)} 个池")
    
    print("\n获取池数据...")
    result = scanner.scan()
    
    print(f"\n扫描结果:")
    print(f"  扫描池数: {result.pools_scanned}")
    print(f"  有流动性: {result.pools_with_liquidity}")
    print(f"  发现机会: {len(result.opportunities)}")
    print(f"  网络耗时: {result.time_network_ms:.0f}ms")
    print(f"  计算耗时: {result.time_calc_ms:.0f}ms")
    
    # 显示价格
    prices = scanner.get_pool_prices()
    for addr, info in prices.items():
        print(f"\n[{info['fee_name']}] {addr[:10]}...")
        print(f"  Liquidity: {info['liquidity']}")
        print(f"  Price 0->1: {info['price_0_to_1']:.8f}")

