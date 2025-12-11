"""
FlashArb V3 - Validated Strategy Configuration
Generated: 2025-12-07 18:37:14

✅ All decimals verified on-chain via Multicall
   Network: Base Chain (Chain ID: 8453)
   Tokens: 10 validated

⚠️  TRANSFER TAX WARNING:
   The following tokens have >3% spread (potential transfer tax):
   cbETH, BRETT, TOSHI, KEYCAT, DEGEN
   These may have hidden fees that reduce profit!
"""

VALIDATED_TOKENS = [
    # AERO
    {
        "symbol": "AERO",
        "address": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
        "decimals": 18,  # ✅ Verified
        "fee_tiers": [500, 3000, 10000],
        "min_profit": 0.001,
    },

    # ⚠️ cbETH - HIGH SPREAD (Potential Transfer Tax)
    {
        "symbol": "cbETH",
        "address": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22",
        "decimals": 18,  # ✅ Verified
        "fee_tiers": [500, 3000],
        "min_profit": 0.0003,
    },

    # VIRTUAL
    {
        "symbol": "VIRTUAL",
        "address": "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b",
        "decimals": 18,  # ✅ Verified
        "fee_tiers": [3000, 10000],
        "min_profit": 0.0005,
    },

    # ⚠️ BRETT - HIGH SPREAD (Potential Transfer Tax)
    {
        "symbol": "BRETT",
        "address": "0x532f27101965dd16442E59d40670FaF5eBB142E4",
        "decimals": 18,  # ✅ Verified
        "fee_tiers": [3000, 10000],
        "min_profit": 0.0003,
    },

    # ⚠️ TOSHI - HIGH SPREAD (Potential Transfer Tax)
    {
        "symbol": "TOSHI",
        "address": "0xAC1Bd2486aAf3B5C0fc3Fd868558b082a531B2B4",
        "decimals": 18,  # ✅ Verified
        "fee_tiers": [3000, 10000],
        "min_profit": 0.0003,
    },

    # ⚠️ KEYCAT - HIGH SPREAD (Potential Transfer Tax)
    {
        "symbol": "KEYCAT",
        "address": "0x9a26F5433671751C3276a065f57e5a02D2817973",
        "decimals": 18,  # ✅ Verified
        "fee_tiers": [3000, 10000],
        "min_profit": 0.0003,
    },

    # ⚠️ DEGEN - HIGH SPREAD (Potential Transfer Tax)
    {
        "symbol": "DEGEN",
        "address": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        "decimals": 18,  # ✅ Verified
        "fee_tiers": [3000, 10000],
        "min_profit": 0.0003,
    },

    # HIGHER
    {
        "symbol": "HIGHER",
        "address": "0x0578d8A44db98B23BF096A382e016e29a5Ce0ffe",
        "decimals": 18,  # ✅ Verified
        "fee_tiers": [3000, 10000],
        "min_profit": 0.0003,
    },

    # USDC
    {
        "symbol": "USDC",
        "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "decimals": 6,  # ✅ Verified
        "fee_tiers": [500, 3000],
        "min_profit": 0.0005,
    },

    # USDbC
    {
        "symbol": "USDbC",
        "address": "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA",
        "decimals": 6,  # ✅ Verified
        "fee_tiers": [500, 3000],
        "min_profit": 0.0005,
    },

]


# =========================================
# Helper Functions
# =========================================

def get_token_by_symbol(symbol: str) -> dict:
    """Get token config by symbol."""
    for token in VALIDATED_TOKENS:
        if token["symbol"].upper() == symbol.upper():
            return token
    return None


def get_all_addresses() -> list:
    """Get all token addresses."""
    return [token["address"] for token in VALIDATED_TOKENS]
