"""Tests for CLOB helpers: midpoint, orderbook simulation, retry."""

from unittest.mock import MagicMock

import pytest
import requests

from timba.clob_helpers import (
    _cancel,
    _get_midpoint,
    retry_api,
    simulate_fill,
)


class TestGetMidpoint:
    def test_returns_float(self):
        clob = MagicMock()
        mid_obj = MagicMock()
        mid_obj.value = 0.55
        clob.get_midpoint.return_value = mid_obj
        result = _get_midpoint(clob, "token123")
        assert result == pytest.approx(0.55)

    def test_returns_none_on_error(self):
        from unittest.mock import patch
        clob = MagicMock()
        clob.get_midpoint.side_effect = requests.ConnectionError("err")
        with patch("timba.clob_helpers.time.sleep"):
            assert _get_midpoint(clob, "token123") is None


class TestRetryApi:
    def test_succeeds_first_try(self):
        fn = MagicMock(return_value=42)
        assert retry_api(fn, retries=2) == 42
        assert fn.call_count == 1

    def test_retries_on_failure(self):
        fn = MagicMock(side_effect=[requests.ConnectionError("err"), requests.Timeout("err"), 42])
        assert retry_api(fn, retries=2, backoff=0.01) == 42
        assert fn.call_count == 3

    def test_raises_after_all_retries(self):
        fn = MagicMock(side_effect=requests.ConnectionError("permanent"))
        with pytest.raises(requests.ConnectionError, match="permanent"):
            retry_api(fn, retries=1, backoff=0.01)
        assert fn.call_count == 2


class TestSimulateFill:
    def test_walks_through_asks(self):
        clob = MagicMock()
        ask1 = MagicMock(price=0.93, size=80)
        ask2 = MagicMock(price=0.95, size=50)
        ask3 = MagicMock(price=0.97, size=200)
        book = MagicMock(asks=[ask1, ask2, ask3])
        clob.get_order_book.return_value = book

        result = simulate_fill(clob, "token", 200)
        assert result is not None
        avg_price, filled, tick = result
        assert filled == 200
        assert avg_price == pytest.approx(189.8 / 200, abs=0.001)

    def test_partial_fill(self):
        clob = MagicMock()
        ask1 = MagicMock(price=0.92, size=50)
        book = MagicMock(asks=[ask1])
        clob.get_order_book.return_value = book

        result = simulate_fill(clob, "token", 200)
        avg_price, filled, tick = result
        assert filled == 50
        assert avg_price == pytest.approx(0.92)

    def test_empty_book_returns_none(self):
        clob = MagicMock()
        book = MagicMock(asks=[])
        clob.get_order_book.return_value = book
        assert simulate_fill(clob, "token", 200) is None

    def test_api_error_returns_none(self):
        from unittest.mock import patch
        clob = MagicMock()
        clob.get_order_book.side_effect = requests.Timeout("timeout")
        with patch("timba.clob_helpers.time.sleep"):
            assert simulate_fill(clob, "token", 200) is None


class TestCancel:
    def test_cancel_paper_order_skipped(self):
        clob = MagicMock()
        _cancel(clob, "paper_up_test")
        clob.cancel.assert_not_called()

    def test_cancel_none_skipped(self):
        clob = MagicMock()
        _cancel(clob, None)
        clob.cancel.assert_not_called()

    def test_cancel_real_order(self):
        clob = MagicMock()
        _cancel(clob, "0xorder123")
        clob.cancel.assert_called_once_with("0xorder123")

    def test_cancel_error_ignored(self):
        clob = MagicMock()
        clob.cancel.side_effect = requests.ConnectionError("err")
        _cancel(clob, "0xorder123")  # should not raise


class TestHttpxImportFallback:
    """Cover the httpx import fallback (lines 17-19)."""

    def test_network_errors_without_httpx(self):
        """When httpx is not available, _NETWORK_ERRORS should not include httpx types."""
        from timba.clob_helpers import _NETWORK_ERRORS
        # _NETWORK_ERRORS always includes requests.RequestException
        assert requests.RequestException in _NETWORK_ERRORS
        assert TimeoutError in _NETWORK_ERRORS
        assert OSError in _NETWORK_ERRORS

    def test_fallback_when_httpx_missing(self):
        """Reload module with httpx import blocked to cover lines 17-19."""
        import importlib
        import sys

        # Save original modules
        original_httpx = sys.modules.get("httpx")
        original_clob = sys.modules.get("timba.clob_helpers")

        # Block httpx import
        sys.modules["httpx"] = None  # type: ignore[assignment]
        if "timba.clob_helpers" in sys.modules:
            del sys.modules["timba.clob_helpers"]

        try:
            import timba.clob_helpers as reloaded
            assert reloaded.httpx is None
            assert requests.RequestException in reloaded._NETWORK_ERRORS
            assert TimeoutError in reloaded._NETWORK_ERRORS
            # Should only have 4 base error types (no httpx types)
            assert len(reloaded._NETWORK_ERRORS) == 4
        finally:
            # Restore original modules
            if original_httpx is not None:
                sys.modules["httpx"] = original_httpx
            else:
                sys.modules.pop("httpx", None)
            if original_clob is not None:
                sys.modules["timba.clob_helpers"] = original_clob
            else:
                sys.modules.pop("timba.clob_helpers", None)
            # Re-import to restore original state
            importlib.import_module("timba.clob_helpers")


class TestGetMidpointStringConversion:
    """Cover _get_midpoint when result has no .value attribute (line 59)."""

    def test_midpoint_without_value_attr(self):
        """When get_midpoint returns a plain string/number, float(str(mid)) is used."""
        clob = MagicMock()
        # Return a plain string (no .value attribute)
        clob.get_midpoint.return_value = "0.75"
        result = _get_midpoint(clob, "token123")
        assert result == pytest.approx(0.75)

    def test_midpoint_plain_float(self):
        """When get_midpoint returns a plain float."""
        clob = MagicMock()
        clob.get_midpoint.return_value = 0.60
        result = _get_midpoint(clob, "token123")
        assert result == pytest.approx(0.60)


class TestSimulateFillEdgeCases:
    """Cover simulate_fill edge cases: zero-size asks (line 88), zero filled (line 96)."""

    def test_zero_size_asks_skipped(self):
        """Asks with size 0 should be skipped (line 88: take <= 0 -> continue)."""
        clob = MagicMock()
        ask_zero = MagicMock(price=0.90, size=0)
        ask_real = MagicMock(price=0.95, size=100)
        book = MagicMock(asks=[ask_zero, ask_real])
        clob.get_order_book.return_value = book

        result = simulate_fill(clob, "token", 50)
        assert result is not None
        avg_price, filled, tick = result
        assert filled == 50
        assert avg_price == pytest.approx(0.95)

    def test_all_zero_size_asks_returns_none(self):
        """If all asks have size 0, total_filled is 0 -> returns None (line 96)."""
        clob = MagicMock()
        ask1 = MagicMock(price=0.90, size=0)
        ask2 = MagicMock(price=0.95, size=0)
        book = MagicMock(asks=[ask1, ask2])
        clob.get_order_book.return_value = book

        result = simulate_fill(clob, "token", 100)
        assert result is None
