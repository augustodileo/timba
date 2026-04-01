"""Market discovery for recurring crypto up/down markets on Polymarket.

Two slug patterns:
  5m/15m: {coin}-updown-{interval}-{unix_timestamp}  (e.g. btc-updown-15m-1774221300)
  Hourly: {coin_full}-up-or-down-{month}-{day}-{year}-{hour}{am/pm}-et
          (e.g. bitcoin-up-or-down-march-23-2026-5pm-et)
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from timba.constants import GAMMA_API

logger = logging.getLogger(__name__)

# Full coin names for hourly market slugs
COIN_FULL_NAME = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "xrp": "xrp",
    "bnb": "bnb",
    "doge": "dogecoin",
    "hype": "hype",
}

FULL_NAME_TO_COIN = {v: k for k, v in COIN_FULL_NAME.items()}


def parse_slug(slug: str) -> tuple[str, str]:
    """Extract (coin, interval) from a market slug.

    5m/15m: btc-updown-5m-1774609200 → ("btc", "5m")
    1h: bitcoin-up-or-down-march-27-2026-9am-et → ("btc", "1h")
    """
    if "-updown-" in slug:
        parts = slug.split("-")
        return parts[0], parts[2]
    if "-up-or-down-" in slug:
        full_name = slug.split("-up-or-down-")[0]
        coin = FULL_NAME_TO_COIN.get(full_name, full_name)
        return coin, "1h"
    return "", ""


MONTH_NAMES = [
    "", "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]


@dataclass
class UpDownMarket:
    """A single crypto up/down market with its two outcome tokens."""
    condition_id: str
    question: str
    slug: str
    token_id_up: str
    token_id_down: str
    end_timestamp: int  # unix ts when the market window closes
    coin: str           # e.g. "btc"
    interval: str       # e.g. "15m"
    gamma_price_up: float
    gamma_price_down: float
    volume: float = 0.0
    liquidity: float = 0.0
    tick_size: float = 0.01


@dataclass
class MarketSeries:
    """A coin + interval combination, e.g. BTC 15m."""
    coin: str
    interval: str
    interval_sec: int

    @property
    def slug_prefix(self) -> str:
        return f"{self.coin}-updown-{self.interval}"


# All known series on Polymarket
DEFAULT_SERIES = [
    # Hourly (uses different slug pattern — date-based, not timestamp)
    MarketSeries("btc", "1h", 3600),
    MarketSeries("eth", "1h", 3600),
    MarketSeries("sol", "1h", 3600),
    MarketSeries("xrp", "1h", 3600),
    MarketSeries("bnb", "1h", 3600),
    MarketSeries("doge", "1h", 3600),
    MarketSeries("hype", "1h", 3600),
    # 15-minute
    MarketSeries("btc", "15m", 900),
    MarketSeries("eth", "15m", 900),
    MarketSeries("sol", "15m", 900),
    MarketSeries("xrp", "15m", 900),
    MarketSeries("bnb", "15m", 900),
    MarketSeries("doge", "15m", 900),
    MarketSeries("hype", "15m", 900),
    # 5-minute
    MarketSeries("btc", "5m", 300),
    MarketSeries("eth", "5m", 300),
    MarketSeries("sol", "5m", 300),
    MarketSeries("xrp", "5m", 300),
    MarketSeries("bnb", "5m", 300),
    MarketSeries("doge", "5m", 300),
    MarketSeries("hype", "5m", 300),
]


def discover_active_markets(
    series_list: list[MarketSeries] | None = None,
    look_ahead: int = 3,
    known_slugs: set[str] | None = None,
) -> list[UpDownMarket]:
    """Find currently active (open, not closed) crypto up/down markets.

    For each series, we generate slug candidates around the current time
    and query the Gamma API. look_ahead controls how many future intervals
    to check. Slugs in known_slugs are skipped (no HTTP call).
    """
    if series_list is None:
        series_list = DEFAULT_SERIES

    known = known_slugs or set()
    now = int(time.time())
    markets = []
    skipped = 0

    for series in series_list:
        interval = series.interval_sec

        # Generate slug candidates
        if series.interval == "1h":
            slugs = _generate_hourly_slugs(series.coin, now, look_ahead)
        else:
            base_ts = now - (now % interval)
            slugs = [
                f"{series.slug_prefix}-{base_ts + offset}"
                for offset in range(-interval, interval * (look_ahead + 1), interval)
            ]

        for slug in slugs:
            if slug in known:
                skipped += 1
                continue
            try:
                market = _fetch_market_by_slug(slug, series)
                if market:
                    markets.append(market)
            except (requests.RequestException, KeyError, ValueError):
                logger.debug("Failed to fetch slug %s", slug, exc_info=True)

    logger.debug("Discovered %d active markets (skipped %d known)", len(markets), skipped)
    return markets


def _parse_hourly_slug_timestamp(slug: str) -> int:
    """Parse a hourly slug into a unix timestamp for the window start.

    E.g., 'bitcoin-up-or-down-march-23-2026-5pm-et' → unix ts for March 23 2026 5PM EST
    """
    try:
        parts = slug.split("-")
        # Find month, day, year, hour from the slug
        # Pattern: ...{month}-{day}-{year}-{hour}{am/pm}-et
        time_part = parts[-2]  # e.g., "5pm"
        year = int(parts[-3])
        day = int(parts[-4])
        month_name = parts[-5]
        month = MONTH_NAMES.index(month_name)

        ampm = time_part[-2:]
        hour = int(time_part[:-2])
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0

        # "ET" = America/New_York (handles EST/EDT automatically)
        et_tz = ZoneInfo("America/New_York")
        dt_et = datetime(year, month, day, hour, 0, 0, tzinfo=et_tz)
        return int(dt_et.timestamp())
    except Exception:
        return 0


def _generate_hourly_slugs(coin: str, now: int, look_ahead: int) -> list[str]:
    """Generate hourly market slugs using date-based pattern.

    Pattern: {coin_full}-up-or-down-{month}-{day}-{year}-{hour}{am/pm}-et
    Polymarket uses EST (UTC-5) for "ET" times.
    """
    coin_full = COIN_FULL_NAME.get(coin, coin)
    slugs = []
    et_tz = ZoneInfo("America/New_York")

    for hour_offset in range(-1, look_ahead + 1):
        ts = now + hour_offset * 3600
        # Align to hour boundary
        ts = ts - (ts % 3600)
        dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
        dt_et = dt_utc.astimezone(et_tz)

        month = MONTH_NAMES[dt_et.month]
        day = dt_et.day
        year = dt_et.year
        hour = dt_et.hour
        ampm = "am" if hour < 12 else "pm"
        hour_12 = hour % 12 or 12

        slug = f"{coin_full}-up-or-down-{month}-{day}-{year}-{hour_12}{ampm}-et"
        slugs.append(slug)

    return slugs


def _fetch_market_by_slug(slug: str, series: MarketSeries) -> UpDownMarket | None:
    """Fetch a single market by slug from the Gamma REST API."""
    resp = requests.get(
        f"{GAMMA_API}/markets",
        params={"slug": slug},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    if not data:
        return None

    raw = data[0]

    # Only return active, non-closed markets
    if not raw.get("active") or raw.get("closed"):
        return None


    return _parse_updown_market(raw, series)


def _parse_updown_market(raw: dict, series: MarketSeries) -> UpDownMarket | None:
    """Parse a raw Gamma API response into an UpDownMarket."""
    outcomes = raw.get("outcomes", "[]")
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)

    if len(outcomes) != 2:
        return None

    prices = raw.get("outcomePrices", "[]")
    if isinstance(prices, str):
        prices = json.loads(prices)

    tokens = raw.get("clobTokenIds", raw.get("tokenIds", "[]"))
    if isinstance(tokens, str):
        tokens = json.loads(tokens)

    if len(tokens) != 2:
        return None

    # Match Up/Down by outcome name, not position
    lower = [o.strip().lower() for o in outcomes]
    if set(lower) != {"up", "down"}:
        return None

    up_idx = lower.index("up")
    down_idx = lower.index("down")

    # Extract end timestamp from slug
    slug = raw.get("slug", "")
    end_ts = 0
    if series.interval == "1h":
        # Hourly: parse date from slug, end = start + 3600
        end_ts = _parse_hourly_slug_timestamp(slug)
        if end_ts:
            end_ts += 3600
    else:
        # 5m/15m: slug ends with unix timestamp
        try:
            slug_ts = int(slug.rsplit("-", 1)[1])
            end_ts = slug_ts + series.interval_sec
        except (ValueError, IndexError):
            end_ts = 0

    return UpDownMarket(
        condition_id=raw.get("conditionId", raw.get("condition_id", "")),
        question=raw.get("question", ""),
        slug=slug,
        token_id_up=tokens[up_idx],
        token_id_down=tokens[down_idx],
        end_timestamp=end_ts,
        coin=series.coin,
        interval=series.interval,
        gamma_price_up=float(prices[up_idx]) if prices else 0.5,
        gamma_price_down=float(prices[down_idx]) if prices else 0.5,
        volume=float(raw.get("volumeNum") or 0),
        liquidity=float(raw.get("liquidityNum") or 0),
        tick_size=float(raw.get("orderPriceMinTickSize") or 0.01),
    )
