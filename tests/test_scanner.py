"""End-to-end scans over the log fixtures.  Nothing here touches the live system."""

from __future__ import annotations

from datetime import datetime

import pytest

from cron_postmortem.model import FAILURE, MISSED, OVERLAP
from cron_postmortem.scanner import ScanOptions, scan

from .conftest import FIXTURES, NOW


def options(**kwargs) -> ScanOptions:
    kwargs.setdefault("now", NOW)
    return ScanOptions(**kwargs)


@pytest.fixture
def cron_result():
    return scan(options(
        crontab_paths=[FIXTURES / "etc" / "crontab", FIXTURES / "spool" / "root"],
        log_paths=[FIXTURES / "syslog-cron.log"],
    ))


@pytest.fixture
def systemd_result():
    return scan(options(
        show_paths=[FIXTURES / "systemctl-show.txt"],
        log_paths=[FIXTURES / "journal-systemd.log"],
    ))


def kinds(result) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for finding in result.findings:
        grouped.setdefault(finding.kind, []).append(finding.job_id)
    return grouped


def test_window_is_derived_from_the_log_when_not_given(cron_result):
    assert cron_result.window_start == datetime(2026, 9, 18, 2, 50, 0)
    assert cron_result.window_end == datetime(2026, 9, 18, 5, 33, 10)


def test_cron_scan_finds_the_missed_and_overlapping_run(cron_result):
    assert kinds(cron_result) == {
        MISSED: ["cron:root:/usr/local/bin/sync-metrics.sh"],
        OVERLAP: ["cron:root:/usr/local/bin/sync-metrics.sh"],
    }
    missed = next(f for f in cron_result.findings if f.kind == MISSED)
    assert missed.when == datetime(2026, 9, 18, 4, 0)


def test_cron_scan_leaves_healthy_jobs_alone(cron_result):
    by_id = {report.job.id: report for report in cron_result.job_reports}
    backup = by_id["cron:root:/usr/local/bin/backup.sh"]
    assert (backup.expected, len(backup.runs), backup.findings) == (1, 1, [])
    # pam session pairing recovered the duration of the long backup run.
    assert backup.runs[0].duration == pytest.approx(2471.0)
    rotate = by_id["cron:www-data:/usr/local/bin/rotate-cache.sh"]
    assert (rotate.expected, len(rotate.runs), rotate.findings) == (3, 3, [])


def test_user_format_crontab_is_autodetected(cron_result):
    heartbeat = next(
        report for report in cron_result.job_reports
        if report.job.command == "/usr/local/bin/heartbeat.sh"
    )
    assert heartbeat.job.user == "root"
    assert (heartbeat.expected, len(heartbeat.runs), heartbeat.findings) == (1, 1, [])


def test_runs_without_a_crontab_entry_become_a_diagnostic(cron_result):
    assert any("sessionclean" in diag.message for diag in cron_result.diagnostics)


def test_systemd_scan_finds_all_three_problem_kinds(systemd_result):
    assert kinds(systemd_result) == {
        MISSED: ["systemd:metrics-push.timer"],
        OVERLAP: ["systemd:logship.timer"],
        FAILURE: ["systemd:backup-db.timer"],
    }


def test_systemd_failure_carries_the_exit_code(systemd_result):
    failure = next(f for f in systemd_result.findings if f.kind == FAILURE)
    assert failure.details["exit_code"] == 1
    assert failure.details["result"] == "exit-code"
    assert failure.details["unit"] == "backup-db.service"


def test_systemd_overlap_spans_two_hourly_runs(systemd_result):
    overlap = next(f for f in systemd_result.findings if f.kind == OVERLAP)
    assert overlap.details["previous_start"] == "2026-09-18T03:30:00"
    assert overlap.details["overlap_seconds"] == 308.0


def test_timer_tolerance_includes_accuracy(systemd_result):
    missed = next(f for f in systemd_result.findings if f.kind == MISSED)
    # 120s base + AccuracyUSec=1min.
    assert missed.details["tolerance_seconds"] == 180.0


