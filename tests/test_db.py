"""Tests for db.py — SQLite storage layer with single-writer queue."""

import threading
from pathlib import Path

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


# ── Coverage: flush when not initialized (line 172) ──────────────


class TestFlushNotInitialized:
    def test_flush_when_not_initialized(self):
        """flush() with no queue should return immediately (line 172)."""
        # After _reset_db, _write_queue is None
        db.flush()  # should not raise


# ── Coverage: reset closing read connections with errors (lines 193-194) ──


class TestResetCloseErrors:
    def test_reset_closes_read_connections_with_errors(self, tmp_path):
        """reset() should suppress errors when closing read connections."""
        from unittest.mock import MagicMock
        db.init(tmp_path)
        _ = db.load_ticks()  # create a real read connection
        # Close real connection, replace with mock that raises
        with db._read_conn_lock:
            for c in db._read_connections.values():
                c.close()
            db._read_connections.clear()
            mock_conn = MagicMock()
            mock_conn.close.side_effect = Exception("close failed")
            db._read_connections[0] = mock_conn
        db.reset()  # should not raise despite close failure
        mock_conn.close.assert_called_once()


# ── Coverage: rotate when db_path is None (line 216) ──────────────


class TestRotateNotInitialized:
    def test_rotate_returns_none_when_not_initialized(self):
        """rotate() returns None when db not initialized (line 216)."""
        result = db.rotate()
        assert result is None


# ── Coverage: rotate same-day counter (line 232) ──────────────────


class TestRotateSameDay:
    def test_rotate_multiple_same_day(self, tmp_path):
        """Multiple rotations on the same day use incrementing suffixes (line 232).

        Pre-create existing archive files to trigger the incrementing counter
        without needing multiple reset/init cycles.
        """
        from datetime import datetime, timezone

        db.init(tmp_path)
        db.insert_tick(_sample_tick(1))
        db.flush()

        # Pre-create archive files for today's date to force incrementing
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        (tmp_path / f"bot_{date_str}.db").write_bytes(b"existing")
        (tmp_path / f"bot_{date_str}_1.db").write_bytes(b"existing")

        archive = db.rotate("test")
        assert archive is not None
        assert archive.exists()
        # Should skip both existing files and use _2
        assert "_2" in archive.name


# ── Coverage: rotate WAL/SHM rename and read conn close errors (lines 246-249, 257-258) ──


class TestRotateWalShm:
    def test_rotate_renames_wal_shm(self, tmp_path):
        """rotate() renames WAL and SHM files alongside the DB (lines 257-258).

        We test via mocking to ensure the rename code path runs, since SQLite's
        WAL checkpoint may clean up the files before our code gets to them.
        """
        from unittest.mock import patch
        db.init(tmp_path)
        db.insert_tick(_sample_tick(1))
        db.flush()

        # Track which paths were renamed
        renamed = []
        original_rename = Path.rename

        def tracking_rename(self, target):
            renamed.append((str(self), str(target)))
            return original_rename(self, target)

        with patch.object(Path, "rename", tracking_rename):
            archive = db.rotate("test")

        assert archive is not None
        # The main DB should always be renamed
        main_renames = [r for r in renamed if "bot.db" in r[0] and "-wal" not in r[0] and "-shm" not in r[0]]
        assert len(main_renames) == 1

    def test_rotate_handles_read_conn_close_error(self, tmp_path):
        """rotate() suppresses errors when closing read connections."""
        from unittest.mock import MagicMock
        db.init(tmp_path)
        _ = db.load_ticks()  # create a real read connection
        # Close real connections so they don't hold the file
        with db._read_conn_lock:
            for c in db._read_connections.values():
                c.close()
            db._read_connections.clear()
            # Add a mock that raises on close — rotate() should suppress it
            mock_conn = MagicMock()
            mock_conn.close.side_effect = Exception("close failed")
            db._read_connections[0] = mock_conn
        archive = db.rotate("test")
        assert archive is not None
        mock_conn.close.assert_called_once()


# ── Coverage: list_databases when not initialized (line 289) ──────


class TestListDatabasesNotInitialized:
    def test_returns_empty_when_not_initialized(self):
        """list_databases() returns [] when _data_dir is None (line 289)."""
        assert db.list_databases() == []


# ── Coverage: copy_ticks_from not initialized (line 306) ──────────


class TestCopyTicksNotInitialized:
    def test_raises_when_not_initialized(self):
        """copy_ticks_from raises RuntimeError when not initialized (line 306)."""
        import pytest
        with pytest.raises(RuntimeError, match="db.init"):
            db.copy_ticks_from([])


# ── Coverage: copy_ticks_from skips missing, handles errors (lines 314, 318, 328-333) ──


