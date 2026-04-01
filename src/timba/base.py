"""Shared types and helpers for all trading strategies.

Provides:
  - PositionState: state machine enum shared by all strategies
  - MarketPosition: base dataclass with market identity, timewindow, order tracking
  - Helpers: window checks, liquidity gate, order placement, order verification, resolution
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger(__name__)

# Safety margins for live trading
CLOSE_WINDOW_SEC = 5    # Default close window (seconds before close to stop)
RESOLVE_DELAY_SEC = 30  # Wait after close for Polymarket oracle to settle
ORDER_POLL_ATTEMPTS = 5  # How many times to poll order status after placement
ORDER_POLL_INTERVAL = 1  # Seconds between polls
MIN_LIQUIDITY = 100      # Minimum $ liquidity to evaluate a market


class PositionState(str, Enum):
    """State machine for any market position."""
    WATCHING = "watching"
    PENDING_ORDER = "pending_order"
    SNIPED = "sniped"
    WON = "won"
    LOST = "lost"
    SKIPPED = "skipped"
    FAILED = "failed"  # wanted to bet but couldn't fill (tick/price)
    SKIP_WON = "skip_won"
    SKIP_LOST = "skip_lost"
    SKIP_NONE = "skip_none"
    FAIL_WON = "fail_won"
    FAIL_LOST = "fail_lost"
    PAPER = "paper"
    PAPER_WON = "paper_won"
    PAPER_LOST = "paper_lost"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATES

    @property
    def is_pending(self) -> bool:
        return self in _PENDING_STATES


_TERMINAL_STATES = frozenset({
    PositionState.WON, PositionState.LOST,
    PositionState.SKIP_WON, PositionState.SKIP_LOST, PositionState.SKIP_NONE,
    PositionState.FAIL_WON, PositionState.FAIL_LOST,
    PositionState.PAPER_WON, PositionState.PAPER_LOST,
})
_PENDING_STATES = frozenset({
    PositionState.SNIPED, PositionState.SKIPPED, PositionState.FAILED, PositionState.PAPER,
})

# Resolution: state → (log_tag, won_state, lost_state)
RESOLVE_MAP = {
    PositionState.SNIPED:  ("TRADE", PositionState.WON,       PositionState.LOST),
    PositionState.SKIPPED: ("SKIP",  PositionState.SKIP_WON,  PositionState.SKIP_LOST),
    PositionState.FAILED:  ("FAIL",  PositionState.FAIL_WON,  PositionState.FAIL_LOST),
    PositionState.PAPER:   ("PAPER", PositionState.PAPER_WON, PositionState.PAPER_LOST),
}

# Valid state transitions — anything not listed here is rejected.
_VALID_TRANSITIONS: dict[PositionState, frozenset[PositionState]] = {
    PositionState.WATCHING:       frozenset({PositionState.PENDING_ORDER, PositionState.SKIPPED}),
    PositionState.PENDING_ORDER:  frozenset({PositionState.SNIPED, PositionState.PAPER,
                                             PositionState.SKIPPED, PositionState.FAILED}),
    PositionState.SNIPED:         frozenset({PositionState.WON, PositionState.LOST}),
    PositionState.PAPER:          frozenset({PositionState.PAPER_WON, PositionState.PAPER_LOST}),
    PositionState.SKIPPED:        frozenset({PositionState.SKIP_WON, PositionState.SKIP_LOST,
                                             PositionState.SKIP_NONE}),
    PositionState.FAILED:         frozenset({PositionState.FAIL_WON, PositionState.FAIL_LOST}),
}


@dataclass
class MarketPosition:
    """Base position tracking for any strategy on an UpDown market.

    Contains everything that is NOT strategy-specific:
    market identity, timewindow config, order tracking, common state.
    """
    # ── Market identity (from UpDownMarket) ──
    condition_id: str
    question: str
    slug: str
    coin: str
    interval: str
    token_id_up: str
    token_id_down: str
    end_timestamp: int
    window_start_ts: int  # When the market window starts (epoch)

    # ── Timewindow config ──
    entry_window_sec: float = 20
    close_window_sec: float = CLOSE_WINDOW_SEC

    # ── Trading config ──
    contracts: int = 200
    market_mode: str = "live"  # "live" = real orders, "paper" = track only
    tick_size: float = 0.01  # CLOB tick size — max price = 1 - tick_size
    data_dir: str = "data"
    resolve_delay_sec: float = RESOLVE_DELAY_SEC

    # ── Common state ──
    state: PositionState = PositionState.WATCHING
    side: str = ""
    midpoint: float = 0.0
    buy_price: float = 0.0
    cost: float = 0.0
    pnl: float = 0.0
    skip_reason: str = ""
    ticks_evaluated: int = 0

    # ── Liquidity ──
    liquidity: float = -1.0
    liquidity_checked: bool = False

    # ── Traceability ──
    ev_id: int = 0  # which EV triggered the bet (→ evs.id)

    # ── Order tracking ──
    order_id: str = ""
    filled_at: str = ""

    # ── Timestamps ──
    opened_at: str = ""
    sniped_at: str = ""
    resolved_at: str = ""

    # ── Thread safety ──
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def transition(self, new_state: PositionState, **kwargs) -> bool:
        """Atomically transition to a new state, with validation.

        Only allows transitions defined in _VALID_TRANSITIONS.
        Optionally sets additional fields (skip_reason, sniped_at, etc.)
        under the same lock. Returns True if transition succeeded.
        """
        with self._lock:
            allowed = _VALID_TRANSITIONS.get(self.state, frozenset())
            if new_state not in allowed:
                logger.warning(
                    "Rejected state transition %s → %s for %s",
                    self.state.value, new_state.value, self.label,
                )
                return False
            self.state = new_state
            for k, v in kwargs.items():
                setattr(self, k, v)
            return True

    def time_remaining(self) -> float:
        return max(0, self.end_timestamp - time.time())

    def remaining_at(self, tick_ts: float) -> float:
        """Compute seconds remaining at a given timestamp."""
        return max(0, self.end_timestamp - tick_ts)

    def progress_at(self, tick_ts: float) -> float:
        """Compute window progress (0.0 to 1.0) at a given timestamp."""
        remaining = self.remaining_at(tick_ts)
        if remaining > self.entry_window_sec:
            return 0.0
        if remaining < self.close_window_sec:
            return 1.0
        obs = self.entry_window_sec - self.close_window_sec
        if obs <= 0:
            return 1.0
        return max(0.0, min(1.0, (self.entry_window_sec - remaining) / obs))

    def in_window_at(self, tick_ts: float) -> bool:
        """Check if a timestamp falls within this position's entry window."""
        remaining = self.remaining_at(tick_ts)
        return self.close_window_sec <= remaining <= self.entry_window_sec

    @property
    def label(self) -> str:
        q = self.question
        try:
            after_dash = q.split(" - ", 1)[1]
            time_part = after_dash.split(" ET")[0].strip()
        except (IndexError, AttributeError):
            time_part = self.slug[-10:]
        return f"{self.coin.upper()} {self.interval} {time_part}"


