"""CLOB helpers: midpoint, orderbook simulation, retry, cancel."""

import logging
import random
import time

import requests

logger = logging.getLogger(__name__)

# The Polymarket SDK uses httpx internally, not requests.
# Catch both families so retries and fallbacks work regardless of transport.
try:
    import httpx
    _NETWORK_ERRORS = (requests.RequestException, TimeoutError, OSError, ValueError,
                       httpx.HTTPError, httpx.StreamError)
except ImportError:
    httpx = None
    _NETWORK_ERRORS = (requests.RequestException, TimeoutError, OSError, ValueError)


def retry_api(fn: object, *args: object, retries: int = 2, backoff: float = 0.5, **kwargs: object) -> object:
    """Call fn with retries, exponential backoff, jitter, and 429 awareness.

    On HTTP 429: respects Retry-After header if present, otherwise uses backoff.
    On other errors: exponential backoff with random jitter to prevent thundering herd.
    """
    last_err = None
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except _NETWORK_ERRORS as e:
            last_err = e
            if attempt >= retries:
                break

            # 429 rate-limit: respect Retry-After header
            if (httpx is not None
                    and isinstance(e, httpx.HTTPStatusError)
                    and e.response.status_code == 429):
                retry_after = e.response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else backoff * (2 ** attempt)
                time.sleep(wait + random.uniform(0, 0.5))
                logger.warning("Rate limited (429), waiting %.1fs", wait)
                continue

            # Other errors: exponential backoff with jitter
            wait = backoff * (2 ** attempt) + random.uniform(0, backoff)
            time.sleep(wait)
    raise last_err


def _get_midpoint(clob_client: object, token_id: str) -> float | None:
    """Fetch the current midpoint price for a token from the CLOB (with retry)."""
    try:
        mid = retry_api(clob_client.get_midpoint, token_id)
        if hasattr(mid, "value"):
            return float(mid.value)
        return float(str(mid))
    except _NETWORK_ERRORS:
        return None


def simulate_fill(clob_client: object, token_id: str, size: int) -> tuple[float, int, float] | None:
    """Simulate filling a BUY order against the live orderbook.

    Walks through ask levels to calculate the average fill price and
    how many contracts would actually fill.

    Returns (avg_fill_price, filled_size, tick_size) or None if orderbook unavailable.
    """
    try:
        book = retry_api(clob_client.get_order_book, token_id)
        asks = sorted(book.asks, key=lambda a: a.price)
    except _NETWORK_ERRORS:
        return None

    if not asks:
        return None

    total_cost = 0.0
    total_filled = 0
    remaining = size

    for ask in asks:
        take = min(remaining, int(ask.size))
        if take <= 0:
            continue
        total_cost += take * ask.price
        total_filled += take
        remaining -= take
        if remaining <= 0:
            break

    if total_filled == 0:
        return None

    avg_price = total_cost / total_filled
    tick_size = float(getattr(book, 'tick_size', 0.01) or 0.01)
    return (avg_price, total_filled, tick_size)


def _cancel(clob_client: object, order_id: str | None) -> None:
    """Cancel an order, ignoring errors."""
    if not order_id or order_id.startswith("paper_"):
        return
    try:
        clob_client.cancel(order_id)
    except _NETWORK_ERRORS:
        pass
