#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════════╗
║                   FlashArb V3 - Strategic Config Generator                       ║
║                                                                                  ║
║  🎯 Purpose:  Generate TARGET_TOKENS config for FlashArb V3 MEV Bot             ║
║  🔗 Network:  Base Chain (OP Stack) - Uniswap V3 & Aerodrome Focus              ║
║  📊 Source:   DexScreener API                                                   ║
║  📦 Output:   Python config file ready for import                               ║
╚══════════════════════════════════════════════════════════════════════════════════╝

Usage:
    python scripts/market_screener.py
    python scripts/market_screener.py --min-liquidity 100000 --min-spread 1.0
    python scripts/market_screener.py --top 15 --output config/target_tokens.py
    python scripts/market_screener.py --include-caution  # Include risky tokens
"""

import asyncio
import argparse
import sys
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum

# Async HTTP
try:
    import aiohttp
except ImportError:
    print("❌ Missing: aiohttp")
    print("   Run: pip install aiohttp")
    sys.exit(1)

# Try rich for beautiful output, fallback to tabulate
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
    from rich.live import Live
    from rich import box
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
    try:
        from tabulate import tabulate
    except ImportError:
        print("❌ Missing: rich or tabulate")
        print("   Run: pip install rich  (recommended)")
        print("   Or:  pip install tabulate")
        sys.exit(1)


# ============================================
# Constants & Configuration
# ============================================

# API
DEXSCREENER_API = "https://api.dexscreener.com"
BASE_CHAIN_ID = "base"

# Rate Limiting
API_RATE_LIMIT_DELAY = 0.25  # seconds between requests
API_MAX_RETRIES = 3
API_RETRY_BACKOFF = 2.0  # multiplier

# Filtering Defaults
DEFAULT_MIN_LIQUIDITY = 50_000.0   # $50k - V3 needs depth
DEFAULT_MIN_VOLUME = 10_000.0      # $10k 24h volume
DEFAULT_MIN_SPREAD = 0.5           # 0.5% minimum spread
DEFAULT_TOP_N = 10

# Honeypot Detection Thresholds
MAX_FDV_TO_LIQUIDITY_RATIO = 100   # FDV > 100x liquidity is suspicious
MIN_TRANSACTIONS_24H = 10          # Less than 10 txns is low activity
MAX_SPREAD_THRESHOLD = 50.0        # Spreads > 50% are usually fake

# DEX Priority (for V3 arbitrage)
PRIORITY_DEXS = {"uniswap": 3, "aerodrome": 2, "baseswap": 1, "sushiswap": 1}

# Display Names
DEX_DISPLAY = {
    "uniswap": "UniV3",
    "aerodrome": "Aero",
    "sushiswap": "Sushi",
    "baseswap": "BaseSwap",
    "pancakeswap": "PCS",
    "balancer": "Balancer",
    "curve": "Curve",
}

# Hot Tokens on Base (seed list)
HOT_TOKENS = [
    # Meme coins with high activity
    ("0x532f27101965dd16442E59d40670FaF5eBB142E4", "BRETT"),
    ("0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed", "DEGEN"),
    ("0xAC1Bd2486aAf3B5C0fc3Fd868558b082a531B2B4", "TOSHI"),
    ("0x0578d8A44db98B23BF096A382e016e29a5Ce0ffe", "HIGHER"),
    ("0x768BE13e1680b5ebE0024c42c896E3dB59Ec0149", "MOG"),
    ("0x9a26F5433671751C3276a065f57e5a02D2817973", "KEYCAT"),
    
    # DeFi & Ecosystem
    ("0x940181a94A35A4569E4529A3CDfB74e38FD98631", "AERO"),
    ("0x22e6966B799c4D5B13BE962E1D117b56327FDa66", "VIRTUAL"),
    ("0x1C7a460413dD4e964f96D8dFC56E7223cE88CD85", "SEAM"),
    
    # Majors
    ("0x4200000000000000000000000000000000000006", "WETH"),
    ("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "USDC"),
    ("0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA", "USDbC"),
    ("0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb", "DAI"),
    ("0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22", "cbETH"),
    ("0xB6fe221Fe9EeF5aBa221c348bA20A1Bf5e73624c", "rETH"),
    
    # Additional
    ("0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b", "PEPE"),
    ("0xfA980cEd6895AC314E7dE34Ef1bFAE90a5AdD21b", "PRIME"),
    ("0xCfA3Ef56d303AE4fAabA0592388F19d7C3399FB4", "eUSD"),
]


# ============================================
# Data Structures
# ============================================

class RiskLevel(Enum):
    """Token risk classification"""
    SAFE = "safe"
    CAUTION = "caution"
    HONEYPOT = "honeypot"


@dataclass
class PairData:
    """Trading pair information"""
    dex_id: str
    dex_name: str
    pair_address: str
    base_token: str
    quote_token: str
    price_usd: float
    liquidity_usd: float
    volume_24h: float
    txns_24h_buys: int
    txns_24h_sells: int
    fdv: float
    price_change_24h: float
    
    @property
    def is_priority_dex(self) -> bool:
        return self.dex_id in PRIORITY_DEXS
    
    @property
    def priority_score(self) -> int:
        return PRIORITY_DEXS.get(self.dex_id, 0)
    
    @property
    def total_txns_24h(self) -> int:
        return self.txns_24h_buys + self.txns_24h_sells


@dataclass
class TokenAnalysis:
    """Complete token analysis"""
    symbol: str
    name: str
    address: str
    pairs: List[PairData] = field(default_factory=list)
    decimals: int = 18  # Default, needs verification
    
    # Computed properties
    @property
    def total_liquidity(self) -> float:
        return sum(p.liquidity_usd for p in self.pairs)
    
    @property
    def priority_liquidity(self) -> float:
        return sum(p.liquidity_usd for p in self.pairs if p.is_priority_dex)
    
    @property
    def total_volume_24h(self) -> float:
        return sum(p.volume_24h for p in self.pairs)
    
    @property
    def total_txns_24h(self) -> int:
        return sum(p.total_txns_24h for p in self.pairs)
    
    @property
    def avg_fdv(self) -> float:
        fdvs = [p.fdv for p in self.pairs if p.fdv > 0]
        return sum(fdvs) / len(fdvs) if fdvs else 0
    
    @property
    def fdv_to_liquidity_ratio(self) -> float:
        if self.total_liquidity > 0:
            return self.avg_fdv / self.total_liquidity
        return float('inf')
    
    @property
    def dex_count(self) -> int:
        return len(set(p.dex_id for p in self.pairs))
    
    @property
    def priority_dex_count(self) -> int:
        return len(set(p.dex_id for p in self.pairs if p.is_priority_dex))
    
    @property
    def dex_list(self) -> List[str]:
        return sorted(set(p.dex_name for p in self.pairs))
    
    @property
    def prices(self) -> List[float]:
        return [p.price_usd for p in self.pairs if p.price_usd > 0]
    
    @property
    def max_price(self) -> float:
        return max(self.prices) if self.prices else 0
    
    @property
    def min_price(self) -> float:
        return min(self.prices) if self.prices else 0
    
    @property
    def spread_pct(self) -> float:
        """Spread = (Max - Min) / Min * 100"""
        if self.min_price > 0 and len(self.prices) >= 2:
            return ((self.max_price - self.min_price) / self.min_price) * 100
        return 0.0
    
    @property
    def avg_price_change_24h(self) -> float:
        changes = [p.price_change_24h for p in self.pairs if p.price_change_24h]
        return sum(changes) / len(changes) if changes else 0
    
    def get_risk_level(self, strict: bool = True) -> RiskLevel:
        """
        Assess honeypot/manipulation risk.
        
        Honeypot Heuristics:
        1. FDV > 100x Liquidity (inflated metrics)
        2. < 10 transactions in 24h (low activity, illiquid)
        3. Spread > 50% (usually fake or manipulated)
        """
        # Obvious honeypot signals
        if self.spread_pct > MAX_SPREAD_THRESHOLD:
            return RiskLevel.HONEYPOT
        
        if strict:
            # FDV manipulation check
            if self.fdv_to_liquidity_ratio > MAX_FDV_TO_LIQUIDITY_RATIO:
                return RiskLevel.HONEYPOT
            
            # Low activity check
            if self.total_txns_24h < MIN_TRANSACTIONS_24H:
                return RiskLevel.CAUTION
        
        # Reasonable token
        if self.priority_dex_count >= 1 and self.total_liquidity >= 10000:
            return RiskLevel.SAFE
        
        return RiskLevel.CAUTION
    
    def get_best_arb_path(self) -> Tuple[Optional[PairData], Optional[PairData]]:
        """Get buy low / sell high pair"""
        if len(self.pairs) < 2:
            return None, None
        sorted_pairs = sorted(self.pairs, key=lambda p: p.price_usd)
        return sorted_pairs[0], sorted_pairs[-1]
    
    def calculate_min_profit(self) -> float:
        """
        Calculate suggested min_profit based on spread.
        
        Logic:
        - Spread < 1%: 0.0003 ETH (conservative)
        - Spread 1-2%: 0.0005 ETH (normal)
        - Spread > 2%: 0.001 ETH (aggressive)
        """
        if self.spread_pct < 1.0:
            return 0.0003
        elif self.spread_pct < 2.0:
            return 0.0005
        else:
            return 0.001


# ============================================
# DexScreener API Client
# ============================================

class DexScreenerClient:
    """Async DexScreener API client with rate limiting & retry"""
    
    def __init__(self):
        self.base_url = DEXSCREENER_API
        self.session: Optional[aiohttp.ClientSession] = None
        self.request_count = 0
    
    async def __aenter__(self):
        timeout = aiohttp.ClientTimeout(total=30)
        self.session = aiohttp.ClientSession(timeout=timeout)
        return self
    
    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
    
    async def _request(self, endpoint: str, retries: int = 0) -> Optional[dict]:
        """Make request with retry & backoff"""
        url = f"{self.base_url}{endpoint}"
        
        try:
            async with self.session.get(url) as resp:
                self.request_count += 1
                
                if resp.status == 200:
                    return await resp.json()
                
                elif resp.status == 429:
                    # Rate limited - exponential backoff
                    if retries < API_MAX_RETRIES:
                        wait = API_RETRY_BACKOFF ** (retries + 1)
                        await asyncio.sleep(wait)
                        return await self._request(endpoint, retries + 1)
                    
        except asyncio.TimeoutError:
            if retries < API_MAX_RETRIES:
                return await self._request(endpoint, retries + 1)
        except Exception:
            pass
        
        return None
    
    async def get_token_pairs(self, token_address: str) -> List[dict]:
        """Get all pairs for a token on Base"""
        data = await self._request(f"/latest/dex/tokens/{token_address}")
        if data and "pairs" in data:
            return [p for p in data["pairs"] if p.get("chainId", "").lower() == "base"]
        return []


# ============================================
# Data Processing
# ============================================

def parse_pair(raw: dict) -> Optional[PairData]:
    """Parse raw API pair data"""
    try:
        dex_id = raw.get("dexId", "").lower()
        
        # Extract transaction counts
        txns = raw.get("txns", {}).get("h24", {})
        buys = int(txns.get("buys", 0) or 0)
        sells = int(txns.get("sells", 0) or 0)
        
        return PairData(
            dex_id=dex_id,
            dex_name=DEX_DISPLAY.get(dex_id, dex_id.title()),
            pair_address=raw.get("pairAddress", ""),
            base_token=raw.get("baseToken", {}).get("address", ""),
            quote_token=raw.get("quoteToken", {}).get("address", ""),
            price_usd=float(raw.get("priceUsd") or 0),
            liquidity_usd=float(raw.get("liquidity", {}).get("usd") or 0),
            volume_24h=float(raw.get("volume", {}).get("h24") or 0),
            txns_24h_buys=buys,
            txns_24h_sells=sells,
            fdv=float(raw.get("fdv") or 0),
            price_change_24h=float(raw.get("priceChange", {}).get("h24") or 0),
        )
    except Exception:
        return None


def aggregate_tokens(pairs: List[PairData], symbol_map: Dict[str, str]) -> Dict[str, TokenAnalysis]:
    """Group pairs by token"""
    tokens: Dict[str, TokenAnalysis] = {}
    
    for pair in pairs:
        addr = pair.base_token.lower()
        
        if addr not in tokens:
            tokens[addr] = TokenAnalysis(
                symbol=symbol_map.get(addr, "???"),
                name="",
                address=pair.base_token,
            )
        
        tokens[addr].pairs.append(pair)
    
    return tokens


def filter_tokens(
    tokens: Dict[str, TokenAnalysis],
    min_liquidity: float,
    min_volume: float,
    min_spread: float,
    include_caution: bool,
) -> List[TokenAnalysis]:
    """Filter tokens by criteria"""
    filtered = []
    
    for token in tokens.values():
        # Skip unknown symbols
        if not token.symbol or token.symbol == "???":
            continue
        
        # Liquidity
        if token.total_liquidity < min_liquidity:
            continue
        
        # Volume
        if token.total_volume_24h < min_volume:
            continue
        
        # Multi-DEX (need at least 2 for arb)
        if token.dex_count < 2:
            continue
        
        # Spread
        if token.spread_pct < min_spread:
            continue
        
        # Risk level
        risk = token.get_risk_level(strict=not include_caution)
        if risk == RiskLevel.HONEYPOT:
            continue
        if risk == RiskLevel.CAUTION and not include_caution:
            continue
        
        filtered.append(token)
    
    return filtered


# ============================================
# Output: Rich Console UI
# ============================================

def format_usd(num: float) -> str:
    """Format USD amount"""
    if num >= 1_000_000:
        return f"${num/1_000_000:.2f}M"
    elif num >= 1_000:
        return f"${num/1_000:.1f}K"
    else:
        return f"${num:.0f}"


def format_spread(pct: float) -> str:
    """Format spread with emoji"""
    if pct >= 2.0:
        return f"🔥 {pct:.2f}%"
    elif pct >= 1.0:
        return f"⚡ {pct:.2f}%"
    elif pct >= 0.5:
        return f"✨ {pct:.2f}%"
    else:
        return f"   {pct:.2f}%"


def display_rich(tokens: List[TokenAnalysis], args) -> None:
    """Display results using Rich library"""
    console = Console()
    
    # Header
    console.print()
    console.print(Panel.fit(
        "[bold cyan]🔍 FlashArb V3 - Strategic Config Generator[/]\n"
        "[dim]Base Chain (OP Stack) | Uniswap V3 & Aerodrome Focus[/]",
        border_style="cyan"
    ))
    
    # Config
    console.print()
    config_text = (
        f"[bold]Configuration:[/]\n"
        f"  Min Liquidity: [green]${args.min_liquidity:,.0f}[/]\n"
        f"  Min Volume:    [green]${args.min_volume:,.0f}[/]\n"
        f"  Min Spread:    [green]{args.min_spread}%[/]\n"
        f"  Top Results:   [green]{args.top}[/]\n"
        f"  Include Caution: [{'green' if args.include_caution else 'red'}]{args.include_caution}[/]"
    )
    console.print(Panel(config_text, title="⚙️ Settings", border_style="blue"))
    
    # Summary
    if tokens:
        safe_count = sum(1 for t in tokens if t.get_risk_level() == RiskLevel.SAFE)
        avg_spread = sum(t.spread_pct for t in tokens) / len(tokens)
        total_liq = sum(t.total_liquidity for t in tokens)
        
        console.print()
        summary = (
            f"[bold]Analysis Summary:[/]\n"
            f"  Tokens Found:   [cyan]{len(tokens)}[/]\n"
            f"  Safe Tokens:    [green]{safe_count}[/]\n"
            f"  Avg Spread:     [yellow]{avg_spread:.2f}%[/]\n"
            f"  Total Liq:      [cyan]{format_usd(total_liq)}[/]"
        )
        console.print(Panel(summary, title="📊 Summary", border_style="green"))
    
    # Main Table
    console.print()
    table = Table(
        title="🎯 Arbitrage Opportunities",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
    )
    
    table.add_column("#", style="dim", width=3)
    table.add_column("Symbol", style="bold")
    table.add_column("Address", style="dim")
    table.add_column("Spread %", justify="right")
    table.add_column("Liquidity", justify="right")
    table.add_column("Vol 24h", justify="right")
    table.add_column("Txns", justify="right")
    table.add_column("Risk", justify="center")
    table.add_column("DEXs")
    table.add_column("Arb Path", style="dim")
    
    # Sort by spread descending
    sorted_tokens = sorted(tokens, key=lambda t: t.spread_pct, reverse=True)[:30]
    
    for i, token in enumerate(sorted_tokens, 1):
        risk = token.get_risk_level()
        risk_style = "green" if risk == RiskLevel.SAFE else "yellow" if risk == RiskLevel.CAUTION else "red"
        risk_text = "✅" if risk == RiskLevel.SAFE else "⚠️" if risk == RiskLevel.CAUTION else "🚫"
        
        # Spread styling
        spread_val = token.spread_pct
        if spread_val >= 1.0:
            spread_text = Text(f"{spread_val:.2f}%", style="bold green")
        elif spread_val >= 0.5:
            spread_text = Text(f"{spread_val:.2f}%", style="yellow")
        else:
            spread_text = Text(f"{spread_val:.2f}%")
        
        # Arb path
        buy, sell = token.get_best_arb_path()
        arb_path = f"{buy.dex_name}→{sell.dex_name}" if buy and sell else "-"
        
        table.add_row(
            str(i),
            token.symbol[:10],
            token.address[:12] + "...",
            spread_text,
            format_usd(token.total_liquidity),
            format_usd(token.total_volume_24h),
            str(token.total_txns_24h),
            Text(risk_text, style=risk_style),
            ", ".join(token.dex_list[:3]),
            arb_path,
        )
    
    console.print(table)
    console.print()


def display_tabulate(tokens: List[TokenAnalysis], args) -> None:
    """Fallback display using tabulate"""
    print()
    print("=" * 80)
    print("🔍 FlashArb V3 - Strategic Config Generator")
    print("   Base Chain | Uniswap V3 & Aerodrome Focus")
    print("=" * 80)
    print()
    print(f"Configuration:")
    print(f"  Min Liquidity: ${args.min_liquidity:,.0f}")
    print(f"  Min Volume:    ${args.min_volume:,.0f}")
    print(f"  Min Spread:    {args.min_spread}%")
    print(f"  Top Results:   {args.top}")
    print()
    
    table_data = []
    sorted_tokens = sorted(tokens, key=lambda t: t.spread_pct, reverse=True)[:30]
    
    for i, token in enumerate(sorted_tokens, 1):
        risk = token.get_risk_level()
        risk_text = "✅" if risk == RiskLevel.SAFE else "⚠️" if risk == RiskLevel.CAUTION else "🚫"
        
        buy, sell = token.get_best_arb_path()
        arb_path = f"{buy.dex_name}→{sell.dex_name}" if buy and sell else "-"
        
        table_data.append([
            i,
            token.symbol[:10],
            token.address[:12] + "...",
            format_spread(token.spread_pct),
            format_usd(token.total_liquidity),
            format_usd(token.total_volume_24h),
            token.total_txns_24h,
            risk_text,
            arb_path,
        ])
    
    headers = ["#", "Symbol", "Address", "Spread", "Liquidity", "Vol 24h", "Txns", "Risk", "Path"]
    print(tabulate(table_data, headers=headers, tablefmt="rounded_grid"))
    print()


# ============================================
# Output: Config File Generation
# ============================================

def generate_config_file(tokens: List[TokenAnalysis], output_path: str, top_n: int) -> str:
    """Generate TARGET_TOKENS Python config file"""
    
    # Sort by liquidity (safest first)
    sorted_tokens = sorted(tokens, key=lambda t: t.total_liquidity, reverse=True)[:top_n]
    
    lines = []
    lines.append('"""')
    lines.append('FlashArb V3 - Target Tokens Configuration')
    lines.append(f'Generated by market_screener.py at {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append('')
    lines.append('⚠️ CAUTION: Verify "decimals" on-chain before running with real funds!')
    lines.append('   Use: contract.functions.decimals().call()')
    lines.append('"""')
    lines.append('')
    lines.append('TARGET_TOKENS = [')
    
    for token in sorted_tokens:
        # Calculate min_profit based on spread
        min_profit = token.calculate_min_profit()
        
        # Comment with stats
        liq_str = format_usd(token.total_liquidity)
        lines.append(f'    # {token.symbol} | Spread: {token.spread_pct:.2f}% | Liq: {liq_str} | DEXs: {", ".join(token.dex_list[:3])}')
        
        # Config dict
        lines.append('    {')
        lines.append(f'        "symbol": "{token.symbol}",')
        lines.append(f'        "address": "{token.address}",')
        lines.append(f'        "decimals": {token.decimals},  # TODO: Verify Decimals')
        lines.append(f'        "fee_tiers": [500, 3000, 10000],')
        lines.append(f'        "min_profit": {min_profit},')
        lines.append('    },')
        lines.append('')
    
    lines.append(']')
    lines.append('')
    
    # Add env format as comment
    lines.append('')
    lines.append('# Alternative: .env format (single line)')
    lines.append('# ' + '-' * 60)
    env_parts = [f"{t.symbol}:{t.address}:{t.decimals}" for t in sorted_tokens]
    lines.append(f'# TARGET_TOKENS={";".join(env_parts)}')
    lines.append('')
    
    content = '\n'.join(lines)
    
    # Write to file
    output_dir = Path(output_path).parent
    if output_dir and not output_dir.exists():
        output_dir.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    return content


def print_config_preview(tokens: List[TokenAnalysis], top_n: int) -> None:
    """Print config preview to console"""
    console = Console() if HAS_RICH else None
    
    sorted_tokens = sorted(tokens, key=lambda t: t.total_liquidity, reverse=True)[:top_n]
    
    if HAS_RICH:
        console.print()
        console.print(Panel.fit(
            "[bold yellow]📋 TARGET_TOKENS Configuration Preview[/]",
            border_style="yellow"
        ))
        console.print()
    else:
        print()
        print("=" * 80)
        print("📋 TARGET_TOKENS Configuration Preview")
        print("=" * 80)
        print()
    
    print('TARGET_TOKENS = [')
    
    for token in sorted_tokens:
        min_profit = token.calculate_min_profit()
        liq_str = format_usd(token.total_liquidity)
        
        print(f'    # {token.symbol} | Spread: {token.spread_pct:.2f}% | Liq: {liq_str}')
        print('    {')
        print(f'        "symbol": "{token.symbol}",')
        print(f'        "address": "{token.address}",')
        print(f'        "decimals": {token.decimals},  # TODO: Verify Decimals')
        print(f'        "fee_tiers": [500, 3000, 10000],')
        print(f'        "min_profit": {min_profit},')
        print('    },')
        print()
    
    print(']')
    print()


# ============================================
# Main Entry Point
# ============================================

async def fetch_all_data(client: DexScreenerClient, console: Optional[Any] = None) -> Tuple[List[PairData], Dict[str, str]]:
    """Fetch data for all tokens"""
    all_pairs: List[PairData] = []
    symbol_map: Dict[str, str] = {}
    
    total = len(HOT_TOKENS)
    
    if HAS_RICH and console:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=console,
        ) as progress:
            task = progress.add_task("Fetching market data...", total=total)
            
            for addr, known_symbol in HOT_TOKENS:
                raw_pairs = await client.get_token_pairs(addr)
                
                for raw in raw_pairs:
                    pair = parse_pair(raw)
                    if pair and pair.liquidity_usd > 0:
                        all_pairs.append(pair)
                        
                        # Extract symbol
                        api_symbol = raw.get("baseToken", {}).get("symbol", known_symbol)
                        symbol_map[addr.lower()] = api_symbol
                
                progress.update(task, advance=1)
                await asyncio.sleep(API_RATE_LIMIT_DELAY)
    else:
        print("📡 Fetching market data from DexScreener...")
        for i, (addr, known_symbol) in enumerate(HOT_TOKENS):
            raw_pairs = await client.get_token_pairs(addr)
            
            for raw in raw_pairs:
                pair = parse_pair(raw)
                if pair and pair.liquidity_usd > 0:
                    all_pairs.append(pair)
                    
                    api_symbol = raw.get("baseToken", {}).get("symbol", known_symbol)
                    symbol_map[addr.lower()] = api_symbol
            
            pct = ((i + 1) / total) * 100
            print(f"\r   Progress: {i+1}/{total} ({pct:.0f}%)", end="", flush=True)
            await asyncio.sleep(API_RATE_LIMIT_DELAY)
        
        print()
    
    return all_pairs, symbol_map


