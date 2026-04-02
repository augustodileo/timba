"""Tests for ticks.py — tick/EV ID generation and data writing."""

import json
import sys
from unittest.mock import patch

import pytest

from timba import db
from timba.ticks import (
    _max_id_in_jsonl,
    _next_ev_id,
    _next_tick_id,
    init_ids,
    record_tick,
    write_strategy_data,
)


class TestTickIdCounter:
    def test_increments(self):
        id1 = _next_tick_id()
        id2 = _next_tick_id()
        assert id2 == id1 + 1

    def test_ev_id_increments(self):
        id1 = _next_ev_id()
        id2 = _next_ev_id()
        assert id2 == id1 + 1


class TestInitIds:
    def test_seeds_from_existing_ticks(self, tmp_path):
        """After restart, tick IDs continue from where they left off."""
        db.init(tmp_path)
        db.insert_tick({
            "id": 500, "ts": "2026-03-29T00:00:00", "slug": "test",
            "coin": "btc", "interval": "5m",
            "mid_up": 0.5, "mid_down": 0.5, "fill_up": 0.5, "fill_down": 0.5,
            "signal_dir": "flat", "signal_chg": 0, "signal_trend_sec": 0,
            "signal_rev": False, "price_open": 0, "price_now": 0,
        })
        db.insert_tick({
            "id": 501, "ts": "2026-03-29T00:00:01", "slug": "test",
            "coin": "btc", "interval": "5m",
            "mid_up": 0.5, "mid_down": 0.5, "fill_up": 0.5, "fill_down": 0.5,
            "signal_dir": "flat", "signal_chg": 0, "signal_trend_sec": 0,
            "signal_rev": False, "price_open": 0, "price_now": 0,
        })

        db.flush()
        init_ids()
        next_id = _next_tick_id()
        assert next_id == 502

    def test_seeds_from_existing_evs(self, tmp_path):
        """After restart, EV IDs continue from where they left off."""
        db.init(tmp_path)
        db.insert_tick({
            "id": 1, "ts": "2026-03-29T00:00:00", "slug": "test",
            "coin": "btc", "interval": "5m",
            "mid_up": 0.5, "mid_down": 0.5, "fill_up": 0.5, "fill_down": 0.5,
            "signal_dir": "flat", "signal_chg": 0, "signal_trend_sec": 0,
            "signal_rev": False, "price_open": 0, "price_now": 0,
        })
        db.insert_ev({"id": 200, "tick_id": 1, "slug": "test"}, "favorite")

        db.flush()
        init_ids()
        next_id = _next_ev_id()
        assert next_id == 201

    def test_empty_db_starts_from_one(self, tmp_path):
        db.init(tmp_path)
        db.flush()
        init_ids()
        next_id = _next_tick_id()
        assert next_id >= 1


class TestRecordTick:
    def test_writes_to_sqlite(self, tmp_path):
        db.init(tmp_path)
        tick_id = record_tick(
            "btc-updown-5m-123", "btc", "5m",
            mid_up=0.98, mid_down=0.02,
            fill_up=0.99, fill_down=0.05,
            signal_dir="up", signal_chg=-0.05,
            signal_trend_sec=45.23, signal_rev=False,
            price_open=66000.0, price_now=65980.0,
        )

        db.flush()
        ticks_data = db.load_ticks()
        assert len(ticks_data) >= 1

        tick = next(t for t in ticks_data if t["id"] == tick_id)
        assert tick["slug"] == "btc-updown-5m-123"
        assert tick["coin"] == "btc"
        assert tick["mid_up"] == 0.98
        assert tick["fill_down"] == 0.05
        assert tick["signal_dir"] == "up"
        assert tick["price_open"] == 66000.0

    def test_rounds_signal_values(self, tmp_path):
        db.init(tmp_path)
        tick_id = record_tick(
            "s", "btc", "5m",
            0.5, 0.5, 0.5, 0.5,
            "flat", 0.123456789, 12.3456, False,
        )

        db.flush()
        tick = next(t for t in db.load_ticks() if t["id"] == tick_id)
        assert tick["signal_chg"] == 0.123457  # rounded to 6
        assert tick["signal_trend_sec"] == 12.3  # rounded to 1

    def test_returns_incrementing_ids(self, tmp_path):
        db.init(tmp_path)
        id1 = record_tick("s", "btc", "5m", 0, 0, 0, 0, "flat", 0, 0, False)
        id2 = record_tick("s", "btc", "5m", 0, 0, 0, 0, "flat", 0, 0, False)
        assert id2 == id1 + 1


