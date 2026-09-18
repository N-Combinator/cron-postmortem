"""Warnings: the cases where a clean report would be a lie.

A scan can come back with no findings for two very different reasons - nothing
is wrong, or nothing was looked at.  The second one has to be loud and has to
move the exit code, because the whole point of the tool is to be wired into a
monitoring check that only ever reads that code.
"""

from __future__ import annotations

import json
from datetime import datetime

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


# --- a window the tolerance eats whole ---------------------------------------
#
# An occurrence is judged only once its tolerance has elapsed, so the last
# moment a scan can rule on is window_end - tolerance.  With a tolerance longer
# than the window that deadline falls before the window opens: every job expects
# nothing, no detector has anything to say, and the report used to come back
# "No problems found", exit 0, for a scan that looked at nothing.

# The first line sits exactly on the window start used below, so the start clamp
# does not shave a second off the spans these tests assert on.
BUSY_LOG = (
    "Sep 18 03:00:00 h CRON[1]: (root) CMD (/bin/collect)\n"
    "Sep 18 03:10:01 h CRON[2]: (root) CMD (/bin/collect)\n"
    "Sep 18 03:20:01 h CRON[3]: (root) CMD (/bin/collect)\n"
)


def busy_scan(tmp_path, **kwargs) -> tuple:
    crontab = tmp_path / "root"
    crontab.write_text("*/10 * * * * /bin/collect\n")
    log = tmp_path / "syslog"
    log.write_text(BUSY_LOG)
    return crontab, log


def test_a_window_shorter_than_the_tolerance_is_a_usage_error(tmp_path):
    crontab, log = busy_scan(tmp_path)

    result = scan(ScanOptions(
        crontab_paths=[crontab], log_paths=[log], now=NOW,
        since=datetime(2026, 9, 18, 3, 0, 0),
        until=datetime(2026, 9, 18, 3, 5, 0),
        tolerance=3600,
    ))

    assert codes(result) == ["empty-window"]
    assert result.problems == 0
    assert result.usage_error is True
    assert result.alerts is True
    assert "300s long but the tolerance is 3600s" in result.warnings[0].message


def test_an_until_before_the_since_is_a_usage_error(tmp_path):
    crontab, log = busy_scan(tmp_path)

    result = scan(ScanOptions(
        crontab_paths=[crontab], log_paths=[log], now=NOW,
        since=datetime(2026, 9, 18, 4, 0, 0),
        until=datetime(2026, 9, 18, 3, 0, 0),
    ))

    assert codes(result) == ["empty-window"]
    assert result.usage_error is True


def test_a_window_exactly_as_long_as_the_tolerance_is_still_scanned(tmp_path):
    crontab, log = busy_scan(tmp_path)

    result = scan(ScanOptions(
        crontab_paths=[crontab], log_paths=[log], now=NOW,
        since=datetime(2026, 9, 18, 3, 0, 0),
        until=datetime(2026, 9, 18, 3, 10, 0),
        tolerance=600,
    ))

    # The deadline lands exactly on the window start: one judgeable instant, and
    # the 03:00 occurrence is answered by the 03:00:01 run.
    assert codes(result) == []
    assert result.job_reports[0].expected == 1
    assert result.problems == 0


def test_the_empty_window_is_reported_instead_of_no_problems_found(tmp_path):
    crontab, log = busy_scan(tmp_path)
    result = scan(ScanOptions(
        crontab_paths=[crontab], log_paths=[log], now=NOW,
        since=datetime(2026, 9, 18, 3, 0, 0),
        until=datetime(2026, 9, 18, 3, 5, 0),
        tolerance=3600,
    ))

    text = to_markdown(result)
    assert "## Warnings (1)" in text
    assert "`empty-window`" in text
    assert "No problems found." not in text

    warning = json.loads(to_json(result))["warnings"][0]
    assert (warning["code"], warning["usage_error"]) == ("empty-window", True)


def test_the_cli_exits_two_on_an_empty_window(tmp_path, capsys):
    crontab, log = busy_scan(tmp_path)

    code = cli.main([
        "scan", "--crontab", str(crontab), "--log-file", str(log), "--now", NOW_ARG,
        "--since", "2026-09-18T03:00:00", "--until", "2026-09-18T03:05:00",
        "--tolerance", "3600",
    ])

    captured = capsys.readouterr()
    assert code == cli.EXIT_USAGE
    assert "`empty-window`" in captured.out
    assert "cron-postmortem: the window" in captured.err


