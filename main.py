#!/usr/bin/env python3
"""
=========================================================
     🚀 FlashArb V3 - Native Uniswap V3 Arbitrage Bot
=========================================================

Pure V3 implementation - no V2/Solidly legacy code.

Features:
- V3 flash loans (0.05% fee vs V2's 0.3%)
- Multi-fee tier scanning (0.05%, 0.3%, 1%)
- sqrtPriceX96 price calculation
- Sniper Mode gas strategy

Base Mainnet Constants:
- V3 Factory: 0x33128a8fC17869897dcE68Ed026d694621f6FDfD
- SwapRouter02: 0x2626664c2603336E57B271c5C0b26F421741e481
- WETH: 0x4200000000000000000000000000000000000006

Usage:
    python main.py
"""

import os
import sys
import time
import signal
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from web3 import Web3

# Project root
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load environment
load_dotenv(PROJECT_ROOT / ".env")

# ============================================
# Configuration
# ============================================

# Network
RPC_URL = os.getenv("RPC_URL", "http://127.0.0.1:8545")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
CONTRACT_ADDRESS = os.getenv("FLASHBOT_ADDRESS", "")

# V3 Constants
WETH = "0x4200000000000000000000000000000000000006"
V3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
SWAP_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"

# Arbitrage settings
MIN_PROFIT_ETH = float(os.getenv("MIN_PROFIT_ETH", "0.001"))
MIN_PROFIT_WEI = int(MIN_PROFIT_ETH * 10**18)
BORROW_AMOUNT_ETH = float(os.getenv("BORROW_AMOUNT_ETH", "1.0"))
BORROW_AMOUNT = int(BORROW_AMOUNT_ETH * 10**18)

