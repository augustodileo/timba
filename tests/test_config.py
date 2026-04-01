import pytest

from timba.config import Config, parse_token_data
from timba.schema import ConfigValidationError


class TestConfig:
    def test_load_from_yaml(self, sample_config_yaml):
        cfg = Config.load(sample_config_yaml)
        assert cfg.log_level == "INFO"
        assert cfg.calculate_portfolio() > 0
        fav = cfg.get_strategy("favorite")
        assert len(fav.markets) >= 1

    def test_defaults(self, tmp_path):
        cfg_file = tmp_path / "empty.yaml"
        cfg_file.write_text("{}")
        cfg = Config.load(cfg_file)
        assert cfg.log_level == "INFO"
        # No markets configured -> portfolio is 0
        assert cfg.calculate_portfolio() == 0

    def test_partial_strategy(self, tmp_path):
        cfg_file = tmp_path / "partial.yaml"
        cfg_file.write_text("favorite:\n  min_price: 0.90\n")
        cfg = Config.load(cfg_file)
        fav = cfg.get_strategy("favorite")
        assert fav.min_price == 0.90
        # Missing keys return None (strategy handles its own defaults)
        assert fav.contracts_per_trade is None

    def test_configurable_timeouts(self, tmp_path):
        cfg_file = tmp_path / "timeouts.yaml"
        cfg_file.write_text("favorite:\n  resolve_delay_sec: 60\n")
        cfg = Config.load(cfg_file)
        fav = cfg.get_strategy("favorite")
        assert fav.resolve_delay_sec == 60

    def test_validation_rejects_unknown_key(self, tmp_path):
        cfg_file = tmp_path / "bad.yaml"
        cfg_file.write_text("favorite:\n  typo_key: 123\n")
        with pytest.raises(ConfigValidationError):
            Config.load(cfg_file)

    def test_validation_rejects_bad_coin(self, tmp_path):
        cfg_file = tmp_path / "bad_coin.yaml"
        cfg_file.write_text("""
favorite:
  markets:
    - coin: bitcoin
      interval: 5m
      entry_window_sec: 10
      close_window_sec: 3
""")
        with pytest.raises(ConfigValidationError):
            Config.load(cfg_file)

    def test_validation_rejects_bad_interval(self, tmp_path):
        cfg_file = tmp_path / "bad_interval.yaml"
        cfg_file.write_text("""
favorite:
  markets:
    - coin: btc
      interval: 10m
      entry_window_sec: 10
      close_window_sec: 3
""")
        with pytest.raises(ConfigValidationError):
            Config.load(cfg_file)

    def test_validation_rejects_bad_mode(self, tmp_path):
        cfg_file = tmp_path / "bad_mode.yaml"
        cfg_file.write_text("""
favorite:
  markets:
    - coin: btc
      interval: 5m
      mode: yolo
      entry_window_sec: 10
      close_window_sec: 3
""")
        with pytest.raises(ConfigValidationError):
            Config.load(cfg_file)

    def test_validation_rejects_wrong_type(self, tmp_path):
        cfg_file = tmp_path / "bad_type.yaml"
        cfg_file.write_text('favorite:\n  enabled: "yes"\n')
        with pytest.raises(ConfigValidationError):
            Config.load(cfg_file)

    def test_validation_rejects_unknown_strategy(self, tmp_path):
        cfg_file = tmp_path / "bad_strat.yaml"
        cfg_file.write_text("mystery_strat:\n  enabled: true\n")
        with pytest.raises(ConfigValidationError):
            Config.load(cfg_file)

    def test_validation_rejects_out_of_range(self, tmp_path):
        cfg_file = tmp_path / "bad_range.yaml"
        cfg_file.write_text("favorite:\n  min_price: 1.5\n")
        with pytest.raises(ConfigValidationError):
            Config.load(cfg_file)

    def test_validation_rejects_missing_required(self, tmp_path):
        cfg_file = tmp_path / "missing_req.yaml"
        cfg_file.write_text("""
favorite:
  markets:
    - coin: btc
      interval: 5m
""")
        with pytest.raises(ConfigValidationError):
            Config.load(cfg_file)

    def test_validate_false_skips_schema(self, tmp_path):
        cfg_file = tmp_path / "skip.yaml"
        cfg_file.write_text("favorite:\n  typo_key: 123\n")
        cfg = Config.load(cfg_file, validate=False)
        fav = cfg.get_strategy("favorite")
        assert fav is not None


class TestSchemaEdgeCases:
    def test_validate_returns_empty_when_jsonschema_missing(self, monkeypatch):
        import timba.schema as schema_mod
        schema_mod.reset_cache()
        original = schema_mod.jsonschema
        schema_mod.jsonschema = None
        try:
            from timba.schema import validate_config
            errors = validate_config({"unknown_key": True})
            assert errors == []
        finally:
            schema_mod.jsonschema = original
            schema_mod.reset_cache()

    def test_reset_cache_clears_built_schema(self):
        import timba.schema as schema_mod
        from timba.schema import _build_schema, reset_cache
        _build_schema()
        assert schema_mod._built_cache is not None
        reset_cache()
        assert schema_mod._built_cache is None

    def test_build_schema_handles_discovery_failure(self, monkeypatch):
        from timba.schema import _build_schema, reset_cache
        reset_cache()
        monkeypatch.setattr("timba.strategies.load_strategies", lambda: (_ for _ in ()).throw(ImportError("test")))
        try:
            schema = _build_schema()
            # Should still have base properties
            assert "properties" in schema
        finally:
            reset_cache()


class TestParseTokenData:
    def test_parse_json_string(self):
        result = parse_token_data('["token1", "token2"]')
        assert result == ["token1", "token2"]

    def test_parse_list(self):
        result = parse_token_data(["token1", "token2"])
        assert result == ["token1", "token2"]

    def test_parse_numeric_string(self):
        result = parse_token_data("[0.55, 0.45]")
        assert result == [0.55, 0.45]
