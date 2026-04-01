"""Startup reconciliation: sync in-memory state with CLOB reality.

Called once between ID seeding and Trader creation. Handles:
1. Query CLOB for open orders and log them
2. Cancel any found orders (replaces blind cancel_all in Trader.__init__)
3. Use CLOB USDC balance as authoritative cash
4. Reset reserved_cash to 0
"""

import logging

logger = logging.getLogger(__name__)


def reconcile_startup(clob_client, state) -> dict:
    """Reconcile local state with CLOB reality. Returns summary dict.

    Args:
        clob_client: authenticated PolymarketClobClient
        state: State instance (will be mutated and saved)

    Returns:
        dict with keys: open_orders_found, orders_cancelled,
                        clob_balance, old_cash, cash_delta
    """
    summary = {
        "open_orders_found": 0,
        "orders_cancelled": False,
        "clob_balance": 0.0,
        "old_cash": state.cash,
        "cash_delta": 0.0,
    }

    # ── Step 1: Query open orders ──
    try:
        orders = clob_client.get_orders()
        summary["open_orders_found"] = len(orders)
        if orders:
            logger.warning(
                "Reconcile: found %d open orders from previous session",
                len(orders),
            )
            for order in orders:
                logger.info(
                    "  Order %s: %s @ $%.4f (matched: %s/%s)",
                    getattr(order, "order_id", "?"),
                    getattr(order, "side", "?"),
                    float(getattr(order, "price", 0)),
                    getattr(order, "size_matched", 0),
                    getattr(order, "original_size", getattr(order, "size", "?")),
                )
    except Exception:
        logger.exception("Reconcile: failed to query open orders")

    # ── Step 2: Cancel open orders ──
    if summary["open_orders_found"] > 0:
        try:
            clob_client.cancel_all()
            summary["orders_cancelled"] = True
            logger.info("Reconcile: cancelled %d open orders", summary["open_orders_found"])
        except Exception:
            logger.exception("Reconcile: failed to cancel open orders")
    else:
        logger.info("Reconcile: no open orders found")

    # ── Step 3: Sync cash from CLOB balance ──
    try:
        usdc = clob_client.get_usdc_balance()
        summary["clob_balance"] = usdc
        summary["cash_delta"] = usdc - state.cash

        if abs(summary["cash_delta"]) > 0.01:
            logger.warning(
                "Reconcile: cash mismatch — CLOB=$%.2f local=$%.2f delta=$%+.2f",
                usdc, state.cash, summary["cash_delta"],
            )

        # CLOB balance is authoritative — overwrite local cash
        state.cash = usdc
        logger.info("Reconcile: cash set to CLOB balance $%.2f", usdc)
    except Exception:
        logger.exception(
            "Reconcile: failed to get CLOB balance, keeping local cash=$%.2f",
            state.cash,
        )

    # ── Step 4: Reset reserved_cash ──
    state.reserved_cash = 0.0

    logger.info(
        "Reconcile complete: orders=%d cancelled=%s balance=$%.2f delta=$%+.2f",
        summary["open_orders_found"],
        summary["orders_cancelled"],
        summary["clob_balance"],
        summary["cash_delta"],
    )

    return summary