class TestWriteStrategyData:
    def test_writes_to_strategy_subdir(self, tmp_path):
        """Backtest mode: data_dir provided → writes JSONL."""
        data = {"ev_up": 0.05, "ev_down": -0.02}
        ev_id = write_strategy_data(
            tmp_path, "favorite", "evs", data, slug="btc-5m-123", tick_id=42,
        )

        strat_dir = tmp_path / "favorite"
        assert strat_dir.exists()

        files = list(strat_dir.glob("evs_*.jsonl"))
        assert len(files) == 1

        line = json.loads(files[0].read_text().strip())
        assert line["id"] == ev_id
        assert line["tick_id"] == 42
        assert line["slug"] == "btc-5m-123"
        assert line["ev_up"] == 0.05

    def test_writes_to_sqlite(self, tmp_path):
        """Live mode: data_dir=None → writes SQLite."""
        db.init(tmp_path)
        # Need a tick for FK
        db.insert_tick({
            "id": 42, "ts": "2026-03-29T00:00:00", "slug": "btc-5m-123",
            "coin": "btc", "interval": "5m",
            "mid_up": 0.5, "mid_down": 0.5, "fill_up": 0.5, "fill_down": 0.5,
            "signal_dir": "flat", "signal_chg": 0, "signal_trend_sec": 0,
            "signal_rev": False, "price_open": 0, "price_now": 0,
        })

        data = {"ev_up": 0.05, "ev_down": -0.02}
        ev_id = write_strategy_data(
            None, "favorite", "evs", data, slug="btc-5m-123", tick_id=42,
        )

        db.flush()
        evs = db.load_evs(strategy="favorite")
        assert 42 in evs
        assert evs[42]["id"] == ev_id
        assert evs[42]["ev_up"] == 0.05

    def test_omits_slug_when_empty(self, tmp_path):
        data = {"ev": 0.01}
        write_strategy_data(tmp_path, "favorite", "evs", data, slug="", tick_id=1)

        files = list((tmp_path / "favorite").glob("evs_*.jsonl"))
        line = json.loads(files[0].read_text().strip())
        assert "slug" not in line

    def test_returns_incrementing_ev_ids(self, tmp_path):
        id1 = write_strategy_data(tmp_path, "favorite", "evs", {}, tick_id=1)
        id2 = write_strategy_data(tmp_path, "favorite", "evs", {}, tick_id=2)
        assert id2 == id1 + 1


