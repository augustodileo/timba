"""Maintenance scheduler: sync balances and redeem during safe windows.

Calculates market close times from configured intervals, then identifies
safe windows (2 min buffer around each close) for maintenance tasks.
"""

import logging
import time

from timba.constants import INTERVAL_SECS

logger = logging.getLogger(__name__)

BUFFER_SEC = 120  # 2 minutes before/after each market close


def get_close_minutes(intervals: list[str]) -> set[int]:
    """Get all unique market close minutes within a 60-minute cycle.

    5m → closes at 0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55
    15m → closes at 0, 15, 30, 45
    1h → closes at 0
    """
    minutes = set()
    for iv in intervals:
        secs = INTERVAL_SECS.get(iv, 300)
        period_min = secs // 60
        for m in range(0, 60, period_min):
            minutes.add(m)
    return minutes


def is_safe_window(intervals: list[str], buffer_sec: int = BUFFER_SEC) -> bool:
    """Check if current time is in a safe maintenance window.

    Returns True if we're at least buffer_sec away from any market close.
    """
    now = time.time()
    close_minutes = get_close_minutes(intervals)

    for close_min in close_minutes:
        # Calculate seconds until/since this close within the current hour
        int(now % 3600) // 60
        now % 60

        # Distance in seconds to this close minute
        sec_in_hour = (now % 3600)
        close_sec_in_hour = close_min * 60

        # Check distance (wrapping around the hour)
        dist = abs(sec_in_hour - close_sec_in_hour)
        dist = min(dist, 3600 - dist)  # wrap around

        if dist < buffer_sec:
            return False

    return True


def get_next_safe_window(intervals: list[str], buffer_sec: int = BUFFER_SEC) -> float:
    """Get seconds until the next safe maintenance window starts."""
    now = time.time()
    close_minutes = get_close_minutes(intervals)

    # Check each second in the next 5 minutes
    for offset in range(300):
        future = now + offset
        sec_in_hour = future % 3600
        safe = True
        for close_min in close_minutes:
            close_sec = close_min * 60
            dist = abs(sec_in_hour - close_sec)
            dist = min(dist, 3600 - dist)
            if dist < buffer_sec:
                safe = False
                break
        if safe:
            return offset

    return 300  # fallback: 5 minutes


class MaintenanceScheduler:
    """Runs maintenance tasks during safe windows between market closes."""

    def __init__(self, intervals: list[str], buffer_sec: int = BUFFER_SEC) -> None:
        self.intervals = list(set(intervals))
        self.buffer_sec = buffer_sec
        self.close_minutes = get_close_minutes(self.intervals)
        self._last_balance_sync = 0
        self._last_redeem_scan = 0
        self._balance_interval = 300  # sync balance every 5 min
        self._redeem_interval = 300   # redeem scan every 5 min
        # DB rotation
        self._last_rotation_check = 0
        self._rotation_check_interval = 300  # check every 5 min
        self._last_rotation_date: str | None = None

    def seed_rotation_date(self) -> None:
        """Call after db.init() to prevent spurious rotation on restart."""
        from datetime import datetime, timezone

        from timba import db
        if db.db_size_mb() > 0.1:
            self._last_rotation_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def should_rotate_db(self) -> str | None:
        """Check if DB rotation is due. Returns reason string or None.

        Triggers on: new UTC day (daily rotation) or size > threshold.
        Only called during safe windows.
        """
        from datetime import datetime, timezone

        from timba import db

        now = time.time()
        if (now - self._last_rotation_check) < self._rotation_check_interval:
            return None
        self._last_rotation_check = now

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Size threshold
        size = db.db_size_mb()
        if size >= db.DB_MAX_SIZE_MB:
            return f"size ({size:.0f} MB >= {db.DB_MAX_SIZE_MB} MB)"

        # Daily rotation at midnight UTC
        if self._last_rotation_date != today and size > 1.0:
            return f"daily ({today})"

        return None

    def describe(self) -> str:
        """Describe the schedule for logging."""
        minutes = sorted(self.close_minutes)
        return f"Market closes at :{', :'.join(f'{m:02d}' for m in minutes)} | buffer={self.buffer_sec}s"
