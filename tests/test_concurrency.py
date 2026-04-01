"""Concurrency tests: verify thread safety under real contention.

These tests exercise the cash lock and state machine under ThreadPoolExecutor
and multi-thread scenarios to prove the safety mechanisms actually work.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from timba.base import MarketPosition, PositionState


def _make_position(**kw):
    defaults = dict(
        condition_id="0x1", question="test", slug="test-slug",
        coin="btc", interval="5m", token_id_up="tu", token_id_down="td",
        end_timestamp=int(time.time()) + 300, window_start_ts=int(time.time()),
    )
    defaults.update(kw)
    return MarketPosition(**defaults)


class TestCashLockUnderPool:
    """Verify _cash_lock prevents double-spend under real ThreadPoolExecutor."""

    def test_pool_workers_respect_cash_limit(self, trader_setup):
        """10 workers each trying to reserve $20 on $100 — max 5 should succeed."""
        trader, state = trader_setup
        state.cash = 100.0
        state.reserved_cash = 0.0

        reserved_count = {"n": 0}

        def try_reserve():
            with trader._cash_lock:
                if state.available_cash >= 20.0:
                    state.reserved_cash += 20.0
                    reserved_count["n"] += 1

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(try_reserve) for _ in range(10)]
            for f in futures:
                f.result()

        assert reserved_count["n"] == 5
        assert state.reserved_cash == pytest.approx(100.0)
        assert state.available_cash == pytest.approx(0.0)

    def test_pool_never_overreserves(self, trader_setup):
        """Run 50 times: reserved_cash must never exceed cash."""
        trader, state = trader_setup

        for _ in range(50):
            state.cash = 50.0
            state.reserved_cash = 0.0

            def try_reserve():
                with trader._cash_lock:
                    if state.available_cash >= 30.0:
                        state.reserved_cash += 30.0

            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = [pool.submit(try_reserve) for _ in range(5)]
                for f in futures:
                    f.result()

            assert state.reserved_cash <= state.cash + 0.01


class TestTransitionRaces:
    """Verify pos.transition() serializes concurrent state mutations."""

    def test_order_vs_cleanup_race(self):
        """Order thread sets SNIPED, cleanup sets SKIPPED — exactly one wins.
        Run 100 times to increase chance of hitting the race window.
        """
        for _ in range(100):
            pos = _make_position()
            pos.state = PositionState.PENDING_ORDER
            results = []
            barrier = threading.Barrier(2)

            def order_thread():
                barrier.wait()
                results.append(("sniped", pos.transition(PositionState.SNIPED)))

            def cleanup_thread():
                barrier.wait()
                results.append(("skipped", pos.transition(PositionState.SKIPPED)))

            t1 = threading.Thread(target=order_thread)
            t2 = threading.Thread(target=cleanup_thread)
            t1.start(); t2.start()
            t1.join(); t2.join()

            # Exactly one should succeed
            successes = [name for name, ok in results if ok]
            assert len(successes) == 1
            # Final state matches the winner
            if successes[0] == "sniped":
                assert pos.state == PositionState.SNIPED
            else:
                assert pos.state == PositionState.SKIPPED

    def test_resolution_vs_order_race(self):
        """Resolution tries WON while order is still transitioning.
        SNIPED → WON is valid, but only if SNIPED was reached first.
        """
        for _ in range(100):
            pos = _make_position()
            pos.state = PositionState.PENDING_ORDER
            results = []
            barrier = threading.Barrier(2)

            def order_thread():
                barrier.wait()
                ok = pos.transition(PositionState.SNIPED)
                results.append(("sniped", ok))
                if ok:
                    # Immediately try to resolve
                    ok2 = pos.transition(PositionState.WON)
                    results.append(("won", ok2))

            def resolution_thread():
                barrier.wait()
                # Try WON directly — should fail if still PENDING_ORDER
                ok = pos.transition(PositionState.WON)
                results.append(("res_won", ok))

            t1 = threading.Thread(target=order_thread)
            t2 = threading.Thread(target=resolution_thread)
            t1.start(); t2.start()
            t1.join(); t2.join()

            # WON from PENDING_ORDER is invalid — resolution's direct attempt should fail
            res_attempts = [ok for name, ok in results if name == "res_won"]
            assert all(ok is False for ok in res_attempts)

    def test_many_threads_single_position(self):
        """20 threads all trying different transitions from WATCHING.
        WON is always invalid from WATCHING. Valid transitions are serialized
        by the lock — no corruption, no double-write.
        """
        for _ in range(50):
            pos = _make_position()
            results = []
            barrier = threading.Barrier(20)

            targets = ([PositionState.PENDING_ORDER] * 10
                       + [PositionState.SKIPPED] * 5
                       + [PositionState.WON] * 5)  # WON is invalid from WATCHING

            def try_transition(target):
                barrier.wait()
                ok = pos.transition(target)
                results.append((target, ok))

            threads = [threading.Thread(target=try_transition, args=(t,))
                       for t in targets]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # All WON attempts must fail (invalid from WATCHING)
            won_attempts = [ok for target, ok in results if target == PositionState.WON]
            assert all(ok is False for ok in won_attempts)

            # At least one valid transition succeeded
            valid_successes = [target for target, ok in results
                               if ok and target in (PositionState.PENDING_ORDER, PositionState.SKIPPED)]
            assert len(valid_successes) >= 1

            # Final state is a valid reachable state (serialized transitions)
            assert pos.state != PositionState.WATCHING  # something changed
            assert pos.state != PositionState.WON  # WON was never valid


class TestRetryApiJitter:
    """Verify retry_api uses jitter and respects 429."""

    def test_jitter_varies_wait_times(self):
        """Two calls to retry_api with the same error should not wait identical times."""
        from timba.clob_helpers import retry_api

        wait_times = []

        def capture_sleep(duration):
            wait_times.append(duration)
            # Don't actually sleep in tests

        call_count = 0

        def failing_fn():
            nonlocal call_count
            call_count += 1
            raise TimeoutError("test")

        with patch("timba.clob_helpers.time.sleep", side_effect=capture_sleep):
            # Run twice, collect wait times
            for _ in range(2):
                call_count = 0
                try:
                    retry_api(failing_fn, retries=1, backoff=1.0)
                except TimeoutError:
                    pass

        # With jitter, the two wait times should differ
        if len(wait_times) >= 2:
            assert wait_times[0] != wait_times[1]

    def test_429_respects_retry_after(self):
        """On 429 with Retry-After header, should wait that many seconds."""
        from timba.clob_helpers import retry_api

        try:
            import httpx
        except ImportError:
            pytest.skip("httpx not installed")

        wait_times = []

        def capture_sleep(duration):
            wait_times.append(duration)

        call_count = 0

        def rate_limited_fn():
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                response = MagicMock()
                response.status_code = 429
                response.headers = {"Retry-After": "2"}
                request = MagicMock()
                raise httpx.HTTPStatusError("rate limited", request=request, response=response)
            return "success"

        with patch("timba.clob_helpers.time.sleep", side_effect=capture_sleep):
            result = retry_api(rate_limited_fn, retries=2, backoff=0.5)

        assert result == "success"
        # First wait should be ~2s (Retry-After) + jitter (0-0.5)
        assert 2.0 <= wait_times[0] <= 2.5
