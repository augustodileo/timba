"""SQLite storage layer for ticks, EVs, and trades.

Architecture:
  - ONE writer thread owns the single write connection (serializes all inserts)
  - Any thread can call insert_tick/ev/trade — they queue the write
  - Read connections are opened on demand for queries
  - DB rotates daily (or at size threshold) — writer swaps connection live

Uses WAL journal mode for concurrent read/write.  On macOS native this is safe;
Docker bind mounts should use --local mode (no WAL corruption risk).
"""

import json
import logging
import queue
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Module-level state — set once by init(), updated by rotate().
_db_path: Path | None = None
_data_dir: Path | None = None
_write_queue: queue.Queue | None = None
_writer_thread: threading.Thread | None = None
_writer_running = False

# Rotation coordination
_rotation_event = threading.Event()   # signal writer to close connection
_rotation_done = threading.Event()    # writer confirms connection closed
_rotation_lock = threading.Lock()
_db_version = 0  # incremented on each rotation; read conns check this

DB_FILENAME = "bot.db"
DB_MAX_SIZE_MB = 500  # rotate if DB exceeds this

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS ticks (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    slug TEXT NOT NULL,
    coin TEXT NOT NULL,
    interval TEXT NOT NULL,
    mid_up REAL NOT NULL,
    mid_down REAL NOT NULL,
    fill_up REAL NOT NULL,
    fill_down REAL NOT NULL,
    signal_dir TEXT NOT NULL,
    signal_chg REAL NOT NULL,
    signal_trend_sec REAL NOT NULL,
    signal_rev INTEGER NOT NULL,
    price_open REAL NOT NULL DEFAULT 0.0,
    price_now REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS evs (
    id INTEGER PRIMARY KEY,
    tick_id INTEGER NOT NULL,
    slug TEXT NOT NULL DEFAULT '',
    strategy TEXT NOT NULL,
    remaining REAL,
    progress REAL,
    ev_up REAL,
    ev_down REAL,
    p_up REAL,
    p_down REAL,
    extras TEXT,
    FOREIGN KEY (tick_id) REFERENCES ticks(id)
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL,
    strategy TEXT NOT NULL,
    slug TEXT NOT NULL,
    condition_id TEXT,
    coin TEXT NOT NULL DEFAULT '',
    interval TEXT NOT NULL DEFAULT '',
    side TEXT,
    buy_price REAL,
    contracts INTEGER,
    pnl REAL,
    sniped_at TEXT,
    resolved_at TEXT,
    end_timestamp INTEGER,
    market_mode TEXT,
    skip_reason TEXT,
    ticks_evaluated INTEGER,
    ev_id INTEGER,
    token_id TEXT,
    redeemed INTEGER DEFAULT 0,
    order_id TEXT,
    min_price REAL,
    midpoint REAL,
    extras TEXT,
    FOREIGN KEY (ev_id) REFERENCES evs(id)
);

CREATE INDEX IF NOT EXISTS idx_ticks_slug ON ticks(slug);
CREATE INDEX IF NOT EXISTS idx_ticks_ts ON ticks(ts);
CREATE INDEX IF NOT EXISTS idx_evs_tick_id ON evs(tick_id);
CREATE INDEX IF NOT EXISTS idx_evs_strategy ON evs(strategy);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
CREATE INDEX IF NOT EXISTS idx_trades_slug ON trades(slug);
CREATE INDEX IF NOT EXISTS idx_trades_type ON trades(type);
CREATE INDEX IF NOT EXISTS idx_trades_unredeemed ON trades(type, redeemed);
"""

# Known columns per table — everything else goes to extras JSON
_EV_KNOWN = frozenset({
    "id", "tick_id", "slug", "strategy",
    "remaining", "progress", "ev_up", "ev_down", "p_up", "p_down",
})
_TRADE_KNOWN = frozenset({
    "id", "type", "strategy", "slug", "condition_id", "coin", "interval",
    "side", "buy_price", "contracts", "pnl", "sniped_at", "resolved_at",
    "end_timestamp", "market_mode", "skip_reason",
    "ticks_evaluated", "ev_id", "token_id", "redeemed", "order_id",
    "min_price", "midpoint",
})


# ── Initialization ──────────────────────────────────────────────────


def init(data_dir: str | Path) -> Path:
    """Initialize the database and start the writer thread.

    Call once at startup. Creates data/{env}/bot.db with tables and indexes.
    Returns the db_path for reference.
    """
    global _db_path, _data_dir, _write_queue, _writer_thread, _writer_running
    _data_dir = Path(data_dir)
    _db_path = _data_dir / DB_FILENAME
    _db_path.parent.mkdir(parents=True, exist_ok=True)

    # Create schema using a temporary connection
    conn = sqlite3.connect(str(_db_path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript(SCHEMA_SQL)
    # Schema migrations for existing databases
    try:
        conn.execute("ALTER TABLE trades ADD COLUMN order_id TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    conn.close()

    # Start writer thread
    _write_queue = queue.Queue(maxsize=50_000)
    _writer_running = True
    _writer_thread = threading.Thread(target=_writer_loop, daemon=True, name="db-writer")
    _writer_thread.start()

    logger.info("Database ready: %s", _db_path)
    return _db_path


def is_initialized() -> bool:
    """Check if the database has been initialized."""
    return _db_path is not None and _writer_running


def flush() -> None:
    """Wait for all queued writes to be committed. Call before reading after writes."""
    if _write_queue is None:
        return
    _write_queue.join()


def reset() -> None:
    """Reset module state. For tests only."""
    global _db_path, _data_dir, _write_queue, _writer_thread, _writer_running, _db_version
    if _write_queue is not None:
        _write_queue.join()  # drain pending writes first
    _writer_running = False
    if _write_queue is not None:
        _write_queue.put(None)  # signal writer to stop
    if _writer_thread is not None:
        _writer_thread.join(timeout=5)
    _write_queue = None
    _writer_thread = None
    # Close any read connections on this thread
    conns = getattr(_read_local, "connections", {})
    for conn in conns.values():
        try:
            conn.close()
        except Exception:
            pass
    _read_local.connections = {}
    _db_path = None
    _data_dir = None
    _db_version = 0
    _rotation_event.clear()
    _rotation_done.clear()


# ── Rotation ───────────────────────────────────────────────────────


def rotate(reason: str = "scheduled") -> Path | None:
    """Rotate bot.db to bot_YYYY-MM-DD.db and start a fresh one.

    Thread-safe.  The write queue stays alive throughout — no items are lost.
    The writer thread swaps its connection via _rotation_event.
    Returns the archived file path, or None if rotation was skipped.
    """
    global _db_path, _db_version

    if _db_path is None or _data_dir is None:
        return None

    with _rotation_lock:
        old_path = _db_path
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        archive_name = f"bot_{date_str}.db"
        archive_path = _data_dir / archive_name

        # Handle multiple rotations on the same day
        if archive_path.exists():
            i = 1
            while True:
                archive_name = f"bot_{date_str}_{i}.db"
                archive_path = _data_dir / archive_name
                if not archive_path.exists():
                    break
                i += 1

        # 1. Flush pending writes to the old DB
        if _write_queue is not None:
            _write_queue.join()

        # 2. Close all connections before renaming (required on Windows)
        _rotation_done.clear()
        _rotation_event.set()  # signal writer to close
        _rotation_done.wait(timeout=5)  # wait for writer to confirm

        # Close read connections on this thread
        conns = getattr(_read_local, "connections", {})
        for c in conns.values():
            try:
                c.close()
            except Exception:
                pass
        _read_local.connections = {}

        # 3. Rename current DB (+ WAL/SHM files) — safe now, all connections closed
        old_path.rename(archive_path)
        for suffix in ("-wal", "-shm"):
            src = Path(str(old_path) + suffix)
            if src.exists():
                dst = Path(str(archive_path) + suffix)
                src.rename(dst)

        # 4. Create fresh bot.db with schema
        new_path = _data_dir / DB_FILENAME
        _db_path = new_path
        conn = sqlite3.connect(str(_db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        conn.close()

        # 5. Invalidate read connection caches + let writer reopen
        _db_version += 1
        _rotation_event.clear()  # writer sees this and reopens connection

        logger.info("DB rotated: %s -> %s (%s)", DB_FILENAME, archive_name, reason)
        return archive_path


def db_size_mb() -> float:
    """Return current DB file size in MB."""
    if _db_path is None or not _db_path.exists():
        return 0.0
    return _db_path.stat().st_size / (1024 * 1024)


def list_databases() -> list[Path]:
    """Return all DB files (rotated + current), sorted chronologically."""
    if _data_dir is None:
        return []
    dbs = sorted(_data_dir.glob("bot_*.db"))
    current = _data_dir / DB_FILENAME
    if current.exists():
        dbs.append(current)
    return dbs


def copy_ticks_from(source_db_paths: list[Path], since: str | None = None) -> int:
    """Copy ticks from source DBs into the current DB using ATTACH.

    Used by backtest to seed the backtest DB with production tick data.
    Must be called after init() and before the simulation loop.
    since: ISO date string (e.g. '2026-03-30') to only copy ticks from that date onward.
    Returns total ticks copied.
    """
    if _db_path is None:
        raise RuntimeError("db.init() must be called before copy_ticks_from()")

    total = 0
    conn = sqlite3.connect(str(_db_path), timeout=30)
    conn.execute("PRAGMA foreign_keys=OFF")  # bulk copy, no FK checks on ticks

    for src_path in source_db_paths:
        if not src_path.exists():
            continue
        try:
            conn.execute("ATTACH DATABASE ? AS src", (str(src_path),))
            if since:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO ticks SELECT * FROM src.ticks WHERE ts >= ?",
                    (since,),
                )
            else:
                cursor = conn.execute("INSERT OR IGNORE INTO ticks SELECT * FROM src.ticks")
            copied = cursor.rowcount
            total += copied
            conn.commit()
            conn.execute("DETACH DATABASE src")
        except Exception:
            logger.debug("Failed to copy ticks from %s", src_path, exc_info=True)
            try:
                conn.execute("DETACH DATABASE src")
            except Exception:
                pass

    conn.close()
    return total


def list_source_databases(data_dir: str | Path) -> list[Path]:
    """Return all DB files in a given data dir (for reading from another env)."""
    d = Path(data_dir)
    dbs = sorted(d.glob("bot_*.db"))
    current = d / DB_FILENAME
    if current.exists():
        dbs.append(current)
    return dbs


# ── Writer thread ───────────────────────────────────────────────────


def _open_writer_conn() -> sqlite3.Connection:
    """Open a write connection to the current _db_path."""
    conn = sqlite3.connect(str(_db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _writer_loop():
    """Single writer thread: drains the queue and commits in batches.

    Owns the only write connection. Commits after draining all pending items
    (or every 100 items) for throughput. Checks _rotation_event after each
    batch to swap connections when the DB is rotated.
    """
    conn = _open_writer_conn()
    BATCH_SIZE = 100

    # Capture queue ref so teardown (reset()) setting _write_queue=None doesn't crash us
    q = _write_queue

    while _writer_running or (q is not None and not q.empty()):
        # Rotation: close connection, signal done, wait for rotate() to finish
        if _rotation_event.is_set():
            conn.close()
            _rotation_done.set()
            # Wait until rotate() clears _rotation_event (file ops done)
            while _rotation_event.is_set():
                import time as _t
                _t.sleep(0.005)
            conn = _open_writer_conn()

        try:
            item = q.get(timeout=0.5)
        except queue.Empty:
            continue

        if item is None:
            q.task_done()
            break  # shutdown signal

        batch = [item]
        while len(batch) < BATCH_SIZE:
            try:
                item = q.get_nowait()
                if item is None:
                    break
                batch.append(item)
            except queue.Empty:
                break

        try:
            for sql, params in batch:
                conn.execute(sql, params)
            conn.commit()
        except Exception:
            logger.exception("db writer: batch commit failed (%d items), retrying individually", len(batch))
            try:
                conn.rollback()
            except Exception:
                pass
            # Retry each item individually so one bad statement doesn't sink the batch
            for sql, params in batch:
                try:
                    conn.execute(sql, params)
                    conn.commit()
                except Exception:
                    logger.exception("db writer: item failed permanently: %s", sql[:80])
                    try:
                        conn.rollback()
                    except Exception:
                        pass
        finally:
            for _ in batch:
                q.task_done()

    conn.close()


def _enqueue(sql: str, params: tuple) -> None:
    """Put a write operation on the queue. Non-blocking, never raises."""
    if _write_queue is None:
        return  # db not initialized — silently drop
    try:
        _write_queue.put_nowait((sql, params))
    except Exception:
        pass  # queue full or broken — never block the caller


# ── Read connection (any thread) ────────────────────────────────────

_read_local = threading.local()


def _get_read_connection() -> sqlite3.Connection:
    """Return a thread-local read connection. Invalidates on DB rotation."""
    if _db_path is None:
        raise RuntimeError("db.init() must be called before any database operation")
    if not hasattr(_read_local, "connections"):
        _read_local.connections = {}
    if not hasattr(_read_local, "version"):
        _read_local.version = -1

    # If DB was rotated, close stale connections and reopen
    if _read_local.version != _db_version:
        for conn in _read_local.connections.values():
            try:
                conn.close()
            except Exception:
                pass
        _read_local.connections = {}
        _read_local.version = _db_version

    key = str(_db_path)
    conn = _read_local.connections.get(key)
    if conn is None:
        conn = sqlite3.connect(key, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        _read_local.connections[key] = conn
    return conn


# ── ID seeding ──────────────────────────────────────────────────────


def max_id(table: str) -> int:
    """Return the maximum id in a table, or 0 if empty."""
    if table not in ("ticks", "evs", "trades"):
        raise ValueError(f"Unknown table: {table}")
    conn = _get_read_connection()
    row = conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()  # noqa: S608
    return row[0] or 0


# ── Write operations (queue to writer thread) ───────────────────────


_INSERT_TICK_SQL = """\
INSERT INTO ticks (id, ts, slug, coin, interval,
   mid_up, mid_down, fill_up, fill_down,
   signal_dir, signal_chg, signal_trend_sec, signal_rev,
   price_open, price_now)
   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""

_INSERT_EV_SQL = """\
INSERT INTO evs (id, tick_id, slug, strategy,
   remaining, progress, ev_up, ev_down, p_up, p_down, extras)
   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""

_INSERT_TRADE_SQL = """\
INSERT INTO trades (id, type, strategy, slug, condition_id, coin,
   interval, side, buy_price, contracts, pnl,
   sniped_at, resolved_at, end_timestamp, market_mode,
   skip_reason, ticks_evaluated, ev_id,
   token_id, redeemed, order_id, min_price, midpoint, extras)
   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


def insert_tick(tick: dict) -> None:
    """Queue a tick insert."""
    _enqueue(_INSERT_TICK_SQL, (
        tick["id"], tick["ts"], tick["slug"], tick["coin"], tick["interval"],
        tick["mid_up"], tick["mid_down"], tick["fill_up"], tick["fill_down"],
        tick["signal_dir"], tick["signal_chg"], tick["signal_trend_sec"],
        1 if tick["signal_rev"] else 0,
        tick.get("price_open", 0.0), tick.get("price_now", 0.0),
    ))


def insert_ev(ev: dict, strategy: str) -> None:
    """Queue an EV insert."""
    extras = {k: v for k, v in ev.items() if k not in _EV_KNOWN}
    _enqueue(_INSERT_EV_SQL, (
        ev["id"], ev["tick_id"], ev.get("slug", ""), strategy,
        ev.get("remaining"), ev.get("progress"),
        ev.get("ev_up"), ev.get("ev_down"),
        ev.get("p_up"), ev.get("p_down"),
        json.dumps(extras) if extras else None,
    ))


def insert_trade(entry: dict) -> None:
    """Queue a trade insert."""
    extras = {k: v for k, v in entry.items() if k not in _TRADE_KNOWN}
    ev_id = entry.get("ev_id") or None
    _enqueue(_INSERT_TRADE_SQL, (
        entry["id"], entry["type"], entry.get("strategy", ""),
        entry["slug"], entry.get("condition_id"), entry.get("coin", ""),
        entry.get("interval", ""), entry.get("side"),
        entry.get("buy_price"), entry.get("contracts"), entry.get("pnl"),
        entry.get("sniped_at"), entry.get("resolved_at"),
        entry.get("end_timestamp"), entry.get("market_mode"),
        entry.get("skip_reason"),
        entry.get("ticks_evaluated"), ev_id,
        entry.get("token_id"), 1 if entry.get("redeemed") else 0,
        entry.get("order_id"), entry.get("min_price"), entry.get("midpoint"),
        json.dumps(extras) if extras else None,
    ))


# ── Read operations ─────────────────────────────────────────────────


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _unpack_tick(d: dict) -> dict:
    d["signal_rev"] = bool(d["signal_rev"])
    return d


def _unpack_ev(d: dict) -> dict:
    extras_raw = d.pop("extras", None)
    if extras_raw:
        try:
            d.update(json.loads(extras_raw))
        except (json.JSONDecodeError, TypeError):
            pass
    return d


def _unpack_trade(d: dict) -> dict:
    d["redeemed"] = bool(d.get("redeemed"))
    extras_raw = d.pop("extras", None)
    if extras_raw:
        try:
            d.update(json.loads(extras_raw))
        except (json.JSONDecodeError, TypeError):
            pass
    return d


def load_ticks(slug: str | None = None) -> list[dict]:
    conn = _get_read_connection()
    if slug:
        rows = conn.execute(
            "SELECT * FROM ticks WHERE slug = ? ORDER BY ts", (slug,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM ticks ORDER BY ts").fetchall()
    return [_unpack_tick(_row_to_dict(r)) for r in rows]


def load_ticks_grouped() -> tuple[dict[str, list[dict]], int]:
    all_ticks = load_ticks()
    by_slug: dict[str, list[dict]] = {}
    for t in all_ticks:
        by_slug.setdefault(t["slug"], []).append(t)
    return by_slug, 0


def load_evs(strategy: str | None = None) -> dict[int, dict]:
    conn = _get_read_connection()
    if strategy:
        rows = conn.execute(
            "SELECT * FROM evs WHERE strategy = ?", (strategy,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM evs").fetchall()
    evs: dict[int, dict] = {}
    for r in rows:
        d = _unpack_ev(_row_to_dict(r))
        tid = d.get("tick_id")
        if tid is not None:
            evs[tid] = d
    return evs


def load_evs_by_id(strategy: str | None = None) -> dict[int, dict]:
    conn = _get_read_connection()
    if strategy:
        rows = conn.execute(
            "SELECT * FROM evs WHERE strategy = ?", (strategy,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM evs").fetchall()
    evs: dict[int, dict] = {}
    for r in rows:
        d = _unpack_ev(_row_to_dict(r))
        evs[d["id"]] = d
    return evs


def load_ticks_by_id() -> dict[int, dict]:
    conn = _get_read_connection()
    rows = conn.execute("SELECT * FROM ticks").fetchall()
    ticks: dict[int, dict] = {}
    for r in rows:
        d = _unpack_tick(_row_to_dict(r))
        ticks[d["id"]] = d
    return ticks


def load_trades(
    strategy: str | None = None,
    since: str | None = None,
) -> list[dict]:
    conn = _get_read_connection()
    clauses: list[str] = []
    params: list = []
    if strategy:
        clauses.append("strategy = ?")
        params.append(strategy)
    if since:
        clauses.append("sniped_at >= ?")
        params.append(since)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(f"SELECT * FROM trades{where} ORDER BY id", params).fetchall()  # noqa: S608
    return [_unpack_trade(_row_to_dict(r)) for r in rows]


def load_trade_outcomes(strategy: str | None = None) -> dict[str, dict]:
    outcomes: dict[str, dict] = {}
    for t in load_trades(strategy=strategy):
        slug = t.get("slug")
        if slug:
            outcomes[slug] = t
    return outcomes


# ── Redemption & stats queries ─────────────────────────────────────


def get_unredeemed_wins() -> list[dict]:
    """Return trades that won but haven't been redeemed yet."""
    conn = _get_read_connection()
    rows = conn.execute(
        "SELECT * FROM trades WHERE type = 'win' AND redeemed = 0 ORDER BY id"
    ).fetchall()
    return [_unpack_trade(_row_to_dict(r)) for r in rows]


def get_pending_redemption() -> float:
    """Sum of contracts from unredeemed wins."""
    conn = _get_read_connection()
    row = conn.execute(
        "SELECT COALESCE(SUM(contracts), 0) FROM trades WHERE type = 'win' AND redeemed = 0"
    ).fetchone()
    return float(row[0])


def mark_trade_redeemed(condition_id: str) -> None:
    """Mark all trades with this condition_id as redeemed."""
    _enqueue(
        "UPDATE trades SET redeemed = 1 WHERE condition_id = ? AND redeemed = 0",
        (condition_id,),
    )


def get_total_pnl() -> float:
    """Sum of PnL across all real (non-paper) trades."""
    conn = _get_read_connection()
    row = conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE type IN ('win', 'loss')"
    ).fetchone()
    return float(row[0])


def get_strategy_pnl(strategy: str) -> float:
    """Sum of PnL for a specific strategy."""
    conn = _get_read_connection()
    row = conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE strategy = ? AND type IN ('win', 'loss')",
        (strategy,),
    ).fetchone()
    return float(row[0])


def get_strategy_stats(strategy: str) -> dict[str, int]:
    """Count trades by type for a strategy."""
    conn = _get_read_connection()
    rows = conn.execute(
        "SELECT type, COUNT(*) FROM trades WHERE strategy = ? GROUP BY type",
        (strategy,),
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def get_all_strategy_stats() -> dict[str, dict]:
    """Count trades by strategy and type. Returns {strategy: {type: count}}."""
    conn = _get_read_connection()
    rows = conn.execute(
        "SELECT strategy, type, COUNT(*) FROM trades GROUP BY strategy, type"
    ).fetchall()
    result: dict[str, dict] = {}
    for strategy, trade_type, count in rows:
        result.setdefault(strategy, {})[trade_type] = count
    return result
