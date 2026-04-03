"""Configuration: YAML loading, strategy config discovery, infrastructure settings.

Strategy configs are auto-discovered from YAML top-level keys that match
registered strategy names. Each strategy section has:
  - enabled: bool
  - markets: list of per-market dicts
  - any strategy-specific global defaults

Non-strategy top-level keys: log_level, polymarket.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from timba.schema import ConfigValidationError, validate_config

# Keys in YAML that are NOT strategy configs
_RESERVED_KEYS = frozenset({
    "log_level", "polymarket",
})


class StrategyConfig:
    """Generic strategy config — just a dict wrapper with attribute access.

    Loaded directly from the YAML section matching the strategy name.
    Each strategy knows its own expected keys.
    """
    def __init__(self, raw: dict | None = None) -> None:
        self._raw = raw or {}

    @property
    def enabled(self) -> bool:
        return self._raw.get("enabled", False)

    @property
    def markets(self) -> list[dict]:
        return self._raw.get("markets", [])

    def get(self, key: str, default: object = None) -> object:
        return self._raw.get(key, default)

    def __getattr__(self, name: str) -> object:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._raw.get(name)

    def __repr__(self) -> str:
        return f"StrategyConfig({self._raw})"


@dataclass
class PolymarketConfig:
    private_key: str = ""
    funder: str = ""
    signature_type: int = 2
    relayer_api_key: str = ""
    relayer_api_key_address: str = ""




class Config:
    """Bot configuration. Strategy sections auto-discovered from YAML keys."""

    def __init__(self) -> None:
        self.log_level: str = "INFO"
        self.max_workers: int = 10
        self.polymarket: PolymarketConfig = PolymarketConfig()
        # Strategy configs keyed by name — auto-populated from YAML
        self.strategies: dict[str, StrategyConfig] = {}

    @classmethod
    def load(cls, path: str | Path, *, validate: bool = True) -> "Config":
        path = Path(path)
        with open(path) as f:
            raw = yaml.safe_load(f) or {}

        if validate:
            errors = validate_config(raw)
            if errors:
                raise ConfigValidationError(errors)

        cfg = cls()
        cfg.log_level = raw.get("log_level", "INFO").upper()
        poly_raw = raw.get("polymarket", {})
        cfg.max_workers = int(poly_raw.get("max_workers", 10) if poly_raw else 10)

        # Sensitive values from environment variables only
        cfg.polymarket = PolymarketConfig(
            private_key=os.environ.get("POLYMARKET_PRIVATE_KEY", ""),
            funder=os.environ.get("POLYMARKET_FUNDER", ""),
            relayer_api_key=os.environ.get("RELAYER_API_KEY", ""),
            relayer_api_key_address=os.environ.get("RELAYER_API_KEY_ADDRESS", ""),
        )
        # Auto-discover strategy configs: any top-level key not in _RESERVED_KEYS
        # that has a dict value with "markets" or "enabled" is a strategy config
        for key, value in raw.items():
            if key in _RESERVED_KEYS:
                continue
            if isinstance(value, dict):
                cfg.strategies[key] = StrategyConfig(value)

        return cfg

    def get_strategy(self, name: str) -> StrategyConfig:
        """Get a strategy config by name. Returns disabled empty config if not found."""
        return self.strategies.get(name, StrategyConfig())

    # ── Derived from all strategies ──

    def get_discovery_markets(self) -> list[tuple[str, str]]:
        """Deduplicated (coin, interval) pairs from all enabled strategies."""
        seen = set()
        result = []
        for scfg in self.strategies.values():
            if not scfg.enabled:
                continue
            for m in scfg.markets:
                key = (m["coin"], m["interval"])
                if key not in seen:
                    seen.add(key)
                    result.append(key)
        return result

    def get_all_intervals(self) -> list[str]:
        intervals = set()
        for scfg in self.strategies.values():
            if not scfg.enabled:
                continue
            for m in scfg.markets:
                intervals.add(m["interval"])
        return list(intervals)

    def get_all_coins(self) -> list[str]:
        coins = set()
        for scfg in self.strategies.values():
            if not scfg.enabled:
                continue
            for m in scfg.markets:
                coins.add(m["coin"])
        return list(coins)

    def calculate_portfolio(self) -> float:
        """Estimate capital needed based on configured live markets."""
        from timba.constants import AVG_BUY_PRICE, BANKROLL_BUFFER, CONCURRENT_PER_INTERVAL
        total = 0.0
        for scfg in self.strategies.values():
            if not scfg.enabled:
                continue
            contracts = scfg.get("contracts_per_trade")
            if contracts is None:
                contracts = 200
            by_interval: dict[str, int] = {}
            for m in scfg.markets:
                if m.get("mode", "live") != "live":
                    continue
                iv = m["interval"]
                by_interval[iv] = by_interval.get(iv, 0) + 1
            for iv, num_coins in by_interval.items():
                concurrent = CONCURRENT_PER_INTERVAL.get(iv, 1)
                total += num_coins * concurrent * contracts * AVG_BUY_PRICE
        return round(total * BANKROLL_BUFFER, 2)

    def any_strategy_enabled(self) -> bool:
        return any(s.enabled for s in self.strategies.values())

    def needs_feed(self) -> bool:
        return self.any_strategy_enabled()


def parse_token_data(data: str | list) -> list:
    """Parse outcome_prices or token_ids that may be JSON strings or lists."""
    if isinstance(data, str):
        return json.loads(data)
    return list(data)
