from __future__ import annotations

from datetime import datetime

import pytest

from cron_postmortem import cronspec


def test_parses_every_field_shape():
    schedule = cronspec.parse("*/15 2-4 1,15 jan-mar mon,fri")
    assert sorted(schedule.minutes) == [0, 15, 30, 45]
    assert sorted(schedule.hours) == [2, 3, 4]
    assert sorted(schedule.days_of_month) == [1, 15]
    assert sorted(schedule.months) == [1, 2, 3]
    assert sorted(schedule.days_of_week) == [1, 5]


def test_step_from_a_start_value():
    assert sorted(cronspec.parse("5/20 * * * *").minutes) == [5, 25, 45]


def test_sunday_is_both_zero_and_seven():
    assert cronspec.parse("0 0 * * 7").days_of_week == cronspec.parse("0 0 * * 0").days_of_week


def test_macros():
    assert cronspec.parse("@daily").occurrences(
        datetime(2026, 9, 17), datetime(2026, 9, 18, 12)
    ) == [datetime(2026, 9, 17), datetime(2026, 9, 18)]


def test_reboot_is_not_schedulable():
    with pytest.raises(cronspec.CronParseError):
        cronspec.parse("@reboot")


@pytest.mark.parametrize(
    "expression",
    ["", "0 0 * *", "0 0 * * * *", "61 * * * *", "* * * xxx *", "5-1 * * * *", "*/0 * * * *"],
)
def test_rejects_broken_expressions(expression):
    with pytest.raises(cronspec.CronParseError):
        cronspec.parse(expression)


def test_day_of_month_and_day_of_week_are_ored():
    # Vixie cron: with neither day field written as a star, either one matching fires.
    schedule = cronspec.parse("0 0 13 * fri")
    days = [moment.day for moment in schedule.occurrences(
        datetime(2026, 11, 1), datetime(2026, 11, 30, 23, 59)
    )]
    assert 13 in days  # the 13th, a Friday
    assert 6 in days  # another Friday
    assert 12 not in days


def test_day_of_week_alone_restricts():
    schedule = cronspec.parse("0 0 * * mon")
    days = schedule.occurrences(datetime(2026, 9, 14), datetime(2026, 9, 27, 23, 59))
    assert [moment.date().isoformat() for moment in days] == ["2026-09-14", "2026-09-21"]


def test_occurrences_are_inclusive_and_empty_when_inverted():
    schedule = cronspec.parse("0 3 * * *")
    assert schedule.occurrences(datetime(2026, 9, 18, 3), datetime(2026, 9, 18, 3)) == [
        datetime(2026, 9, 18, 3)
    ]
    assert schedule.occurrences(datetime(2026, 9, 18, 4), datetime(2026, 9, 18, 3)) == []


def test_step_in_day_of_month_constrains_the_day():
    schedule = cronspec.parse("0 3 */2 * *")
    days = schedule.occurrences(datetime(2026, 9, 1), datetime(2026, 9, 7, 23, 59))
    assert [moment.day for moment in days] == [1, 3, 5, 7]


def test_step_in_day_of_week_constrains_the_day():
    # `*/2` in DOW is every other weekday number: Sunday, Tuesday, Thursday, Saturday.
    schedule = cronspec.parse("0 3 * * */2")
    days = schedule.occurrences(datetime(2026, 9, 14), datetime(2026, 9, 20, 23, 59))
    assert [moment.date().isoformat() for moment in days] == [
        "2026-09-15",  # Tuesday
        "2026-09-17",  # Thursday
        "2026-09-19",  # Saturday
        "2026-09-20",  # Sunday
    ]


def test_starred_day_of_month_step_ands_with_day_of_week():
    # A field starting with `*` picks Vixie's AND branch, so this is
    # "odd days of the month that are also Fridays", not the OR of the two.
    schedule = cronspec.parse("0 0 */2 * fri")
    days = schedule.occurrences(datetime(2026, 9, 1), datetime(2026, 9, 30, 23, 59))
    assert [moment.day for moment in days] == [11, 25]
