import time
from unittest.mock import MagicMock, patch

from timba.market import (
    COIN_FULL_NAME,
    MarketSeries,
    UpDownMarket,
    _fetch_market_by_slug,
    _generate_hourly_slugs,
    _parse_hourly_slug_timestamp,
    _parse_updown_market,
    discover_active_markets,
)


class TestParseUpdownMarket:
    def _series(self):
        return MarketSeries("btc", "15m", 900)

    def test_valid_up_down(self):
        raw = {
            "conditionId": "0xabc",
            "question": "Bitcoin Up or Down - March 23, 7:00PM-7:15PM ET",
            "slug": "btc-updown-15m-1774400000",
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["tok_up", "tok_down"]',
            "outcomePrices": '["0.55", "0.45"]',
        }
        market = _parse_updown_market(raw, self._series())
        assert market is not None
        assert market.token_id_up == "tok_up"
        assert market.token_id_down == "tok_down"
        assert market.gamma_price_up == 0.55
        assert market.gamma_price_down == 0.45
        assert market.end_timestamp == 1774400000 + 900

    def test_reversed_outcome_order(self):
        raw = {
            "conditionId": "0xdef",
            "question": "BTC Up or Down",
            "slug": "btc-updown-15m-1774400000",
            "outcomes": '["Down", "Up"]',
            "clobTokenIds": '["tok_down", "tok_up"]',
            "outcomePrices": '["0.45", "0.55"]',
        }
        market = _parse_updown_market(raw, self._series())
        assert market is not None
        assert market.token_id_up == "tok_up"
        assert market.token_id_down == "tok_down"

    def test_rejects_yes_no(self):
        raw = {
            "conditionId": "0x",
            "question": "Will BTC hit $100k?",
            "slug": "btc-above-100k",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["t1", "t2"]',
            "outcomePrices": '["0.5", "0.5"]',
        }
        assert _parse_updown_market(raw, self._series()) is None

    def test_rejects_three_outcomes(self):
        raw = {
            "conditionId": "0x",
            "question": "?",
            "slug": "x",
            "outcomes": '["Up", "Down", "Flat"]',
            "clobTokenIds": '["t1", "t2", "t3"]',
        }
        assert _parse_updown_market(raw, self._series()) is None

    def test_outcomes_as_list(self):
        raw = {
            "conditionId": "0x",
            "question": "ETH Up or Down",
            "slug": "eth-updown-15m-1774400000",
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["tok_up", "tok_down"],
            "outcomePrices": [0.50, 0.50],
        }
        market = _parse_updown_market(raw, MarketSeries("eth", "15m", 900))
        assert market is not None


class TestFetchMarketBySlug:
    @patch("timba.market.requests.get")
    def test_returns_none_for_closed(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=[{"active": False, "closed": True}]),
        )
        mock_get.return_value.raise_for_status = MagicMock()
        result = _fetch_market_by_slug("slug", MarketSeries("btc", "15m", 900))
        assert result is None

    @patch("timba.market.requests.get")
    def test_returns_none_for_empty(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=[]),
        )
        mock_get.return_value.raise_for_status = MagicMock()
        result = _fetch_market_by_slug("slug", MarketSeries("btc", "15m", 900))
        assert result is None


class TestDiscoverActiveMarkets:
    @patch("timba.market._fetch_market_by_slug")
    def test_discovers_markets(self, mock_fetch):
        market = UpDownMarket(
            condition_id="0x1",
            question="BTC Up or Down",
            slug="btc-updown-15m-123",
            token_id_up="tu",
            token_id_down="td",
            end_timestamp=int(time.time()) + 900,
            coin="btc",
            interval="15m",
            gamma_price_up=0.5,
            gamma_price_down=0.5,
        )
        # Return the market for every slug query
        mock_fetch.return_value = market

        series = [MarketSeries("btc", "15m", 900)]
        results = discover_active_markets(series, look_ahead=1)
        assert len(results) >= 1

    @patch("timba.market._fetch_market_by_slug")
    def test_handles_fetch_errors(self, mock_fetch):
        import requests as req
        mock_fetch.side_effect = req.ConnectionError("timeout")
        results = discover_active_markets([MarketSeries("btc", "15m", 900)], look_ahead=1)
        assert results == []


# ── Hourly slugs, parsing, edge cases ──


