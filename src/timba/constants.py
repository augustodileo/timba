"""Centralized constants: API endpoints, addresses, and operational defaults.

All external URLs, on-chain addresses, and non-configurable defaults in one
place for easy switching between environments (mainnet, testnet, etc.).
"""

# ── Polymarket APIs ──
GAMMA_API = "https://gamma-api.polymarket.com"
RELAYER_URL = "https://relayer-v2.polymarket.com"

# ── Coinbase APIs ──
COINBASE_SPOT_API = "https://api.coinbase.com/v2/prices/{pair}/spot"
COINBASE_EXCHANGE_API = "https://api.exchange.coinbase.com/products/{product}/candles"

# ── Polygon contract addresses ──
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# ── Chain config ──
POLYGON_CHAIN_ID = 137

# ── Operational defaults ──
DISCOVERY_INTERVAL_SEC = 240
INTERVAL_SECS = {"4h": 14400, "1h": 3600, "15m": 900, "5m": 300}

# ── Portfolio estimation ──
CONCURRENT_PER_INTERVAL = {"5m": 2, "15m": 1, "1h": 1, "4h": 1}
AVG_BUY_PRICE = 0.95
BANKROLL_BUFFER = 1.5
