"""Wall-clock quality for history (plan section 8, offline time).

The Pi 5 has no battery-backed RTC by default and runs offline, so its wall clock may be wrong.
Order comes from database sequences and durations from the monotonic clock; wall time is stored with
a quality flag: synced / manually_set / unverified.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

QUALITIES = ("synced", "manually_set", "unverified")
TIMESYNC_FLAG = Path("/run/systemd/timesync/synchronized")   # created by systemd-timesyncd on sync


class Clock:
    """Injectable clock: tests replace now()/monotonic()/quality()."""

    def __init__(self, quality_override: str | None = None):
        env = os.environ.get("LASER_HHRP_CLOCK_QUALITY")
        self._override = quality_override or (env if env in QUALITIES else None)

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def quality(self) -> str:
        if self._override:
            return self._override
        return "synced" if TIMESYNC_FLAG.exists() else "unverified"


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FakeClock(Clock):
    """Deterministic clock for tests."""

    def __init__(self, quality: str = "synced", start: datetime | None = None):
        super().__init__(quality)
        self.t = 1000.0
        self.start = start or datetime(2026, 11, 17, 9, 0, 0, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self.t

    def now(self) -> datetime:
        from datetime import timedelta
        return self.start + timedelta(seconds=self.t - 1000.0)

    def advance(self, seconds: float) -> None:
        self.t += seconds