class TestHourlySlugTimestamp:
    def test_parse_5pm(self):
        ts = _parse_hourly_slug_timestamp("bitcoin-up-or-down-march-23-2026-5pm-et")
        assert ts > 0
        import datetime as dt
        d = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
        assert d.hour == 21
        assert d.day == 23

    def test_parse_12pm(self):
        ts = _parse_hourly_slug_timestamp("bitcoin-up-or-down-march-23-2026-12pm-et")
        assert ts > 0
        import datetime as dt
        d = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
        assert d.hour == 16

    def test_parse_12am(self):
        ts = _parse_hourly_slug_timestamp("bitcoin-up-or-down-march-24-2026-12am-et")
        assert ts > 0
        import datetime as dt
        d = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
        assert d.hour == 4

    def test_parse_invalid(self):
        assert _parse_hourly_slug_timestamp("garbage") == 0

    def test_parse_am(self):
        ts = _parse_hourly_slug_timestamp("ethereum-up-or-down-march-23-2026-9am-et")
        assert ts > 0
        import datetime as dt
        d = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
        assert d.hour == 13


class TestGenerateHourlySlugs:
    def test_generates_slugs(self):
        slugs = _generate_hourly_slugs("btc", int(time.time()), 2)
        assert len(slugs) >= 3
        for s in slugs:
            assert "bitcoin-up-or-down" in s
            assert s.endswith("-et")

    def test_all_coins(self):
        for coin, full_name in COIN_FULL_NAME.items():
            slugs = _generate_hourly_slugs(coin, int(time.time()), 1)
            assert any(full_name in s for s in slugs)


class TestParseUpdownMarketHourly:
    def test_hourly_market(self):
        raw = {
            "conditionId": "0xabc",
            "question": "Bitcoin Up or Down - March 23, 5PM ET",
            "slug": "bitcoin-up-or-down-march-23-2026-5pm-et",
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["tok_up", "tok_down"]',
            "outcomePrices": '["0.55", "0.45"]',
            "active": True,
            "closed": False,
        }
        series = MarketSeries("btc", "1h", 3600)
        market = _parse_updown_market(raw, series)
        assert market is not None
        assert market.end_timestamp > 0
        assert market.coin == "btc"
        assert market.interval == "1h"

    def test_hourly_bad_slug(self):
        raw = {
            "conditionId": "0xabc",
            "question": "Bad",
            "slug": "bad-slug",
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["t1", "t2"]',
            "outcomePrices": '["0.5", "0.5"]',
            "active": True,
            "closed": False,
        }
        series = MarketSeries("btc", "1h", 3600)
        market = _parse_updown_market(raw, series)
        assert market is not None
        assert market.end_timestamp == 0


class TestFetchMarketBySlugExtra:
    @patch("timba.market.requests.get")
    def test_http_error(self, mock_get):
        resp = MagicMock()
        resp.raise_for_status.side_effect = Exception("500")
        mock_get.return_value = resp
        try:
            result = _fetch_market_by_slug("test", MarketSeries("btc", "5m", 300))
        except Exception:
            result = None
        assert result is None

    @patch("timba.market.requests.get")
    def test_active_market_returned(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=[{
                "conditionId": "0x1",
                "question": "BTC Up or Down",
                "slug": "btc-updown-5m-1000000",
                "outcomes": '["Up", "Down"]',
                "clobTokenIds": '["tu", "td"]',
                "outcomePrices": '["0.5", "0.5"]',
                "active": True,
                "closed": False,
            }]),
        )
        mock_get.return_value.raise_for_status = MagicMock()
        result = _fetch_market_by_slug("btc-updown-5m-1000000", MarketSeries("btc", "5m", 300))
        assert result is not None


class TestDiscoverWithHourly:
    @patch("timba.market._fetch_market_by_slug")
    def test_discovers_hourly(self, mock_fetch):
        market = UpDownMarket(
            condition_id="0x1", question="Bitcoin Up or Down - March 23, 5PM ET",
            slug="bitcoin-up-or-down-march-23-2026-5pm-et",
            token_id_up="tu", token_id_down="td",
            end_timestamp=int(time.time()) + 3600,
            coin="btc", interval="1h",
            gamma_price_up=0.5, gamma_price_down=0.5,
        )
        mock_fetch.return_value = market
        results = discover_active_markets([MarketSeries("btc", "1h", 3600)], look_ahead=1)
        assert len(results) >= 1


# ── Coverage gap tests ──


class TestParseSlug:
    """Test parse_slug function (lines 42-49)."""

    def test_5m_slug(self):
        from timba.market import parse_slug
        assert parse_slug("btc-updown-5m-1774609200") == ("btc", "5m")

    def test_15m_slug(self):
        from timba.market import parse_slug
        assert parse_slug("eth-updown-15m-1774400000") == ("eth", "15m")

    def test_hourly_slug(self):
        from timba.market import parse_slug
        assert parse_slug("bitcoin-up-or-down-march-23-2026-5pm-et") == ("btc", "1h")

    def test_hourly_slug_unknown_coin(self):
        from timba.market import parse_slug
        coin, interval = parse_slug("newcoin-up-or-down-march-23-2026-5pm-et")
        assert interval == "1h"
        assert coin == "newcoin"  # falls back to full_name

    def test_unknown_slug(self):
        from timba.market import parse_slug
        assert parse_slug("unknown-slug") == ("", "")


