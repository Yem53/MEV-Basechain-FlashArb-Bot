#!/usr/bin/env python3
"""
=========================================================
     🚀 FlashArb V3 - Native Uniswap V3 Arbitrage Bot
=========================================================

原生 Uniswap V3 闪电贷套利机器人

核心优势：
- V3 闪电贷费率低（0.05% vs V2 的 0.3%）
- 支持多费率层级套利（0.01%, 0.05%, 0.3%, 1%）
- 从 sqrtPriceX96 精确计算价格
- 跨协议套利（V3 -> V2, V3 -> Solidly）

Base Mainnet 常量：
- V3 Factory: 0x33128a8fC17869897dcE68Ed026d694621f6FDfD
- V3 Router:  0x2626664c2603336E57B271c5C0b26F421741e481
- WETH:       0x4200000000000000000000000000000000000006

使用方法：
    python main_v3.py
"""

import os
import sys
import json
import time
import signal
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

from dotenv import load_dotenv
from web3 import Web3

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# 加载环境变量
load_dotenv(PROJECT_ROOT / ".env")

# ============================================
# V3 组件导入
# ============================================

from core.scanner_v3 import (
    V3ArbitrageScanner,
    V3ArbitrageOpportunity,
    V3ScanResult,
    WETH_ADDRESS,
    V3_FACTORY,
    V3_ROUTER,
    V3_FEE_TIERS,
    FEE_TIER_NAMES
)
from core.executor_v3 import (
    V3ArbitrageExecutor,
    V3ExecutionResult,
    SwapType
)

# ============================================
# 配置
# ============================================

# 网络配置
RPC_URL = os.getenv("RPC_URL", "http://127.0.0.1:8545")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
CONTRACT_ADDRESS = os.getenv("FLASHBOT_V3_ADDRESS", "")

# 套利配置
MIN_PROFIT_ETH = float(os.getenv("MIN_PROFIT_ETH", "0.001"))
MIN_PROFIT_WEI = int(MIN_PROFIT_ETH * 10**18)

# 闪电贷配置
PREFERRED_FLASH_FEE = int(os.getenv("FLASH_FEE_TIER", "500"))  # 0.05%
DEFAULT_BORROW_AMOUNT_ETH = float(os.getenv("BORROW_AMOUNT_ETH", "1.0"))
DEFAULT_BORROW_AMOUNT = int(DEFAULT_BORROW_AMOUNT_ETH * 10**18)

