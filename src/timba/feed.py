"""Real-time crypto price feed from Coinbase.

Runs a background thread that polls Coinbase REST API every second,
storing price history per coin. Strategies query this to determine
direction and confidence for entry decisions.

Uses Coinbase because Binance blocks requests from GCP US regions.
"""

import logging
import threading
import time
from dataclasses import dataclass

import requests

from timba.constants import COINBASE_EXCHANGE_API, COINBASE_SPOT_API

logger = logging.getLogger(__name__)

# Map our coin names to Coinbase pairs and exchange products
COIN_TO_PAIR = {
    "btc": "BTC-USD",
    "eth": "ETH-USD",
    "sol": "SOL-USD",
    "xrp": "XRP-USD",
    "bnb": "BNB-USD",
    "doge": "DOGE-USD",
    "hype": "HYPE-USD",
}

# Keep 20 minutes of history (enough for 15m windows + buffer)
MAX_HISTORY_SECONDS = 1200
# Circuit breaker: stop trading if no successful feed update in this many seconds
FEED_STALE_THRESHOLD_SEC = 30


@dataclass
class DirectionSignal:
    """Result of analyzing the price direction for a market window."""
    direction: str          # "up" or "down" or "flat"
    change_pct: float       # % change from window open to now
    seconds_trending: float # how long the current direction has held
    reversed_recently: bool # did direction flip in last 30 seconds
    confidence: float       # 0.0 to 1.0
    price_open: float = 0.0 # underlying price at window start (Coinbase)
    price_now: float = 0.0  # underlying price right now (Coinbase)


