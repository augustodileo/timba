"""Favorite strategy: buy the near-certain side in the final seconds.

Dead simple:
  - Last 10s to 3s before close
  - Buy whichever side has midpoint >= min_price (e.g. $0.95)
  - No EV formula, no signal — just price + timing
  - Profit = $1.00 - price per contract if we're right
  - Risk = price per contract if we're wrong (rare at 95%+)
"""


def _cfg(val: object, default: object) -> object:
    """Return val if not None, else default. Unlike `or`, respects 0 and 0.0."""
    return val if val is not None else default

import logging
from dataclasses import dataclass

from timba.base import MarketPosition, PositionState
from timba.market import UpDownMarket
from timba.strategies import BetDecision, Strategy, TickData, register

logger = logging.getLogger(__name__)


@dataclass
class FavoritePosition(MarketPosition):
    """Favorite-specific position."""
    min_price: float = -1.0  # must be set from config
    min_signal_chg: float = -1.0  # must be set from config


class FavoriteStrategy(Strategy):

    @property
    def name(self) -> str:
        return "favorite"

    @classmethod
    def config_schema(cls) -> dict:
        return {
            "strategy": {
                "min_price": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
                "min_signal_chg": {"type": "number", "exclusiveMinimum": 0, "maximum": 5},
                "contracts_per_trade": {"type": "integer", "minimum": 1},
                "resolve_delay_sec": {"type": "integer", "minimum": 1},
            },
            "market": {
                "properties": {
                    "min_price": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
                    "min_signal_chg": {"type": "number", "exclusiveMinimum": 0, "maximum": 5},
                },
            },
        }

    def create_position(self, market: UpDownMarket, market_cfg: dict, global_cfg: object) -> FavoritePosition | None:
        entry_window = market_cfg.get("entry_window_sec")
        close_window = market_cfg.get("close_window_sec")
        if entry_window is None or close_window is None:
            return None

        try:
            window_start = int(market.slug.rsplit("-", 1)[1])
        except (ValueError, IndexError):
            window_start = market.end_timestamp - 300

        return FavoritePosition(
            condition_id=market.condition_id,
            question=market.question,
            slug=market.slug,
            coin=market.coin,
            interval=market.interval,
            token_id_up=market.token_id_up,
            token_id_down=market.token_id_down,
            end_timestamp=market.end_timestamp,
            window_start_ts=window_start,
            contracts=_cfg(global_cfg.get("contracts_per_trade"), 5),
            entry_window_sec=entry_window,
            close_window_sec=close_window,
            min_price=_cfg(market_cfg.get("min_price"), global_cfg.get("min_price")),
            min_signal_chg=_cfg(market_cfg.get("min_signal_chg"), global_cfg.get("min_signal_chg")),
            market_mode=market_cfg.get("mode", "live"),
            resolve_delay_sec=_cfg(global_cfg.get("resolve_delay_sec"), 30),
            liquidity=market.liquidity,
            tick_size=market.tick_size,
        )

    def evaluate(self, pos: FavoritePosition, tick: TickData) -> BetDecision:
        already_bet = pos.state in (PositionState.PAPER, PositionState.SNIPED)
        pos.ticks_evaluated += 1

        # EV for both sides: simply P(win) - fill_price
        remaining = pos.remaining_at(tick.ts)
        progress = pos.progress_at(tick.ts)

        # EV = profit if we win (1.0 - fill). Only for sides above min_price.
        # This is NOT a probability-weighted EV — it's just the potential profit.
        # The bet decision is purely: is the midpoint above the threshold?
        ev_up = (1.0 - tick.fill_up) if tick.mid_up >= pos.min_price else 0.0
        ev_down = (1.0 - tick.fill_down) if tick.mid_down >= pos.min_price else 0.0

        # Pick the side with higher midpoint (more likely to win)
        if tick.mid_up >= tick.mid_down:
            side, ev, mid, fill, _size = "up", ev_up, tick.mid_up, tick.fill_up, tick.size_up
        else:
            side, ev, mid, fill, _size = "down", ev_down, tick.mid_down, tick.fill_down, tick.size_down

        computed = {
            "tick_id": tick.tick_id,
            "remaining": round(remaining, 1),
            "progress": round(progress, 3),
            "ev_up": round(ev_up, 6),
            "ev_down": round(ev_down, 6),
            "p_up": round(tick.mid_up, 4),
            "p_down": round(tick.mid_down, 4),
        }

        if already_bet:
            return BetDecision(should_bet=False, reason="already bet", computed=computed)

        # Always set side to leading midpoint — even when not betting.
        # This lets resolution track W/L for skipped trades.
        pos.side = side
        pos.midpoint = mid
        pos.buy_price = fill

        if ev <= 0:
            return BetDecision(
                should_bet=False,
                reason=f"no favorite (up=${tick.mid_up:.4f} down=${tick.mid_down:.4f} min=${pos.min_price})",
                computed=computed,
            )

        if abs(tick.signal.change_pct) < pos.min_signal_chg:
            return BetDecision(
                should_bet=False,
                reason=f"weak signal ({abs(tick.signal.change_pct):.4f}% < {pos.min_signal_chg}%)",
                computed=computed,
            )

        logger.debug(
            "FAVORITE %s | %s @$%.4f | EV=%+.4f | %.0fs left",
            pos.label, side.upper(), fill, ev, remaining,
        )

        return BetDecision(
            should_bet=True,
            side=side,
            price=fill,
            size=pos.contracts,
            reason=f"favorite {side}@${fill:.4f} EV={ev:+.4f}",
            computed=computed,
        )

    def on_bet(self, pos: FavoritePosition, decision: BetDecision) -> None:
        logger.info(
            "FAVORITE %s | %s @$%.4f | profit=$%.4f if win",
            pos.label, decision.side.upper(), decision.price, 1.0 - decision.price,
        )

    def resolve(self, pos: FavoritePosition, won: bool) -> None:
        state = pos.state
        if state == PositionState.SNIPED:
            tag = "LIVE"
        elif state == PositionState.PAPER:
            tag = "PAPER"
        elif state == PositionState.FAILED:
            tag = "FAIL"
        else:
            tag = "SKIP"

        result = "WIN" if won else "LOSS"
        if won:
            pos.pnl = (1.0 - pos.buy_price) * pos.contracts
        else:
            pos.pnl = -pos.buy_price * pos.contracts

        logger.info("%s %s %s | %s @$%.4f | %s$%.4f",
                    tag, result, pos.label, pos.side.upper(), pos.buy_price,
                    "+" if won else "-", abs(pos.pnl))

    def extra_fields(self, pos: FavoritePosition) -> dict:
        return {
            "min_price": pos.min_price,
            "midpoint": pos.midpoint,
        }


register(FavoriteStrategy())