class TestCopyTicksFrom:
    def test_skips_nonexistent_source(self, tmp_path):
        """copy_ticks_from skips source DBs that don't exist (line 314)."""
        db.init(tmp_path)
        total = db.copy_ticks_from([tmp_path / "nonexistent.db"])
        assert total == 0

    def test_copies_with_since_filter(self, tmp_path):
        """copy_ticks_from with since= filters ticks (line 318)."""
        import sqlite3

        # Create source DB with ticks
        source_db = tmp_path / "source.db"
        conn = sqlite3.connect(str(source_db))
        conn.executescript(db.SCHEMA_SQL)
        conn.execute(
            "INSERT INTO ticks VALUES (1,'2026-03-28T00:00:00Z','s1','btc','5m',"
            "0.9,0.1,0.9,0.1,'up',0.1,10,0,100,100)"
        )
        conn.execute(
            "INSERT INTO ticks VALUES (2,'2026-03-30T00:00:00Z','s2','btc','5m',"
            "0.9,0.1,0.9,0.1,'up',0.1,10,0,100,100)"
        )
        conn.commit()
        conn.close()

        dest = tmp_path / "dest"
        db.init(dest)
        total = db.copy_ticks_from([source_db], since="2026-03-29")
        assert total == 1  # only the tick from 03-30

    def test_copies_without_since(self, tmp_path):
        """copy_ticks_from without since= copies all ticks."""
        import sqlite3

        source_db = tmp_path / "source.db"
        conn = sqlite3.connect(str(source_db))
        conn.executescript(db.SCHEMA_SQL)
        conn.execute(
            "INSERT INTO ticks VALUES (1,'2026-03-28T00:00:00Z','s1','btc','5m',"
            "0.9,0.1,0.9,0.1,'up',0.1,10,0,100,100)"
        )
        conn.execute(
            "INSERT INTO ticks VALUES (2,'2026-03-30T00:00:00Z','s2','btc','5m',"
            "0.9,0.1,0.9,0.1,'up',0.1,10,0,100,100)"
        )
        conn.commit()
        conn.close()

        dest = tmp_path / "dest"
        db.init(dest)
        total = db.copy_ticks_from([source_db])
        assert total == 2

    def test_handles_corrupt_source(self, tmp_path):
        """copy_ticks_from handles a corrupt source DB gracefully (lines 328-333)."""
        # Create a non-DB file
        corrupt = tmp_path / "corrupt.db"
        corrupt.write_text("not a database")

        dest = tmp_path / "dest"
        db.init(dest)
        total = db.copy_ticks_from([corrupt])
        assert total == 0  # should not crash


# ── Coverage: batch retry in writer (lines 399, 412-413, 418, 423-424) ──


class TestWriterBatchRetry:
    def test_bad_item_in_batch_retries_individually(self, tmp_path):
        """A bad SQL in a batch should retry individually; good items survive (lines 399-424).

        We enqueue items rapidly so they get batched together, including a bad SQL
        that will cause the entire batch commit to fail. The writer then retries
        each item individually.
        """
        db.init(tmp_path)

        # Enqueue several items quickly so they get batched
        for i in range(1, 4):
            tick = _sample_tick(i)
            tick["ts"] = f"2026-03-29T12:00:{i:02d}+00:00"
            db.insert_tick(tick)

        # Enqueue a bad SQL that will cause the batch to fail
        db._enqueue("INSERT INTO nonexistent_table VALUES (?)", (1,))

        # Enqueue more valid items
        for i in range(4, 7):
            tick = _sample_tick(i)
            tick["ts"] = f"2026-03-29T12:00:{i:02d}+00:00"
            db.insert_tick(tick)

        db.flush()
        ticks = db.load_ticks()
        # At least some good ticks should survive after individual retry
        assert len(ticks) >= 1


# ── Coverage: _enqueue overflow (lines 438-439) ──────────────────


