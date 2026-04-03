"""Config schema validation — dynamic JSON Schema built from strategy classes.

Loads the base schema from config.schema.yaml (global fields + shared $defs),
then discovers all strategies and injects their config_schema() declarations
as top-level properties. This means adding a new strategy with config_schema()
automatically gets validated — no manual schema edits needed.
"""

import copy
import logging
from pathlib import Path

import yaml

try:
    import jsonschema
except ImportError:
    jsonschema = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).parent / "config.schema.yaml"
_base_cache: dict | None = None
_built_cache: dict | None = None

# Common market fields added to every strategy's market items
_COMMON_MARKET_REQUIRED = ["coin", "interval", "entry_window_sec", "close_window_sec"]
_COMMON_MARKET_PROPERTIES = {
    "coin": {"$ref": "#/$defs/coin"},
    "interval": {"$ref": "#/$defs/interval"},
    "mode": {"$ref": "#/$defs/mode"},
    "entry_window_sec": {"type": "number", "minimum": 0},
    "close_window_sec": {"type": "number", "minimum": 0},
}


class ConfigValidationError(Exception):
    """Raised when config.yaml fails schema validation."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        msg = f"Config validation failed ({len(errors)} error{'s' if len(errors) != 1 else ''}):\n"
        msg += "\n".join(f"  - {e}" for e in errors)
        super().__init__(msg)


def _load_base_schema() -> dict:
    global _base_cache
    if _base_cache is None:
        with open(_SCHEMA_PATH) as f:
            _base_cache = yaml.safe_load(f)
    return _base_cache


def _build_strategy_schema(strat: object) -> dict:
    """Build JSON Schema for one strategy from its config_schema() declaration."""
    decl = strat.config_schema()

    # Strategy-level properties (enabled + strategy-specific globals + markets)
    strat_props: dict = {"enabled": {"type": "boolean"}}
    strat_props.update(decl.get("strategy", {}))

    # Market item: common fields + strategy-specific fields
    market_props = dict(_COMMON_MARKET_PROPERTIES)
    market_required = list(_COMMON_MARKET_REQUIRED)

    market_decl = decl.get("market", {})
    market_props.update(market_decl.get("properties", {}))
    market_required.extend(market_decl.get("required", []))

    strat_props["markets"] = {
        "type": "array",
        "items": {
            "type": "object",
            "required": market_required,
            "additionalProperties": False,
            "properties": market_props,
        },
    }

    return {
        "type": "object",
        "additionalProperties": False,
        "properties": strat_props,
    }


def _build_schema() -> dict:
    """Build the complete schema: base globals + dynamically discovered strategies."""
    global _built_cache
    if _built_cache is not None:
        return _built_cache

    schema = copy.deepcopy(_load_base_schema())

    # Discover strategies and inject their schemas
    try:
        from timba.strategies import get_all, load_strategies
        load_strategies()
        strategies = get_all()
    except Exception as e:
        logger.warning("Could not discover strategies for schema: %s", e)
        strategies = {}

    for name, strat in strategies.items():
        schema["properties"][name] = _build_strategy_schema(strat)

    # Block unknown top-level keys now that all strategies are registered
    schema["additionalProperties"] = False

    _built_cache = schema
    return schema


def validate_config(raw: dict) -> list[str]:
    """Validate raw config dict against the schema.

    Returns a list of human-readable error strings. Empty list = valid.
    Silently returns [] if jsonschema is not installed.
    """
    if jsonschema is None:
        logger.debug("jsonschema not installed — skipping config validation")
        return []

    schema = _build_schema()
    validator = jsonschema.Draft202012Validator(schema)

    errors = []
    for error in sorted(validator.iter_errors(raw), key=lambda e: list(e.path)):
        path = ".".join(str(p) for p in error.absolute_path)
        prefix = f"{path}: " if path else ""

        # Simplify common error messages
        if error.validator == "additionalProperties":
            errors.append(f"{prefix}{error.message}")
        elif error.validator == "enum":
            allowed = ", ".join(repr(v) for v in error.validator_value)
            errors.append(f"{prefix}{error.instance!r} is not valid (allowed: {allowed})")
        elif error.validator == "type":
            errors.append(f"{prefix}expected {error.validator_value}, got {type(error.instance).__name__}")
        elif error.validator == "minimum":
            errors.append(f"{prefix}{error.instance} is less than minimum {error.validator_value}")
        elif error.validator == "maximum":
            errors.append(f"{prefix}{error.instance} is greater than maximum {error.validator_value}")
        elif error.validator == "required":
            errors.append(f"{prefix}{error.message}")
        elif error.validator == "pattern":
            errors.append(f"{prefix}{error.instance!r} does not match pattern {error.validator_value!r}")
        else:
            errors.append(f"{prefix}{error.message}")

    return errors


def reset_cache() -> None:
    """Clear cached schemas. Useful for tests that modify strategy registry."""
    global _built_cache
    _built_cache = None
