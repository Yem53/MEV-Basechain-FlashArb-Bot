#!/usr/bin/env python3
"""
套利扫描器模块

功能：
- 监控多个 DEX 上的配对价格
- 使用 Super-Batch Multicall 在单次请求中获取所有储备数据
- 计算套利机会并输出结果
- 支持持续监控模式
- 支持延迟性能分析

支持的 DEX（Base Mainnet）：
- BaseSwap: Factory 0xFDa619b6d20975be80A10332cD39b9a4b0FAa8BB
- Uniswap V2: Factory 0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6
- SushiSwap: Factory 0x71524B4f93c58fcbF659783284E38825f0622859
- Aerodrome: 需要特殊处理（Solidly fork）

使用方法：
    python -m core.scanner
    
    或在代码中：
    scanner = ArbitrageScanner(w3)
    result = scanner.scan()  # 返回 ScanResult 包含机会和延迟信息
    scanner.run_loop(interval=1.0)  # 持续监控
"""

import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, NamedTuple
from dataclasses import dataclass, field
from web3 import Web3

# 添加项目根目录到路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from core.multicall import Multicall
from core.calculator import (
    calculate_arb_profit,
    find_optimal_borrow_fixed_steps,
    check_both_directions,
    estimate_gas_cost,
    is_profitable_after_gas,
    ArbitrageResult,
    get_price_ratio
)


# ============================================
# 配置常量
# ============================================

# Base Mainnet 代币地址
WETH_ADDRESS = "0x4200000000000000000000000000000000000006"
USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # 原生 USDC
USDbC_ADDRESS = "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA"  # 桥接 USDC
DAI_ADDRESS = "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb"

# DEX 配置（包含工厂和路由器地址）
DEX_CONFIG = {
    "BaseSwap": {
        "factory": "0xFDa619b6d20975be80A10332cD39b9a4b0FAa8BB",
        "router": "0x327Df1E6de05895d2ab08513aaDD9313Fe505d86",
        "type": "uniswap_v2",  # 标准 Uniswap V2 fork
    },
    "UniswapV2": {
        "factory": "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6",
        "router": "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
        "type": "uniswap_v2",
    },
    "SushiSwap": {
        "factory": "0x71524B4f93c58fcbF659783284E38825f0622859",
        "router": "0x6BDED42c6DA8FBf0d2bA55B2fa120C5e0c8D7891",
        "type": "uniswap_v2",
    },
    "Aerodrome": {
        "factory": "0x420DD381b31aEf6683db6B902084cB0FFECe40Da",
        "router": "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43",
        "type": "solidly",  # Solidly fork，需要特殊处理
    },
}

# 向后兼容的工厂地址字典
DEX_FACTORIES = {name: cfg["factory"] for name, cfg in DEX_CONFIG.items()}

# 预先硬编码的配对地址（避免动态查找）
# 格式: (配对地址, token0, token1, DEX名称, 路由器地址)
HARDCODED_PAIRS = [
    # ============================================
    # WETH/USDbC 配对 - 用于套利比较
    # ============================================
    
    # BaseSwap WETH/USDbC - 主要借贷源（流动性高）
    ("0x41d160033C222E6f3722EC97379867324567d883", WETH_ADDRESS, USDbC_ADDRESS, "BaseSwap", "0x327Df1E6de05895d2ab08513aaDD9313Fe505d86"),
    
    # SushiSwap WETH/USDbC - 独立 DEX
    # 注意：需要先验证此配对是否存在
    # ("0x...", WETH_ADDRESS, USDbC_ADDRESS, "SushiSwap", "0x6BDED42c6DA8FBf0d2bA55B2fa120C5e0c8D7891"),
    
    # Aerodrome WETH/USDbC (volatile) - Base 上最大的 DEX
    # 注意：Aerodrome 使用 getPool(tokenA, tokenB, stable) 接口
    # 需要先通过 discover_aerodrome_pool() 获取配对地址
    # ("0x...", WETH_ADDRESS, USDbC_ADDRESS, "Aerodrome", "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"),
    
    # ============================================
    # WETH/USDC 配对（原生 USDC）
    # ============================================
    
    # Aerodrome WETH/USDC (volatile) - 主要流动性池
    # ("0x...", WETH_ADDRESS, USDC_ADDRESS, "Aerodrome", "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"),
]

# ============================================
# 套利路径配置
# ============================================

# 独立 DEX 套利路径（避免配对锁定问题）
# 格式: (借贷 DEX, 交易 DEX)
INDEPENDENT_ARB_PATHS = [
    # BaseSwap 借入 -> Aerodrome 交易（推荐，Aerodrome 流动性最大）
    ("BaseSwap", "Aerodrome"),
    
    # BaseSwap 借入 -> SushiSwap 交易
    ("BaseSwap", "SushiSwap"),
    
    # Aerodrome 借入 -> BaseSwap 交易
    ("Aerodrome", "BaseSwap"),
    
    # Aerodrome 借入 -> SushiSwap 交易
    ("Aerodrome", "SushiSwap"),
]

