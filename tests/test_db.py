"""Tests for db.py — SQLite storage layer with single-writer queue."""

import threading

import pytest

from timba import db


def _sample_tick(id=1):
    return {
        "id": id, "ts": "2026-03-29T12:00:00+00:00", "slug": "btc-updown-5m-123",
        "coin": "btc", "interval": "5m",
        "mid_up": 0.98, "mid_down": 0.02,
        "fill_up": 0.99, "fill_down": 0.05,
        "signal_dir": "up", "signal_chg": -0.05,
        "signal_trend_sec": 45.0, "signal_rev": True,
        "price_open": 66000.0, "price_now": 65980.0,
    }


def _sample_ev(id=1, tick_id=1):
    return {
        "id": id, "tick_id": tick_id, "slug": "btc-updown-5m-123",
        "remaining": 28.5, "progress": 0.75,
        "ev_up": 0.05, "ev_down": -0.08,
        "p_up": 0.72, "p_down": 0.35,
        "custom_field": "extra_value",
    }


def _sample_trade(id=1, ev_id=None):
    return {
        "id": id, "type": "win", "strategy": "favorite",
        "slug": "btc-updown-5m-123", "condition_id": "0x1",
        "coin": "btc", "interval": "5m",
        "side": "up", "buy_price": 0.90, "contracts": 5, "pnl": 0.50,
        "sniped_at": "2026-03-29T12:00:00Z", "resolved_at": "2026-03-29T12:05:00Z",
        "end_timestamp": 1743398400, "market_mode": "paper",
        "skip_reason": "",
        "ticks_evaluated": 12, "ev_id": ev_id,
        "token_id": "tok_up", "redeemed": False,
    }


class TestInit:
    def test_creates_tables(self, tmp_path):
        db.init(tmp_path)
        assert (tmp_path / "bot.db").exists()
        assert db.is_initialized()

    def test_idempotent(self, tmp_path):
        db.init(tmp_path)
        db.init(tmp_path)  # should not raise


class TestMaxId:
    def test_empty_table(self, tmp_path):
        db.init(tmp_path)
        assert db.max_id("ticks") == 0
        assert db.max_id("evs") == 0
        assert db.max_id("trades") == 0

    def test_after_insert(self, tmp_path):
        db.init(tmp_path)
        db.insert_tick(_sample_tick(42))
        db.flush()
        assert db.max_id("ticks") == 42

    def test_invalid_table(self, tmp_path):
        db.init(tmp_path)
        with pytest.raises(ValueError):
            db.max_id("nonexistent")


class TestInsertTick:
    def test_roundtrip(self, tmp_path):
        db.init(tmp_path)
        db.insert_tick(_sample_tick())
        db.flush()
        ticks = db.load_ticks()
        assert len(ticks) == 1
        t = ticks[0]
        assert t["id"] == 1
        assert t["slug"] == "btc-updown-5m-123"
        assert t["signal_rev"] is True
        assert t["price_open"] == 66000.0

    def test_signal_rev_bool(self, tmp_path):
        db.init(tmp_path)
        db.insert_tick(_sample_tick())
        db.flush()
        t = db.load_ticks()[0]
        assert isinstance(t["signal_rev"], bool)


class TestInsertEv:
    def test_roundtrip(self, tmp_path):
        db.init(tmp_path)
        db.insert_tick(_sample_tick())
        db.insert_ev(_sample_ev(), "favorite")
        db.flush()
        evs = db.load_evs(strategy="favorite")
        assert 1 in evs
        ev = evs[1]
        assert ev["ev_up"] == 0.05
        assert ev["custom_field"] == "extra_value"

    def test_extras_merged(self, tmp_path):
        db.init(tmp_path)
        db.insert_tick(_sample_tick())
        db.insert_ev(_sample_ev(), "favorite")
        db.flush()
        ev = db.load_evs()[1]
        assert "extras" not in ev
        assert ev["custom_field"] == "extra_value"


class TestInsertTrade:
    def test_roundtrip(self, tmp_path):
        db.init(tmp_path)
        db.insert_trade(_sample_trade())
        db.flush()
        trades = db.load_trades()
        assert len(trades) == 1
        t = trades[0]
        assert t["type"] == "win"
        assert t["buy_price"] == 0.90
        assert t["redeemed"] is False

    def test_ev_id_zero_stored_as_null(self, tmp_path):
        db.init(tmp_path)
        db.insert_trade(_sample_trade(ev_id=0))
        db.flush()
        trades = db.load_trades()
        assert trades[0]["ev_id"] is None


class TestLoadTrades:
    def test_filter_by_strategy(self, tmp_path):
        db.init(tmp_path)
        t1 = _sample_trade(1)
        t1["strategy"] = "favorite"
        t2 = _sample_trade(2)
        t2["strategy"] = "other"
        db.insert_trade(t1)
        db.insert_trade(t2)
        db.flush()
        assert len(db.load_trades(strategy="favorite")) == 1
        assert len(db.load_trades(strategy="other")) == 1

    def test_filter_by_since(self, tmp_path):
        db.init(tmp_path)
        t1 = _sample_trade(1)
        t1["sniped_at"] = "2026-03-28T00:00:00Z"
        t2 = _sample_trade(2)
        t2["sniped_at"] = "2026-03-30T00:00:00Z"
        db.insert_trade(t1)
        db.insert_trade(t2)
        db.flush()
        recent = db.load_trades(since="2026-03-29")
        assert len(recent) == 1
        assert recent[0]["id"] == 2


