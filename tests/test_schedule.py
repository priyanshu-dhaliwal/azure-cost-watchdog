"""Tests for schedule parsing and the stopped-minutes maths.

`stopped_minutes_per_week` is the number the savings report multiplies by the
hourly price, so an off-by-one here is a wrong number in a document that claims
to be measured.
"""

from __future__ import annotations

import sys
import pathlib
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from shared.schedule import (  # noqa: E402
    ScheduleError, next_window_start, parse, stopped_minutes_per_week,
)

TZ = "Asia/Kolkata"


def test_simple_weekday_window():
    s = parse("mon-fri 09:00-19:00")
    assert len(s.days) == 5
    assert s.daily_minutes() == 600
    assert s.weekly_minutes() == 3000
    assert stopped_minutes_per_week(s) == 7 * 24 * 60 - 3000   # 7080


def test_running_inside_and_outside_the_window():
    # Schedule maths is on wall-clock time; the caller converts with local_now().
    s = parse("mon-fri 09:00-19:00")
    assert s.is_running_at(datetime(2025, 3, 3, 10, 0))      # Monday 10:00
    assert not s.is_running_at(datetime(2025, 3, 3, 20, 0))  # Monday 20:00


def test_boundary_is_half_open():
    """19:00 exactly is outside a 09:00-19:00 window — otherwise a resource is
    reported as running for a minute it is not."""
    s = parse("mon-fri 09:00-19:00")
    assert s.is_running_at(datetime(2025, 3, 3, 18, 59))
    assert not s.is_running_at(datetime(2025, 3, 3, 19, 0))


def test_lunch_break_gap():
    s = parse("mon-fri 09:00-13:00,14:00-19:00")
    assert s.daily_minutes() == 240 + 300
    gap = datetime(2025, 3, 3, 13, 30)
    assert not s.is_running_at(gap)


def test_weekend_off():
    s = parse("mon-fri 09:00-19:00")
    saturday = datetime(2025, 3, 8, 10, 0)
    assert not s.is_running_at(saturday)


def test_explicit_weekend_schedule():
    s = parse("sat,sun 10:00-14:00")
    assert s.days == frozenset({5, 6})
    assert stopped_minutes_per_week(s) == 7 * 24 * 60 - 2 * 240


def test_off_schedule_means_never_running():
    s = parse("sat,sun off")
    assert s.never
    assert stopped_minutes_per_week(s) == 7 * 24 * 60


def test_24x7_saves_nothing():
    s = parse("24x7")
    assert s.always
    assert stopped_minutes_per_week(s) == 0


def test_overnight_window_crosses_midnight():
    # The window is expressed in local wall-clock time, so the test constructs
    # naive local times. 2025-03-03 is a Monday.
    s = parse("mon 22:00-02:00")
    assert s.is_running_at(datetime(2025, 3, 3, 23, 0))
    assert s.is_running_at(datetime(2025, 3, 4, 1, 0))     # Tuesday 01:00 — window still open
    assert not s.is_running_at(datetime(2025, 3, 4, 3, 0))
    assert s.daily_minutes() == 240


def test_wrapping_day_range():
    s = parse("fri-mon 09:00-17:00")
    assert s.days == frozenset({4, 5, 6, 0})


@pytest.mark.parametrize("bad", [
    "", "mon-fri", "mon-fri 9-19", "mon-fri 09:00", "xyz 09:00-19:00",
    "mon-fri 25:00-26:00", "mon-fri 09:00-19:00,", "  ",
])
def test_malformed_schedules_raise_rather_than_guess(bad):
    with pytest.raises(ScheduleError):
        parse(bad)


def test_start_boundary_detected_within_tolerance():
    s = parse("mon-fri 09:00-19:00")
    assert s.is_start_boundary(datetime(2025, 3, 3, 9, 5))
    assert not s.is_start_boundary(datetime(2025, 3, 3, 9, 40))


def test_next_window_start_is_utc_and_in_the_future():
    s = parse("mon-fri 09:00-19:00")
    now = datetime(2025, 3, 8, 12, 0, tzinfo=timezone.utc)   # Saturday
    nxt = next_window_start(s, TZ, now)
    assert nxt is not None and nxt > now
    assert nxt.tzinfo is timezone.utc
    # Monday 09:00 IST == Monday 03:30 UTC
    assert (nxt.hour, nxt.minute) == (3, 30)


def test_unknown_timezone_raises_a_clear_error():
    s = parse("mon-fri 09:00-19:00")
    with pytest.raises(ScheduleError, match="unknown timezone"):
        next_window_start(s, "Mars/Olympus_Mons", datetime(2025, 3, 8, tzinfo=timezone.utc))