def test_exit_zero_does_not_silence_a_usage_error(tmp_path, capsys):
    crontab, log = busy_scan(tmp_path)

    code = cli.main([
        "scan", "--crontab", str(crontab), "--log-file", str(log), "--now", NOW_ARG,
        "--since", "2026-09-18T03:00:00", "--until", "2026-09-18T03:05:00",
        "--tolerance", "3600", "--exit-zero",
    ])

    capsys.readouterr()
    # --exit-zero mutes findings for a monitoring check; it must not mute the
    # news that the scan never ran.
    assert code == cli.EXIT_USAGE


def test_a_log_too_short_for_the_tolerance_is_caught_by_the_clamp(tmp_path):
    """No --since/--until: the window collapses onto a one-line log."""
    crontab = tmp_path / "root"
    crontab.write_text("0 3 * * * /bin/true\n")
    log = tmp_path / "syslog"
    log.write_text("Sep 18 03:00:01 h CRON[2]: (root) CMD (/bin/true)\n")

    result = scan(ScanOptions(crontab_paths=[crontab], log_paths=[log], now=NOW))

    assert codes(result) == ["empty-window"]
    assert result.usage_error is True


TIMER_SLACK_SHOW = """\
Id=slow.timer
Description=Slow timer
LoadState=loaded
ActiveState=active
SubState=waiting
Unit=slow.service
AccuracyUSec=1h
RandomizedDelayUSec=0
TimersCalendar={ OnCalendar=*-*-* 03:00:00 ; next_elapse=n/a }

Id=slow.service
Description=Slow job
LoadState=loaded
ActiveState=inactive
SubState=dead
Result=success
ExecMainStatus=0
Type=oneshot
"""


def test_a_timer_whose_slack_outlasts_the_window_warns_without_a_usage_error(tmp_path):
    """The window is fine in general - AccuracySec is what empties it here."""
    show = tmp_path / "show.txt"
    show.write_text(TIMER_SLACK_SHOW)
    log = tmp_path / "journal.log"
    log.write_text(
        "2026-09-18T03:00:01+0200 h systemd[1]: Starting slow.service - Slow job...\n"
        "2026-09-18T03:00:09+0200 h systemd[1]: slow.service: Deactivated successfully.\n"
    )

    result = scan(ScanOptions(
        show_paths=[show], log_paths=[log], now=NOW,
        since=datetime(2026, 9, 18, 2, 55, 0),
        until=datetime(2026, 9, 18, 3, 5, 0),
    ))

    assert codes(result) == ["empty-window"]
    # The caller's arguments are not at fault, so this one exits 1, not 2.
    assert result.usage_error is False
    assert result.alerts is True
    assert "systemd:slow.timer" in result.warnings[0].message
    assert "3720s" in result.warnings[0].message


# One command, running every minute for an hour, as the user named below: the
# shape of an offline capture where the crontab and the log do line up.
def _busy_log(user: str, command: str = "/usr/local/bin/poll.sh") -> str:
    lines = []
    for minute in range(60):
        stamp = f"Sep 18 03:{minute:02d}:01"
        end = f"Sep 18 03:{minute:02d}:06"
        pid = 1000 + minute * 2
        lines.append(
            f"{stamp} h CRON[{pid}]: pam_unix(cron:session): "
            f"session opened for user {user}"
        )
        lines.append(f"{stamp} h CRON[{pid + 1}]: ({user}) CMD ({command})")
        lines.append(
            f"{end} h CRON[{pid}]: pam_unix(cron:session): "
            f"session closed for user {user}"
        )
    return "\n".join(lines) + "\n"


def busy_options(tmp_path, log_user: str, name: str = "collected.crontab", **kwargs):
    crontab = tmp_path / name
    crontab.write_text("* * * * * /usr/local/bin/poll.sh\n")
    log = tmp_path / "syslog"
    log.write_text(_busy_log(log_user))
    kwargs.setdefault("now", NOW)
    return ScanOptions(
        crontab_paths=[crontab],
        log_paths=[log],
        since=datetime(2026, 9, 18, 3, 0, 0),
        until=datetime(2026, 9, 18, 4, 0, 0),
        **kwargs,
    )