class TestLoadTicksGrouped:
    def test_groups_by_slug(self, tmp_path):
        db.init(tmp_path)
        t1 = _sample_tick(1)
        t1["slug"] = "btc-5m-1"
        t2 = _sample_tick(2)
        t2["slug"] = "eth-5m-1"
        t2["ts"] = "2026-03-29T12:00:01+00:00"
        db.insert_tick(t1)
        db.insert_tick(t2)
        db.flush()
        grouped, skip = db.load_ticks_grouped()
        assert "btc-5m-1" in grouped
        assert "eth-5m-1" in grouped
        assert skip == 0


class TestMarkTradeRedeemed:
    def test_marks_trade_as_redeemed(self, tmp_path):
        db.init(tmp_path)
        t = _sample_trade(1)
        t["type"] = "win"
        t["redeemed"] = False
        t["condition_id"] = "0xabc"
        db.insert_trade(t)
        db.flush()

        db.mark_trade_redeemed("0xabc")
        db.flush()

        trades = db.load_trades()
        assert trades[0]["redeemed"] is True

    def test_does_not_mark_other_conditions(self, tmp_path):
        db.init(tmp_path)
        t1 = _sample_trade(1)
        t1["condition_id"] = "0xabc"
        t1["redeemed"] = False
        t2 = _sample_trade(2)
        t2["condition_id"] = "0xdef"
        t2["redeemed"] = False
        db.insert_trade(t1)
        db.insert_trade(t2)
        db.flush()

        db.mark_trade_redeemed("0xabc")
        db.flush()

        trades = db.load_trades()
        by_cid = {t["condition_id"]: t for t in trades}
        assert by_cid["0xabc"]["redeemed"] is True
        assert by_cid["0xdef"]["redeemed"] is False


class TestGetPendingRedemption:
    def test_sums_unredeemed_contracts(self, tmp_path):
        db.init(tmp_path)
        t1 = _sample_trade(1)
        t1["type"] = "win"
        t1["contracts"] = 10
        t1["redeemed"] = False
        t2 = _sample_trade(2)
        t2["type"] = "win"
        t2["contracts"] = 5
        t2["redeemed"] = False
        db.insert_trade(t1)
        db.insert_trade(t2)
        db.flush()

        assert db.get_pending_redemption() == 15.0

    def test_excludes_redeemed(self, tmp_path):
        db.init(tmp_path)
        t = _sample_trade(1)
        t["type"] = "win"
        t["contracts"] = 10
        t["redeemed"] = True
        db.insert_trade(t)
        db.flush()

        assert db.get_pending_redemption() == 0.0

    def test_empty_returns_zero(self, tmp_path):
        db.init(tmp_path)
        assert db.get_pending_redemption() == 0.0


class TestGetAllStrategyStats:
    def test_groups_by_strategy_and_type(self, tmp_path):
        db.init(tmp_path)
        t1 = _sample_trade(1)
        t1["strategy"] = "favorite"
        t1["type"] = "win"
        t2 = _sample_trade(2)
        t2["strategy"] = "favorite"
        t2["type"] = "paper_win"
        t3 = _sample_trade(3)
        t3["strategy"] = "other"
        t3["type"] = "loss"
        db.insert_trade(t1)
        db.insert_trade(t2)
        db.insert_trade(t3)
        db.flush()

        stats = db.get_all_strategy_stats()
        assert stats["favorite"]["win"] == 1
        assert stats["favorite"]["paper_win"] == 1
        assert stats["other"]["loss"] == 1

    def test_empty_returns_empty(self, tmp_path):
        db.init(tmp_path)
        assert db.get_all_strategy_stats() == {}


class TestGetUnredeemedWins:
    def test_returns_unredeemed_wins_only(self, tmp_path):
        db.init(tmp_path)
        t1 = _sample_trade(1)
        t1["type"] = "win"
        t1["redeemed"] = False
        t2 = _sample_trade(2)
        t2["type"] = "win"
        t2["redeemed"] = True
        t3 = _sample_trade(3)
        t3["type"] = "paper_win"
        t3["redeemed"] = False
        db.insert_trade(t1)
        db.insert_trade(t2)
        db.insert_trade(t3)
        db.flush()

        unredeemed = db.get_unredeemed_wins()
        assert len(unredeemed) == 1
        assert unredeemed[0]["id"] == 1


class TestThreadSafety:
    def test_concurrent_inserts(self, tmp_path):
        db.init(tmp_path)
        errors = []

        def writer(start_id):
            try:
                for i in range(10):
                    tick = _sample_tick(start_id + i)
                    tick["ts"] = f"2026-03-29T12:00:{i:02d}+00:00"
                    db.insert_tick(tick)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i * 100,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        db.flush()
        assert not errors
        ticks = db.load_ticks()
        assert len(ticks) == 30
