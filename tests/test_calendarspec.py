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


@pytest.mark.parametrize(
    "expression",
    ["*-*-* 03:00:00 Europe/Berlin", "Mon *-*-* 09:00:00 UTC", "daily UTC"],
)
def test_a_timezone_suffix_is_refused_rather_than_evaluated_locally(expression):
    # Silently building naive local occurrences from a foreign zone reports a
    # healthy timer as missed by the offset between the two zones.
    with pytest.raises(calendarspec.UnsupportedTimezoneError) as excinfo:
        calendarspec.parse(expression)
    assert "not supported" in str(excinfo.value)


def test_the_refusal_is_a_parse_error_so_callers_report_a_coverage_gap():
    assert issubclass(
        calendarspec.UnsupportedTimezoneError, calendarspec.CalendarParseError
    )


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
