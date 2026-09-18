from __future__ import annotations

from datetime import datetime

import pytest

from cron_postmortem import systemd

SHOW = """\
Id=backup.timer
Description=Backup timer
Unit=backup.service
AccuracyUSec=1min
RandomizedDelayUSec=30s
TimersCalendar={ OnCalendar=*-*-* 03:00:00 ; next_elapse=Sat 2026-09-19 03:00:00 CEST }

Id=backup.service
Description=Nightly backup
ActiveState=failed
SubState=failed
Result=exit-code
ExecMainStatus=2
ExecMainExitTimestamp=Fri 2026-09-18 03:04:11 CEST
"""


def test_parse_show_splits_units_on_blank_lines():
    units = systemd.parse_show(SHOW)
    assert [unit.unit for unit in units] == ["backup.timer", "backup.service"]
    assert units[0].get("Unit") == "backup.service"


def test_parse_show_splits_on_a_repeated_id_without_a_blank_line():
    units = systemd.parse_show("Id=a.service\nActiveState=active\nId=b.service\n")
    assert [unit.unit for unit in units] == ["a.service", "b.service"]


def test_timer_jobs_use_oncalendar_and_the_activated_unit():
    jobs, problems = systemd.timer_jobs(systemd.parse_show(SHOW), "show")
    assert problems == []
    assert len(jobs) == 1
    assert jobs[0].id == "systemd:backup.timer"
    assert jobs[0].schedule == "*-*-* 03:00:00"
    assert jobs[0].unit == "backup.service"
    assert jobs[0].timer == "backup.timer"


def test_monotonic_timer_is_reported_as_unanalysable(fixtures):
    units = systemd.parse_show((fixtures / "systemctl-show.txt").read_text())
    _, problems = systemd.timer_jobs(units, "show")
    assert any("boot-cleanup.timer" in problem for problem in problems)


def test_multiple_oncalendar_lines_are_one_job_systemd_ors_them():
    text = (
        "Id=x.timer\nUnit=x.service\n"
        "TimersCalendar={ OnCalendar=*-*-* 03:00:00 ; next_elapse=n/a }"
        "{ OnCalendar=*-*-* 15:00:00 ; next_elapse=n/a }\n"
    )
    jobs, _ = systemd.timer_jobs(systemd.parse_show(text), "show")
    assert [job.id for job in jobs] == ["systemd:x.timer"]
    assert jobs[0].schedules == ("*-*-* 03:00:00", "*-*-* 15:00:00")
    assert jobs[0].schedule == "*-*-* 03:00:00 ; *-*-* 15:00:00"


def test_failure_state_detection():
    timer, service = systemd.parse_show(SHOW)
    assert service.is_failed
    assert service.exit_status == 2
    assert not timer.is_failed


def test_tolerance_adds_accuracy_and_randomised_delay():
    timer, _ = systemd.parse_show(SHOW)
    assert systemd.timer_tolerance(timer) == pytest.approx(90.0)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("0", 0.0), ("30s", 30.0), ("1min 30s", 90.0), ("2h", 7200.0),
     ("500ms", 0.5), ("60000000", 60.0), ("infinity", 0.0), ("", 0.0)],
)
def test_duration_parsing(text, seconds):
    assert systemd.parse_duration(text) == pytest.approx(seconds)


def test_timestamp_parsing():
    assert systemd.parse_timestamp("Fri 2026-09-18 04:00:15 CEST") == datetime(
        2026, 9, 18, 4, 0, 15
    )
    assert systemd.parse_timestamp("n/a") is None
    assert systemd.parse_timestamp("") is None


def test_description_map_only_covers_services():
    mapping = systemd.description_map(systemd.parse_show(SHOW))
    assert mapping == {"Nightly backup": "backup.service"}
