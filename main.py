#!/usr/bin/env python3
"""
FlashArb-Core 主应用入口

功能：
- 初始化所有模块（Scanner、Calculator、Executor）
- 运行套利扫描和执行循环
- 处理信号和优雅关闭

使用方法：
    python main.py

环境变量：
    RPC_URL: RPC 节点地址（默认 http://127.0.0.1:8545）
    PRIVATE_KEY: 执行交易的私钥
    MIN_PROFIT_THRESHOLD: 最小利润阈值（ETH，默认 0.001）
    SCAN_INTERVAL: 扫描间隔（秒，默认 0.5）
    DRY_RUN: 是否只模拟不执行（默认 false）

配置：
    从 deployments.json 加载合约地址
    从 config/chains.json 加载链配置（可选）
"""

import os
import sys
import json
import signal
import asyncio
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime

from dotenv import load_dotenv
from web3 import Web3

# 添加项目根目录到路径
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# 导入自定义模块
from core.multicall import Multicall
from core.calculator import (
    calculate_arb_profit,
    check_both_directions,
    estimate_gas_cost,
    is_profitable_after_gas,
)
from core.scanner import (
    ArbitrageScanner, 
    ArbitrageOpportunity,
    ScanResult,
    ShadowOpportunity,
    HARDCODED_PAIRS,
    DEX_CONFIG,
    discover_all_pairs,
    discover_aerodrome_pool,
    discover_sushiswap_pair,
    discover_token_pairs,
    get_pair_address,
)
from core.executor import ArbitrageExecutor, ExecutionResult, create_executor_from_env
from core.journal import TradeJournal


# ============================================
# 配置
# ============================================

# 加载环境变量
load_dotenv(PROJECT_ROOT / ".env")

# 基础配置
RPC_URL = os.getenv("RPC_URL", "http://127.0.0.1:8545")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
MIN_PROFIT_THRESHOLD = float(os.getenv("MIN_PROFIT_THRESHOLD", "0.001"))  # ETH
SCAN_INTERVAL = float(os.getenv("SCAN_INTERVAL", "0.5"))  # 秒
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
GAS_PRICE_GWEI = float(os.getenv("GAS_PRICE_GWEI", "0.01"))
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"  # 详细日志模式
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "60"))  # 失败后冷却时间（秒）
MAX_FAIL_COUNT = int(os.getenv("MAX_FAIL_COUNT", "3"))  # 最大失败次数，超过后长时间冷却
LONG_COOLDOWN_SECONDS = int(os.getenv("LONG_COOLDOWN_SECONDS", "3600"))  # 长冷却时间（1小时）

# ============================================
# 🔍 Shadow Mode 配置
# ============================================
# Shadow Mode: 记录价差好但利润为负的机会，用于诊断
SHADOW_SPREAD_THRESHOLD = float(os.getenv("SHADOW_SPREAD_THRESHOLD", "0.005"))  # 0.5%
SHADOW_MODE_ENABLED = os.getenv("SHADOW_MODE", "true").lower() == "true"

# ============================================
# ⏱️ 延迟分析配置
# ============================================
LATENCY_PROFILING_ENABLED = os.getenv("LATENCY_PROFILING", "true").lower() == "true"

# ==========================================
# 🎯 Base Mainnet Target Tokens (Verified)
# ==========================================

# Base Mainnet WETH
WETH_ADDRESS = "0x4200000000000000000000000000000000000006"

# 稳定币地址（用于备用路径）
USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # 原生 USDC
USDbC_ADDRESS = "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA"  # 桥接 USDC

# 蓝筹 Meme 币 - 高波动性、价差大、Renounced ownership、0% Tax
TARGET_TOKENS = [
    {
        "symbol": "KEYCAT",
        # 你的扫描结果提供的地址
        "address": "0x9a26F5433671751C3276a065f57e5a02D2817973",
        "decimals": 18,
        "min_profit": 0.0002, # 约 $0.7, 这种高价差币种，稍微降低门槛确保命中
    },
    {
        "symbol": "SKI",
        # 你的扫描结果提供的地址
        "address": "0x768BE13e1680b5ebE0024C42c896E3dB59ec0149",
        "decimals": 18,
        "min_profit": 0.0002,
    },
    {
        "symbol": "VIRTUAL",
        # 你的扫描结果提供的地址
        "address": "0x0b3e328455c4059eeb9e3f84b5543f74e24e7e1b",
        "decimals": 18,
        "min_profit": 0.0002,
    },
    {
        "symbol": "BRETT",
        "address": "0x532f27101965dd16442E59d40670FaF5eBB142E4",
        "decimals": 18,
        "min_profit": 0.0002,
    },
    {
        "symbol": "TOSHI",
        "address": "0xAC1Bd2486aAf3B5C0fc3Fd868558b082a531B2B4",
        "decimals": 18,
        "min_profit": 0.0002,
    }
]

