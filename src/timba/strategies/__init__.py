"""Strategy framework: abstract base class + registry.

Each strategy is a Python file in this package. The file name IS the strategy name
and matches the config key. E.g., strategies/favorite.py → config key "favorite".

To add a new strategy:
  1. Create strategies/mystrat.py
  2. Define class MyStrategy(Strategy) with name = "mystrat"
  3. Add mystrat: section to config.yaml with enabled: true and markets: [...]
  4. Done — the trader discovers and runs it automatically.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from timba.base import MarketPosition
from timba.base import PositionState as PositionState
from timba.feed import DirectionSignal
from timba.market import UpDownMarket


@dataclass
class TickData:
    """Market observation for one tick. Pure market data — no strategy context."""
    tick_id: int
    ts: float           # unix timestamp of this tick
    signal: DirectionSignal
    mid_up: float
    mid_down: float
    fill_up: float
    fill_down: float
    size_up: int
    size_down: int
    tick_size: float = 0.01


@dataclass
class BetDecision:
    """Result of strategy.evaluate(). Tells the trader what to do."""
    should_bet: bool = False
    side: str = ""           # "up" or "down"
    price: float = 0.0       # fill price for the order
    size: int = 0            # number of contracts
    reason: str = ""         # why we bet or didn't (for skip_reason)
    computed: dict | None = None  # strategy-specific computed values (EVs, signals, etc.)


class Strategy(ABC):
    """Base class for all trading strategies.

    Implement the 5 abstract methods. Everything else (discovery, tick recording,
    order placement, cash management, resolution, redemption) is handled by the
    trader using shared infrastructure in base.py.

    The strategy name MUST match the config key and the filename:
      strategies/favorite.py → name = "favorite" → config key "favorite"
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy name. Must match config key and filename."""

    @abstractmethod
    def create_position(
        self,
        market: UpDownMarket,
        market_cfg: dict,
        global_cfg: object,
    ) -> MarketPosition:
        """Create a position for a discovered market.

        market_cfg: per-market dict from your config's markets[] list.
        global_cfg: your strategy's config object (e.g., BondingConfig).
        Return None to skip this market (e.g., missing required params).
        """

    @abstractmethod
    def evaluate(
        self,
        pos: MarketPosition,
        tick: TickData,
    ) -> BetDecision:
        """Your strategy logic. Called every tick during the entry window.

        Return BetDecision(should_bet=True, side, price, size) to bet.
        Return BetDecision(should_bet=False, reason=...) to wait.

        The trader handles: window checks, liquidity, order placement, cash.
        You handle: signal interpretation, EV computation, decision logic.

        You should also call write_strategy_data() here to record your
        computed values (EVs, signals, etc.) to your strategy's subdirectory.
        """

    @abstractmethod
    def on_bet(self, pos: MarketPosition, decision: BetDecision) -> None:
        """Called after a bet is placed (or paper-recorded). Update position state.

        Use this to store decision-time values on the position
        (e.g., ev_at_decision, p_win, window_progress).
        """

    @abstractmethod
    def resolve(self, pos: MarketPosition, won: bool) -> None:
        """Calculate PnL and log outcome. Called after resolve_winner determines winner.

        Mutate pos.pnl. The trader handles state transitions (WON/LOST/etc.)
        and trade recording.
        """

    @abstractmethod
    def extra_fields(self, pos: MarketPosition) -> dict:
        """Strategy-specific fields to include in the trade record."""

    @classmethod
    def config_schema(cls) -> dict:
        """Declare config fields for schema validation.

        Override to declare strategy-specific config. Return a dict with:

          "strategy": {                          # global strategy-level fields
              "contracts_per_trade": {"type": "integer", "minimum": 5},
              ...
          },
          "market": {                            # per-market fields
              "required": ["change_cap_pct"],    # extra required fields beyond common
              "properties": {
                  "change_cap_pct": {"type": "number", "minimum": 0, "maximum": 1},
                  ...
              }
          }

        Common fields added automatically:
          - Strategy level: enabled (bool), markets (array)
          - Market level: coin, interval, mode, entry_window_sec, close_window_sec
        """
        return {}


# ── Strategy registry ──

_registry: dict[str, Strategy] = {}


def register(strategy: Strategy) -> None:
    """Register a strategy instance by name."""
    _registry[strategy.name] = strategy


def get(name: str) -> Strategy | None:
    """Get a registered strategy by name."""
    return _registry.get(name)


def get_all() -> dict[str, Strategy]:
    """Get all registered strategies."""
    return dict(_registry)


def load_strategies() -> None:
    """Auto-discover and import all strategy modules in this package.

    Scans strategies/ for .py files (excluding __init__), imports each one.
    Each module calls register() at module level, which adds it to _registry.
    """
    import importlib
    from pathlib import Path

    pkg_dir = Path(__file__).parent
    for f in sorted(pkg_dir.glob("*.py")):
        if f.name.startswith("_"):
            continue
        module_name = f.stem
        try:
            importlib.import_module(f"timba.strategies.{module_name}")
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Failed to load strategy '%s': %s", module_name, e)