def test_a_crontab_named_after_the_capture_still_matches_its_runs(tmp_path):
    """The offline mode acceptance criterion 1 asks for, under any filename.

    The crontab owner used to be taken from the filename whatever it was, so
    the same file called ``collected.crontab`` instead of ``root`` was read as
    belonging to a user the log never mentions: 0 runs and an hour of invented
    missed runs.
    """
    result = scan(busy_options(tmp_path, log_user="root"))

    assert [report.job.user for report in result.job_reports] == ["root"]
    assert len(result.job_reports[0].runs) == 60
    assert result.problems == 0
    assert codes(result) == []
    assert result.alerts is False


def test_not_one_matching_run_is_a_warning_not_a_pile_of_missed_runs(tmp_path):
    """The runs are all there, under a user no crontab entry claims.

    Some entries never firing is a finding about those jobs; every entry
    missing while the log is full of cron runs means the two sides are being
    compared on a key one of them does not use, and the report is not a verdict
    on the jobs at all.
    """
    result = scan(busy_options(tmp_path, log_user="alice"))

    assert codes(result) == ["no-runs-matched"]
    assert result.alerts is True
    # Not the caller's arguments contradicting each other: exit 1, not 2.
    assert result.usage_error is False
    message = result.warnings[0].message
    assert "user(s) alice" in message and "user(s) root" in message
    assert "--crontab-user" in message
    # The diagnostic it replaces is not also emitted.
    assert [diag for diag in result.diagnostics if "matched no known" in diag.message] == []


def test_naming_the_user_makes_the_warning_and_the_missed_runs_go_away(tmp_path):
    result = scan(busy_options(tmp_path, log_user="alice", crontab_user="alice"))

    assert codes(result) == []
    assert len(result.job_reports[0].runs) == 60
    assert result.problems == 0


def test_some_runs_matching_stays_a_diagnostic(tmp_path):
    """One stray command in the log is a coverage gap, not a broken scan."""
    crontab = tmp_path / "root"
    crontab.write_text("0 3 * * * /bin/true\n")
    log = tmp_path / "syslog"
    log.write_text(
        HEALTHY_LOG
        + "Sep 18 03:10:01 h CRON[9]: (root) CMD (/usr/lib/php/sessionclean)\n"
    )

    result = scan(ScanOptions(crontab_paths=[crontab], log_paths=[log], now=NOW))

    assert codes(result) == []
    assert any("matched no known" in diag.message for diag in result.diagnostics)


def test_cron_runs_with_no_crontab_at_all_are_not_the_same_complaint(tmp_path):
    """Scanning only timers over a journal that also carries cron lines."""
    show = tmp_path / "show.txt"
    show.write_text(TIMER_SLACK_SHOW)
    log = tmp_path / "syslog"
    log.write_text(_busy_log("root"))

    result = scan(ScanOptions(
        show_paths=[show], log_paths=[log], now=NOW,
        since=datetime(2026, 9, 18, 3, 0, 0),
        until=datetime(2026, 9, 18, 4, 0, 0),
    ))

    assert "no-runs-matched" not in codes(result)
    assert any("matched no known" in diag.message for diag in result.diagnostics)


# --- dates the scan does not believe ------------------------------------------

def test_one_out_of_order_line_does_not_invent_a_year_of_missed_runs(tmp_path):
    """The whole report used to hinge on one line being out of order.

    Reading a small backwards step as a December -> January rollover dated every
    earlier line a year out, that line became the log's first timestamp, the
    window was clamped to it and an hourly job came back with 8760 missed runs
    from a four-line log - with no warning and exit 1.
    """
    crontab = tmp_path / "root"
    crontab.write_text("0 * * * * /bin/hourly\n")
    log = tmp_path / "syslog"
    log.write_text(
        "Sep 18 01:00:01 web01 CRON[100]: (root) CMD (/bin/hourly)\n"
        "Sep 17 23:59:59 web01 CRON[105]: (root) CMD (/bin/other)\n"
        "Sep 18 02:00:01 web01 CRON[110]: (root) CMD (/bin/hourly)\n"
        "Sep 18 03:00:01 web01 CRON[120]: (root) CMD (/bin/hourly)\n"
    )

    result = scan(ScanOptions(crontab_paths=[crontab], log_paths=[log], now=NOW))

    assert result.window_start == datetime(2026, 9, 17, 23, 59, 59)
    assert result.window_end == datetime(2026, 9, 18, 3, 0, 1)
    # Only the 00:00 occurrence, which really has no run in the log.
    assert [finding.when for finding in result.findings] == [
        datetime(2026, 9, 18, 0, 0)
    ]
    assert codes(result) == []
