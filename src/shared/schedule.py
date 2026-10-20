"""
Schedule policy: when should non-production infrastructure be running?

All times are wall-clock in a configured IANA timezone (default Asia/Kolkata),
which is what the tag `autoShutdownSchedule` refers to. Converting at the edges
and computing in UTC avoids the two classic bugs: DST drift and "it shut down an
hour early in winter".

Schedule format (tag value):
    "mon-fri 09:00-19:00"            one window
    "mon-fri 09:00-13:00,14:00-19:00" two windows (lunch break)
    "sat,sun off"                    never running
    "24x7"                           always running (effectively excluded)

Outside every window the scheduler issues "stop"; inside the first window of the
day it issues "start" (so a machine stopped overnight comes back up).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Iterable
try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]

DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

WINDOW_RE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


class ScheduleError(ValueError):
    """A malformed schedule tag. Never guessed at — a bad schedule is a bug."""


@dataclass(frozen=True)
class Window:
    start: time
    end: time

    def contains(self, t: time) -> bool:
        if self.start <= self.end:
            return self.start <= t < self.end
        # Window crosses midnight, e.g. 22:00-02:00
        return t >= self.start or t < self.end

    def minutes(self) -> int:
        start = self.start.hour * 60 + self.start.minute
        end = self.end.hour * 60 + self.end.minute
        return (end - start) % (24 * 60) or 24 * 60


@dataclass(frozen=True)
class Schedule:
    days: frozenset[int]              # 0=Mon
    windows: tuple[Window, ...]
    always: bool = False
    never: bool = False

    def is_running_at(self, when: datetime) -> bool:
        if self.always:
            return True
        if self.never:
            return False
        if when.weekday() in self.days and any(w.contains(when.time()) for w in self.windows):
            return True
        # A window that crosses midnight belongs to the day it STARTS on, so
        # Monday 22:00-02:00 still covers Tuesday 01:00.
        yesterday = (when - timedelta(days=1)).weekday()
        if yesterday in self.days:
            for w in self.windows:
                if w.start > w.end and when.time() < w.end:
                    return True
        return False

    def is_start_boundary(self, when: datetime, tolerance_minutes: int = 15) -> bool:
        """
        True if `when` is at (or just after) the start of a window today.

        The scheduler runs every 15 minutes, so "the window opened since the last
        run" is the practical definition of a start boundary.
        """
        if self.always or self.never or when.weekday() not in self.days:
            return False
        now_min = when.hour * 60 + when.minute
        for w in self.windows:
            start_min = w.start.hour * 60 + w.start.minute
            if 0 <= now_min - start_min < tolerance_minutes:
                return True
        return False

    def daily_minutes(self) -> int:
        if self.always:
            return 24 * 60
        if self.never:
            return 0
        return sum(w.minutes() for w in self.windows)

    def weekly_minutes(self) -> int:
        return self.daily_minutes() * len(self.days)


def _parse_days(spec: str) -> frozenset[int]:
    spec = spec.strip().lower()
    days: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            if a not in DAY_INDEX or b not in DAY_INDEX:
                raise ScheduleError(f"unknown day in range '{part}'")
            start, end = DAY_INDEX[a], DAY_INDEX[b]
            if start <= end:
                days.update(range(start, end + 1))
            else:                       # e.g. fri-mon
                days.update(list(range(start, 7)) + list(range(0, end + 1)))
        else:
            if part not in DAY_INDEX:
                raise ScheduleError(f"unknown day '{part}'")
            days.add(DAY_INDEX[part])
    if not days:
        raise ScheduleError("no days specified")
    return frozenset(days)


def _parse_window(spec: str) -> Window:
    m = WINDOW_RE.match(spec.strip())
    if not m:
        raise ScheduleError(f"bad window '{spec}', expected HH:MM-HH:MM")
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    if not (0 <= h1 <= 23 and 0 <= h2 <= 24 and 0 <= m1 <= 59 and 0 <= m2 <= 59):
        raise ScheduleError(f"window out of range: '{spec}'")
    return Window(time(h1, m1), time(0, 0) if h2 == 24 else time(h2, m2))


def parse(spec: str) -> Schedule:
    """Parse a schedule tag value. Raises ScheduleError on anything ambiguous."""
    raw = (spec or "").strip().lower()
    if not raw:
        raise ScheduleError("empty schedule")
    if raw in ("24x7", "always", "always-on"):
        return Schedule(days=frozenset(range(7)), windows=(), always=True)
    if raw in ("off", "never", "disabled"):
        return Schedule(days=frozenset(), windows=(), never=True)

    if raw.endswith(" off"):
        days = _parse_days(raw[: -len(" off")])
        return Schedule(days=days, windows=(), never=True)

    if " " not in raw:
        raise ScheduleError(f"expected '<days> <windows>', got '{spec}'")
    day_part, window_part = raw.split(" ", 1)
    days = _parse_days(day_part)
    windows = tuple(_parse_window(w) for w in window_part.split(","))
    if not windows:
        raise ScheduleError("no windows")
    return Schedule(days=days, windows=windows)


def _tz(tz_name: str):
    """
    Resolve a timezone, falling back to a fixed offset for the handful of zones
    that matter if the tzdata package is absent (it is not shipped with Windows
    Python by default — a real deployment footgun worth handling).
    """
    if ZoneInfo is not None:
        try:
            return ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError and friends
            pass
    fallbacks = {
        "asia/kolkata": timezone(timedelta(hours=5, minutes=30), "IST"),
        "utc": timezone.utc,
    }
    try:
        return fallbacks[tz_name.lower()]
    except KeyError as exc:
        raise ScheduleError(
            f"unknown timezone '{tz_name}' and no tzdata available; "
            "install the 'tzdata' package or use one of: " + ", ".join(fallbacks)
        ) from exc


def local_now(tz_name: str, now_utc: datetime | None = None) -> datetime:
    """The wall clock in the policy timezone. All schedule maths happens here."""
    now_utc = now_utc or datetime.now(timezone.utc)
    return now_utc.astimezone(_tz(tz_name))


def next_window_start(schedule: Schedule, tz_name: str,
                      now_utc: datetime | None = None) -> datetime | None:
    """
    When will this resource next be allowed to run? Used by the report and by
    the keep-alive endpoint to tell a developer how long they can have.
    """
    if schedule.always:
        return None
    if schedule.never:
        return None

    tz = _tz(tz_name)
    start_utc = (now_utc or datetime.now(timezone.utc)).astimezone(tz)
    for day_offset in range(0, 8):
        day = (start_utc + timedelta(days=day_offset)).date()
        if datetime.combine(day, time(0, 0), tzinfo=tz).weekday() not in schedule.days:
            continue
        for w in sorted(schedule.windows, key=lambda w: w.start):
            candidate = datetime.combine(day, w.start, tzinfo=tz)
            if candidate > start_utc:
                return candidate.astimezone(timezone.utc)
    return None


def stopped_minutes_per_week(schedule: Schedule) -> int:
    """
    Minutes per week the resource is OFF, per this schedule.

    This is the number the savings report multiplies by the hourly price, so it
    is the one number that must be right.
    """
    total = 7 * 24 * 60
    return max(0, total - schedule.weekly_minutes())
