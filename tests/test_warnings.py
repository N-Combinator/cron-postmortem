"""Warnings: the cases where a clean report would be a lie.

A scan can come back with no findings for two very different reasons - nothing
is wrong, or nothing was looked at.  The second one has to be loud and has to
move the exit code, because the whole point of the tool is to be wired into a
monitoring check that only ever reads that code.
"""

from __future__ import annotations

import json

from cron_postmortem import cli
from cron_postmortem.report import to_json, to_markdown
from cron_postmortem.scanner import ScanOptions, scan

from .conftest import NOW

NOW_ARG = "2026-09-18T12:00:00"

HEALTHY_LOG = (
    "Sep 18 03:00:01 h CRON[1]: pam_unix(cron:session): session opened for user root\n"
    "Sep 18 03:00:01 h CRON[2]: (root) CMD (/bin/true)\n"
    "Sep 18 03:05:01 h CRON[1]: pam_unix(cron:session): session closed for user root\n"
)

# An access log is the file people reach for by mistake; not one line of it is a
# cron or systemd record, yet every line is well-formed text.
FOREIGN_LOG = (
    '10.0.0.1 - - [18/Sep/2026:03:00:01 +0200] "GET / HTTP/1.1" 200 1234\n'
    '10.0.0.2 - - [18/Sep/2026:03:00:02 +0200] "GET /health HTTP/1.1" 200 2\n'
    '10.0.0.3 - - [18/Sep/2026:03:00:03 +0200] "POST /api HTTP/1.1" 500 17\n'
)


def codes(result) -> list[str]:
    return [warning.code for warning in result.warnings]


def test_a_log_nothing_was_parsed_from_is_a_warning_not_silence(tmp_path):
    crontab = tmp_path / "root"
    # January only: nothing is due inside the window, so the scan has no
    # findings of its own and the warning is the only thing it can say.
    crontab.write_text("0 3 1 1 * /bin/true\n")
    log = tmp_path / "access.log"
    log.write_text(FOREIGN_LOG)

    result = scan(ScanOptions(crontab_paths=[crontab], log_paths=[log], now=NOW))

    assert result.problems == 0
    assert codes(result) == ["no-log-lines"]
    assert result.alerts is True
    assert (result.lines_total, result.lines_parsed) == (3, 0)
    assert "none of the 3 line(s)" in result.warnings[0].message


def test_the_line_counters_reach_the_report(tmp_path):
    crontab = tmp_path / "root"
    crontab.write_text("0 3 * * * /bin/true\n")
    log = tmp_path / "syslog"
    log.write_text(HEALTHY_LOG)

    result = scan(ScanOptions(crontab_paths=[crontab], log_paths=[log], now=NOW))

    summary = json.loads(to_json(result))["summary"]
    assert (summary["log_lines_total"], summary["log_lines_parsed"]) == (3, 3)
    assert "Log lines read 3, understood 3." in to_markdown(result)


def test_a_scan_without_any_log_source_warns_about_the_missing_log(tmp_path):
    crontab = tmp_path / "root"
    crontab.write_text("0 3 1 1 * /bin/true\n")

    result = scan(ScanOptions(crontab_paths=[crontab], now=NOW))

    assert codes(result) == ["no-log-lines"]
    assert "no log source" in result.warnings[0].message


def test_an_empty_log_file_warns_too(tmp_path):
    crontab = tmp_path / "root"
    crontab.write_text("0 3 1 1 * /bin/true\n")
    log = tmp_path / "syslog"
    log.write_text("")

    result = scan(ScanOptions(crontab_paths=[crontab], log_paths=[log], now=NOW))

    assert codes(result) == ["no-log-lines"]
    assert "produced no output" in result.warnings[0].message


def test_a_scan_that_found_no_schedules_is_a_warning_not_a_success(tmp_path):
    crontab = tmp_path / "root"
    crontab.write_text("# every entry commented out\n")
    log = tmp_path / "syslog"
    log.write_text(HEALTHY_LOG)

    result = scan(ScanOptions(crontab_paths=[crontab], log_paths=[log], now=NOW))

    assert result.job_reports == []
    assert result.problems == 0
    assert codes(result) == ["no-schedules"]
    assert result.alerts is True
    assert str(crontab) in result.warnings[0].message


