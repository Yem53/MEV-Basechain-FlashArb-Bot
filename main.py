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

# Suppress pkg_resources deprecation warning from web3
import warnings
warnings.filterwarnings("ignore", message="pkg_resources is deprecated")

import os
import sys
import time
import signal
from pathlib import Path
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
from web3 import Web3

# Try to import orjson for faster JSON parsing
try:
    import orjson
    HAS_ORJSON = True
except ImportError:
    HAS_ORJSON = False

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
RPC_TIMEOUT = int(os.getenv("RPC_TIMEOUT", "30"))
CHAIN_ID = int(os.getenv("CHAIN_ID", "8453"))
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
CONTRACT_ADDRESS = os.getenv("FLASHBOT_ADDRESS", "")

# V3 Constants (Base Mainnet defaults)
WETH = os.getenv("WETH", "0x4200000000000000000000000000000000000006")
V3_FACTORY = os.getenv("V3_FACTORY", "0x33128a8fC17869897dcE68Ed026d694621f6FDfD")
SWAP_ROUTER = os.getenv("SWAP_ROUTER", "0x2626664c2603336E57B271c5C0b26F421741e481")
POOL_INIT_CODE_HASH = os.getenv("POOL_INIT_CODE_HASH", "0xe34f199b19b2b4f47f68442619d555527d244f78a3297ea89325f843f87b8b54")
MULTICALL3 = os.getenv("MULTICALL3", "0xcA11bde05977b3631167028862bE2a173976CA11")

# Arbitrage settings
MIN_PROFIT_ETH = float(os.getenv("MIN_PROFIT_ETH", "0.001"))
MIN_PROFIT_WEI = int(MIN_PROFIT_ETH * 10**18)
# Dynamic amount optimization range (no hardcoded borrow amount)
MIN_BORROW_ETH = float(os.getenv("MIN_BORROW_ETH", "0.01"))
MAX_BORROW_ETH = float(os.getenv("MAX_BORROW_ETH", "20.0"))
AMOUNT_PRECISION_ETH = float(os.getenv("AMOUNT_PRECISION_ETH", "0.001"))

# Gas settings
MAX_GAS_GWEI = float(os.getenv("MAX_GAS_GWEI", "1.0"))
GAS_LIMIT = int(os.getenv("GAS_LIMIT", "500000"))
SNIPER_MODE_ENABLED = os.getenv("SNIPER_MODE_ENABLED", "true").lower() == "true"
SNIPER_MODE_MULTIPLIER = float(os.getenv("SNIPER_MODE_MULTIPLIER", "1.2"))

# Fee tiers
FEE_TIERS_STR = os.getenv("FEE_TIERS", "500,3000,10000")
FEE_TIERS_CONFIG = [int(f.strip()) for f in FEE_TIERS_STR.split(",")]
FLASH_FEE_TIER = int(os.getenv("FLASH_FEE_TIER", "500"))

# Scanning
SCAN_INTERVAL = float(os.getenv("SCAN_INTERVAL", "1.0"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

# Profiling & Diagnostics
LATENCY_PROFILING = os.getenv("LATENCY_PROFILING", "true").lower() == "true"
SHADOW_MODE_ENABLED = os.getenv("SHADOW_MODE_ENABLED", "true").lower() == "true"
SHADOW_SPREAD_THRESHOLD = float(os.getenv("SHADOW_SPREAD_THRESHOLD", "0.005"))

# Liquidity filters
MIN_LIQUIDITY = int(os.getenv("MIN_LIQUIDITY", "1000000000000000"))
MIN_LIQUIDITY_ETH = float(os.getenv("MIN_LIQUIDITY_ETH", "0.5"))

# Safety limits
MAX_CONSECUTIVE_FAILURES = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "5"))
FAILURE_PAUSE_DURATION = int(os.getenv("FAILURE_PAUSE_DURATION", "60"))
MAX_TX_PER_HOUR = int(os.getenv("MAX_TX_PER_HOUR", "100"))
MIN_BALANCE_ETH = float(os.getenv("MIN_BALANCE_ETH", "0.01"))

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "logs/flasharb.log")
TRADE_HISTORY_FILE = os.getenv("TRADE_HISTORY_FILE", "logs/trade_history.csv")

# ============================================
# Target Tokens - Load from config file or env
# ============================================