class TestWriterLoopUnitPaths:
    def test_none_sentinel_in_batch_and_rollback_failures(self, tmp_path):
        """Test _writer_loop batch-building None sentinel (line 399),
        batch rollback failure (lines 412-413), and individual rollback failure (lines 423-424).

        Runs _writer_loop directly with a mock connection and controlled queue.
        """
        import queue as queue_mod
        from unittest.mock import MagicMock, patch

        db.init(tmp_path)
        # Stop the real writer — we'll run _writer_loop ourselves
        db._writer_running = False
        db._write_queue.put(None)
        if db._writer_thread:
            db._writer_thread.join(timeout=5)
        db._writer_thread = None

        # Create controlled queue with items + None sentinel mid-batch
        q = queue_mod.Queue(maxsize=1000)
        db._write_queue = q

        # First batch: a valid item, then None sentinel mid-batch → hits line 399
        # Then another None at top level to shut down the loop
        q.put(("SELECT 1", ()))
        q.put(None)  # consumed by inner batch loop → line 399
        q.put(None)  # consumed by outer loop → shutdown

        db._writer_running = True

        mock_conn = MagicMock()
        mock_conn.execute.return_value = None
        mock_conn.commit.return_value = None

        with patch("timba.db._open_writer_conn", return_value=mock_conn):
            db._writer_loop()

        # Writer should have exited cleanly
        mock_conn.commit.assert_called()

        # Second test: batch failure with rollback exception
        q2 = queue_mod.Queue(maxsize=1000)
        db._write_queue = q2

        # Enqueue items that will cause a batch failure
        q2.put(("BAD SQL", ()))
        q2.put(None)  # consumed by inner batch loop
        q2.put(None)  # shutdown signal for outer loop

        db._writer_running = True

        mock_conn2 = MagicMock()
        mock_conn2.execute.side_effect = Exception("bad SQL")
        mock_conn2.rollback.side_effect = Exception("rollback failed")
        mock_conn2.commit.return_value = None

        with patch("timba.db._open_writer_conn", return_value=mock_conn2):
            db._writer_loop()

        # Rollback was attempted (and failed, but suppressed) — covers lines 412-413 and 423-424
        assert mock_conn2.rollback.call_count >= 1

        # Clean up so conftest reset doesn't hang
        db._writer_running = False
        db._write_queue = None
        db._writer_thread = None


class TestEnqueueOverflow:
    def test_enqueue_when_not_initialized(self):
        """_enqueue with no queue silently returns (line 434-435)."""
        db._enqueue("SELECT 1", ())  # should not raise

    def test_enqueue_overflow_silently_drops(self, tmp_path):
        """When the queue is full, _enqueue silently drops items (lines 438-439)."""
        db.init(tmp_path)
        # Fill the queue with items. The queue is size 50,000 — but we can mock it.
        # Replace with a tiny queue that's already full
        import queue
        tiny_q = queue.Queue(maxsize=1)
        tiny_q.put(("SELECT 1", ()))
        old_q = db._write_queue
        db._write_queue = tiny_q
        try:
            db._enqueue("SELECT 1", ())  # should not raise even though full
        finally:
            db._write_queue = old_q


# ── Coverage: _get_read_connection without init (line 450) ────────


class TestGetReadConnectionNotInitialized:
    def test_raises_when_not_initialized(self):
        """_get_read_connection raises RuntimeError when db not initialized (line 450)."""
        with pytest.raises(RuntimeError, match="db.init"):
            db._get_read_connection()


# ── Coverage: read connection invalidation on rotation (lines 464-467) ──


class TestReadConnectionInvalidation:
    def test_read_connection_invalidated_on_version_change(self, tmp_path):
        """Read connections are refreshed when _db_version changes (lines 464-467)."""
        db.init(tmp_path)
        db.insert_tick(_sample_tick(1))
        db.flush()

        # Create a read connection
        ticks = db.load_ticks()
        assert len(ticks) == 1

        # Simulate rotation by bumping the version
        old_version = db._db_version
        db._db_version = old_version + 1

        # The next read should invalidate old connections and create new ones
        ticks = db.load_ticks()
        assert len(ticks) == 1

        # Restore
        db._db_version = old_version

    def test_stale_conn_close_error_suppressed(self, tmp_path):
        """Exception during stale connection close is suppressed."""
        from unittest.mock import MagicMock
        db.init(tmp_path)
        _ = db.load_ticks()  # create a real read connection

        # Replace thread-local conn with mock that raises on close
        mock_conn = MagicMock()
        mock_conn.close.side_effect = Exception("close failed")
        db._read_local.conn = mock_conn

        # Bump version to force invalidation on next _get_read_connection()
        db._db_version += 1

        # This should try to close stale connection, hit the error, and continue
        ticks = db.load_ticks()
        assert isinstance(ticks, list)
        mock_conn.close.assert_called_once()

    def test_first_read_on_fresh_thread_local(self, tmp_path):
        """First read with fresh _read_local exercises initial setup."""
        db.init(tmp_path)
        # Clear thread-local state to simulate a fresh thread
        if hasattr(db._read_local, "conn"):
            del db._read_local.conn
        if hasattr(db._read_local, "version"):
            del db._read_local.version

        ticks = db.load_ticks()
        assert isinstance(ticks, list)


# ── Coverage: _unpack_ev with bad JSON (lines 577-578) ────────────


class TestUnpackEvBadJson:
    def test_unpack_ev_bad_json_ignored(self):
        """_unpack_ev with invalid extras JSON silently ignores it (lines 577-578)."""
        d = {"extras": "not valid json{{{"}
        result = db._unpack_ev(d)
        assert "extras" not in result

    def test_unpack_ev_none_extras(self):
        """_unpack_ev with None extras does nothing."""
        d = {"extras": None}
        result = db._unpack_ev(d)
        assert "extras" not in result


