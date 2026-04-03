"""Tests for schema validation — covering minimum, maximum, pattern, and else branches."""

import pytest

import timba.schema as schema_mod
from timba.schema import reset_cache, validate_config


@pytest.fixture(autouse=True)
def _clear_schema_cache():
    """Ensure a fresh schema for every test."""
    reset_cache()
    yield
    reset_cache()


class TestValidateMinimum:
    def test_minimum_violation_reported(self):
        """Trigger the 'minimum' validator branch (line 142)."""
        errors = validate_config({
            "favorite": {
                "markets": [
                    {
                        "coin": "btc",
                        "interval": "5m",
                        "entry_window_sec": -1,
                        "close_window_sec": 3,
                    }
                ]
            }
        })
        minimum_errors = [e for e in errors if "less than minimum" in e]
        assert len(minimum_errors) >= 1


class TestValidateMaximum:
    def test_maximum_violation_reported(self):
        """Trigger the 'maximum' validator branch (line 143-144)."""
        errors = validate_config({
            "polymarket": {
                "max_workers": 999,
            }
        })
        maximum_errors = [e for e in errors if "greater than maximum" in e]
        assert len(maximum_errors) >= 1


class TestValidatePattern:
    def test_pattern_violation_reported(self):
        """Trigger the 'pattern' validator branch (lines 147-148).

        We need to inject a pattern constraint into the schema to test this.
        We do it by temporarily modifying the base schema cache.
        """
        import copy

        # Build the schema first so we can modify it
        from timba.schema import _build_schema
        schema = _build_schema()
        reset_cache()

        # Inject a pattern field into the schema for testing
        modified = copy.deepcopy(schema)
        modified["properties"]["log_level"] = {
            "type": "string",
            "pattern": "^[A-Z]+$",
        }

        # Monkey-patch _build_schema to return our modified schema
        original_build = schema_mod._build_schema

        def patched_build():
            return modified

        schema_mod._build_schema = patched_build
        try:
            errors = validate_config({"log_level": "not-uppercase-123"})
            pattern_errors = [e for e in errors if "does not match pattern" in e]
            assert len(pattern_errors) >= 1
        finally:
            schema_mod._build_schema = original_build


class TestValidateElseBranch:
    def test_else_branch_for_unknown_validator(self):
        """Trigger the else branch (lines 149-150) for an unhandled validator type.

        We use exclusiveMinimum which is not specifically handled.
        """
        errors = validate_config({
            "favorite": {
                "min_price": 0,
            }
        })
        # exclusiveMinimum: 0 means value must be > 0, so 0 triggers it.
        # This falls through to the 'else' branch since 'exclusiveMinimum'
        # is not one of the specifically handled validators.
        assert len(errors) >= 1


class TestJsonschemaImportFallback:
    def test_jsonschema_none_returns_empty(self):
        """When jsonschema is not installed, validate_config returns []."""
        original = schema_mod.jsonschema
        schema_mod.jsonschema = None
        try:
            errors = validate_config({"unknown_key": True})
            assert errors == []
        finally:
            schema_mod.jsonschema = original

    def test_import_fallback_when_jsonschema_missing(self):
        """Reload schema module with jsonschema import blocked to cover lines 17-18."""
        import importlib
        import sys

        original_js = sys.modules.get("jsonschema")
        original_schema = sys.modules.get("timba.schema")

        # Block jsonschema import
        sys.modules["jsonschema"] = None  # type: ignore[assignment]
        if "timba.schema" in sys.modules:
            del sys.modules["timba.schema"]

        try:
            import timba.schema as reloaded
            assert reloaded.jsonschema is None
            # validate_config should return [] when jsonschema is None
            errors = reloaded.validate_config({"bad_key": True})
            assert errors == []
        finally:
            if original_js is not None:
                sys.modules["jsonschema"] = original_js
            elif "jsonschema" in sys.modules:
                del sys.modules["jsonschema"]
            if original_schema is not None:
                sys.modules["timba.schema"] = original_schema
            elif "timba.schema" in sys.modules:
                del sys.modules["timba.schema"]
            importlib.import_module("timba.schema")