def load_target_tokens() -> list:
    """
    Load target tokens with priority:
    1. config/target_tokens.py (full config with fee_tiers, min_profit)
    2. .env TARGET_TOKENS variable (simple format: SYMBOL:ADDRESS:DECIMALS)
    3. Default hardcoded tokens
    """
    # Priority 1: Try to import from config file
    try:
        from config.target_tokens import TARGET_TOKENS as CONFIG_TOKENS
        if CONFIG_TOKENS:
            print(f"📋 Loaded {len(CONFIG_TOKENS)} tokens from config/target_tokens.py")
            return CONFIG_TOKENS
    except ImportError:
        pass
    except Exception as e:
        print(f"⚠️ Error loading config/target_tokens.py: {e}")
    
    # Priority 2: Parse from .env (simple format)
    tokens_str = os.getenv("TARGET_TOKENS", "")
    if tokens_str:
        tokens = []
        for token_def in tokens_str.split(","):
            parts = token_def.strip().split(":")
            if len(parts) >= 3:
                token = {
                    "symbol": parts[0],
                    "address": parts[1],
                    "decimals": int(parts[2]),
                    "fee_tiers": FEE_TIERS_CONFIG,  # Use global fee tiers
                    "min_profit": MIN_PROFIT_ETH,   # Use global min profit
                }
                # Optional: parse fee_tiers if provided (format: SYMBOL:ADDR:DEC:500-3000-10000)
                if len(parts) >= 4:
                    try:
                        token["fee_tiers"] = [int(f) for f in parts[3].split("-")]
                    except:
                        pass
                # Optional: parse min_profit if provided
                if len(parts) >= 5:
                    try:
                        token["min_profit"] = float(parts[4])
                    except:
                        pass
                tokens.append(token)
        if tokens:
            print(f"📋 Loaded {len(tokens)} tokens from .env TARGET_TOKENS")
            return tokens
    
    # Priority 3: Default tokens for Base Mainnet
    print("📋 Using default token list")
    return [
        {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6, "fee_tiers": [500, 3000], "min_profit": 0.0005},
        {"symbol": "USDbC", "address": "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA", "decimals": 6, "fee_tiers": [500, 3000], "min_profit": 0.0005},
        {"symbol": "DAI", "address": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb", "decimals": 18, "fee_tiers": [500, 3000], "min_profit": 0.0005},
        {"symbol": "cbETH", "address": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22", "decimals": 18, "fee_tiers": [500, 3000], "min_profit": 0.001},
        {"symbol": "wstETH", "address": "0xc1CBa3fCea344f92D9239c08C0568f6F2F0ee452", "decimals": 18, "fee_tiers": [500, 3000], "min_profit": 0.001},
    ]

TARGET_TOKENS = load_target_tokens()

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

from core.scanner import V3Scanner, ScanResult, ArbitrageOpportunity, NearMiss, FEE_NAMES
from core.executor import V3Executor, ExecutionResult


# ============================================
# Main Bot Class
# ============================================

class FlashArbBot:
    """
    Native Uniswap V3 Arbitrage Bot
    
    ⚡ High-Performance Features:
    - HTTP Keep-Alive with connection pooling
    - orjson for fast JSON parsing (if available)
    - EIP-2930 Access Lists for gas optimization
    """
    
    def __init__(self):
        self.w3 = None
        self.contract = None
        self.scanner = None
        self.executor = None
        self._http_session = None  # Persistent HTTP session
        
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
    
    def _create_persistent_session(self) -> requests.Session:
        """
        Create a persistent HTTP session with connection pooling.
        
        ⚡ Optimization: Prevents TCP handshake on every request.
        The connection stays open between scan cycles.
        """
        session = requests.Session()
        
        # Configure retry strategy
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        
        # Create adapter with connection pooling
        adapter = HTTPAdapter(
            pool_connections=10,      # Number of connection pools
            pool_maxsize=20,          # Connections per pool
            max_retries=retry_strategy,
            pool_block=False          # Don't block when pool is full
        )
        
        # Mount adapter for both HTTP and HTTPS
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        # Set default headers for keep-alive
        session.headers.update({
            "Connection": "keep-alive",
            "Keep-Alive": "timeout=60, max=1000",
            "Content-Type": "application/json",
        })
        
        return session
    
    def _cleanup_session(self):
        """Cleanup persistent HTTP session."""
        if self._http_session:
            try:
                self._http_session.close()
            except:
                pass
            self._http_session = None
    
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
        
        # Connect to network with persistent HTTP session (Keep-Alive)
        print(f"\n🌐 Connecting to: {RPC_URL[:50]}...")
        print(f"   ⚡ Using HTTP Keep-Alive for low latency")
        
        # Create persistent session with connection pooling
        self._http_session = self._create_persistent_session()
        
        # Create Web3 provider with the persistent session
        provider = Web3.HTTPProvider(
            RPC_URL,
            request_kwargs={
                "timeout": RPC_TIMEOUT,
            },
            session=self._http_session
        )
        self.w3 = Web3(provider)
        
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
        print("⚙️  Configuration")
        print("=" * 60)
        print(f"  Chain ID:           {CHAIN_ID}")
        print(f"  Min Profit:         {MIN_PROFIT_ETH} ETH")
        print(f"  Amount Range:       {MIN_BORROW_ETH} - {MAX_BORROW_ETH} ETH (Dynamic)")
        print(f"  Precision:          {AMOUNT_PRECISION_ETH} ETH")
        print(f"  Scan Interval:      {SCAN_INTERVAL}s")
        print(f"  Max Gas:            {MAX_GAS_GWEI} gwei")
        print(f"  Gas Limit:          {GAS_LIMIT}")
        print(f"  Fee Tiers:          {FEE_TIERS_CONFIG}")
        print(f"  Flash Fee Tier:     {FLASH_FEE_TIER} ({FLASH_FEE_TIER/10000:.2f}%)")  # 500=0.05%, 3000=0.30%, 10000=1.00%
        print("=" * 60)
        print("🔧 Modes")
        print("=" * 60)
        print(f"  Dry Run:            {'✅ Yes' if DRY_RUN else '❌ No (LIVE)'}")
        print(f"  Debug Mode:         {'✅ On' if DEBUG_MODE else '❌ Off'}")
        print(f"  Sniper Mode:        {'✅ On (×' + str(SNIPER_MODE_MULTIPLIER) + ')' if SNIPER_MODE_ENABLED else '❌ Off'}")
        print(f"  Shadow Mode:        {'✅ On (' + str(SHADOW_SPREAD_THRESHOLD*100) + '%)' if SHADOW_MODE_ENABLED else '❌ Off'}")
        print(f"  Latency Profiling:  {'✅ On' if LATENCY_PROFILING else '❌ Off'}")
        print("=" * 60)
        print("⚡ Performance Optimizations")
        print("=" * 60)
        print(f"  orjson (Fast JSON): {'✅ Enabled' if HAS_ORJSON else '❌ Not installed'}")
        print(f"  HTTP Keep-Alive:    ✅ Enabled (Connection Pooling)")
        print(f"  Access Lists:       ✅ Enabled (EIP-2930)")
        print("=" * 60)
        print("📊 Discovery")
        print("=" * 60)
        print(f"  Target Tokens:      {len(TARGET_TOKENS)}")
        for token in TARGET_TOKENS:
            print(f"    - {token['symbol']} ({token['decimals']} decimals)")
        print(f"  Pools Found:        {len(pools)}")
        print(f"  Min Liquidity:      {MIN_LIQUIDITY_ETH} ETH")
        print("=" * 60)
        print("🛡️ Safety Limits")
        print("=" * 60)
        print(f"  Max Failures:       {MAX_CONSECUTIVE_FAILURES}")
        print(f"  Max TX/Hour:        {MAX_TX_PER_HOUR}")
        print(f"  Min Balance:        {MIN_BALANCE_ETH} ETH")
        print("=" * 60)
        
        return True
    
    def run(self):
        """Run the main loop."""
        if not self.scanner:
            print("❌ Scanner not initialized")
            return
        
        self.running = True
        self.start_time = time.time()
        
        # Near-miss logging configuration
        NEAR_MISS_THRESHOLD_PCT = 0.1  # Log spreads above 0.1%
        MAX_NEAR_MISSES_PER_CYCLE = 3  # Don't spam logs
        
        print(f"\n🏃 Starting scan loop... (Ctrl+C to stop)")
        print(f"   📊 Near-Miss logging enabled (threshold: {NEAR_MISS_THRESHOLD_PCT}%)\n")
        
        while self.running:
            try:
                cycle_start = time.time()
                
                # Scan with dynamic amount optimization and near-miss tracking
                result = self.scanner.scan(
                    min_profit_wei=MIN_PROFIT_WEI,
                    use_optimization=True,
                    near_miss_threshold_pct=NEAR_MISS_THRESHOLD_PCT
                )
                self.scan_count += 1
                
                # Handle opportunities
                if result.opportunities:
                    self._handle_opportunities(result.opportunities)
                
                # Log near-misses (prove the math is working)
                if result.near_misses:
                    self._log_near_misses(result.near_misses[:MAX_NEAR_MISSES_PER_CYCLE])
                
                # Display status with best spread
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
            # Dynamic amount optimization results
            opt_label = "✨ OPTIMIZED" if opp.is_optimized else "FIXED"
            print(f"  Borrow Amount: {opp.borrow_amount / 10**18:.4f} ETH ({opt_label})")
            if opp.is_optimized and opp.price_impact_pct > 0:
                print(f"  Price Impact:  {opp.price_impact_pct:.2f}%")
            if opp.swap1_output > 0:
                print(f"  Swap1 Output:  {opp.swap1_output / 10**18:.6f}")
            if opp.swap2_output > 0:
                print(f"  Swap2 Output:  {opp.swap2_output / 10**18:.6f}")
            print(f"  Flash Fee:     {opp.flash_fee / 10**18:.6f} ETH")
            print(f"  Net Profit:    {opp.net_profit / 10**18:.6f} ETH")
            
            # Check minimum profit
            if opp.net_profit < MIN_PROFIT_WEI:
                # Shadow Mode: Log near-miss opportunities
                if SHADOW_MODE_ENABLED and opp.price_diff_pct >= SHADOW_SPREAD_THRESHOLD * 100:
                    print(f"\n  👻 [SHADOW] Near-miss opportunity detected!")
                    print(f"     Spread:        {opp.price_diff_pct:.4f}% ✓ (threshold: {SHADOW_SPREAD_THRESHOLD*100}%)")
                    print(f"     Gross Profit:  {opp.expected_profit / 10**18:.6f} ETH")
                    print(f"     Flash Fee:     {opp.flash_fee / 10**18:.6f} ETH")
                    print(f"     Net Profit:    {opp.net_profit / 10**18:.6f} ETH ✗ (need: {MIN_PROFIT_ETH} ETH)")
                    print(f"     Reason:        Flash loan fee exceeds spread benefit")
                else:
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
    
    def _log_near_misses(self, near_misses: list):
        """
        Log near-miss opportunities to prove the math is working.
        
        Shows opportunities where spread was detected but profit was insufficient.
        """
        for nm in near_misses:
            gross_eth = nm.gross_profit_wei / 10**18
            gas_eth = nm.gas_cost_wei / 10**18
            net_eth = nm.net_profit_wei / 10**18
            
            print(f"\n⚠️  [NEAR MISS] {nm.symbol}: "
                  f"Spread {nm.spread_pct:.2f}% | "
                  f"Gross: {gross_eth:.6f} ETH | "
                  f"Gas: ~{gas_eth:.6f} ETH | "
                  f"Net: {net_eth:.6f} ETH ({nm.reason})")
    
    def _display_status(self, result: ScanResult):
        """Display scan status with best spread."""
        status = "🟢" if result.pools_active > 0 else "🔴"
        opp = "🎯" if result.opportunities else "⏳"
        
        # Best spread info
        spread_info = ""
        if result.best_spread_pct > 0:
            spread_info = f" | Best: {result.best_spread_pct:.2f}% ({result.best_spread_symbol})"
        
        latency = ""
        if LATENCY_PROFILING:
            latency = f" | Net: {result.time_network_ms:.0f}ms | Calc: {result.time_calc_ms:.0f}ms"
        
        print(f"\r{status} Scan #{self.scan_count} | "
              f"Pools: {result.pools_active}/{result.pools_scanned}{spread_info} | "
              f"Opps: {len(result.opportunities)} {opp}"
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
        
        # Cleanup persistent HTTP session
        self._cleanup_session()
        
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

