from __future__ import annotations

from datetime import datetime

import pytest

from cron_postmortem import calendarspec

DAY_START = datetime(2026, 9, 17)  # a Thursday
DAY_END = datetime(2026, 9, 18, 23, 59, 59)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("daily", ["2026-09-17 00:00:00", "2026-09-18 00:00:00"]),
        ("*-*-* 03:00:00", ["2026-09-17 03:00:00", "2026-09-18 03:00:00"]),
        ("03:00", ["2026-09-17 03:00:00", "2026-09-18 03:00:00"]),
        ("Fri *-*-* 09:00:00", ["2026-09-18 09:00:00"]),
        ("Mon..Fri 09:00", ["2026-09-17 09:00:00", "2026-09-18 09:00:00"]),
        ("Sat,Sun 09:00", []),
        ("*-09-18 04:30:00", ["2026-09-18 04:30:00"]),
        ("09-18 04:30", ["2026-09-18 04:30:00"]),
        ("2026-09-18 04:30:00", ["2026-09-18 04:30:00"]),
        ("2025-09-18 04:30:00", []),
    ],
)
def test_expressions(expression, expected):
    got = calendarspec.parse(expression).occurrences(DAY_START, DAY_END)
    assert [moment.isoformat(sep=" ") for moment in got] == expected


def test_hourly_and_steps():
    hourly = calendarspec.parse("hourly").occurrences(DAY_START, datetime(2026, 9, 17, 5))
    assert len(hourly) == 6
    stepped = calendarspec.parse("*-*-* 00/6:00:00").occurrences(
        DAY_START, datetime(2026, 9, 17, 23, 59)
    )
    assert [moment.hour for moment in stepped] == [0, 6, 12, 18]


def test_weekday_range_wraps():
    schedule = calendarspec.parse("Fri..Mon 00:00:00")
    assert schedule.weekdays == frozenset({4, 5, 6, 0})


def test_timezone_suffix_is_recorded_not_applied():
    schedule = calendarspec.parse("*-*-* 03:00:00 Europe/Berlin")
    assert schedule.timezone == "Europe/Berlin"
    assert schedule.occurrences(DAY_START, DAY_END)[0] == datetime(2026, 9, 17, 3)


def test_minutely_shorthand():
    got = calendarspec.parse("minutely").occurrences(
        datetime(2026, 9, 17, 0, 0), datetime(2026, 9, 17, 0, 4, 59)
    )
    assert len(got) == 5


@pytest.mark.parametrize(
    "expression",
    ["", "*-*-* *:*:*", "Mon..Xyz 09:00", "*-*-* 25:00:00", "*-*-* 03:00:00 extra bits here",
     "*-*-~1 03:00:00"],
)
def test_rejects_unsupported(expression):
    with pytest.raises(calendarspec.CalendarParseError):
        calendarspec.parse(expression)


def test_refuses_to_enumerate_an_absurd_window():
    schedule = calendarspec.parse("minutely")
    with pytest.raises(calendarspec.CalendarParseError):
        schedule.occurrences(datetime(2026, 1, 1), datetime(2026, 12, 31))