# ── Shared helpers ──

def check_entry_window(pos: MarketPosition) -> tuple[str, float, float]:
    """Check if position is within its entry window.

    Returns (status, remaining, progress) where:
      status: "early" | "active" | "timeout"
      remaining: seconds until market close
      progress: 0.0 at window entry, 1.0 at close_window
    """
    remaining = pos.time_remaining()

    if remaining > pos.entry_window_sec:
        return "early", remaining, 0.0

    if remaining < pos.close_window_sec:
        return "timeout", remaining, 1.0

    observation_window = pos.entry_window_sec - pos.close_window_sec
    if observation_window > 0:
        progress = (pos.entry_window_sec - remaining) / observation_window
        progress = max(0.0, min(1.0, progress))
    else:
        progress = 1.0

    return "active", remaining, progress


def check_liquidity(pos: MarketPosition) -> bool:
    """Check liquidity using value from discovery (no HTTP call).

    Mutates pos.state on failure.
    """
    if pos.liquidity_checked or pos.liquidity < 0:
        return True  # already checked or no liquidity data (skip check)

    pos.liquidity_checked = True

    if pos.liquidity < MIN_LIQUIDITY:
        logger.debug("Skip %s — liquidity $%.0f < $%d min", pos.label, pos.liquidity, MIN_LIQUIDITY)
        pos.transition(
            PositionState.SKIPPED,
            skip_reason=f"low liquidity (${pos.liquidity:.0f})",
            sniped_at=datetime.now(timezone.utc).isoformat(),
        )
        return False

    return True