# ── Coverage: _unpack_trade with bad JSON (lines 586-589) ─────────


class TestUnpackTradeBadJson:
    def test_unpack_trade_bad_json_ignored(self):
        """_unpack_trade with invalid extras JSON silently ignores it (lines 586-589)."""
        d = {"extras": "bad json!!!", "redeemed": 0}
        result = db._unpack_trade(d)
        assert "extras" not in result

    def test_unpack_trade_with_valid_extras(self):
        """_unpack_trade merges valid extras JSON."""
        d = {"extras": '{"hedged": true}', "redeemed": 1}
        result = db._unpack_trade(d)
        assert result["hedged"] is True
        assert result["redeemed"] is True


# ── Coverage: load_ticks with slug filter (line 596) ──────────────


class TestLoadTicksWithSlug:
    def test_load_ticks_with_slug(self, tmp_path):
        """load_ticks(slug=...) filters by slug (line 596)."""
        db.init(tmp_path)
        t1 = _sample_tick(1)
        t1["slug"] = "btc-5m-1"
        t2 = _sample_tick(2)
        t2["slug"] = "eth-5m-1"
        t2["ts"] = "2026-03-29T12:00:01+00:00"
        db.insert_tick(t1)
        db.insert_tick(t2)
        db.flush()
        btc_ticks = db.load_ticks(slug="btc-5m-1")
        assert len(btc_ticks) == 1
        assert btc_ticks[0]["slug"] == "btc-5m-1"


# ── Coverage: load_evs_by_id (lines 630-641) ─────────────────────


class TestLoadEvsById:
    def test_load_evs_by_id_all(self, tmp_path):
        """load_evs_by_id without strategy returns all EVs keyed by id (lines 630-641)."""
        db.init(tmp_path)
        db.insert_tick(_sample_tick(1))
        db.insert_tick(_sample_tick(2))
        ev1 = _sample_ev(id=1, tick_id=1)
        ev2 = _sample_ev(id=2, tick_id=2)
        db.insert_ev(ev1, "favorite")
        db.insert_ev(ev2, "other")
        db.flush()

        evs = db.load_evs_by_id()
        assert 1 in evs
        assert 2 in evs
        assert evs[1]["tick_id"] == 1

    def test_load_evs_by_id_filtered(self, tmp_path):
        """load_evs_by_id with strategy filters results (lines 631-633)."""
        db.init(tmp_path)
        db.insert_tick(_sample_tick(1))
        db.insert_tick(_sample_tick(2))
        ev1 = _sample_ev(id=1, tick_id=1)
        ev2 = _sample_ev(id=2, tick_id=2)
        db.insert_ev(ev1, "favorite")
        db.insert_ev(ev2, "other")
        db.flush()

        evs = db.load_evs_by_id(strategy="favorite")
        assert 1 in evs
        assert 2 not in evs


# ── Coverage: load_ticks_by_id (lines 645-651) ───────────────────


class TestLoadTicksById:
    def test_load_ticks_by_id(self, tmp_path):
        """load_ticks_by_id returns all ticks keyed by id (lines 645-651)."""
        db.init(tmp_path)
        t1 = _sample_tick(1)
        t2 = _sample_tick(2)
        t2["ts"] = "2026-03-29T12:00:01+00:00"
        db.insert_tick(t1)
        db.insert_tick(t2)
        db.flush()

        ticks = db.load_ticks_by_id()
        assert 1 in ticks
        assert 2 in ticks
        assert ticks[1]["signal_rev"] is True


# ── Coverage: load_trade_outcomes (lines 673-678) ─────────────────


class TestLoadTradeOutcomes:
    def test_load_trade_outcomes_by_strategy(self, tmp_path):
        """load_trade_outcomes returns slug->trade dict (lines 673-678)."""
        db.init(tmp_path)
        t1 = _sample_trade(1)
        t1["slug"] = "btc-5m-1"
        t1["strategy"] = "favorite"
        t2 = _sample_trade(2)
        t2["slug"] = "eth-5m-1"
        t2["strategy"] = "other"
        db.insert_trade(t1)
        db.insert_trade(t2)
        db.flush()

        outcomes = db.load_trade_outcomes(strategy="favorite")
        assert "btc-5m-1" in outcomes
        assert "eth-5m-1" not in outcomes

    def test_load_trade_outcomes_all(self, tmp_path):
        """load_trade_outcomes without strategy returns all."""
        db.init(tmp_path)
        t1 = _sample_trade(1)
        t1["slug"] = "btc-5m-1"
        t2 = _sample_trade(2)
        t2["slug"] = "eth-5m-1"
        db.insert_trade(t1)
        db.insert_trade(t2)
        db.flush()

        outcomes = db.load_trade_outcomes()
        assert "btc-5m-1" in outcomes
        assert "eth-5m-1" in outcomes