def test_old_style_journal_lines_resolve_through_systemctl_show(systemd_result):
    certbot = next(
        report for report in systemd_result.job_reports
        if report.job.timer == "certbot.timer"
    )
    assert len(certbot.runs) == 2
    assert certbot.findings == []
    assert not any("could not map" in diag.message for diag in systemd_result.diagnostics)


def test_monotonic_timer_is_a_diagnostic_not_a_job(systemd_result):
    assert all(report.job.timer != "boot-cleanup.timer" for report in systemd_result.job_reports)
    assert any("boot-cleanup.timer" in diag.message for diag in systemd_result.diagnostics)


def test_one_scan_can_cover_cron_and_systemd_together():
    result = scan(options(
        crontab_paths=[FIXTURES / "etc" / "crontab", FIXTURES / "spool" / "root"],
        show_paths=[FIXTURES / "systemctl-show.txt"],
        log_paths=[FIXTURES / "syslog-cron.log", FIXTURES / "journal-systemd.log"],
    ))
    assert len(result.job_reports) == 8
    assert result.counts() == {MISSED: 2, OVERLAP: 2, FAILURE: 1}
    assert result.problems == 5


def test_ignoring_a_kind_drops_it_from_the_findings():
    result = scan(options(
        show_paths=[FIXTURES / "systemctl-show.txt"],
        log_paths=[FIXTURES / "journal-systemd.log"],
        ignore=frozenset({MISSED, OVERLAP}),
    ))
    assert result.counts() == {MISSED: 0, OVERLAP: 0, FAILURE: 1}


def test_a_window_starting_before_the_log_is_clamped_at_the_start_only():
    result = scan(options(
        crontab_paths=[FIXTURES / "etc" / "crontab"],
        log_paths=[FIXTURES / "syslog-cron.log"],
        since=datetime(2026, 9, 17, 0, 0),
        until=datetime(2026, 9, 18, 12, 0),
    ))
    # Clamping the start is what keeps yesterday's un-logged runs from all
    # looking missed; the end stays where it was asked for.
    assert result.window_start == datetime(2026, 9, 18, 2, 50, 0)
    assert result.window_end == datetime(2026, 9, 18, 12, 0)
    assert sum("clamped" in diag.message for diag in result.diagnostics) == 1


def test_a_log_that_stops_mid_window_reports_the_silent_tail_as_missed(tmp_path):
    """The cron daemon dying at 06:00 is the failure this tool exists to catch."""
    crontab = tmp_path / "root"
    crontab.write_text("*/30 * * * * /bin/collect\n")
    log = tmp_path / "syslog"
    log.write_text("".join(
        f"Sep 18 {hour:02d}:{minute:02d}:01 h CRON[{hour}{minute}]: "
        "(root) CMD (/bin/collect)\n"
        for hour in range(0, 7) for minute in (0, 30)
        if (hour, minute) <= (6, 0)
    ))
    result = scan(options(
        crontab_paths=[crontab],
        log_paths=[log],
        since=datetime(2026, 9, 17, 12, 0),
        until=NOW,
    ))
    assert result.window_end == NOW
    # 06:30 through 11:30 inclusive, every 30 minutes.
    missed = [f.when for f in result.findings if f.kind == MISSED]
    assert missed == [
        datetime(2026, 9, 18, hour, minute)
        for hour in range(6, 12) for minute in (0, 30)
        if (hour, minute) >= (6, 30)
    ]
    assert result.problems == 11


def test_a_narrower_window_limits_what_is_checked():
    result = scan(options(
        crontab_paths=[FIXTURES / "etc" / "crontab"],
        log_paths=[FIXTURES / "syslog-cron.log"],
        since=datetime(2026, 9, 18, 4, 30),
    ))
    assert result.window_start == datetime(2026, 9, 18, 4, 30)
    assert result.counts() == {MISSED: 0, OVERLAP: 1, FAILURE: 0}


