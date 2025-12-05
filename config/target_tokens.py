"""
FlashArb V3 - Target Tokens Configuration
Generated: 2024-12-05

⚠️ IMPORTANT:
- Verify 'decimals' on-chain before running with real funds!
- Use: contract.functions.decimals().call()

Tiers:
- Tier 1: High Liquidity & Safety (Blue Chips)
- Tier 2: High Volatility & Profit Potential (Top Memes)
"""

TARGET_TOKENS = [

    # =========================================
    # Tier 1: High Liquidity & Safety (Blue Chips)
    # =========================================

    # AERO | Spread: 2.45% | Liq: $55.87M
    {
        "symbol": "AERO",
        "address": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
        "decimals": 18,
        "fee_tiers": [500, 3000, 10000],
        "min_profit": 0.001,
    },

    # cbETH | Spread: 6.37% | Liq: $9.40M
    {
        "symbol": "cbETH",
        "address": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22",
        "decimals": 18,
        "fee_tiers": [500, 3000],  # Blue chip usually in low fee tiers
        "min_profit": 0.0003,
    },

    # VIRTUAL | Spread: 1.18% | Liq: $11.97M
    {
        "symbol": "VIRTUAL",
        "address": "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b",
        "decimals": 18,
        "fee_tiers": [3000, 10000],
        "min_profit": 0.0005,
    },

    # =========================================
    # Tier 2: High Volatility & Profit Potential (Top Memes)
    # =========================================

    # BRETT | Spread: 4.47% | Liq: $5.40M
    {
        "symbol": "BRETT",
        "address": "0x532f27101965dd16442E59d40670FaF5eBB142E4",
        "decimals": 18,
        "fee_tiers": [3000, 10000],
        "min_profit": 0.0003,
    },

    # TOSHI | Spread: 4.42% | Liq: $3.06M
    {
        "symbol": "TOSHI",
        "address": "0xAC1Bd2486aAf3B5C0fc3Fd868558b082a531B2B4",
        "decimals": 18,
        "fee_tiers": [3000, 10000],
        "min_profit": 0.0003,
    },

    # KEYCAT | Spread: 12.33% | Liq: $1.55M
    {
        "symbol": "KEYCAT",
        "address": "0x9a26F5433671751C3276a065f57e5a02D2817973",
        "decimals": 18,
        "fee_tiers": [3000, 10000],
        "min_profit": 0.0003,
    },

]


# =========================================
# Helper function for quick access
# =========================================

def get_token_by_symbol(symbol: str) -> dict:
    """Get token config by symbol."""
    for token in TARGET_TOKENS:
        if token["symbol"].upper() == symbol.upper():
            return token
    return None


def get_all_addresses() -> list:
    """Get all token addresses."""
    return [token["address"] for token in TARGET_TOKENS]


def get_all_symbols() -> list:
    """Get all token symbols."""
    return [token["symbol"] for token in TARGET_TOKENS]