class PriceFeed:
    """Background price feed from Coinbase."""

    def __init__(self, coins: list[str] | None = None, poll_interval: float = 1.0, stale_threshold: float = FEED_STALE_THRESHOLD_SEC) -> None:
        self.coins = coins or list(COIN_TO_PAIR.keys())
        self.poll_interval = poll_interval
        self._lock = threading.Lock()
        self._prices: dict[str, float] = {}           # coin → current price
        self._history: dict[str, list[tuple[float, float]]] = {}  # coin → [(ts, price), ...]
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_success: float = 0  # timestamp of last successful price update
        self._stale_threshold = stale_threshold

    def start(self) -> None:
        """Start the background polling thread and backfill history."""
        if self._running:
            return
        self._backfill_history()
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        for _ in range(10):
            if self._prices:
                break
            time.sleep(0.5)
        logger.info(
            "PriceFeed started | %d coins | %s | %d min history",
            len(self.coins),
            ", ".join(c.upper() for c in self.coins if c in self._prices),
            min(len(h) for h in self._history.values()) if self._history else 0,
        )

    def stop(self) -> None:
        """Stop the background thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def is_healthy(self) -> bool:
        """Returns False if feed has been stale for too long (circuit breaker)."""
        with self._lock:
            if self._last_success == 0:
                return True  # haven't started polling yet
            return (time.time() - self._last_success) < self._stale_threshold

    def get_price(self, coin: str) -> float | None:
        """Get the latest price for a coin."""
        with self._lock:
            return self._prices.get(coin)

    def get_direction(self, coin: str, window_start_ts: int) -> DirectionSignal | None:
        """Analyze price direction since window_start_ts.

        Returns a DirectionSignal with direction, confidence, and trend info.
        Returns None if we don't have enough data.
        """
        with self._lock:
            history = self._history.get(coin, [])
            current = self._prices.get(coin)

        if not history or current is None:
            return None

        now = time.time()

        # Find the price at window start (or closest available after it)
        open_price = None
        for ts, price in history:
            if ts >= window_start_ts:
                open_price = price
                break

        if open_price is None:
            # No data from window start — use earliest available
            if history:
                open_price = history[0][1]
            else:
                return None

        # Direction — scale flat threshold by window duration
        # 5m (300s) → 0.005%, 15m (900s) → 0.0017%, 4h (14400s) → 0.0001%
        change_pct = ((current - open_price) / open_price) * 100
        window_duration = now - window_start_ts
        flat_threshold = 0.005 * (300 / max(window_duration, 60))
        if abs(change_pct) < flat_threshold:
            direction = "flat"
        elif current > open_price:
            direction = "up"
        else:
            direction = "down"

        # How long has the current direction been consistent?
        # Walk backward through history to find the last direction flip
        seconds_trending = 0
        for i in range(len(history) - 1, 0, -1):
            ts, price = history[i]
            this_dir = "up" if price >= open_price else "down"
            if this_dir != direction:
                seconds_trending = now - ts
                break
        else:
            # Direction consistent for entire history
            if history:
                seconds_trending = now - history[0][0]

        # Check for reversal — scale window proportionally (10% of market duration)
        reversed_recently = False
        reversal_window = max(30, window_duration * 0.10)
        thirty_ago = now - reversal_window
        recent_prices = [(ts, p) for ts, p in history if ts >= thirty_ago]
        if len(recent_prices) >= 2:
            recent_dirs = set()
            for _, p in recent_prices:
                if p > open_price:
                    recent_dirs.add("up")
                elif p < open_price:
                    recent_dirs.add("down")
            if len(recent_dirs) > 1:
                reversed_recently = True

        # Confidence score
        confidence = 0.0

        # Factor 1: magnitude — scale thresholds by window duration
        # 5m base: 0.05/0.1/0.3. Longer windows → lower thresholds
        scale = 300 / max(window_duration, 60)
        if abs(change_pct) > 0.3 * scale:
            confidence += 0.4
        elif abs(change_pct) > 0.1 * scale:
            confidence += 0.25
        elif abs(change_pct) > 0.05 * scale:
            confidence += 0.1

        # Factor 2: trend duration (trending for >60% of window = good)
        if window_duration > 0 and seconds_trending / window_duration > 0.6:
            confidence += 0.3
        elif window_duration > 0 and seconds_trending / window_duration > 0.3:
            confidence += 0.15

        # Factor 3: time remaining (less time = less chance of reversal)
        # This is estimated from window_duration vs typical window (300s for 5m)
        confidence += 0.2  # Base for being in the entry window at all

        # Penalty: recent reversal = dangerous
        if reversed_recently:
            confidence -= 0.3

        # Penalty: flat market
        if direction == "flat":
            confidence = 0.0

        confidence = max(0.0, min(1.0, confidence))

        logger.debug(
            "SIGNAL %s | %s chg=%.4f%% conf=%.0f%% trend=%ds%s",
            coin.upper(), direction, change_pct, confidence * 100,
            int(seconds_trending), " REV" if reversed_recently else "",
        )

        return DirectionSignal(
            direction=direction,
            change_pct=change_pct,
            seconds_trending=seconds_trending,
            reversed_recently=reversed_recently,
            confidence=confidence,
            price_open=open_price,
            price_now=current,
        )

    def _backfill_history(self) -> None:
        """Fetch 20 minutes of 1-minute candles from Coinbase Exchange API.

        This gives the bot price history from before it started, so markets
        already in progress can still get direction signals.
        """
        now = int(time.time())
        start = now - MAX_HISTORY_SECONDS

        for coin in self.coins:
            pair = COIN_TO_PAIR.get(coin)
            if not pair:
                continue
            try:
                resp = requests.get(
                    COINBASE_EXCHANGE_API.format(product=pair),
                    params={"granularity": 60, "start": start, "end": now},
                    timeout=5,
                )
                resp.raise_for_status()
                candles = resp.json()
                # Candles: [timestamp, low, high, open, close, volume]
                with self._lock:
                    if coin not in self._history:
                        self._history[coin] = []
                    for candle in sorted(candles, key=lambda c: c[0]):
                        ts = float(candle[0])
                        close = float(candle[4])
                        self._history[coin].append((ts, close))
                    if candles:
                        latest = sorted(candles, key=lambda c: c[0])[-1]
                        self._prices[coin] = float(latest[4])
                logger.debug("Backfilled %d candles for %s", len(candles), coin.upper())
            except (requests.RequestException, KeyError, ValueError):
                logger.debug("Failed to backfill %s", coin.upper(), exc_info=True)

    def _poll_loop(self) -> None:
        """Background thread: poll Coinbase every second."""
        session = requests.Session()

        while self._running:
            now = time.time()
            for coin in self.coins:
                pair = COIN_TO_PAIR.get(coin)
                if not pair:
                    continue
                try:
                    resp = session.get(
                        COINBASE_SPOT_API.format(pair=pair),
                        timeout=3,
                    )
                    resp.raise_for_status()
                    price = float(resp.json()["data"]["amount"])

                    with self._lock:
                        self._prices[coin] = price
                        if coin not in self._history:
                            self._history[coin] = []
                        self._history[coin].append((now, price))

                        cutoff = now - MAX_HISTORY_SECONDS
                        self._history[coin] = [
                            (ts, p) for ts, p in self._history[coin]
                            if ts >= cutoff
                        ]
                        self._last_success = now
                except (requests.RequestException, KeyError, ValueError):
                    pass  # skip this coin this tick, try again next second

            time.sleep(self.poll_interval)