def test_an_unparsable_schedule_is_a_diagnostic_not_a_crash(tmp_path):
    crontab = tmp_path / "crontab"
    crontab.write_text("0 3 99 * * root /bin/true\n")
    result = scan(options(crontab_paths=[crontab], log_paths=[FIXTURES / "syslog-cron.log"]))
    assert result.findings == []
    assert result.job_reports[0].schedule_ok is False
    assert any("not analysable" in diag.message for diag in result.diagnostics)


def test_a_job_with_no_session_lines_reports_that_overlaps_are_unknown(tmp_path):
    crontab = tmp_path / "root"
    crontab.write_text("*/30 * * * * /bin/collect\n")
    log = tmp_path / "syslog"
    log.write_text(
        "Sep 18 03:00:01 h CRON[1]: (root) CMD (/bin/collect)\n"
        "Sep 18 03:30:01 h CRON[2]: (root) CMD (/bin/collect)\n"
    )
    result = scan(options(crontab_paths=[crontab], log_paths=[log]))
    assert any("durations are unknown" in diag.message for diag in result.diagnostics)


def test_an_empty_log_is_not_a_crash(tmp_path):
    log = tmp_path / "empty.log"
    log.write_text("")
    result = scan(options(crontab_paths=[FIXTURES / "etc" / "crontab"], log_paths=[log]))
    assert result.window_end == NOW
    assert result.counts()[MISSED] > 0


def test_serialisation_round_trips(systemd_result):
    payload = systemd_result.as_dict()
    assert payload["tool"] == "cron-postmortem"
    assert payload["summary"]["problems"] == 3
    assert len(payload["jobs"]) == 4
    assert payload["sources"]["logs"] == [str(FIXTURES / "journal-systemd.log")]
    first = payload["jobs"][0]
    assert set(first) >= {"id", "schedule", "expected_runs", "observed_runs", "findings"}


def test_a_timer_with_two_calendars_expects_both_and_reports_once(tmp_path):
    show = tmp_path / "show.txt"
    show.write_text(
        "Id=twice.timer\nUnit=twice.service\nAccuracyUSec=1min\n"
        "TimersCalendar={ OnCalendar=*-*-* 03:00:00 ; next_elapse=n/a }"
        "{ OnCalendar=*-*-* 04:00:00 ; next_elapse=n/a }\n"
        "\nId=twice.service\nDescription=Runs twice\nActiveState=failed\n"
        "Result=exit-code\nExecMainStatus=1\n"
        "ExecMainExitTimestamp=Fri 2026-09-18 03:00:05 CEST\n"
    )
    log = tmp_path / "journal.log"
    log.write_text(
        "2026-09-18T02:59:00+0200 h systemd[1]: Starting twice.service - Runs twice...\n"
        "2026-09-18T02:59:30+0200 h systemd[1]: twice.service: Deactivated successfully.\n"
        "2026-09-18T03:00:01+0200 h systemd[1]: Starting twice.service - Runs twice...\n"
        "2026-09-18T03:00:05+0200 h systemd[1]: twice.service: "
        "Main process exited, code=exited, status=1/FAILURE\n"
        "2026-09-18T03:00:05+0200 h systemd[1]: twice.service: Failed with result 'exit-code'.\n"
        "2026-09-18T05:00:00+0200 h systemd[1]: Starting twice.service - Runs twice...\n"
        "2026-09-18T05:00:02+0200 h systemd[1]: twice.service: Deactivated successfully.\n"
    )
    result = scan(options(show_paths=[show], log_paths=[log]))
    assert len(result.job_reports) == 1
    report = result.job_reports[0]
    assert report.job.schedules == ("*-*-* 03:00:00", "*-*-* 04:00:00")
    assert report.expected == 2
    # One missed 04:00 occurrence and exactly one failure, not one per calendar.
    assert result.counts() == {MISSED: 1, OVERLAP: 0, FAILURE: 1}