def poll_order_fill(clob_client, order_id: str, label: str) -> tuple[float, float]:
    """Poll CLOB order status to get actual fill size and price.

    Returns (filled_size, avg_fill_price). Cancels and returns (0, 0) if not filled.
    """
    for attempt in range(ORDER_POLL_ATTEMPTS):
        try:
            orders = clob_client.get_orders(order_id=order_id)
            if orders:
                order = orders[0]
                matched = float(order.size_matched)
                if matched > 0:
                    logger.info(
                        "ORDER %s | %s filled %.0f @ $%.4f",
                        label, order_id, matched, float(order.price),
                    )
                    return (matched, float(order.price))
        except Exception:
            logger.debug("Poll attempt %d failed for %s", attempt + 1, order_id)
        if attempt < ORDER_POLL_ATTEMPTS - 1:
            time.sleep(ORDER_POLL_INTERVAL)

    # Not filled — cancel to avoid lingering orders on the CLOB
    logger.warning("ORDER %s | %s not filled after %d polls, cancelling",
                   label, order_id, ORDER_POLL_ATTEMPTS)
    try:
        clob_client.cancel(order_id)
    except Exception:
        logger.debug("Cancel failed for %s (may have already expired)", order_id)
    return (0, 0)


def place_order(
    pos: MarketPosition,
    side: str,
    fill_price: float,
    fill_size: int,
    clob_client,
) -> bool:
    """Place a real order on the CLOB.

    Cash management is handled by OrderManager (reservation system).
    Mutates pos with order result. Returns True if order was placed and filled.
    """
    ORDER_EXPIRY_SEC = 65  # CLOB requires minimum 60s threshold + actual expiry

    token_id = pos.token_id_up if side == "up" else pos.token_id_down

    order_price = round(fill_price, 3)
    cost = order_price * fill_size

    logger.info("ORDER %s | posting %s @$%.4f", pos.label, side.upper(), order_price)

    pos.skip_reason = ""
    pos.cost = cost
    pos.sniped_at = datetime.now(timezone.utc).isoformat()

    try:
        from polymarket_apis.types.clob_types import OrderArgs, OrderType
        order_args = OrderArgs(
            token_id=token_id,
            price=order_price,
            size=fill_size,
            side="BUY",
            expiration=int(time.time()) + ORDER_EXPIRY_SEC,
        )
        resp = clob_client.create_and_post_order(order_args, order_type=OrderType.GTD)
        if resp is None:
            raise RuntimeError("order rejected by CLOB (no response)")
        if hasattr(resp, "success") and not resp.success:
            error_msg = getattr(resp, "error_msg", "unknown error")
            raise RuntimeError(f"order rejected: {error_msg}")
        if resp and hasattr(resp, "order_id") and resp.order_id:
            pos.order_id = str(resp.order_id)
        logger.debug("Order response for %s: %s", pos.label, resp)
    except Exception as e:
        logger.error("ORDER %s | Failed to place order: %s", pos.label, e)
        pos.cost = 0
        pos.transition(PositionState.SKIPPED, skip_reason=f"order failed: {e}")
        return False

    # Poll order status — cancel if not filled
    if pos.order_id:
        filled_size, filled_price = poll_order_fill(clob_client, pos.order_id, pos.label)
        if filled_size == 0:
            pos.cost = 0
            pos.transition(PositionState.SKIPPED, skip_reason="order not filled")
            return False
        pos.filled_at = datetime.now(timezone.utc).isoformat()
        if filled_price > 0:
            pos.buy_price = filled_price
        pos.contracts = int(filled_size)
        pos.cost = pos.buy_price * pos.contracts

    return True


def resolve_winner(
    pos: MarketPosition,
    feed,
    clob_client,
    get_midpoint_fn,
) -> bool | None:
    """Determine if our side won after market close.

    Returns True (won), False (lost), or None (not ready yet).
    Mutates pos.resolved_at on resolution.
    """
    mapping = RESOLVE_MAP.get(pos.state)
    if mapping is None:
        return None

    # Wait for oracle to settle
    seconds_since_close = time.time() - pos.end_timestamp
    if seconds_since_close < pos.resolve_delay_sec:
        return None

    pos.resolved_at = datetime.now(timezone.utc).isoformat()

    if not pos.side:
        return False

    # Check CLOB first (authoritative after resolution)
    token_id = pos.token_id_up if pos.side == "up" else pos.token_id_down
    mid = get_midpoint_fn(clob_client, token_id)
    if mid is not None:
        won = mid >= 0.5
        logger.debug("Resolve %s | CLOB mid=$%.4f → %s", pos.label, mid, "won" if won else "lost")
        return won

    # Fallback: Coinbase signal
    signal = feed.get_direction(pos.coin, pos.window_start_ts)
    won = signal is not None and signal.direction == pos.side
    logger.debug("Resolve %s | CLOB unavailable, Coinbase fallback → %s", pos.label, "won" if won else "lost")
    return won