# Scanning
SCAN_INTERVAL = float(os.getenv("SCAN_INTERVAL", "1.0"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
LATENCY_PROFILING = os.getenv("LATENCY_PROFILING", "true").lower() == "true"

# ============================================
# Target Tokens - Base Mainnet
# ============================================

TARGET_TOKENS = [
    {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
    {"symbol": "USDbC", "address": "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA", "decimals": 6},
    {"symbol": "DAI", "address": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb", "decimals": 18},
    {"symbol": "cbETH", "address": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22", "decimals": 18},
    {"symbol": "wstETH", "address": "0xc1CBa3fCea344f92D9239c08C0568f6F2F0ee452", "decimals": 18},
]

# ============================================
# FlashBotV3 ABI
# ============================================

FLASHBOT_ABI = [
    {
        "inputs": [
            {"name": "pool", "type": "address"},
            {"name": "tokenBorrow", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "swapData", "type": "bytes"}
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
        "inputs": [{"name": "token", "type": "address"}],
        "name": "approveToken",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "tokens", "type": "address[]"}],
        "name": "batchApproveTokens",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]


# ============================================
# Import Core Modules
# ============================================

from core.scanner import V3Scanner, ScanResult, ArbitrageOpportunity, FEE_NAMES
from core.executor import V3Executor, ExecutionResult


# ============================================
# Main Bot Class
# ============================================

class FlashArbBot:
    """
    Native Uniswap V3 Arbitrage Bot
    """
    
    def __init__(self):
        self.w3 = None
        self.contract = None
        self.scanner = None
        self.executor = None
        
        # State
        self.running = False
        self.scan_count = 0
        self.opportunity_count = 0
        self.execution_count = 0
        self.total_profit = 0
        self.start_time = None
        
        # Signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signal."""
        print("\n\n🛑 Shutting down...")
        self.running = False
    
    def initialize(self) -> bool:
        """
        Initialize the bot.
        
        Returns:
            True if successful
        """
        print("\n" + "=" * 60)
        print("     🚀 FlashArb V3 - Native Uniswap V3 Arbitrage Bot")
        print("=" * 60)
        
        # Connect to network
        print(f"\n🌐 Connecting to: {RPC_URL[:50]}...")
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
        
        if not self.w3.is_connected():
            print("❌ Failed to connect")
            return False
        
        chain_id = self.w3.eth.chain_id
        print(f"✅ Connected, Chain ID: {chain_id}")
        
        if chain_id != 8453:
            print(f"⚠️ Warning: Not Base Mainnet (8453)")
        
        # Load contract
        if CONTRACT_ADDRESS:
            print(f"\n📜 Loading contract: {CONTRACT_ADDRESS[:20]}...")
            try:
                self.contract = self.w3.eth.contract(
                    address=self.w3.to_checksum_address(CONTRACT_ADDRESS),
                    abi=FLASHBOT_ABI
                )
                owner = self.contract.functions.owner().call()
                print(f"✅ Contract loaded, owner: {owner[:16]}...")
            except Exception as e:
                print(f"⚠️ Contract load failed: {e}")
                self.contract = None
        else:
            print("⚠️ No FLASHBOT_ADDRESS set - simulation mode")
        
        # Initialize executor
        if PRIVATE_KEY and self.contract:
            print("\n🔐 Initializing executor...")
            try:
                self.executor = V3Executor(
                    self.w3,
                    self.contract,
                    PRIVATE_KEY
                )
                balance = self.executor.get_balance()
                print(f"✅ Executor ready: {self.executor.address[:16]}...")
                print(f"   Balance: {balance / 10**18:.4f} ETH")
            except Exception as e:
                print(f"⚠️ Executor failed: {e}")
                self.executor = None
        
        # Initialize scanner
        print("\n🔍 Initializing V3 scanner...")
        self.scanner = V3Scanner(self.w3, target_tokens=TARGET_TOKENS)
        
        # Discover pools
        print(f"\n📊 Discovering V3 pools...")
        pools = self.scanner.discover_pools(WETH)
        
        # Configuration summary
        print("\n" + "=" * 60)
        print("Configuration")
        print("=" * 60)
        print(f"  Min Profit:      {MIN_PROFIT_ETH} ETH")
        print(f"  Borrow Amount:   {BORROW_AMOUNT_ETH} ETH")
        print(f"  Scan Interval:   {SCAN_INTERVAL}s")
        print(f"  Dry Run:         {'Yes' if DRY_RUN else 'No'}")
        print(f"  Pools Found:     {len(pools)}")
        print("=" * 60)
        
        return True
    
    def run(self):
        """Run the main loop."""
        if not self.scanner:
            print("❌ Scanner not initialized")
            return
        
        self.running = True
        self.start_time = time.time()
        
        print(f"\n🏃 Starting scan loop... (Ctrl+C to stop)\n")
        
        while self.running:
            try:
                cycle_start = time.time()
                
                # Scan
                result = self.scanner.scan()
                self.scan_count += 1
                
                # Handle opportunities
                if result.opportunities:
                    self._handle_opportunities(result.opportunities)
                
                # Display status
                self._display_status(result)
                
                # Wait for next cycle
                elapsed = time.time() - cycle_start
                sleep_time = max(0, SCAN_INTERVAL - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)
                
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"\n[ERROR] {e}")
                time.sleep(SCAN_INTERVAL)
        
        self._display_final_stats()
    
    def _handle_opportunities(self, opportunities: list):
        """Handle discovered opportunities."""
        for opp in opportunities:
            self.opportunity_count += 1
            
            print(f"\n{'=' * 60}")
            print(f"🎯 Opportunity #{self.opportunity_count}")
            print(f"{'=' * 60}")
            print(f"  Direction:     {opp.direction}")
            print(f"  Pool Low:      {opp.pool_low.address[:20]}... ({FEE_NAMES[opp.pool_low.fee]})")
            print(f"  Pool High:     {opp.pool_high.address[:20]}... ({FEE_NAMES[opp.pool_high.fee]})")
            print(f"  Price Diff:    {opp.price_diff_pct:.4f}%")
            print(f"  Gross Profit:  {opp.expected_profit / 10**18:.6f} ETH")
            print(f"  Flash Fee:     {opp.flash_fee / 10**18:.6f} ETH")
            print(f"  Net Profit:    {opp.net_profit / 10**18:.6f} ETH")
            
            # Check minimum profit
            if opp.net_profit < MIN_PROFIT_WEI:
                print(f"  ❌ Below minimum ({MIN_PROFIT_ETH} ETH)")
                continue
            
            # Execute
            if self.executor and not DRY_RUN:
                print(f"\n  🚀 Executing...")
                result = self._execute(opp)
                
                if result.success:
                    print(f"  ✅ Success! TX: {result.tx_hash}")
                    print(f"     Gas Used: {result.gas_used}")
                    self.execution_count += 1
                    self.total_profit += opp.net_profit
                else:
                    print(f"  ❌ Failed: {result.error}")
                
                if LATENCY_PROFILING:
                    print(f"  ⏱️ LATENCY: Sim: {result.time_sim_ms:.0f}ms | "
                          f"Sign: {result.time_sign_ms:.0f}ms | "
                          f"Broadcast: {result.time_broadcast_ms:.0f}ms | "
                          f"Confirm: {result.time_confirm_ms:.0f}ms")
            else:
                print(f"  📝 [DRY RUN] Not executing")
    
    def _execute(self, opp) -> ExecutionResult:
        """Execute arbitrage opportunity."""
        try:
            # Use lower fee pool for flash loan
            flash_pool = opp.pool_low if opp.pool_low.fee <= opp.pool_high.fee else opp.pool_high
            trade_pool = opp.pool_high if flash_pool == opp.pool_low else opp.pool_low
            
            # Determine target token (the non-WETH token)
            target_token = (
                flash_pool.token0 
                if flash_pool.token1.lower() == WETH.lower() 
                else flash_pool.token1
            )
            
            return self.executor.execute(
                pool_address=flash_pool.address,
                token_borrow=WETH,
                amount=opp.borrow_amount,
                target_token=target_token,
                target_fee=trade_pool.fee,
                expected_profit=opp.net_profit,
                dry_run=DRY_RUN
            )
            
        except Exception as e:
            return ExecutionResult(success=False, error=str(e))
    
    def _display_status(self, result: ScanResult):
        """Display scan status."""
        status = "🟢" if result.pools_active > 0 else "🔴"
        opp = "🎯" if result.opportunities else "⏳"
        
        latency = ""
        if LATENCY_PROFILING:
            latency = f" | Net: {result.time_network_ms:.0f}ms | Calc: {result.time_calc_ms:.0f}ms"
        
        print(f"\r{status} Scan #{self.scan_count} | "
              f"Pools: {result.pools_active}/{result.pools_scanned} | "
              f"Opportunities: {len(result.opportunities)} {opp}"
              f"{latency}", end="", flush=True)
    
    def _display_final_stats(self):
        """Display final statistics."""
        runtime = time.time() - self.start_time if self.start_time else 0
        hours = int(runtime // 3600)
        minutes = int((runtime % 3600) // 60)
        seconds = int(runtime % 60)
        
        print("\n\n" + "=" * 60)
        print("📊 Final Statistics")
        print("=" * 60)
        print(f"  Runtime:        {hours}h {minutes}m {seconds}s")
        print(f"  Scans:          {self.scan_count}")
        print(f"  Opportunities:  {self.opportunity_count}")
        print(f"  Executions:     {self.execution_count}")
        print(f"  Total Profit:   {self.total_profit / 10**18:.6f} ETH")
        
        if self.executor:
            stats = self.executor.get_stats()
            print(f"\n  Executor Stats:")
            print(f"    Transactions: {stats['tx_count']}")
            print(f"    Success Rate: {stats['success_rate']*100:.1f}%")
        
        print("=" * 60)
        print("👋 Goodbye!")


# ============================================
# Entry Point
# ============================================

def main():
    """Main entry point."""
    bot = FlashArbBot()
    
    if not bot.initialize():
        print("\n❌ Initialization failed")
        sys.exit(1)
    
    bot.run()


if __name__ == "__main__":
    main()

