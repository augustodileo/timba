"""In-memory runtime state for the trading bot.

Cash and portfolio are held in memory during the run, synced from
the CLOB balance on startup (via reconcile) and periodically (balance sync).
Trade history, PnL, stats, and unredeemed wins live in SQLite — see db.py.
"""

import itertools
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_trade_counter = itertools.count(1)


def _next_trade_id() -> int:
    return next(_trade_counter)


def init_trade_ids() -> None:
    """Seed trade counter from SQLite to ensure globally unique IDs."""
    global _trade_counter
    from timba import db

    trade_max = db.max_id("trades")
    _trade_counter = itertools.count(trade_max + 1)

    if trade_max > 0:
        logger.info("Seeded trade IDs: next=%d", trade_max + 1)


class State:
    """In-memory bot state. No file persistence — all durable data is in SQLite."""

    def __init__(self):
        self.cash = 0.0
        self.portfolio = 0.0
        self.pending_redemption = 0.0
        self.reserved_cash = 0.0
        self.started_at = datetime.now(timezone.utc).isoformat()

    @property
    def available_cash(self) -> float:
        """Cash minus reservations for pending orders."""
        return self.cash - self.reserved_cash

    def init_portfolio(self, amount: float):
        """Set portfolio and cash from CLOB balance."""
        self.cash = amount
        self.portfolio = amount + self.pending_redemption

    def deduct_cash(self, amount: float) -> bool:
        if self.cash < amount:
            return False
        self.cash -= amount
        return True

    def refund_cash(self, amount: float):
        self.cash += amount

    def credit_redemption(self, amount: float):
        """Credit a redemption: add to cash, subtract from pending."""
        self.cash += amount
        self.pending_redemption = max(0, self.pending_redemption - amount)

    def record_trade(self, position, strategy: str, extra_fields: dict | None = None):
        """Record a resolved trade to SQLite + update in-memory portfolio.

        Writes to SQLite trades table. In-memory portfolio/pending updated for
        real wins/losses so balance stays accurate between CLOB syncs.
        """
        state_val = position.state.value
        type_map = {
            "won": "win", "lost": "loss",
            "skip_won": "skip_win", "skip_lost": "skip_loss",
            "fail_won": "fail_win", "fail_lost": "fail_loss",
            "paper_won": "paper_win", "paper_lost": "paper_loss",
        }
        trade_type = type_map.get(state_val, state_val)

        entry = {
            "id": _next_trade_id(),
            "type": trade_type,
            "strategy": strategy,
            "slug": position.slug,
            "condition_id": position.condition_id,
            "coin": position.coin,
            "interval": position.interval,
            "side": position.side,
            "buy_price": position.buy_price,
            "contracts": position.contracts,
            "pnl": position.pnl,
            "sniped_at": position.sniped_at,
            "resolved_at": position.resolved_at,
            "end_timestamp": position.end_timestamp,
            "market_mode": position.market_mode,
            "skip_reason": position.skip_reason,
            "ticks_evaluated": position.ticks_evaluated,
            "ev_id": position.ev_id,
            "token_id": position.token_id_up if position.side == "up" else position.token_id_down,
            "redeemed": False,
            "order_id": getattr(position, "order_id", None) or None,
        }

        if extra_fields:
            entry.update(extra_fields)

        logger.debug(
            "Recorded %s/%s | %s %s %s | side=%s price=$%.4f",
            strategy, trade_type, position.coin, position.interval, position.market_mode,
            position.side, position.buy_price,
        )

        # Update in-memory portfolio for real trades (stays accurate between CLOB syncs)
        if state_val == "won":
            self.portfolio += position.pnl
            self.pending_redemption += position.contracts * 1.0
        elif state_val == "lost":
            self.portfolio += position.pnl

        # Write to SQLite
        self._append_trade_log(strategy, entry)

    def _append_trade_log(self, strategy: str, entry: dict):
        """Write trade to SQLite."""
        from timba import db
        entry["strategy"] = strategy
        try:
            db.insert_trade(entry)
        except Exception:
            logger.debug("Trade write to SQLite failed", exc_info=True)

    def to_dashboard_dict(self) -> dict:
        from timba import db
        total_pnl = db.get_total_pnl()
        return {
            "portfolio": self.portfolio,
            "cash": self.cash,
            "pending_redemption": self.pending_redemption,
            "total_pnl": total_pnl,
            "started_at": self.started_at,
        }