# Gas 配置（Base 的 gas 价格很低）
DEFAULT_GAS_PRICE_GWEI = 0.01  # 0.01 Gwei
FLASH_SWAP_GAS = 250000        # 预估 gas 消耗
MIN_PROFIT_USD = 0.10          # 最小利润要求（美元）

# ============================================
# 安全机制配置
# ============================================

# 最小流动性阈值（防止在浅池中交易导致高滑点）
# 如果池中 WETH 储备少于此值，则跳过该池
MIN_LIQUIDITY_ETH = 0.5  # 0.5 ETH ≈ $1,500
MIN_LIQUIDITY_WEI = int(MIN_LIQUIDITY_ETH * 10**18)

# 测试借入金额
TEST_BORROW_AMOUNTS = [
    10**16,       # 0.01 ETH
    5 * 10**16,   # 0.05 ETH
    10**17,       # 0.1 ETH
    5 * 10**17,   # 0.5 ETH
    10**18,       # 1 ETH
    5 * 10**18,   # 5 ETH
    10 * 10**18,  # 10 ETH
]


# ============================================
# 数据结构
# ============================================

@dataclass
class PairInfo:
    """配对信息"""
    address: str
    token0: str
    token1: str
    dex_name: str
    router: str = ""        # 路由器地址
    reserve0: int = 0
    reserve1: int = 0
    last_update: float = 0


@dataclass
class ArbitrageOpportunity:
    """套利机会"""
    pair_a: PairInfo
    pair_b: PairInfo
    direction: str          # "A->B" 或 "B->A"
    borrow_amount: int
    expected_profit: int
    profit_after_gas: int
    price_diff_bps: float
    timestamp: float
    # Shadow Mode 诊断信息
    spread_percent: float = 0.0     # 价差百分比
    gas_cost_wei: int = 0           # Gas 成本
    slippage_loss_wei: int = 0      # 滑点损失
    dex_fee_wei: int = 0            # DEX 手续费


class PairGroup(NamedTuple):
    """相同代币对的配对组"""
    token0: str
    token1: str
    pairs: List[PairInfo]


@dataclass
class ScanResult:
    """
    扫描结果（包含延迟性能分析）
    
    用于 End-to-End Latency Profiling
    """
    opportunities: List[ArbitrageOpportunity] = field(default_factory=list)
    # 延迟指标（毫秒）
    time_network_ms: float = 0.0      # Multicall 网络请求时间
    time_calc_ms: float = 0.0         # 套利计算时间
    time_total_ms: float = 0.0        # 总扫描时间
    # 统计信息
    pairs_scanned: int = 0            # 扫描的配对数
    pairs_with_data: int = 0          # 成功获取数据的配对数
    
    def get_latency_str(self) -> str:
        """获取格式化的延迟字符串"""
        return f"Network: {self.time_network_ms:.0f}ms | Calc: {self.time_calc_ms:.0f}ms | Total: {self.time_total_ms:.0f}ms"


@dataclass
class ShadowOpportunity:
    """
    Shadow Mode 机会
    
    记录被拒绝但有潜在价值的套利机会，用于诊断
    """
    pair_a: PairInfo
    pair_b: PairInfo
    direction: str
    spread_percent: float       # 价差百分比
    expected_profit_wei: int    # 预期利润（负数表示亏损）
    gas_cost_wei: int           # Gas 成本
    slippage_loss_wei: int      # 滑点损失估算
    dex_fee_wei: int            # DEX 手续费
    rejection_reason: str       # 拒绝原因
    timestamp: float
    
    def get_breakdown_str(self) -> str:
        """获取成本分解字符串"""
        profit_eth = self.expected_profit_wei / 10**18
        gas_eth = self.gas_cost_wei / 10**18
        slippage_eth = self.slippage_loss_wei / 10**18
        fee_eth = self.dex_fee_wei / 10**18
        
        return (
            f"Spread: {self.spread_percent:.3f}% | "
            f"Profit: {profit_eth:.6f} ETH | "
            f"Gas: {gas_eth:.6f} ETH | "
            f"Slippage: {slippage_eth:.6f} ETH | "
            f"DEX Fee: {fee_eth:.6f} ETH"
        )


# ============================================
# 工厂合约 ABI
# ============================================