# 扫描配置
SCAN_INTERVAL = float(os.getenv("SCAN_INTERVAL", "1.0"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# 日志配置
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LATENCY_PROFILING = os.getenv("LATENCY_PROFILING", "true").lower() == "true"

# ============================================
# 目标代币 - Base Mainnet
# ============================================

# 主流代币
TARGET_TOKENS = [
    {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
    {"symbol": "USDbC", "address": "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA", "decimals": 6},
    {"symbol": "DAI", "address": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb", "decimals": 18},
    {"symbol": "cbETH", "address": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22", "decimals": 18},
    {"symbol": "wstETH", "address": "0xc1CBa3fCea344f92D9239c08C0568f6F2F0ee452", "decimals": 18},
]

# ============================================
# FlashBotV3 ABI (简化版)
# ============================================

FLASHBOT_V3_ABI = [
    {
        "inputs": [
            {"name": "poolAddress", "type": "address"},
            {"name": "tokenBorrow", "type": "address"},
            {"name": "amountBorrow", "type": "uint256"},
            {"name": "userData", "type": "bytes"}
        ],
        "name": "startArbitrage",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "token", "type": "address"}],
        "name": "getTokenBalance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "owner",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"name": "token", "type": "address"}, {"name": "router", "type": "address"}],
        "name": "approveRouter",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]


# ============================================
# V3 机器人主类
# ============================================

class FlashArbV3Bot:
    """
    V3 原生闪电贷套利机器人
    """
    
    def __init__(self):
        self.w3: Optional[Web3] = None
        self.contract = None
        self.scanner: Optional[V3ArbitrageScanner] = None
        self.executor: Optional[V3ArbitrageExecutor] = None
        
        # 状态
        self.running = False
        self.scan_count = 0
        self.opportunity_count = 0
        self.execution_count = 0
        self.total_profit = 0
        self.start_time = None
        
        # 设置信号处理
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """处理退出信号"""
        print("\n\n🛑 收到停止信号，正在安全退出...")
        self.running = False
    
    def initialize(self) -> bool:
        """
        初始化机器人
        """
        print("\n" + "="*60)
        print("     🚀 FlashArb V3 - Native Uniswap V3 Arbitrage Bot")
        print("="*60)
        
        # 1. 连接网络
        print(f"\n🌐 连接网络: {RPC_URL[:50]}...")
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
        
        if not self.w3.is_connected():
            print("❌ 无法连接到网络")
            return False
        
        chain_id = self.w3.eth.chain_id
        print(f"✅ 已连接，链 ID: {chain_id}")
        
        if chain_id != 8453:
            print(f"⚠️ 警告: 不是 Base Mainnet (8453)，当前链: {chain_id}")
        
        # 2. 加载合约
        if CONTRACT_ADDRESS:
            print(f"\n📜 加载合约: {CONTRACT_ADDRESS[:20]}...")
            try:
                self.contract = self.w3.eth.contract(
                    address=self.w3.to_checksum_address(CONTRACT_ADDRESS),
                    abi=FLASHBOT_V3_ABI
                )
                owner = self.contract.functions.owner().call()
                print(f"✅ 合约已加载，所有者: {owner[:10]}...")
            except Exception as e:
                print(f"⚠️ 合约加载失败: {e}")
                self.contract = None
        else:
            print("⚠️ 未设置 FLASHBOT_V3_ADDRESS，将在模拟模式运行")
        
        # 3. 初始化执行器
        if PRIVATE_KEY and self.contract:
            print("\n🔐 初始化执行器...")
            try:
                self.executor = V3ArbitrageExecutor(
                    self.w3,
                    self.contract,
                    PRIVATE_KEY
                )
                balance = self.executor.get_balance()
                print(f"✅ 执行器就绪，账户: {self.executor.address[:10]}...")
                print(f"   余额: {balance / 10**18:.4f} ETH")
            except Exception as e:
                print(f"⚠️ 执行器初始化失败: {e}")
                self.executor = None
        else:
            print("⚠️ 未设置 PRIVATE_KEY 或合约，执行器禁用")
        
        # 4. 初始化扫描器
        print("\n🔍 初始化 V3 扫描器...")
        self.scanner = V3ArbitrageScanner(
            self.w3,
            target_tokens=TARGET_TOKENS,
            fee_tiers=V3_FEE_TIERS
        )
        
        # 5. 发现 V3 池
        print(f"\n📊 发现 V3 池 (费率: {', '.join(FEE_TIER_NAMES.values())})...")
        pools = self.scanner.discover_pools(WETH_ADDRESS)
        print(f"✅ 发现 {len(pools)} 个 V3 池")
        
        # 6. 显示配置摘要
        print("\n" + "="*60)
        print("配置摘要")
        print("="*60)
        print(f"  最小利润:     {MIN_PROFIT_ETH} ETH")
        print(f"  闪电贷费率:   {FEE_TIER_NAMES.get(PREFERRED_FLASH_FEE, str(PREFERRED_FLASH_FEE))}")
        print(f"  借贷金额:     {DEFAULT_BORROW_AMOUNT_ETH} ETH")
        print(f"  扫描间隔:     {SCAN_INTERVAL}s")
        print(f"  模拟模式:     {'是' if DRY_RUN else '否'}")
        print(f"  延迟分析:     {'启用' if LATENCY_PROFILING else '禁用'}")
        print("="*60)
        
        return True
    
    def run(self):
        """
        运行主循环
        """
        if not self.scanner:
            print("❌ 扫描器未初始化")
            return
        
        self.running = True
        self.start_time = time.time()
        
        print(f"\n🏃 开始扫描循环... (Ctrl+C 停止)\n")
        
        while self.running:
            try:
                cycle_start = time.time()
                
                # 执行扫描
                result = self.scanner.scan()
                self.scan_count += 1
                
                # 处理发现的机会
                if result.opportunities:
                    self._handle_opportunities(result.opportunities)
                
                # 显示扫描状态
                self._display_scan_status(result)
                
                # 等待下一个周期
                elapsed = time.time() - cycle_start
                sleep_time = max(0, SCAN_INTERVAL - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)
                
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"[ERROR] 扫描循环错误: {e}")
                time.sleep(SCAN_INTERVAL)
        
        # 显示最终统计
        self._display_final_stats()
    
    def _handle_opportunities(self, opportunities: List[V3ArbitrageOpportunity]):
        """
        处理发现的套利机会
        """
        for opp in opportunities:
            self.opportunity_count += 1
            
            # 显示机会
            print(f"\n{'='*60}")
            print(f"🎯 发现套利机会 #{self.opportunity_count}")
            print(f"{'='*60}")
            print(f"  方向:         {opp.direction}")
            print(f"  池 A:         {opp.pool_a.address[:20]}... ({FEE_TIER_NAMES[opp.pool_a.fee]})")
            print(f"  池 B:         {opp.pool_b.address[:20]}... ({FEE_TIER_NAMES[opp.pool_b.fee]})")
            print(f"  价差:         {opp.price_diff_percent:.4f}%")
            print(f"  预期利润:     {opp.expected_profit / 10**18:.6f} ETH")
            print(f"  闪电贷费用:   {opp.flash_fee / 10**18:.6f} ETH")
            print(f"  净利润:       {opp.profit_after_fee / 10**18:.6f} ETH")
            
            # 检查是否满足最小利润
            if opp.profit_after_fee < MIN_PROFIT_WEI:
                print(f"  ❌ 利润不足 (< {MIN_PROFIT_ETH} ETH)，跳过")
                continue
            
            # 执行交易
            if self.executor and not DRY_RUN:
                print(f"\n  🚀 执行套利...")
                exec_result = self._execute_opportunity(opp)
                
                if exec_result.success:
                    print(f"  ✅ 成功! TX: {exec_result.tx_hash}")
                    print(f"     Gas Used: {exec_result.gas_used}")
                    self.execution_count += 1
                    self.total_profit += opp.profit_after_fee
                else:
                    print(f"  ❌ 失败: {exec_result.error}")
                
                # 延迟分析
                if LATENCY_PROFILING:
                    print(f"  ⏱️ LATENCY: Sim: {exec_result.time_simulation_ms:.0f}ms | "
                          f"Sign: {exec_result.time_signing_ms:.0f}ms | "
                          f"Broadcast: {exec_result.time_broadcast_ms:.0f}ms | "
                          f"Confirm: {exec_result.time_confirmation_ms:.0f}ms | "
                          f"Total: {exec_result.time_total_ms:.0f}ms")
            else:
                print(f"  📝 [DRY RUN] 不执行实际交易")
    
    def _execute_opportunity(self, opp: V3ArbitrageOpportunity) -> V3ExecutionResult:
        """
        执行单个套利机会
        """
        try:
            # 使用低费率池作为闪电贷源
            flash_pool = opp.pool_a if opp.pool_a.fee <= opp.pool_b.fee else opp.pool_b
            trade_pool = opp.pool_b if flash_pool == opp.pool_a else opp.pool_a
            
            # 编码交换参数
            swap_params = self.executor._encode_v3_swap_data(
                WETH_ADDRESS,
                opp.pool_a.token0 if opp.pool_a.token0.lower() != WETH_ADDRESS.lower() 
                    else opp.pool_a.token1,
                trade_pool.fee
            )
            
            return self.executor.execute_v3_arbitrage(
                pool_address=flash_pool.address,
                token_borrow=WETH_ADDRESS,
                amount_borrow=opp.borrow_amount,
                swap_type=SwapType.V3,
                swap_params=swap_params,
                expected_profit=opp.profit_after_fee,
                dry_run=DRY_RUN
            )
            
        except Exception as e:
            return V3ExecutionResult(
                success=False,
                error=str(e)
            )
    
    def _display_scan_status(self, result: V3ScanResult):
        """
        显示扫描状态
        """
        status_char = "🟢" if result.pools_with_liquidity > 0 else "🔴"
        opp_char = "🎯" if result.opportunities else "⏳"
        
        latency_info = ""
        if LATENCY_PROFILING:
            latency_info = f" | Net: {result.time_network_ms:.0f}ms | Calc: {result.time_calc_ms:.0f}ms"
        
        print(f"\r{status_char} 扫描 #{self.scan_count} | "
              f"池: {result.pools_with_liquidity}/{result.pools_scanned} | "
              f"机会: {len(result.opportunities)} {opp_char}"
              f"{latency_info}", end="", flush=True)
    
    def _display_final_stats(self):
        """
        显示最终统计
        """
        runtime = time.time() - self.start_time if self.start_time else 0
        hours = int(runtime // 3600)
        minutes = int((runtime % 3600) // 60)
        seconds = int(runtime % 60)
        
        print("\n\n" + "="*60)
        print("📊 运行统计")
        print("="*60)
        print(f"  运行时间:     {hours}h {minutes}m {seconds}s")
        print(f"  扫描次数:     {self.scan_count}")
        print(f"  发现机会:     {self.opportunity_count}")
        print(f"  执行交易:     {self.execution_count}")
        print(f"  总利润:       {self.total_profit / 10**18:.6f} ETH")
        
        if self.executor:
            stats = self.executor.get_stats()
            print(f"\n  执行器统计:")
            print(f"    发送交易:   {stats['tx_count']}")
            print(f"    成功率:     {stats['success_rate']*100:.1f}%")
        
        print("="*60)
        print("👋 再见!")


# ============================================
# 入口点
# ============================================

def main():
    """主入口"""
    bot = FlashArbV3Bot()
    
    if not bot.initialize():
        print("\n❌ 初始化失败")
        sys.exit(1)
    
    bot.run()


if __name__ == "__main__":
    main()