def test_both_warnings_fire_when_the_scan_had_nothing_at_all(tmp_path):
    empty = tmp_path / "root"
    empty.write_text("\n")

    result = scan(ScanOptions(crontab_paths=[empty], now=NOW))

    assert codes(result) == ["no-schedules", "no-log-lines"]


def test_warnings_are_rendered_in_both_formats(tmp_path):
    crontab = tmp_path / "root"
    crontab.write_text("# nothing here\n")
    log = tmp_path / "syslog"
    log.write_text(HEALTHY_LOG)
    result = scan(ScanOptions(crontab_paths=[crontab], log_paths=[log], now=NOW))

    text = to_markdown(result)
    assert "## Warnings (1)" in text
    assert "`no-schedules`" in text
    # The reassuring line is reserved for scans that actually checked something.
    assert "No problems found." not in text
    assert "not conclusive" in text

    payload = json.loads(to_json(result))
    assert payload["summary"]["warnings"] == 1
    assert payload["warnings"][0]["code"] == "no-schedules"


def test_the_cli_exits_non_zero_on_a_warning_alone(tmp_path, capsys):
    crontab = tmp_path / "root"
    crontab.write_text("# nothing here\n")
    log = tmp_path / "syslog"
    log.write_text(HEALTHY_LOG)

    code = cli.main([
        "scan", "--crontab", str(crontab), "--log-file", str(log), "--now", NOW_ARG,
    ])

    assert code == cli.EXIT_PROBLEMS
    assert "## Warnings (1)" in capsys.readouterr().out


def test_exit_zero_still_silences_the_exit_code(tmp_path, capsys):
    crontab = tmp_path / "root"
    crontab.write_text("# nothing here\n")
    log = tmp_path / "syslog"
    log.write_text(HEALTHY_LOG)

    code = cli.main([
        "scan", "--crontab", str(crontab), "--log-file", str(log),
        "--now", NOW_ARG, "--exit-zero",
    ])

    assert code == cli.EXIT_OK
    assert "## Warnings (1)" in capsys.readouterr().out


def test_a_healthy_scan_stays_quiet_and_exits_zero(tmp_path, capsys):
    crontab = tmp_path / "root"
    crontab.write_text("0 3 * * * /bin/true\n")
    log = tmp_path / "syslog"
    log.write_text(HEALTHY_LOG)

    code = cli.main([
        "scan", "--crontab", str(crontab), "--log-file", str(log), "--now", NOW_ARG,
    ])

    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "## Warnings" not in out
    assert "No problems found." in out


TIMER_SHOW = """\
Id=report.timer
Description=Report timer
LoadState=loaded
ActiveState=active
SubState=waiting
Unit=report.service
AccuracyUSec=1min
RandomizedDelayUSec=0
TimersCalendar={ OnCalendar=*-*-* 09:00:00 Europe/Berlin ; next_elapse=n/a }

Id=report.service
Description=Report
LoadState=loaded
ActiveState=inactive
SubState=dead
Result=success
ExecMainStatus=0
Type=oneshot
"""


def test_a_timezone_qualified_timer_warns_instead_of_being_checked_wrong(tmp_path):
    show = tmp_path / "show.txt"
    show.write_text(TIMER_SHOW)
    log = tmp_path / "journal.log"
    log.write_text(HEALTHY_LOG)

    result = scan(ScanOptions(show_paths=[show], log_paths=[log], now=NOW))

    report = result.job_reports[0]
    # Evaluating the expression as local time would have expected a run at 09:00
    # local and reported the timer as missed; instead nothing is claimed about it.
    assert report.expected == 0
    assert report.schedule_ok is False
    assert result.problems == 0

    assert codes(result) == ["unsupported-timezone"]
    assert result.alerts is True
    warning = result.warnings[0]
    assert "systemd:report.timer" in warning.message
    assert "Europe/Berlin" in warning.message
    assert any(
        "not analysable" in diagnostic.message for diagnostic in result.diagnostics
    )


def test_the_timezone_warning_names_the_timer_in_the_report(tmp_path):
    show = tmp_path / "show.txt"
    show.write_text(TIMER_SHOW)
    log = tmp_path / "journal.log"
    log.write_text(HEALTHY_LOG)
    result = scan(ScanOptions(show_paths=[show], log_paths=[log], now=NOW))

    text = to_markdown(result)
    assert "`unsupported-timezone`" in text
    assert "Europe/Berlin" in text
