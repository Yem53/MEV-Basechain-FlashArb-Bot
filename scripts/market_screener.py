#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                      FlashArb Market Screener v1.0                          ║
║                                                                              ║
║  分析 Base 链上的代币，发现多 DEX 套利机会                                     ║
║  数据源: DexScreener API                                                     ║
╚══════════════════════════════════════════════════════════════════════════════╝

功能：
- 从 DexScreener 获取 Base 链热门代币数据
- 筛选在多个 DEX 上交易的代币
- 计算跨 DEX 价差
- 生成专业的套利机会报告

使用方法：
    python scripts/market_screener.py
    python scripts/market_screener.py --min-liquidity 50000 --min-spread 0.5
"""

import asyncio
import argparse
import sys
from datetime import datetime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

try:
    import aiohttp
    import pandas as pd
    from tabulate import tabulate
except ImportError as e:
    print(f"❌ 缺少依赖: {e}")
    print("请运行: pip install aiohttp pandas tabulate")
    sys.exit(1)


# ============================================
# 配置
# ============================================

# DexScreener API 配置
DEXSCREENER_BASE_URL = "https://api.dexscreener.com"

# Base 链 ID
BASE_CHAIN_ID = "base"

# 已知的热门代币（用于初始查询）
HOT_TOKENS = [
    "0x4ed4E862860beD51a9570b96d8014731D394fF0d",  # DEGEN
    "0x532f27101965dd16442E59d40670FaF5eBB142E4",  # BRETT
    "0xAC1Bd2486aAf3B5C0fc3Fd868558b082a531B2B4",  # TOSHI
    "0x0578d8A44db98B23BF096A382e016e29a5Ce0ffe",  # HIGHER
    "0x940181a94A35A4569E4529A3CDfB74e38FD98631",  # AERO
    "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22",  # cbETH
    "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC
    "0x4200000000000000000000000000000000000006",  # WETH
    "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb",  # DAI
    "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA",  # USDbC
    "0x22e6966B799c4D5B13BE962E1D117b56327FDa66",  # VIRTUAL
    "0x768BE13e1680b5ebE0024c42c896E3dB59Ec0149",  # MOG
    "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b",  # PEPE (Base)
    "0x9a26F5433671751C3276a065f57e5a02D2817973",  # KEYCAT
]

# 默认筛选参数
DEFAULT_MIN_LIQUIDITY = 10000  # $10,000 最小流动性
DEFAULT_MIN_VOLUME_24H = 5000  # $5,000 最小24小时交易量
DEFAULT_MIN_SPREAD = 0.1      # 0.1% 最小价差

# DEX 名称映射（美化显示）
DEX_NAMES = {
    "aerodrome": "Aero",
    "uniswap": "Uni",
    "sushiswap": "Sushi",
    "baseswap": "Base",
    "pancakeswap": "PCS",
    "balancer": "Bal",
    "curve": "Crv",
    "maverick": "Mav",
}


# ============================================
# 数据结构
# ============================================

@dataclass
class PairData:
    """交易对数据"""
    dex_id: str
    dex_name: str
    pair_address: str
    base_token: str
    quote_token: str
    price_usd: float
    price_native: float
    liquidity_usd: float
    volume_24h: float
    volume_6h: float
    volume_1h: float
    price_change_24h: float
    url: str


@dataclass
class TokenAnalysis:
    """代币分析结果"""
    symbol: str
    address: str
    name: str
    pairs: List[PairData] = field(default_factory=list)
    
    @property
    def total_liquidity(self) -> float:
        return sum(p.liquidity_usd for p in self.pairs)
    
    @property
    def total_volume_24h(self) -> float:
        return sum(p.volume_24h for p in self.pairs)
    
    @property
    def dex_count(self) -> int:
        return len(set(p.dex_id for p in self.pairs))
    
    @property
    def dex_list(self) -> List[str]:
        return list(set(p.dex_name for p in self.pairs))
    
    @property
    def prices(self) -> List[float]:
        return [p.price_usd for p in self.pairs if p.price_usd > 0]
    
    @property
    def max_price(self) -> float:
        prices = self.prices
        return max(prices) if prices else 0
    
    @property
    def min_price(self) -> float:
        prices = self.prices
        return min(prices) if prices else 0
    
    @property
    def spread_percent(self) -> float:
        if self.min_price > 0:
            return ((self.max_price - self.min_price) / self.min_price) * 100
        return 0
    
    @property
    def avg_price_change_24h(self) -> float:
        changes = [p.price_change_24h for p in self.pairs if p.price_change_24h]
        return sum(changes) / len(changes) if changes else 0


# ============================================
# API 客户端
# ============================================

class DexScreenerClient:
    """DexScreener API 客户端"""
    
    def __init__(self):
        self.base_url = DEXSCREENER_BASE_URL
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    async def get_token_pairs(self, token_address: str) -> List[Dict]:
        """获取指定代币的所有交易对"""
        url = f"{self.base_url}/latest/dex/tokens/{token_address}"
        
        try:
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs")
                    return pairs if pairs is not None else []
                else:
                    return []
        except Exception as e:
            # 静默处理错误，避免刷屏
            return []
    
    async def get_chain_pairs(self, chain_id: str = "base", limit: int = 100) -> List[Dict]:
        """获取指定链上的热门交易对"""
        # DexScreener 的搜索端点
        url = f"{self.base_url}/latest/dex/pairs/{chain_id}"
        
        try:
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("pairs", [])[:limit]
                else:
                    return []
        except Exception:
            return []
    
    async def search_pairs(self, query: str) -> List[Dict]:
        """搜索交易对"""
        url = f"{self.base_url}/latest/dex/search/?q={query}"
        
        try:
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("pairs", [])
                else:
                    return []
        except Exception:
            return []


# ============================================
# 数据处理
# ============================================

def parse_pair_data(raw_pair: Dict) -> Optional[PairData]:
    """解析原始交易对数据"""
    try:
        # 只处理 Base 链的数据
        if raw_pair.get("chainId", "").lower() != "base":
            return None
        
        dex_id = raw_pair.get("dexId", "unknown").lower()
        
        # 美化 DEX 名称
        dex_name = DEX_NAMES.get(dex_id, dex_id.capitalize())
        
        return PairData(
            dex_id=dex_id,
            dex_name=dex_name,
            pair_address=raw_pair.get("pairAddress", ""),
            base_token=raw_pair.get("baseToken", {}).get("address", ""),
            quote_token=raw_pair.get("quoteToken", {}).get("address", ""),
            price_usd=float(raw_pair.get("priceUsd") or 0),
            price_native=float(raw_pair.get("priceNative") or 0),
            liquidity_usd=float(raw_pair.get("liquidity", {}).get("usd") or 0),
            volume_24h=float(raw_pair.get("volume", {}).get("h24") or 0),
            volume_6h=float(raw_pair.get("volume", {}).get("h6") or 0),
            volume_1h=float(raw_pair.get("volume", {}).get("h1") or 0),
            price_change_24h=float(raw_pair.get("priceChange", {}).get("h24") or 0),
            url=raw_pair.get("url", ""),
        )
    except Exception:
        return None


def analyze_tokens(all_pairs: List[PairData]) -> Dict[str, TokenAnalysis]:
    """分析所有交易对，按代币分组"""
    tokens: Dict[str, TokenAnalysis] = {}
    
    for pair in all_pairs:
        if not pair.base_token:
            continue
        
        addr = pair.base_token.lower()
        
        if addr not in tokens:
            # 从交易对数据中提取代币信息
            tokens[addr] = TokenAnalysis(
                symbol="",  # 将从 API 更新
                address=pair.base_token,
                name="",
                pairs=[]
            )
        
        tokens[addr].pairs.append(pair)
    
    return tokens


def filter_tokens(
    tokens: Dict[str, TokenAnalysis],
    min_liquidity: float = DEFAULT_MIN_LIQUIDITY,
    min_volume: float = DEFAULT_MIN_VOLUME_24H,
    min_dex_count: int = 2,
    min_spread: float = 0
) -> List[TokenAnalysis]:
    """筛选符合条件的代币"""
    filtered = []
    
    for token in tokens.values():
        # 流动性筛选
        if token.total_liquidity < min_liquidity:
            continue
        
        # 交易量筛选
        if token.total_volume_24h < min_volume:
            continue
        
        # 多 DEX 筛选
        if token.dex_count < min_dex_count:
            continue
        
        # 价差筛选
        if token.spread_percent < min_spread:
            continue
        
        filtered.append(token)
    
    return filtered


# ============================================
# 报告生成
# ============================================

def format_number(num: float, decimals: int = 2) -> str:
    """格式化数字显示"""
    if num >= 1_000_000:
        return f"${num/1_000_000:.{decimals}f}M"
    elif num >= 1_000:
        return f"${num/1_000:.{decimals}f}K"
    else:
        return f"${num:.{decimals}f}"


def format_percent(pct: float) -> str:
    """格式化百分比，带颜色指示"""
    if pct >= 1.0:
        return f"🔥 {pct:.2f}%"
    elif pct >= 0.5:
        return f"⚡ {pct:.2f}%"
    elif pct >= 0.3:
        return f"✨ {pct:.2f}%"
    else:
        return f"   {pct:.2f}%"


def format_price_change(pct: float) -> str:
    """格式化价格变化"""
    if pct >= 0:
        return f"📈 +{pct:.1f}%"
    else:
        return f"📉 {pct:.1f}%"


def generate_report(
    tokens: List[TokenAnalysis],
    sort_by: str = "spread"
) -> str:
    """生成分析报告"""
    if not tokens:
        return "❌ 未找到符合条件的代币"
    
    # 排序
    if sort_by == "spread":
        tokens.sort(key=lambda t: t.spread_percent, reverse=True)
    elif sort_by == "volume":
        tokens.sort(key=lambda t: t.total_volume_24h, reverse=True)
    elif sort_by == "liquidity":
        tokens.sort(key=lambda t: t.total_liquidity, reverse=True)
    
    # 构建表格数据
    table_data = []
    
    for i, token in enumerate(tokens[:30], 1):  # 只显示前 30 个
        dex_str = ", ".join(sorted(token.dex_list)[:4])  # 最多显示 4 个 DEX
        if len(token.dex_list) > 4:
            dex_str += f" +{len(token.dex_list)-4}"
        
        # 价格范围
        if token.min_price > 0:
            if token.min_price < 0.0001:
                price_range = f"${token.min_price:.2e} - ${token.max_price:.2e}"
            else:
                price_range = f"${token.min_price:.6f} - ${token.max_price:.6f}"
        else:
            price_range = "N/A"
        
        table_data.append([
            i,
            token.symbol or token.address[:10] + "...",
            token.address[:10] + "...",
            format_percent(token.spread_percent),
            token.dex_count,
            format_number(token.total_liquidity),
            format_number(token.total_volume_24h),
            format_price_change(token.avg_price_change_24h),
            dex_str,
        ])
    
    # 表头
    headers = [
        "#",
        "Symbol",
        "Address",
        "Spread %",
        "DEXs",
        "Liquidity",
        "Vol 24h",
        "Change",
        "Markets",
    ]
    
    return tabulate(
        table_data, 
        headers=headers, 
        tablefmt="rounded_grid",
        stralign="left",
        numalign="right"
    )


def print_header():
    """打印头部"""
    print()
    print("╔" + "═" * 78 + "╗")
    print("║" + " " * 20 + "🔍 FlashArb Market Screener" + " " * 31 + "║")
    print("║" + " " * 20 + "   Base Chain Analysis" + " " * 35 + "║")
    print("╠" + "═" * 78 + "╣")
    print(f"║  📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}" + " " * 52 + "║")
    print(f"║  🔗 Chain: Base (ID: 8453)" + " " * 51 + "║")
    print("╚" + "═" * 78 + "╝")
    print()


def print_summary(tokens: List[TokenAnalysis], filtered: List[TokenAnalysis]):
    """打印摘要"""
    print("┌" + "─" * 40 + "┐")
    print("│" + " 📊 Analysis Summary".ljust(40) + "│")
    print("├" + "─" * 40 + "┤")
    print(f"│  Tokens Scanned:     {len(tokens):>15} │")
    print(f"│  Multi-DEX Tokens:   {len(filtered):>15} │")
    
    if filtered:
        avg_spread = sum(t.spread_percent for t in filtered) / len(filtered)
        max_spread = max(t.spread_percent for t in filtered)
        total_liq = sum(t.total_liquidity for t in filtered)
        
        print(f"│  Avg Spread:         {avg_spread:>14.2f}% │")
        print(f"│  Max Spread:         {max_spread:>14.2f}% │")
        print(f"│  Total Liquidity:    {format_number(total_liq):>15} │")
    
    print("└" + "─" * 40 + "┘")
    print()


def print_hot_opportunities(tokens: List[TokenAnalysis]):
    """打印热门机会"""
    hot = [t for t in tokens if t.spread_percent >= 0.5]
    
    if not hot:
        return
    
    print()
    print("🔥 " + "═" * 30 + " HOT OPPORTUNITIES " + "═" * 29)
    print()
    
    for token in hot[:5]:
        print(f"  💎 {token.symbol or token.address[:15]}")
        print(f"     Spread: {token.spread_percent:.2f}% | DEXs: {', '.join(token.dex_list)}")
        print(f"     Volume 24h: {format_number(token.total_volume_24h)} | Liquidity: {format_number(token.total_liquidity)}")
        
        # 显示最高和最低价格的 DEX
        if token.pairs:
            sorted_pairs = sorted(token.pairs, key=lambda p: p.price_usd)
            if len(sorted_pairs) >= 2:
                low = sorted_pairs[0]
                high = sorted_pairs[-1]
                print(f"     📉 Buy on: {low.dex_name} @ ${low.price_usd:.8f}")
                print(f"     📈 Sell on: {high.dex_name} @ ${high.price_usd:.8f}")
        print()


def print_footer():
    """打印页脚"""
    print()
    print("─" * 80)
    print("💡 Tips:")
    print("   • Spread > 0.3% may cover flash loan fees (0.3%)")
    print("   • Spread > 1.0% is a strong opportunity 🔥")
    print("   • Always verify liquidity depth before trading")
    print("   • Use --min-spread 0.5 to filter high-spread tokens")
    print("─" * 80)
    print()


# ============================================
# 主函数
# ============================================

async def fetch_all_pairs(client: DexScreenerClient, tokens: List[str]) -> List[PairData]:
    """并行获取所有代币的交易对"""
    print("📡 Fetching market data from DexScreener...")
    print(f"   Scanning {len(tokens)} tokens...")
    
    all_pairs = []
    
    # 分批获取，避免过多并发请求
    batch_size = 5
    for i in range(0, len(tokens), batch_size):
        batch = tokens[i:i+batch_size]
        
        tasks = [client.get_token_pairs(token) for token in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for raw_pairs in results:
            # 跳过错误或空结果
            if raw_pairs is None or isinstance(raw_pairs, Exception):
                continue
            
            for raw_pair in raw_pairs:
                pair = parse_pair_data(raw_pair)
                if pair and pair.liquidity_usd > 0:
                    all_pairs.append(pair)
        
        # 进度显示
        progress = min(i + batch_size, len(tokens))
        print(f"   Progress: {progress}/{len(tokens)} tokens", end="\r")
        
        # 速率限制
        await asyncio.sleep(0.2)
    
    print(f"   ✅ Fetched {len(all_pairs)} pairs from {len(tokens)} tokens")
    print()
    
    return all_pairs


async def main():
    """主函数"""
    # 解析命令行参数
    parser = argparse.ArgumentParser(
        description="FlashArb Market Screener - Analyze arbitrage opportunities on Base"
    )
    parser.add_argument(
        "--min-liquidity", type=float, default=DEFAULT_MIN_LIQUIDITY,
        help=f"Minimum liquidity in USD (default: ${DEFAULT_MIN_LIQUIDITY:,})"
    )
    parser.add_argument(
        "--min-volume", type=float, default=DEFAULT_MIN_VOLUME_24H,
        help=f"Minimum 24h volume in USD (default: ${DEFAULT_MIN_VOLUME_24H:,})"
    )
    parser.add_argument(
        "--min-spread", type=float, default=DEFAULT_MIN_SPREAD,
        help=f"Minimum spread percentage (default: {DEFAULT_MIN_SPREAD}%)"
    )
    parser.add_argument(
        "--sort", choices=["spread", "volume", "liquidity"], default="spread",
        help="Sort results by (default: spread)"
    )
    parser.add_argument(
        "--tokens", type=str, nargs="+",
        help="Custom token addresses to scan"
    )
    
    args = parser.parse_args()
    
    # 打印头部
    print_header()
    
    # 确定要扫描的代币
    tokens_to_scan = args.tokens if args.tokens else HOT_TOKENS
    
    print(f"⚙️  Configuration:")
    print(f"   Min Liquidity: ${args.min_liquidity:,.0f}")
    print(f"   Min Volume 24h: ${args.min_volume:,.0f}")
    print(f"   Min Spread: {args.min_spread}%")
    print(f"   Sort by: {args.sort}")
    print()
    
    # 获取数据
    async with DexScreenerClient() as client:
        all_pairs = await fetch_all_pairs(client, tokens_to_scan)
    
    if not all_pairs:
        print("❌ No pairs found. Check your internet connection or try again later.")
        return
    
    # 分析数据
    print("🔬 Analyzing token data...")
    tokens = analyze_tokens(all_pairs)
    
    # 从 API 数据更新代币符号
    for pair in all_pairs:
        addr = pair.base_token.lower()
        if addr in tokens and not tokens[addr].symbol:
            # 从原始数据获取符号
            tokens[addr].symbol = "?"  # 占位符
    
    # 再次获取符号（从搜索结果）
    async with DexScreenerClient() as client:
        for addr, token_data in tokens.items():
            if token_data.pairs:
                # 使用第一个配对的信息
                raw_pairs = await client.get_token_pairs(addr)
                if raw_pairs:
                    token_data.symbol = raw_pairs[0].get("baseToken", {}).get("symbol", "?")
                    token_data.name = raw_pairs[0].get("baseToken", {}).get("name", "")
    
    # 筛选
    filtered = filter_tokens(
        tokens,
        min_liquidity=args.min_liquidity,
        min_volume=args.min_volume,
        min_dex_count=2,
        min_spread=args.min_spread
    )
    
    # 打印摘要
    print_summary(list(tokens.values()), filtered)
    
    # 打印热门机会
    print_hot_opportunities(filtered)
    
    # 打印主表格
    print("📊 Multi-DEX Arbitrage Opportunities")
    print("=" * 80)
    print()
    report = generate_report(filtered, sort_by=args.sort)
    print(report)
    
    # 打印页脚
    print_footer()
    
    # 导出建议
    if filtered:
        print("📋 Suggested TARGET_TOKENS for main.py:")
        print()
        print("TARGET_TOKENS = [")
        for token in filtered[:5]:
            if token.spread_percent >= 0.3:
                print(f'    {{"symbol": "{token.symbol}", "address": "{token.address}", "decimals": 18, "min_profit": 0.0005}},')
        print("]")
        print()


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