# 标准 Uniswap V2 工厂 ABI
FACTORY_ABI_V2 = [
    {
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"}
        ],
        "name": "getPair",
        "outputs": [{"name": "pair", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# Aerodrome/Solidly 工厂 ABI（getPool 需要 stable 参数）
FACTORY_ABI_SOLIDLY = [
    {
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"},
            {"name": "stable", "type": "bool"}
        ],
        "name": "getPool",
        "outputs": [{"name": "pool", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# 向后兼容
FACTORY_ABI = FACTORY_ABI_V2


# ============================================
# 套利扫描器类
# ============================================

class ArbitrageScanner:
    """
    套利扫描器
    
    监控多个 DEX 上的配对价格，寻找套利机会。
    """
    
    def __init__(
        self,
        w3: Web3,
        pairs: Optional[List[Tuple]] = None,
        gas_price_gwei: float = DEFAULT_GAS_PRICE_GWEI,
        min_profit_wei: int = 0
    ):
        """
        初始化扫描器
        
        参数：
            w3: Web3 实例
            pairs: 配对列表，支持两种格式:
                   - 4 元素: [(地址, token0, token1, DEX名称), ...]
                   - 5 元素: [(地址, token0, token1, DEX名称, 路由器), ...]
            gas_price_gwei: Gas 价格（Gwei）
            min_profit_wei: 最小利润要求（wei）
        """
        self.w3 = w3
        self.multicall = Multicall(w3)
        self.gas_price_gwei = gas_price_gwei
        self.min_profit_wei = min_profit_wei
        
        # 初始化配对信息
        if pairs is None:
            pairs = HARDCODED_PAIRS
        
        self.pairs: Dict[str, PairInfo] = {}
        for pair_data in pairs:
            # 支持 4 元素和 5 元素格式
            if len(pair_data) == 5:
                addr, t0, t1, dex, router = pair_data
            elif len(pair_data) == 4:
                addr, t0, t1, dex = pair_data
                # 从 DEX_CONFIG 获取路由器地址
                router = DEX_CONFIG.get(dex, {}).get("router", "")
            else:
                continue
            
            self.pairs[addr.lower()] = PairInfo(
                address=w3.to_checksum_address(addr),
                token0=w3.to_checksum_address(t0),
                token1=w3.to_checksum_address(t1),
                dex_name=dex,
                router=w3.to_checksum_address(router) if router else ""
            )
        
        # 按代币对分组
        self.pair_groups = self._group_pairs()
        
        # 统计信息
        self.scan_count = 0
        self.opportunity_count = 0
        self.last_scan_time = 0
    
    def _group_pairs(self) -> Dict[Tuple[str, str], PairGroup]:
        """将配对按代币对分组"""
        groups: Dict[Tuple[str, str], List[PairInfo]] = {}
        
        for pair in self.pairs.values():
            # 标准化代币顺序（按地址排序）
            tokens = tuple(sorted([pair.token0.lower(), pair.token1.lower()]))
            
            if tokens not in groups:
                groups[tokens] = []
            groups[tokens].append(pair)
        
        return {
            tokens: PairGroup(tokens[0], tokens[1], pairs)
            for tokens, pairs in groups.items()
        }
    
    def update_reserves(self) -> Tuple[bool, float, int]:
        """
        批量更新所有配对的储备数据（Super-Batch Multicall）
        
        安全机制：
        - 使用 Multicall 批量获取，减少 RPC 调用
        - 单个配对失败不影响其他配对的更新
        - 失败的配对保留上次的储备数据
        
        🚀 Super-Batch 优化:
        - 所有配对在单次 Multicall 请求中获取
        - 每个扫描周期只有 1 个网络请求
        
        返回：
            (是否成功, 网络请求耗时ms, 成功更新的配对数)
        """
        pair_addresses = [p.address for p in self.pairs.values()]
        
        if not pair_addresses:
            return False, 0.0, 0
        
        success_count = 0
        failed_dexes = []
        
        try:
            # 🚀 Super-Batch: 单次 Multicall 获取所有储备
            t_network_start = time.time()
            reserves_list = self.multicall.get_reserves_batch(pair_addresses)
            t_network_end = time.time()
            network_time_ms = (t_network_end - t_network_start) * 1000
            
            now = time.time()
            for addr, reserves in zip(pair_addresses, reserves_list):
                try:
                    if reserves and len(reserves) >= 2:
                        pair = self.pairs[addr.lower()]
                        pair.reserve0 = reserves[0]
                        pair.reserve1 = reserves[1]
                        pair.last_update = now
                        success_count += 1
                    else:
                        # 记录失败的 DEX（用于调试）
                        pair = self.pairs.get(addr.lower())
                        if pair:
                            failed_dexes.append(pair.dex_name)
                except Exception:
                    # 单个配对更新失败，继续处理其他配对
                    pass
            
            # 只有完全失败时才输出警告
            if success_count == 0 and failed_dexes:
                print(f"[WARN] 储备更新全部失败")
            
            return success_count > 0, network_time_ms, success_count
            
        except Exception as e:
            # Multicall 整体失败
            print(f"[WARN] Multicall 失败: {e}")
            return False, 0.0, 0
    
    def find_opportunities(
        self, 
        shadow_spread_threshold: float = 0.005
    ) -> Tuple[List[ArbitrageOpportunity], List[ShadowOpportunity]]:
        """
        在所有配对组中寻找套利机会
        
        安全机制：
        1. 最小流动性检查 - 跳过 WETH < 0.5 ETH 的池
        2. 健壮错误处理 - 单个 DEX 失败不影响其他扫描
        
        Shadow Mode:
        - 记录价差超过阈值但利润为负的机会
        - 用于诊断为什么交易没有执行
        
        参数：
            shadow_spread_threshold: Shadow Mode 价差阈值（默认 0.5%）
        
        返回：
            (套利机会列表, Shadow 机会列表)
        """
        opportunities = []
        shadow_opportunities = []
        gas_cost = estimate_gas_cost(self.gas_price_gwei, FLASH_SWAP_GAS)
        
        for tokens, group in self.pair_groups.items():
            if len(group.pairs) < 2:
                continue  # 需要至少两个配对才能套利
            
            # 比较组内每对配对
            for i in range(len(group.pairs)):
                for j in range(i + 1, len(group.pairs)):
                    pair_a = group.pairs[i]
                    pair_b = group.pairs[j]
                    
                    try:
                        # 安全检查 1: 跳过没有储备的配对
                        if pair_a.reserve0 == 0 or pair_b.reserve0 == 0:
                            continue
                        
                        # 安全检查 2: 最小流动性过滤
                        # 检查 pair_a 的 WETH 流动性
                        weth_lower = WETH_ADDRESS.lower()
                        
                        # 确定 WETH 在 pair_a 中的储备
                        if pair_a.token0.lower() == weth_lower:
                            pair_a_weth_reserve = pair_a.reserve0
                        else:
                            pair_a_weth_reserve = pair_a.reserve1
                        
                        # 确定 WETH 在 pair_b 中的储备
                        if pair_b.token0.lower() == weth_lower:
                            pair_b_weth_reserve = pair_b.reserve0
                        else:
                            pair_b_weth_reserve = pair_b.reserve1
                        
                        # 跳过流动性不足的池
                        if pair_a_weth_reserve < MIN_LIQUIDITY_WEI:
                            continue
                        if pair_b_weth_reserve < MIN_LIQUIDITY_WEI:
                            continue
                        
                        # 检查两个方向的套利机会
                        opp, shadow = self._check_pair_opportunity_with_shadow(
                            pair_a, pair_b, gas_cost, shadow_spread_threshold
                        )
                        
                        if opp:
                            opportunities.append(opp)
                        elif shadow:
                            shadow_opportunities.append(shadow)
                            
                    except Exception as e:
                        # 安全机制 3: 单个配对失败不影响整体扫描
                        # 静默处理，避免日志刷屏
                        pass
        
        return opportunities, shadow_opportunities
    
    def _check_pair_opportunity(
        self,
        pair_a: PairInfo,
        pair_b: PairInfo,
        gas_cost: int
    ) -> Optional[ArbitrageOpportunity]:
        """
        检查两个配对之间的套利机会（旧版兼容）
        
        参数：
            pair_a: 第一个配对
            pair_b: 第二个配对
            gas_cost: Gas 成本（wei）
            
        返回：
            套利机会或 None
        """
        opp, _ = self._check_pair_opportunity_with_shadow(pair_a, pair_b, gas_cost, 0.0)
        return opp
    
    def _check_pair_opportunity_with_shadow(
        self,
        pair_a: PairInfo,
        pair_b: PairInfo,
        gas_cost: int,
        shadow_spread_threshold: float = 0.005
    ) -> Tuple[Optional[ArbitrageOpportunity], Optional[ShadowOpportunity]]:
        """
        检查两个配对之间的套利机会（支持 Shadow Mode）
        
        参数：
            pair_a: 第一个配对
            pair_b: 第二个配对
            gas_cost: Gas 成本（wei）
            shadow_spread_threshold: Shadow Mode 价差阈值
            
        返回：
            (套利机会, Shadow 机会) - 只有一个会非空
        """
        weth_lower = WETH_ADDRESS.lower()
        
        # 确定 WETH 在 pair_a 中的位置
        pair_a_weth_is_token0 = (pair_a.token0.lower() == weth_lower)
        
        # 确定 WETH 在 pair_b 中的位置
        pair_b_weth_is_token0 = (pair_b.token0.lower() == weth_lower)
        
        # 调整储备顺序，确保第一个是 WETH 储备
        if pair_a_weth_is_token0:
            pair_a_reserves = (pair_a.reserve0, pair_a.reserve1)  # (WETH, Other)
        else:
            pair_a_reserves = (pair_a.reserve1, pair_a.reserve0)  # 交换顺序
        
        if pair_b_weth_is_token0:
            pair_b_reserves = (pair_b.reserve0, pair_b.reserve1)  # (WETH, Other)
        else:
            pair_b_reserves = (pair_b.reserve1, pair_b.reserve0)  # 交换顺序
        
        # 检查两个方向（借入 WETH）
        direction, opt_amount, result = check_both_directions(
            pair0_reserves=pair_a_reserves,
            pair1_reserves=pair_b_reserves,
            borrow_is_token0=True,  # 现在 token0 位置始终是 WETH
            test_amounts=TEST_BORROW_AMOUNTS
        )
        
        # 计算价差百分比
        spread_percent = result.price_diff_bps / 100.0  # bps -> %
        
        # 计算成本分解
        # DEX 费用 = 借入金额 * 0.3% * 2 (两次 swap)
        dex_fee_wei = int(opt_amount * 0.003 * 2)
        # 闪电贷费用
        flash_fee_wei = int(opt_amount * 0.003)
        # 滑点损失估算 = 预期输出 - 实际输出（基于 AMM 计算）
        # 简化：用价差和借入金额估算
        expected_no_slippage = int(opt_amount * (1 + spread_percent / 100))
        slippage_loss_wei = max(0, expected_no_slippage - result.swap2_output) if result.swap2_output > 0 else 0
        
        # 构造方向字符串
        if direction == "forward":
            dir_str = f"{pair_a.dex_name} -> {pair_b.dex_name}"
        else:
            dir_str = f"{pair_b.dex_name} -> {pair_a.dex_name}"
        
        # 检查扣除 gas 后是否有利可图
        is_profitable, net_profit = is_profitable_after_gas(result, gas_cost, self.min_profit_wei)
        
        if is_profitable and result.profitable:
            # 发现有利可图的套利机会
            return ArbitrageOpportunity(
                pair_a=pair_a,
                pair_b=pair_b,
                direction=dir_str,
                borrow_amount=opt_amount,
                expected_profit=result.profit,
                profit_after_gas=net_profit,
                price_diff_bps=result.price_diff_bps,
                timestamp=time.time(),
                spread_percent=spread_percent,
                gas_cost_wei=gas_cost,
                slippage_loss_wei=slippage_loss_wei,
                dex_fee_wei=dex_fee_wei
            ), None
        
        # Shadow Mode: 检查是否价差足够但利润为负
        if spread_percent >= shadow_spread_threshold * 100:  # 转换为百分比
            # 确定拒绝原因
            if not result.profitable:
                rejection_reason = "Gross profit negative (slippage > spread)"
            elif net_profit < 0:
                rejection_reason = "Gas cost exceeds gross profit"
            elif net_profit < self.min_profit_wei:
                rejection_reason = "Net profit below minimum threshold"
            else:
                rejection_reason = "Unknown"
            
            shadow = ShadowOpportunity(
                pair_a=pair_a,
                pair_b=pair_b,
                direction=dir_str,
                spread_percent=spread_percent,
                expected_profit_wei=result.profit - gas_cost,
                gas_cost_wei=gas_cost,
                slippage_loss_wei=slippage_loss_wei,
                dex_fee_wei=dex_fee_wei + flash_fee_wei,
                rejection_reason=rejection_reason,
                timestamp=time.time()
            )
            return None, shadow
        
        return None, None
    
    def scan(self, shadow_spread_threshold: float = 0.005) -> ScanResult:
        """
        执行一次扫描（返回完整扫描结果，包含延迟指标）
        
        🚀 Super-Batch Multicall: 所有配对在单次请求中获取
        📊 包含 End-to-End Latency Profiling
        
        参数：
            shadow_spread_threshold: Shadow Mode 价差阈值（默认 0.5%）
        
        返回：
            ScanResult 包含机会列表和延迟指标
        """
        t_start = time.time()
        
        # 🚀 Step 1: Super-Batch Multicall 获取所有储备
        success, network_time_ms, pairs_with_data = self.update_reserves()
        
        if not success:
            return ScanResult(
                opportunities=[],
                time_network_ms=network_time_ms,
                time_calc_ms=0.0,
                time_total_ms=(time.time() - t_start) * 1000,
                pairs_scanned=len(self.pairs),
                pairs_with_data=0
            )
        
        # 📊 Step 2: 计算套利机会
        t_calc_start = time.time()
        opportunities, shadow_opportunities = self.find_opportunities(shadow_spread_threshold)
        t_calc_end = time.time()
        calc_time_ms = (t_calc_end - t_calc_start) * 1000
        
        # 更新统计
        self.scan_count += 1
        self.opportunity_count += len(opportunities)
        self.last_scan_time = time.time() - t_start
        
        # 保存 shadow 机会供外部访问
        self._last_shadow_opportunities = shadow_opportunities
        
        return ScanResult(
            opportunities=opportunities,
            time_network_ms=network_time_ms,
            time_calc_ms=calc_time_ms,
            time_total_ms=(time.time() - t_start) * 1000,
            pairs_scanned=len(self.pairs),
            pairs_with_data=pairs_with_data
        )
    
    def get_last_shadow_opportunities(self) -> List[ShadowOpportunity]:
        """获取上次扫描的 Shadow 机会"""
        return getattr(self, '_last_shadow_opportunities', [])
    
    def run_once(self, shadow_spread_threshold: float = 0.005) -> List[ArbitrageOpportunity]:
        """
        执行一次扫描（旧版兼容）
        
        返回：
            发现的套利机会列表
        """
        result = self.scan(shadow_spread_threshold)
        return result.opportunities
    
    def run_loop(
        self,
        interval: float = 1.0,
        max_iterations: Optional[int] = None,
        callback: Optional[callable] = None,
        shadow_spread_threshold: float = 0.005,
        show_latency: bool = True
    ):
        """
        持续运行扫描循环
        
        参数：
            interval: 扫描间隔（秒）
            max_iterations: 最大迭代次数（None 表示无限）
            callback: 发现机会时的回调函数
            shadow_spread_threshold: Shadow Mode 价差阈值
            show_latency: 是否显示延迟指标
        """
        iteration = 0
        
        print("\n" + "=" * 60)
        print("套利扫描器启动 (Super-Batch Multicall)")
        print("=" * 60)
        print(f"监控配对数量: {len(self.pairs)}")
        print(f"配对组数量: {len(self.pair_groups)}")
        print(f"扫描间隔: {interval} 秒")
        print(f"Gas 价格: {self.gas_price_gwei} Gwei")
        print(f"Shadow Mode 阈值: {shadow_spread_threshold * 100:.1f}%")
        print("=" * 60 + "\n")
        
        try:
            while max_iterations is None or iteration < max_iterations:
                iteration += 1
                
                # 执行扫描（使用新的 scan() 方法）
                scan_result = self.scan(shadow_spread_threshold)
                
                # 输出结果
                self._print_scan_result_v2(iteration, scan_result, show_latency)
                
                # 调用回调
                if callback and scan_result.opportunities:
                    for opp in scan_result.opportunities:
                        callback(opp)
                
                # 等待下一次扫描
                if max_iterations is None or iteration < max_iterations:
                    time.sleep(interval)
        
        except KeyboardInterrupt:
            print("\n\n用户中断扫描")
        
        # 输出统计
        self._print_stats()
    
    def _print_scan_result(
        self,
        iteration: int,
        opportunities: List[ArbitrageOpportunity]
    ):
        """打印扫描结果（旧版兼容）"""
        timestamp = time.strftime("%H:%M:%S")
        
        if opportunities:
            print(f"\n🎯 [{timestamp}] 第 {iteration} 次扫描 - 发现 {len(opportunities)} 个机会!")
            print("-" * 60)
            
            for opp in opportunities:
                profit_eth = opp.profit_after_gas / 10**18
                borrow_eth = opp.borrow_amount / 10**18
                
                print(f"  方向: {opp.direction}")
                print(f"  借入: {borrow_eth:.4f} ETH")
                print(f"  净利润: {profit_eth:.6f} ETH (${profit_eth * 3000:.2f})")
                print(f"  价格差异: {opp.price_diff_bps:.2f} bps")
                print()
        else:
            # 简洁输出
            print(f"[{timestamp}] 扫描 #{iteration}: 无套利机会 ({self.last_scan_time*1000:.1f}ms)", end="\r")
    
    def _print_scan_result_v2(
        self,
        iteration: int,
        scan_result: ScanResult,
        show_latency: bool = True
    ):
        """打印扫描结果（包含延迟指标和 Shadow Mode）"""
        timestamp = time.strftime("%H:%M:%S")
        opportunities = scan_result.opportunities
        shadow_opps = self.get_last_shadow_opportunities()
        
        if opportunities:
            print(f"\n🎯 [{timestamp}] 第 {iteration} 次扫描 - 发现 {len(opportunities)} 个机会!")
            if show_latency:
                print(f"⏱️ LATENCY: {scan_result.get_latency_str()}")
            print("-" * 60)
            
            for opp in opportunities:
                profit_eth = opp.profit_after_gas / 10**18
                borrow_eth = opp.borrow_amount / 10**18
                
                print(f"  方向: {opp.direction}")
                print(f"  借入: {borrow_eth:.4f} ETH")
                print(f"  净利润: {profit_eth:.6f} ETH (${profit_eth * 3000:.2f})")
                print(f"  价格差异: {opp.price_diff_bps:.2f} bps ({opp.spread_percent:.3f}%)")
                print()
        elif shadow_opps:
            # Shadow Mode: 打印被拒绝的机会
            print(f"\n⚠️ [{timestamp}] [SHADOW] 发现 {len(shadow_opps)} 个潜在机会被拒绝:")
            if show_latency:
                print(f"⏱️ LATENCY: {scan_result.get_latency_str()}")
            print("-" * 60)
            
            for shadow in shadow_opps[:3]:  # 只显示前3个
                print(f"  [SHADOW] {shadow.direction}")
                print(f"    {shadow.get_breakdown_str()}")
                print(f"    Reason: {shadow.rejection_reason}")
                print()
        else:
            # 简洁输出
            latency_str = f" | {scan_result.get_latency_str()}" if show_latency else ""
            print(f"[{timestamp}] 扫描 #{iteration}: 无套利机会{latency_str}", end="\r")
    
    def _print_stats(self):
        """打印统计信息"""
        print("\n" + "=" * 60)
        print("扫描统计")
        print("=" * 60)
        print(f"总扫描次数: {self.scan_count}")
        print(f"发现机会次数: {self.opportunity_count}")
        print(f"平均扫描耗时: {self.last_scan_time*1000:.1f}ms")
    
    def get_pair_prices(self) -> Dict[str, Dict]:
        """
        获取所有配对的当前价格
        
        返回：
            配对价格信息字典
        """
        prices = {}
        
        for addr, pair in self.pairs.items():
            if pair.reserve0 == 0 or pair.reserve1 == 0:
                continue
            
            price_01 = get_price_ratio(pair.reserve0, pair.reserve1)
            price_10 = get_price_ratio(pair.reserve1, pair.reserve0)
            
            prices[pair.address] = {
                "dex": pair.dex_name,
                "token0": pair.token0,
                "token1": pair.token1,
                "reserve0": pair.reserve0,
                "reserve1": pair.reserve1,
                "price_01": price_01,
                "price_10": price_10,
            }
        
        return prices


# ============================================
# 辅助函数
# ============================================

def get_pair_address(
    w3: Web3,
    factory_address: str,
    token0: str,
    token1: str,
    dex_type: str = "uniswap_v2",
    stable: bool = False
) -> Optional[str]:
    """
    从工厂合约获取配对地址
    
    参数：
        w3: Web3 实例
        factory_address: 工厂合约地址
        token0: Token0 地址
        token1: Token1 地址
        dex_type: DEX 类型 ("uniswap_v2" 或 "solidly")
        stable: 是否为稳定币配对（仅 Solidly fork 需要）
        
    返回：
        配对地址或 None
    """
    try:
        if dex_type == "solidly":
            # Aerodrome/Solidly 使用 getPool(tokenA, tokenB, stable)
            factory = w3.eth.contract(
                address=w3.to_checksum_address(factory_address),
                abi=FACTORY_ABI_SOLIDLY
            )
            pair = factory.functions.getPool(
                w3.to_checksum_address(token0),
                w3.to_checksum_address(token1),
                stable
            ).call()
        else:
            # 标准 Uniswap V2 使用 getPair(tokenA, tokenB)
            factory = w3.eth.contract(
                address=w3.to_checksum_address(factory_address),
                abi=FACTORY_ABI_V2
            )
            pair = factory.functions.getPair(
                w3.to_checksum_address(token0),
                w3.to_checksum_address(token1)
            ).call()
        
        if pair == "0x0000000000000000000000000000000000000000":
            return None
        
        return w3.to_checksum_address(pair)
    except Exception as e:
        print(f"获取配对地址失败 ({factory_address[:10]}...): {e}")
        return None


def discover_aerodrome_pool(
    w3: Web3,
    token0: str,
    token1: str,
    stable: bool = False
) -> Optional[str]:
    """
    发现 Aerodrome 配对地址
    
    参数：
        w3: Web3 实例
        token0: Token0 地址
        token1: Token1 地址
        stable: 是否为稳定币配对（USDC/USDbC 等）
        
    返回：
        配对地址或 None
    """
    aerodrome_factory = DEX_CONFIG["Aerodrome"]["factory"]
    return get_pair_address(
        w3, 
        aerodrome_factory, 
        token0, 
        token1, 
        dex_type="solidly",
        stable=stable
    )


def discover_sushiswap_pair(
    w3: Web3,
    token0: str,
    token1: str
) -> Optional[str]:
    """
    发现 SushiSwap 配对地址
    
    参数：
        w3: Web3 实例
        token0: Token0 地址
        token1: Token1 地址
        
    返回：
        配对地址或 None
    """
    sushi_factory = DEX_CONFIG["SushiSwap"]["factory"]
    return get_pair_address(
        w3, 
        sushi_factory, 
        token0, 
        token1, 
        dex_type="uniswap_v2"
    )


def discover_all_pairs(
    w3: Web3,
    token0: str,
    token1: str
) -> List[Tuple[str, str, str, str, str]]:
    """
    发现所有 DEX 上的配对
    
    参数：
        w3: Web3 实例
        token0: Token0 地址
        token1: Token1 地址
        
    返回：
        配对信息列表 [(地址, token0, token1, DEX名称, 路由器), ...]
    """
    pairs = []
    
    for dex_name, config in DEX_CONFIG.items():
        dex_type = config["type"]
        router = config["router"]
        
        # 对于 Aerodrome，默认查询 volatile 配对
        if dex_type == "solidly":
            pair_addr = get_pair_address(
                w3, config["factory"], token0, token1, 
                dex_type="solidly", stable=False
            )
        else:
            pair_addr = get_pair_address(
                w3, config["factory"], token0, token1, 
                dex_type="uniswap_v2"
            )
        
        if pair_addr and pair_addr != "0x0000000000000000000000000000000000000000":
            pairs.append((pair_addr, token0, token1, dex_name, router))
    
    return pairs


def discover_token_pairs(
    w3: Web3,
    base_token: str,
    target_tokens: List[dict],
    verbose: bool = False
) -> List[Tuple[str, str, str, str, str]]:
    """
    发现多个目标代币与基础代币（如 WETH）在所有 DEX 上的配对
    
    参数：
        w3: Web3 实例
        base_token: 基础代币地址（如 WETH）
        target_tokens: 目标代币列表 [{"symbol": "DEGEN", "address": "0x..."}, ...]
        verbose: 是否打印详细日志
        
    返回：
        配对信息列表 [(地址, token0, token1, DEX名称, 路由器), ...]
    """
    all_pairs = []
    
    for token_config in target_tokens:
        symbol = token_config.get("symbol", "UNKNOWN")
        token_address = token_config["address"]
        
        if verbose:
            print(f"\n[{symbol}] 扫描 {base_token[:8]}.../{symbol} 配对...")
        
        pairs = discover_all_pairs(w3, base_token, token_address)
        
        if verbose:
            print(f"  找到 {len(pairs)} 个配对")
        
        all_pairs.extend(pairs)
    
    return all_pairs


def discover_pairs(
    w3: Web3,
    tokens: List[str],
    factories: Dict[str, str] = DEX_FACTORIES
) -> List[Tuple[str, str, str, str]]:
    """
    自动发现所有代币对的配对地址（向后兼容）
    
    参数：
        w3: Web3 实例
        tokens: 代币地址列表
        factories: DEX 工厂地址字典
        
    返回：
        配对信息列表 [(地址, token0, token1, DEX名称), ...]
    """
    pairs = []
    
    for i, token0 in enumerate(tokens):
        for token1 in tokens[i+1:]:
            for dex_name, factory_addr in factories.items():
                # 获取 DEX 类型
                dex_type = DEX_CONFIG.get(dex_name, {}).get("type", "uniswap_v2")
                
                pair_addr = get_pair_address(
                    w3, factory_addr, token0, token1,
                    dex_type=dex_type
                )
                if pair_addr:
                    pairs.append((pair_addr, token0, token1, dex_name))
                    print(f"发现配对: {dex_name} {token0[:8]}.../{token1[:8]}...")
    
    return pairs


# ============================================
# 主函数
# ============================================

def main():
    """主函数"""
    # 加载环境变量
    load_dotenv(PROJECT_ROOT / ".env")
    
    # 连接到网络
    rpc_url = os.getenv("RPC_URL", "http://127.0.0.1:8545")
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    
    if not w3.is_connected():
        print("无法连接到网络")
        sys.exit(1)
    
    print(f"已连接到网络，链 ID: {w3.eth.chain_id}")
    
    # 创建扫描器
    scanner = ArbitrageScanner(
        w3=w3,
        pairs=HARDCODED_PAIRS,
        gas_price_gwei=DEFAULT_GAS_PRICE_GWEI,
        min_profit_wei=10**14  # 0.0001 ETH 最小利润
    )
    
    # 获取并显示初始价格
    print("\n获取初始价格...")
    scanner.update_reserves()
    
    prices = scanner.get_pair_prices()
    print("\n当前配对价格:")
    print("-" * 60)
    
    for addr, info in prices.items():
        reserve0_fmt = info["reserve0"] / 10**18  # 假设是 WETH
        reserve1_fmt = info["reserve1"] / 10**6   # 假设是 USDC
        price = reserve1_fmt / reserve0_fmt if reserve0_fmt > 0 else 0
        
        print(f"[{info['dex']}]")
        print(f"  地址: {addr}")
        print(f"  储备: {reserve0_fmt:.4f} WETH / {reserve1_fmt:.2f} USDC")
        print(f"  价格: {price:.2f} USDC/WETH")
        print()
    
    # 运行扫描循环
    print("\n开始持续扫描...")
    print("按 Ctrl+C 停止\n")
    
    scanner.run_loop(
        interval=2.0,  # 每 2 秒扫描一次
        max_iterations=None  # 无限循环
    )


if __name__ == "__main__":
    main()