def _sample_tick(tick_id):
    return {
        "id": tick_id, "ts": "2026-03-30T00:00:00", "slug": "test-slug",
        "coin": "btc", "interval": "5m",
        "mid_up": 0.5, "mid_down": 0.5, "fill_up": 0.5, "fill_down": 0.5,
        "signal_dir": "flat", "signal_chg": 0, "signal_trend_sec": 0,
        "signal_rev": False, "price_open": 0, "price_now": 0,
    }


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot rename open files")
class TestDbRotation:
    def test_rotate_creates_archive(self, tmp_path):
        db.init(tmp_path)
        db.insert_tick(_sample_tick(1))
        db.flush()

        archive = db.rotate("test")
        assert archive is not None
        assert archive.name.startswith("bot_")
        assert archive.exists()
        assert (tmp_path / "bot.db").exists()

    def test_archive_has_old_data(self, tmp_path):
        """Ticks written before rotation end up in the archive."""
        import sqlite3
        db.init(tmp_path)
        db.insert_tick(_sample_tick(1))
        db.insert_tick(_sample_tick(2))
        db.flush()

        archive = db.rotate("test")

        conn = sqlite3.connect(str(archive))
        count = conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
        conn.close()
        assert count == 2

    def test_fresh_db_receives_new_writes(self, tmp_path):
        """After rotation, new writes go to the fresh DB."""
        db.init(tmp_path)
        db.insert_tick(_sample_tick(1))
        db.flush()

        db.rotate("test")

        # Write to the new DB — ID continues from process counter
        db.insert_tick(_sample_tick(99))
        db.flush()

        ticks = db.load_ticks()
        assert len(ticks) == 1
        assert ticks[0]["id"] == 99

    def test_ids_continue_across_rotation(self, tmp_path):
        """IDs are globally monotonic — no reset after rotation."""
        db.init(tmp_path)
        id1 = record_tick("s", "btc", "5m", 0, 0, 0, 0, "flat", 0, 0, False)
        db.flush()

        db.rotate("test")

        id2 = record_tick("s", "btc", "5m", 0, 0, 0, 0, "flat", 0, 0, False)
        assert id2 > id1

    def test_multiple_rotations_same_day(self, tmp_path):
        db.init(tmp_path)
        db.insert_tick(_sample_tick(1))
        db.flush()
        db.rotate("test1")

        db.insert_tick(_sample_tick(2))
        db.flush()
        db.rotate("test2")

        archives = sorted(tmp_path.glob("bot_*.db"))
        assert len(archives) == 2
        assert archives[0].name != archives[1].name

    def test_list_databases(self, tmp_path):
        db.init(tmp_path)
        db.insert_tick(_sample_tick(1))
        db.flush()
        db.rotate("test")

        dbs = db.list_databases()
        assert len(dbs) == 2  # archive + current

    def test_db_size_mb(self, tmp_path):
        db.init(tmp_path)
        db.insert_tick(_sample_tick(1))
        db.flush()
        size = db.db_size_mb()
        assert size > 0
        assert size < 1  # tiny test DB


class TestMaxIdInJsonl:
    def test_nonexistent_file_returns_zero(self, tmp_path):
        assert _max_id_in_jsonl(tmp_path / "nope.jsonl") == 0

    def test_empty_file_returns_zero(self, tmp_path):
        p = tmp_path / "empty.jsonl"
        p.write_text("")
        assert _max_id_in_jsonl(p) == 0

    def test_single_record(self, tmp_path):
        p = tmp_path / "one.jsonl"
        p.write_text(json.dumps({"id": 42, "value": "x"}) + "\n")
        assert _max_id_in_jsonl(p) == 42

    def test_multiple_records(self, tmp_path):
        p = tmp_path / "multi.jsonl"
        lines = [
            json.dumps({"id": 10}),
            json.dumps({"id": 20}),
            json.dumps({"id": 30}),
        ]
        p.write_text("\n".join(lines) + "\n")
        assert _max_id_in_jsonl(p) == 30

    def test_corrupt_lines_skipped(self, tmp_path):
        p = tmp_path / "corrupt.jsonl"
        lines = [
            json.dumps({"id": 5}),
            "NOT VALID JSON {{{",
            json.dumps({"id": 15}),
            "another garbage line",
            json.dumps({"id": 10}),
        ]
        p.write_text("\n".join(lines) + "\n")
        assert _max_id_in_jsonl(p) == 15


class TestErrorPaths:
    def test_record_tick_survives_db_error(self, tmp_path):
        """record_tick returns a tick_id even when db.insert_tick raises."""
        db.init(tmp_path)
        with patch("timba.db.insert_tick", side_effect=RuntimeError("boom")):
            tick_id = record_tick(
                "s", "btc", "5m", 0, 0, 0, 0, "flat", 0, 0, False,
            )
        assert isinstance(tick_id, int)
        assert tick_id > 0

    def test_write_strategy_data_jsonl_survives_oserror(self):
        """JSONL write with an invalid data_dir still returns an ev_id."""
        ev_id = write_strategy_data(
            "/no/such/path/ever", "favorite", "evs", {"ev": 0.01}, tick_id=1,
        )
        assert isinstance(ev_id, int)
        assert ev_id > 0

    def test_write_strategy_data_sqlite_survives_db_error(self, tmp_path):
        """SQLite write failure still returns an ev_id."""
        db.init(tmp_path)
        with patch("timba.db.insert_ev", side_effect=RuntimeError("boom")):
            ev_id = write_strategy_data(
                None, "favorite", "evs", {"ev": 0.01}, tick_id=1,
            )
        assert isinstance(ev_id, int)
        assert ev_id > 0