async def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="FlashArb V3 - Strategic Config Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/market_screener.py
  python scripts/market_screener.py --min-liquidity 100000 --min-spread 1.0
  python scripts/market_screener.py --top 15 --output config/target_tokens.py
  python scripts/market_screener.py --include-caution
        """
    )
    
    parser.add_argument(
        "--min-liquidity", type=float, default=DEFAULT_MIN_LIQUIDITY,
        help=f"Minimum liquidity in USD (default: ${DEFAULT_MIN_LIQUIDITY:,.0f})"
    )
    parser.add_argument(
        "--min-volume", type=float, default=DEFAULT_MIN_VOLUME,
        help=f"Minimum 24h volume in USD (default: ${DEFAULT_MIN_VOLUME:,.0f})"
    )
    parser.add_argument(
        "--min-spread", type=float, default=DEFAULT_MIN_SPREAD,
        help=f"Minimum spread percentage (default: {DEFAULT_MIN_SPREAD}%)"
    )
    parser.add_argument(
        "--top", type=int, default=DEFAULT_TOP_N,
        help=f"Number of tokens for config (default: {DEFAULT_TOP_N})"
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output file path for config (e.g., config/target_tokens.py)"
    )
    parser.add_argument(
        "--include-caution", action="store_true",
        help="Include tokens marked as CAUTION (risky but possible)"
    )
    
    args = parser.parse_args()
    
    console = Console() if HAS_RICH else None
    
    # Fetch data
    async with DexScreenerClient() as client:
        all_pairs, symbol_map = await fetch_all_data(client, console)
    
    if not all_pairs:
        print("❌ No data fetched. Check your internet connection.")
        return
    
    # Aggregate by token
    tokens = aggregate_tokens(all_pairs, symbol_map)
    
    # Update symbols
    for addr, token in tokens.items():
        if addr in symbol_map:
            token.symbol = symbol_map[addr]
    
    # Filter
    filtered = filter_tokens(
        tokens,
        min_liquidity=args.min_liquidity,
        min_volume=args.min_volume,
        min_spread=args.min_spread,
        include_caution=args.include_caution,
    )
    
    if not filtered:
        print("❌ No tokens match the filter criteria. Try lowering thresholds.")
        return
    
    # Display
    if HAS_RICH:
        display_rich(filtered, args)
    else:
        display_tabulate(filtered, args)
    
    # Config preview
    print_config_preview(filtered, args.top)
    
    # Save to file if requested
    if args.output:
        content = generate_config_file(filtered, args.output, args.top)
        
        if HAS_RICH:
            console.print(f"[bold green]✅ Config saved to:[/] {args.output}")
        else:
            print(f"✅ Config saved to: {args.output}")
    
    # Footer
    if HAS_RICH:
        console.print()
        console.print(Panel(
            "[dim]💡 Tips:[/]\n"
            "  • Spread > 0.3% covers V3 flash fees (0.05%-0.3%)\n"
            "  • Spread > 1.0% is a strong opportunity 🔥\n"
            "  • Always verify decimals on-chain\n"
            "  • Copy TARGET_TOKENS to your .env or config",
            border_style="dim"
        ))
    else:
        print()
        print("-" * 80)
        print("💡 Tips:")
        print("   • Spread > 0.3% covers V3 flash fees (0.05%-0.3%)")
        print("   • Spread > 1.0% is a strong opportunity 🔥")
        print("   • Always verify decimals on-chain")
        print("-" * 80)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️ Cancelled by user")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