# 创建代币符号映射（地址 -> 符号）
TOKEN_SYMBOLS = {WETH_ADDRESS.lower(): "WETH"}
for token in TARGET_TOKENS:
    TOKEN_SYMBOLS[token["address"].lower()] = token["symbol"]

# ============================================
# DEX 路由器地址
# ============================================
ROUTER_BASESWAP = "0x327Df1E6de05895d2ab08513aaDD9313Fe505d86"
ROUTER_UNISWAP = "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24"
ROUTER_SUSHISWAP = "0x6BDED42c6DA8FBf0d2bA55B2fa120C5e0c8D7891"
ROUTER_AERODROME = "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"

# 路由器映射（DEX 名称 -> 路由器地址）
DEX_ROUTERS = {
    "BaseSwap": ROUTER_BASESWAP,
    "UniswapV2": ROUTER_UNISWAP,
    "SushiSwap": ROUTER_SUSHISWAP,
    "Aerodrome": ROUTER_AERODROME,
}

# ============================================
# 配对地址（硬编码）
# ============================================
PAIR_BASESWAP = "0x41d160033C222E6f3722EC97379867324567d883"      # WETH/USDbC
PAIR_UNISWAP = "0xe902EF54E437967c8b37D30E80ff887955c90DB6"       # WETH/USDbC
# 以下配对需要在启动时动态发现
PAIR_SUSHISWAP = ""   # WETH/USDbC（待发现）
PAIR_AERODROME = ""   # WETH/USDbC volatile（待发现）

# 部署文件
DEPLOYMENTS_FILE = PROJECT_ROOT / "deployments.json"


# ============================================
# 日志配置
# ============================================

def setup_logging() -> logging.Logger:
    """设置日志"""
    logger = logging.getLogger("FlashArb")
    
    # 根据 DEBUG_MODE 设置日志级别
    log_level = logging.DEBUG if DEBUG_MODE else logging.INFO
    logger.setLevel(log_level)
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    
    # 格式化
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-5s | %(message)s",
        datefmt="%H:%M:%S"
    )
    console_handler.setFormatter(formatter)
    
    logger.addHandler(console_handler)
    
    return logger


logger = setup_logging()


# ============================================
# 主应用类
# ============================================