class TestDiscoverDefaultSeriesList:
    """Test discover_active_markets with default series_list (line 129)."""

    @patch("timba.market._fetch_market_by_slug")
    @patch("timba.market._generate_hourly_slugs")
    def test_uses_default_series_when_none(self, mock_hourly, mock_fetch):
        """When series_list is None, uses DEFAULT_SERIES (line 128-129)."""
        mock_fetch.return_value = None
        mock_hourly.return_value = []
        # Call with series_list=None
        results = discover_active_markets(series_list=None, look_ahead=0)
        assert results == []
        # The function should have iterated over DEFAULT_SERIES
        # _generate_hourly_slugs should have been called for hourly series
        assert mock_hourly.call_count > 0


class TestDiscoverKnownSlugs:
    """Test discover_active_markets with known_slugs (lines 151-152)."""

    @patch("timba.market._fetch_market_by_slug")
    def test_skips_known_slugs(self, mock_fetch):
        """Known slugs are skipped without HTTP call (lines 150-152)."""
        mock_fetch.return_value = None
        now = int(time.time())
        series = [MarketSeries("btc", "5m", 300)]
        base_ts = now - (now % 300)

        # Create known slugs that match what discover would generate
        known = set()
        for offset in range(-300, 300 * 2, 300):
            known.add(f"btc-updown-5m-{base_ts + offset}")

        results = discover_active_markets(series, look_ahead=0, known_slugs=known)
        assert results == []
        # _fetch_market_by_slug should NOT be called for known slugs
        mock_fetch.assert_not_called()


class TestParseUpdownMarketEdgeCases:
    """Edge cases in _parse_updown_market (lines 265, 288-289)."""

    def test_rejects_single_token(self):
        """Two outcomes but only one token → returns None (line 264-265)."""
        raw = {
            "conditionId": "0x1",
            "question": "BTC Up or Down",
            "slug": "btc-updown-5m-100",
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["only_one_token"]',
            "outcomePrices": '["0.5", "0.5"]',
        }
        result = _parse_updown_market(raw, MarketSeries("btc", "5m", 300))
        assert result is None

    def test_rejects_three_tokens(self):
        """Two outcomes but three tokens → returns None (line 264-265)."""
        raw = {
            "conditionId": "0x1",
            "question": "BTC Up or Down",
            "slug": "btc-updown-5m-100",
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["t1", "t2", "t3"]',
            "outcomePrices": '["0.5", "0.5"]',
        }
        result = _parse_updown_market(raw, MarketSeries("btc", "5m", 300))
        assert result is None

    def test_slug_without_timestamp_returns_end_ts_zero(self):
        """Slug that can't be parsed for timestamp → end_ts=0 (lines 288-289)."""
        raw = {
            "conditionId": "0x1",
            "question": "BTC Up or Down",
            "slug": "btc-updown-5m-notanumber",
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["t1", "t2"]',
            "outcomePrices": '["0.5", "0.5"]',
        }
        result = _parse_updown_market(raw, MarketSeries("btc", "5m", 300))
        assert result is not None
        assert result.end_timestamp == 0

    def test_empty_slug_returns_end_ts_zero(self):
        """Empty slug → IndexError on rsplit → end_ts=0 (lines 288-289)."""
        raw = {
            "conditionId": "0x1",
            "question": "BTC Up or Down",
            "slug": "",
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["t1", "t2"]',
            "outcomePrices": '["0.5", "0.5"]',
        }
        result = _parse_updown_market(raw, MarketSeries("btc", "5m", 300))
        assert result is not None
        assert result.end_timestamp == 0

    def test_uses_tokenIds_fallback(self):
        """When clobTokenIds missing, falls back to tokenIds (line 260)."""
        raw = {
            "conditionId": "0x1",
            "question": "BTC Up or Down",
            "slug": "btc-updown-5m-1000",
            "outcomes": '["Up", "Down"]',
            "tokenIds": '["tok_up", "tok_down"]',
            "outcomePrices": '["0.5", "0.5"]',
        }
        result = _parse_updown_market(raw, MarketSeries("btc", "5m", 300))
        assert result is not None
        assert result.token_id_up == "tok_up"

    def test_uses_condition_id_fallback(self):
        """When conditionId missing, uses condition_id (line 292)."""
        raw = {
            "condition_id": "0xfallback",
            "question": "BTC Up or Down",
            "slug": "btc-updown-5m-1000",
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["t1", "t2"]',
            "outcomePrices": '["0.5", "0.5"]',
        }
        result = _parse_updown_market(raw, MarketSeries("btc", "5m", 300))
        assert result is not None
        assert result.condition_id == "0xfallback"