class FlashArbBot:
    """
    FlashArb 套利机器人
    
    整合 Scanner、Calculator 和 Executor，执行套利策略。
    """
    
    def __init__(self):
        """初始化机器人"""
        self.w3: Optional[Web3] = None
        self.contract = None
        self.scanner: Optional[ArbitrageScanner] = None
        self.executor: Optional[ArbitrageExecutor] = None
        
        # 交易日志
        self.journal = TradeJournal()
        
        # 运行状态
        self.running = False
        self.paused = False
        
        # 统计信息
        self.scan_count = 0
        self.opportunity_count = 0
        self.execution_count = 0
        self.success_count = 0
        self.total_profit = 0
        self.start_time = None
        
        # 配置
        self.min_profit_threshold = int(MIN_PROFIT_THRESHOLD * 10**18)  # 转换为 wei
        self.scan_interval = SCAN_INTERVAL
        self.dry_run = DRY_RUN
        self.gas_price_gwei = GAS_PRICE_GWEI
        self.cooldown_seconds = COOLDOWN_SECONDS
        self.max_fail_count = MAX_FAIL_COUNT
        self.long_cooldown_seconds = LONG_COOLDOWN_SECONDS
        
        # 🔍 Shadow Mode 配置
        self.shadow_spread_threshold = SHADOW_SPREAD_THRESHOLD
        self.shadow_mode_enabled = SHADOW_MODE_ENABLED
        
        # ⏱️ 延迟分析配置
        self.latency_profiling_enabled = LATENCY_PROFILING_ENABLED
        
        # 冷却机制：记录失败的机会
        # {token_address: {"timestamp": float, "count": int, "cooldown": int}}
        self.failed_opportunities: Dict[str, Dict] = {}
    
    def initialize(self) -> bool:
        """
        初始化所有组件
        
        返回：
            是否初始化成功
        """
        logger.info("=" * 60)
        logger.info("FlashArb-Core 启动")
        logger.info("=" * 60)
        
        # 1. 连接网络
        logger.info(f"连接网络: {RPC_URL}")
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
        
        if not self.w3.is_connected():
            logger.error("无法连接到网络")
            return False
        
        chain_id = self.w3.eth.chain_id
        block_number = self.w3.eth.block_number
        logger.info(f"已连接 - 链 ID: {chain_id}, 区块: {block_number}")
        
        # 2. 加载合约
        logger.info("加载 FlashBot 合约...")
        
        if not DEPLOYMENTS_FILE.exists():
            logger.error(f"部署文件不存在: {DEPLOYMENTS_FILE}")
            return False
        
        try:
            deployments = json.loads(DEPLOYMENTS_FILE.read_text(encoding="utf-8"))
            chain_id_str = str(chain_id)
            
            if chain_id_str not in deployments:
                logger.error(f"未找到链 {chain_id} 的部署信息")
                return False
            
            contract_address = self.w3.to_checksum_address(
                deployments[chain_id_str]["contract_address"]
            )
            abi = deployments[chain_id_str]["abi"]
            
            self.contract = self.w3.eth.contract(address=contract_address, abi=abi)
            logger.info(f"合约地址: {contract_address}")
            
        except Exception as e:
            logger.error(f"加载合约失败: {e}")
            return False
        
        # 3. 初始化执行器
        logger.info("初始化执行器...")
        
        if not PRIVATE_KEY:
            logger.error("未设置 PRIVATE_KEY 环境变量")
            return False
        
        try:
            self.executor = create_executor_from_env(self.w3, self.contract)
            balance = self.executor.get_balance()
            logger.info(f"执行器地址: {self.executor.address}")
            logger.info(f"账户余额: {balance / 10**18:.4f} ETH")
        except Exception as e:
            logger.error(f"初始化执行器失败: {e}")
            return False
        
        # 4. 初始化扫描器
        logger.info("初始化扫描器...")
        
        # 动态发现配对
        discovered_pairs = self._discover_pairs()
        
        if not discovered_pairs:
            logger.error("未发现任何配对")
            return False
        
        self.scanner = ArbitrageScanner(
            w3=self.w3,
            pairs=discovered_pairs,
            gas_price_gwei=self.gas_price_gwei,
            min_profit_wei=self.min_profit_threshold
        )
        
        # 保存配对到路由器的映射
        self.pair_to_router = {}
        for pair_info in self.scanner.pairs.values():
            self.pair_to_router[pair_info.address.lower()] = pair_info.router
            self.pair_to_router[pair_info.dex_name] = pair_info.router
        
        # 确保授权了所有需要的代币
        self._check_and_setup_approvals()
        
        logger.info(f"监控配对数: {len(self.scanner.pairs)}")
        
        # 5. 显示配置
        logger.info("-" * 60)
        logger.info("配置:")
        logger.info(f"  最小利润阈值: {MIN_PROFIT_THRESHOLD} ETH")
        logger.info(f"  扫描间隔: {self.scan_interval} 秒")
        logger.info(f"  Gas 价格: {self.gas_price_gwei} Gwei")
        logger.info(f"  Dry Run 模式: {self.dry_run}")
        logger.info(f"  Debug 模式: {DEBUG_MODE}")
        logger.info(f"  🔍 Shadow Mode: {self.shadow_mode_enabled} (阈值: {self.shadow_spread_threshold*100:.1f}%)")
        logger.info(f"  ⏱️ 延迟分析: {self.latency_profiling_enabled}")
        logger.info(f"  🎯 Sniper Mode: 启用 (优先费 +20%)")
        logger.info("-" * 60)
        
        return True
    
    def _discover_pairs(self) -> List[Tuple]:
        """
        动态发现所有目标代币与 WETH 在各 DEX 上的配对
        
        返回：
            配对列表 [(地址, token0, token1, DEX名称, 路由器), ...]
        """
        logger.info("发现配对...")
        logger.info(f"  目标代币: {[t['symbol'] for t in TARGET_TOKENS]}")
        pairs = []
        
        # DEX 列表 - 所有支持的 DEX
        dex_list = [
            ("BaseSwap", ROUTER_BASESWAP, "uniswap_v2"),
            ("UniswapV2", ROUTER_UNISWAP, "uniswap_v2"),
            ("SushiSwap", ROUTER_SUSHISWAP, "uniswap_v2"),
            ("Aerodrome", ROUTER_AERODROME, "solidly"),
        ]
        
        # 为每个目标代币发现配对
        for token_config in TARGET_TOKENS:
            symbol = token_config["symbol"]
            token_address = token_config["address"]
            
            logger.info(f"\n  [{symbol}] 扫描 WETH/{symbol} 配对...")
            token_pairs_found = 0
            
            for dex_name, router, dex_type in dex_list:
                try:
                    if dex_type == "solidly":
                        # Aerodrome 使用 getPool
                        pair_addr = discover_aerodrome_pool(
                            self.w3, WETH_ADDRESS, token_address, stable=False
                        )
                    else:
                        # 标准 Uniswap V2 使用 getPair
                        pair_addr = discover_sushiswap_pair(
                            self.w3, WETH_ADDRESS, token_address
                        ) if dex_name == "SushiSwap" else self._get_v2_pair(
                            dex_name, WETH_ADDRESS, token_address
                        )
                    
                    if pair_addr and pair_addr != "0x0000000000000000000000000000000000000000":
                        pairs.append((
                            pair_addr,
                            WETH_ADDRESS,
                            token_address,
                            dex_name,
                            router
                        ))
                        token_pairs_found += 1
                        logger.info(f"    ✅ [{dex_name}] {pair_addr[:10]}...")
                    else:
                        logger.debug(f"    ❌ [{dex_name}] 未找到")
                        
                except Exception as e:
                    logger.debug(f"    ⚠️ [{dex_name}] 错误: {e}")
            
            logger.info(f"    {symbol}: 找到 {token_pairs_found} 个配对")
        
        logger.info(f"\n  📊 总计发现 {len(pairs)} 个配对")
        return pairs
    
    def _get_v2_pair(self, dex_name: str, token0: str, token1: str) -> Optional[str]:
        """
        获取标准 Uniswap V2 配对地址
        """
        from core.scanner import DEX_CONFIG, get_pair_address
        
        factory = DEX_CONFIG.get(dex_name, {}).get("factory")
        if not factory:
            return None
        
        return get_pair_address(
            self.w3, factory, token0, token1, dex_type="uniswap_v2"
        )
    
    def _check_and_setup_approvals(self):
        """检查并设置必要的授权"""
        # 这里可以添加检查/设置路由器授权的逻辑
        # 目前假设已在部署时设置
        pass
    
    async def run(self):
        """
        运行主循环
        """
        if not self.running:
            self.running = True
            self.start_time = datetime.now()
        
        logger.info("\n🚀 开始扫描套利机会...")
        logger.info("按 Ctrl+C 停止\n")
        
        try:
            while self.running:
                if self.paused:
                    await asyncio.sleep(1)
                    continue
                
                # 执行扫描
                await self._scan_and_execute()
                
                # 等待下一次扫描
                await asyncio.sleep(self.scan_interval)
                
        except asyncio.CancelledError:
            logger.info("扫描循环被取消")
        except Exception as e:
            logger.error(f"扫描循环异常: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.running = False
    
    async def _scan_and_execute(self):
        """
        扫描并执行套利
        
        🚀 Super-Batch Multicall: 单次请求获取所有储备
        🔍 Shadow Mode: 诊断被拒绝的机会
        ⏱️ End-to-End Latency Profiling
        """
        self.scan_count += 1
        
        # ⏱️ 性能统计：记录扫描开始时间 (t_start)
        t_start = time.time()
        
        # DEBUG: 显示扫描开始
        if DEBUG_MODE:
            logger.debug("🔄 Scanning market (Super-Batch Multicall)...")
        
        # 🚀 1. 使用 Super-Batch Multicall 扫描机会
        scan_result: ScanResult = self.scanner.scan(
            shadow_spread_threshold=self.shadow_spread_threshold
        )
        opportunities = scan_result.opportunities
        
        # ⏱️ 延迟分析：网络时间
        t_network = scan_result.time_network_ms
        t_calc = scan_result.time_calc_ms
        
        # 🔍 Shadow Mode: 获取被拒绝的机会
        shadow_opportunities = self.scanner.get_last_shadow_opportunities()
        
        # DEBUG: 显示每个配对的价格信息
        if DEBUG_MODE:
            prices = self.scanner.get_pair_prices()
            
            # 按代币分组显示
            token_groups = {}  # {other_token: [(dex, price), ...]}
            
            for addr, info in prices.items():
                dex_name = info.get("dex", "Unknown")
                token0 = info.get("token0", "").lower()
                token1 = info.get("token1", "").lower()
                reserve0 = info.get("reserve0", 0)
                reserve1 = info.get("reserve1", 0)
                
                weth_lower = WETH_ADDRESS.lower()
                
                # 确定配对中的另一个代币
                if token0 == weth_lower:
                    other_token = token1
                    weth_reserve = reserve0
                    other_reserve = reserve1
                else:
                    other_token = token0
                    weth_reserve = reserve1
                    other_reserve = reserve0
                
                # 获取代币符号和小数位
                symbol = TOKEN_SYMBOLS.get(other_token, other_token[:8] + "...")
                
                # 查找代币配置获取小数位
                decimals = 18  # 默认 18
                for t in TARGET_TOKENS:
                    if t["address"].lower() == other_token:
                        decimals = t.get("decimals", 18)
                        break
                
                # 计算价格：1 WETH = ? other_token
                if weth_reserve > 0:
                    # price = (other_reserve / 10^decimals) / (weth_reserve / 10^18)
                    price = (other_reserve / (10 ** decimals)) / (weth_reserve / 10**18)
                else:
                    price = 0
                
                # 分组
                if other_token not in token_groups:
                    token_groups[other_token] = []
                token_groups[other_token].append((dex_name, price, symbol))
            
            # 输出每个代币的价格
            for other_token, dex_prices in token_groups.items():
                if len(dex_prices) == 0:
                    continue
                
                symbol = dex_prices[0][2]
                prices_only = [p[1] for p in dex_prices]
                
                # 显示每个 DEX 的价格
                for dex_name, price, _ in dex_prices:
                    if price > 0:
                        logger.debug(f"  [{dex_name}] WETH/{symbol}: {price:,.2f}")
                
                # 计算价差
                if len(prices_only) >= 2 and min(prices_only) > 0:
                    max_p = max(prices_only)
                    min_p = min(prices_only)
                    diff_pct = ((max_p - min_p) / min_p) * 100
                    
                    # 估算利润
                    fee_pct = 0.3  # 0.3% 闪电贷费
                    net_profit_pct = diff_pct - fee_pct
                    net_profit_eth = net_profit_pct / 100
                    
                    status = "✅" if net_profit_pct > 0 else "❌"
                    logger.debug(f"  📉 {symbol} Spread: {diff_pct:.3f}% | Profit: {net_profit_eth:.4f} ETH {status}")
        
        # 🔍 2. Shadow Mode: 记录被拒绝的机会
        if self.shadow_mode_enabled and shadow_opportunities and not opportunities:
            for shadow in shadow_opportunities[:3]:  # 只显示前3个
                logger.warning(f"[SHADOW] {shadow.direction}")
                logger.warning(f"  Spread is good ({shadow.spread_percent:.3f}%), but Profit is negative ({shadow.expected_profit_wei / 10**18:.6f} ETH)")
                logger.warning(f"  Breakdown: Gas Cost = {shadow.gas_cost_wei / 10**18:.6f} ETH, "
                             f"Slippage Loss = {shadow.slippage_loss_wei / 10**18:.6f} ETH, "
                             f"DEX Fee = {shadow.dex_fee_wei / 10**18:.6f} ETH")
                logger.warning(f"  Reason: {shadow.rejection_reason}")
            
            # ⏱️ 延迟分析（Shadow Mode）
            if self.latency_profiling_enabled:
                t_total = (time.time() - t_start) * 1000
                logger.info(f"⏱️ LATENCY: Network: {t_network:.0f}ms | Calc: {t_calc:.0f}ms | Total: {t_total:.0f}ms")
        
        # 3. 处理每个机会
        if DEBUG_MODE and not opportunities:
            logger.debug("  ⚠️ Scanner 未发现可执行的套利机会")
            logger.debug("     (注意: DEBUG 估算使用简化公式，Scanner 使用精确 AMM 计算)")
        
        for opp in opportunities:
            self.opportunity_count += 1
            
            # 计算预期利润和 Gas 成本
            gas_cost = estimate_gas_cost(self.gas_price_gwei)
            net_profit = opp.profit_after_gas
            
            # 检查是否超过阈值
            if net_profit < self.min_profit_threshold:
                continue
            
            # 获取代币地址（用于冷却检查）
            token_address = self._get_token_address(opp)
            token_symbol = self._get_token_symbol(opp)
            
            # 检查冷却期
            current_time = time.time()
            token_key = token_address.lower()
            if token_key in self.failed_opportunities:
                fail_info = self.failed_opportunities[token_key]
                failed_time = fail_info["timestamp"]
                fail_count = fail_info["count"]
                cooldown_duration = fail_info["cooldown"]
                elapsed = current_time - failed_time
                
                if elapsed < cooldown_duration:
                    remaining = cooldown_duration - elapsed
                    # 格式化剩余时间
                    if remaining >= 3600:
                        time_str = f"{remaining/3600:.1f} 小时"
                    elif remaining >= 60:
                        time_str = f"{remaining/60:.1f} 分钟"
                    else:
                        time_str = f"{remaining:.0f} 秒"
                    logger.debug(f"[COOLDOWN] 跳过 {token_symbol}（失败 {fail_count} 次），还需等待 {time_str}")
                    continue
                else:
                    # 冷却期已过，但保留失败次数记录（不删除）
                    # 只有成功交易才会重置失败次数
                    logger.info(f"[COOLDOWN] {token_symbol} 冷却期结束（已失败 {fail_count} 次），重新尝试")
            
            # 3. 发现有利可图的机会！
            profit_eth = net_profit / 10**18
            borrow_eth = opp.borrow_amount / 10**18
            
            logger.info("=" * 60)
            logger.info("🎯 发现套利机会!")
            logger.info("=" * 60)
            logger.info(f"  方向: {opp.direction}")
            logger.info(f"  借入: {borrow_eth:.4f} ETH")
            logger.info(f"  预期利润: {profit_eth:.6f} ETH (${profit_eth * 3000:.2f})")
            logger.info(f"  价格差异: {opp.price_diff_bps:.2f} bps")
            
            # 4. 执行交易
            if self.dry_run:
                logger.info("  [Dry Run] 跳过执行")
                # 记录到日志（Dry Run 模式）
                self.journal.log_opportunity(
                    token_symbol=token_symbol,
                    borrow_amount=borrow_eth,
                    direction=opp.direction,
                    expected_profit=profit_eth,
                    notes="Dry Run mode"
                )
                continue
            
            # 确定交易参数
            try:
                result = await self._execute_opportunity(opp)
            except Exception as e:
                # 捕获执行过程中的异常（如 AttributeError）
                logger.error(f"  ❌ 执行异常: {e}")
                result = ExecutionResult(
                    success=False,
                    error=str(e)
                )
            
            # 记录到交易日志
            if result.success:
                self.success_count += 1
                self.total_profit += result.profit_realized
                logger.info(f"  ✅ 交易成功!")
                logger.info(f"  Tx Hash: {result.tx_hash}")
                logger.info(f"  Gas 使用: {result.gas_used:,}")
                
                # ⏱️ End-to-End Latency Profiling
                if self.latency_profiling_enabled:
                    t_total = t_network + t_calc + result.time_total_ms
                    logger.info(f"  ⏱️ LATENCY: Network: {t_network:.0f}ms | Calc: {t_calc:.0f}ms | Exec: {result.time_simulation_ms + result.time_signing_ms:.0f}ms | Broadcast: {result.time_broadcast_ms:.0f}ms | Total: {t_total:.0f}ms")
                    logger.info(f"  ⏱️ Speed Stats (Detailed):")
                    logger.info(f"     - Network (Multicall): {t_network:.0f}ms")
                    logger.info(f"     - Calculation:         {t_calc:.0f}ms")
                    logger.info(f"     - Simulation:          {result.time_simulation_ms:.0f}ms")
                    logger.info(f"     - Signing:             {result.time_signing_ms:.0f}ms")
                    logger.info(f"     - Broadcast:           {result.time_broadcast_ms:.0f}ms")
                    logger.info(f"     - Confirmation:        {result.time_confirmation_ms:.0f}ms")
                    logger.info(f"     - Total:               {t_total:.0f}ms")
                
                # 成功交易：从冷却列表中移除并重置失败计数
                token_key = token_address.lower()
                if token_key in self.failed_opportunities:
                    prev_count = self.failed_opportunities[token_key]["count"]
                    del self.failed_opportunities[token_key]
                    logger.info(f"  ✅ {token_symbol} 失败计数已重置（之前失败 {prev_count} 次）")
                
                # 记录成功交易
                self.journal.log_trade(
                    token_symbol=token_symbol,
                    borrow_amount=borrow_eth,
                    direction=opp.direction,
                    expected_profit=profit_eth,
                    tx_hash=result.tx_hash,
                    status="Success",
                    gas_used=result.gas_used,
                    actual_profit=result.profit_realized / 10**18 if result.profit_realized else 0
                )
            else:
                # 交易失败：可能是模拟失败、链上 revert 或软失败
                is_simulation_failure = result.tx_hash is None
                is_soft_fail = result.error and "Soft fail" in result.error
                
                if is_simulation_failure:
                    # 模拟失败：交易未发送，节省了 gas
                    logger.warning(f"  ⚠️ [SIMULATION] 模拟失败，跳过交易以节省 gas")
                    logger.warning(f"     Error: {result.error}")
                    if self.latency_profiling_enabled:
                        t_total = t_network + t_calc + result.time_total_ms
                        logger.info(f"  ⏱️ LATENCY: Network: {t_network:.0f}ms | Calc: {t_calc:.0f}ms | Sim: {result.time_simulation_ms:.0f}ms (failed) | Total: {t_total:.0f}ms")
                elif is_soft_fail:
                    # 软失败：交易成功但没有执行套利（early exit）
                    logger.warning(f"  ⚠️ [SOFT FAIL] 交易未执行套利 (gas={result.gas_used})")
                    if self.latency_profiling_enabled:
                        t_total = t_network + t_calc + result.time_total_ms
                        logger.info(f"  ⏱️ LATENCY: Network: {t_network:.0f}ms | Calc: {t_calc:.0f}ms | Exec: {result.time_simulation_ms + result.time_signing_ms:.0f}ms | Broadcast: {result.time_broadcast_ms:.0f}ms | Total: {t_total:.0f}ms")
                else:
                    # 链上 revert：交易已发送但失败
                    logger.warning(f"  ❌ 交易失败 (链上 revert): {result.error}")
                    if self.latency_profiling_enabled:
                        t_total = t_network + t_calc + result.time_total_ms
                        logger.info(f"  ⏱️ LATENCY: Network: {t_network:.0f}ms | Calc: {t_calc:.0f}ms | Total Exec: {result.time_total_ms:.0f}ms | Total: {t_total:.0f}ms")
                
                # 递进式冷却：失败次数越多，冷却时间越长
                token_key = token_address.lower()
                if token_key in self.failed_opportunities:
                    # 已有失败记录，增加计数
                    prev_count = self.failed_opportunities[token_key]["count"]
                    new_count = prev_count + 1
                else:
                    new_count = 1
                
                # 根据失败次数决定冷却时间
                if new_count >= self.max_fail_count:
                    # 达到最大失败次数，长时间冷却
                    cooldown = self.long_cooldown_seconds
                    cooldown_str = f"{cooldown/3600:.1f} 小时"
                    logger.warning(f"  🚫 {token_symbol} 已失败 {new_count} 次，进入长冷却期 ({cooldown_str})")
                else:
                    # 普通冷却
                    cooldown = self.cooldown_seconds
                    cooldown_str = f"{cooldown} 秒"
                    logger.info(f"  ⏳ [COOLDOWN] {token_symbol} 失败 {new_count}/{self.max_fail_count} 次，冷却 {cooldown_str}")
                
                # 更新冷却列表
                self.failed_opportunities[token_key] = {
                    "timestamp": current_time,
                    "count": new_count,
                    "cooldown": cooldown
                }
                
                # 记录失败交易
                if is_simulation_failure:
                    status = "Simulation Failed"
                elif is_soft_fail:
                    status = "Soft Fail"
                else:
                    status = "Revert"
                    
                self.journal.log_trade(
                    token_symbol=token_symbol,
                    borrow_amount=borrow_eth,
                    direction=opp.direction,
                    expected_profit=profit_eth,
                    tx_hash=result.tx_hash or "N/A (Simulation)",
                    status=status,
                    notes=result.error or ""
                )
            
            self.execution_count += 1
            logger.info("=" * 60 + "\n")
            
            # 执行后暂停一下，避免连续发送
            await asyncio.sleep(2)
        
        # 定期显示状态（每 100 次扫描）
        if self.scan_count % 100 == 0:
            self._log_stats()
    
    async def _execute_opportunity(
        self,
        opp: ArbitrageOpportunity
    ) -> ExecutionResult:
        """
        执行套利机会（跨 DEX 模式）
        
        参数：
            opp: 套利机会对象
            
        返回：
            执行结果
            
        跨 DEX 套利流程：
        1. 从 borrow_dex 借入 WETH
        2. 在 trade_dex 用 WETH 换 USDbC（第一跳）
        3. 在 borrow_dex 用 USDbC 换回 WETH（第二跳）
        4. 还给 borrow_dex
        """
        # 解析方向字符串，格式: "DEX_A -> DEX_B"
        direction_parts = opp.direction.split(" -> ")
        
        if len(direction_parts) == 2:
            borrow_dex = direction_parts[0].strip()
            trade_dex = direction_parts[1].strip()
        else:
            # 回退到旧逻辑
            if "forward" in opp.direction.lower():
                borrow_dex = opp.pair_a.dex_name
                trade_dex = opp.pair_b.dex_name
            else:
                borrow_dex = opp.pair_b.dex_name
                trade_dex = opp.pair_a.dex_name
        
        # 获取借贷配对地址
        pair_address = opp.pair_a.address if opp.pair_a.dex_name == borrow_dex else opp.pair_b.address
        
        # 获取两个路由器
        router1 = DEX_ROUTERS.get(trade_dex, "")      # 第一跳：在 trade_dex 上 swap
        router2 = DEX_ROUTERS.get(borrow_dex, "")     # 第二跳：在 borrow_dex 上 swap 回来
        
        if not router1 or not router2:
            logger.error(f"未知的 DEX: trade={trade_dex}, borrow={borrow_dex}")
            return ExecutionResult(success=False, error=f"未知 DEX")
        
        # 确定中间代币（USDbC 或 USDC）
        if opp.pair_a.dex_name == borrow_dex:
            intermediate_token = opp.pair_a.token1
        else:
            intermediate_token = opp.pair_b.token1
        
        # 跨 DEX 路径：
        # 第一跳：WETH -> USDbC（在 trade_dex）
        # 第二跳：USDbC -> WETH（在 borrow_dex）
        path1 = [WETH_ADDRESS, intermediate_token]
        path2 = [intermediate_token, WETH_ADDRESS]
        
        logger.info(f"  借贷 DEX: {borrow_dex} ({pair_address[:10]}...)")
        logger.info(f"  交易 DEX: {trade_dex} ({router1[:10]}...)")
        logger.info(f"  第一跳: WETH -> {intermediate_token[:10]}... (在 {trade_dex})")
        logger.info(f"  第二跳: {intermediate_token[:10]}... -> WETH (在 {borrow_dex})")
        
        # 执行跨 DEX 交易
        result = self.executor.execute_trade(
            direction=opp.direction,
            borrow_amount=opp.borrow_amount,
            pair_address=pair_address,
            target_router=router1,
            trade_path=path1,
            token_borrow=WETH_ADDRESS,
            expected_profit=opp.profit_after_gas,
            dry_run=self.dry_run,
            # 跨 DEX 参数
            router2=router2,
            path2=path2
        )
        
        return result
    
    def _get_token_address(self, opp: ArbitrageOpportunity) -> str:
        """
        从套利机会中获取代币地址（非 WETH）
        
        参数：
            opp: 套利机会对象
            
        返回：
            代币地址
        """
        weth_lower = WETH_ADDRESS.lower()
        
        if opp.pair_a.token0.lower() != weth_lower:
            return opp.pair_a.token0
        elif opp.pair_a.token1.lower() != weth_lower:
            return opp.pair_a.token1
        else:
            # 回退到 pair_b
            if opp.pair_b.token0.lower() != weth_lower:
                return opp.pair_b.token0
            elif opp.pair_b.token1.lower() != weth_lower:
                return opp.pair_b.token1
        
        return ""
    
    def _get_token_symbol(self, opp: ArbitrageOpportunity) -> str:
        """
        从套利机会中获取代币符号
        
        参数：
            opp: 套利机会对象
            
        返回：
            代币符号（如 "BRETT"）
        """
        # 获取非 WETH 的代币地址
        token_address = self._get_token_address(opp)
        
        # 从 TOKEN_SYMBOLS 映射获取符号
        symbol = TOKEN_SYMBOLS.get(token_address.lower(), "")
        
        if not symbol:
            # 如果映射中没有，返回地址的缩写
            symbol = token_address[:8] + "..." if token_address else "UNKNOWN"
        
        return symbol
    
    def _log_stats(self):
        """记录统计信息"""
        elapsed = (datetime.now() - self.start_time).total_seconds() if self.start_time else 0
        
        logger.info("-" * 40)
        logger.info(f"📊 统计 | 扫描: {self.scan_count} | "
                   f"机会: {self.opportunity_count} | "
                   f"执行: {self.execution_count} | "
                   f"成功: {self.success_count} | "
                   f"利润: {self.total_profit / 10**18:.6f} ETH | "
                   f"运行: {elapsed:.0f}s")
        logger.info("-" * 40)
    
    def stop(self):
        """停止机器人"""
        logger.info("\n正在停止...")
        self.running = False
    
    def pause(self):
        """暂停扫描"""
        self.paused = True
        logger.info("扫描已暂停")
    
    def resume(self):
        """恢复扫描"""
        self.paused = False
        logger.info("扫描已恢复")
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        elapsed = (datetime.now() - self.start_time).total_seconds() if self.start_time else 0
        
        return {
            "running": self.running,
            "paused": self.paused,
            "scan_count": self.scan_count,
            "opportunity_count": self.opportunity_count,
            "execution_count": self.execution_count,
            "success_count": self.success_count,
            "total_profit_eth": self.total_profit / 10**18,
            "elapsed_seconds": elapsed,
            "scans_per_second": self.scan_count / elapsed if elapsed > 0 else 0,
        }
    
    def print_final_stats(self):
        """打印最终统计"""
        stats = self.get_stats()
        
        logger.info("\n" + "=" * 60)
        logger.info("最终统计")
        logger.info("=" * 60)
        logger.info(f"  运行时间: {stats['elapsed_seconds']:.0f} 秒")
        logger.info(f"  总扫描次数: {stats['scan_count']}")
        logger.info(f"  发现机会: {stats['opportunity_count']}")
        logger.info(f"  执行交易: {stats['execution_count']}")
        logger.info(f"  成功交易: {stats['success_count']}")
        logger.info(f"  总利润: {stats['total_profit_eth']:.6f} ETH")
        logger.info(f"  扫描速度: {stats['scans_per_second']:.2f}/秒")
        logger.info("=" * 60)
        
        # 打印交易日志摘要
        self.journal.print_summary()


# ============================================
# 信号处理
# ============================================

bot: Optional[FlashArbBot] = None


def signal_handler(signum, frame):
    """处理中断信号"""
    global bot
    if bot:
        bot.stop()


# ============================================
# 主入口
# ============================================

async def async_main():
    """异步主函数"""
    global bot
    
    # 创建机器人
    bot = FlashArbBot()
    
    # 设置信号处理
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 初始化
    if not bot.initialize():
        logger.error("初始化失败，退出")
        return 1
    
    # 运行主循环
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("\n收到中断信号")
    finally:
        bot.print_final_stats()
    
    return 0


def main():
    """同步主函数"""
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("\n用户中断")
        return 0


if __name__ == "__main__":
    sys.exit(main())

