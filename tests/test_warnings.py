"""Warnings: the cases where a clean report would be a lie.

A scan can come back with no findings for two very different reasons - nothing
is wrong, or nothing was looked at.  The second one has to be loud and has to
move the exit code, because the whole point of the tool is to be wired into a
monitoring check that only ever reads that code.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from cron_postmortem import cli
from cron_postmortem.report import to_json, to_markdown
from cron_postmortem.scanner import ScanOptions, scan

from .conftest import NOW, busy_cron_log

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
    assert "cron-postmortem: empty-window: the window" in captured.err


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


def busy_options(tmp_path, log_user: str, name: str = "collected.crontab", **kwargs):
    crontab = tmp_path / name
    crontab.write_text("* * * * * /usr/local/bin/poll.sh\n")
    log = tmp_path / "syslog"
    log.write_text(busy_cron_log(log_user))
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


def test_a_command_with_a_file_argument_is_not_read_as_a_user_column(tmp_path):
    """A per-user entry whose command takes a path used to become a system one.

    ``backup.sh /data`` read as a user column turns the job into
    ``(backup.sh) /data``, which the log never says: sixty runs matched nothing
    and came back as sixty missed ones plus exit 3, for a crontab and a log that
    agree line for line.
    """
    crontab_file = tmp_path / "web01.crontab"
    crontab_file.write_text("* * * * * backup.sh /data\n")
    log = tmp_path / "syslog"
    log.write_text(busy_cron_log("alice", command="backup.sh /data"))

    result = scan(
        ScanOptions(
            crontab_paths=[crontab_file],
            log_paths=[log],
            crontab_user="alice",
            since=datetime(2026, 9, 18, 3, 0, 0),
            until=datetime(2026, 9, 18, 4, 0, 0),
            now=NOW,
        )
    )

    assert [(report.job.user, report.job.command) for report in result.job_reports] == [
        ("alice", "backup.sh /data")
    ]
    assert len(result.job_reports[0].runs) == 60
    assert (codes(result), result.problems, result.alerts) == ([], 0, False)


def test_the_warning_does_not_ask_for_the_option_that_was_already_passed(tmp_path):
    """The entries name their own user, so ``--crontab-user`` was never applied.

    Telling an operator to pass the option they passed is how a real warning
    gets closed as noise; the remedy has to name what overruled them instead.
    """
    crontab_file = tmp_path / "web01.crontab"
    crontab_file.write_text("* * * * * root /usr/local/bin/poll.sh\n")
    log = tmp_path / "syslog"
    log.write_text(busy_cron_log("alice"))

    result = scan(
        ScanOptions(
            crontab_paths=[crontab_file],
            log_paths=[log],
            crontab_user="alice",
            since=datetime(2026, 9, 18, 3, 0, 0),
            until=datetime(2026, 9, 18, 4, 0, 0),
            now=NOW,
        )
    )

    assert codes(result) == ["no-runs-matched"]
    message = result.warnings[0].message
    assert "pass --crontab-user" not in message
    assert "--crontab-format user" in message
    assert any("was not applied" in diag.message for diag in result.diagnostics)


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
    log.write_text(busy_cron_log("root"))

    result = scan(ScanOptions(
        show_paths=[show], log_paths=[log], now=NOW,
        since=datetime(2026, 9, 18, 3, 0, 0),
        until=datetime(2026, 9, 18, 4, 0, 0),
    ))

    assert "no-runs-matched" not in codes(result)
    assert any("matched no known" in diag.message for diag in result.diagnostics)


# --- a scan that matched nothing has an exit code of its own --------------------

# The crontab from the fixture that started this: a system crontab, collected
# off a host and saved under the host's name.  Read as a per-user crontab, each
# command becomes "root /usr/local/bin/..." - a string no log line can say.
SYSTEM_CAPTURE = """\
SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin

*/1 * * * * root /usr/local/bin/poll.sh
"""


def _system_capture(tmp_path, name: str = "web01.crontab") -> tuple:
    crontab = tmp_path / name
    crontab.write_text(SYSTEM_CAPTURE)
    log = tmp_path / "syslog"
    log.write_text(busy_cron_log("root"))
    return crontab, log


def _scan_argv(crontab, log, *extra: str) -> list[str]:
    return [
        "scan", "--crontab", str(crontab), "--log-file", str(log),
        "--now", NOW_ARG,
        "--since", "2026-09-18T03:00:00", "--until", "2026-09-18T04:00:00",
        *extra,
    ]


def test_the_cli_exits_three_when_not_one_run_matched(tmp_path, capsys):
    """Acceptance criterion 2: loud, and not the exit code an outage uses.

    ``--crontab-format user`` forces the misreading the old path-based
    detection made on its own, so the report is the full hour of invented
    missed runs.  A monitoring check must be able to tell that page from a real
    one without parsing the report.
    """
    crontab, log = _system_capture(tmp_path)

    code = cli.main(_scan_argv(crontab, log, "--crontab-format", "user", "--format", "json"))

    captured = capsys.readouterr()
    assert code == cli.EXIT_NO_MATCH
    assert code != cli.EXIT_PROBLEMS
    assert "cron-postmortem: no-runs-matched:" in captured.err

    report = json.loads(captured.out)
    assert [warning["code"] for warning in report["warnings"]] == ["no-runs-matched"]
    assert report["warnings"][0]["usage_error"] is False
    # The findings it exits on are exactly the ones it is disowning.
    assert report["summary"]["missed"] > 0


def test_the_warning_reaches_the_markdown_report_too(tmp_path, capsys):
    crontab, log = _system_capture(tmp_path)

    code = cli.main(_scan_argv(crontab, log, "--crontab-format", "user"))

    captured = capsys.readouterr()
    assert code == cli.EXIT_NO_MATCH
    assert "## Warnings (1)" in captured.out
    assert "`no-runs-matched`" in captured.out


def test_exit_zero_does_not_silence_a_scan_that_matched_nothing(tmp_path, capsys):
    """Same reasoning as the usage error: the flag mutes findings, not fiction."""
    crontab, log = _system_capture(tmp_path)

    code = cli.main(_scan_argv(crontab, log, "--crontab-format", "user", "--exit-zero"))

    capsys.readouterr()
    assert code == cli.EXIT_NO_MATCH


# A failing timer in the same scan.  Nothing about it goes through the crontab
# matching that no-runs-matched complains about, so the warning cannot disown it.
FAILING_SHOW = """\
Id=backup.timer
Description=Nightly backup timer
LoadState=loaded
ActiveState=active
SubState=waiting
Unit=backup.service
AccuracyUSec=1min
RandomizedDelayUSec=0
TimersCalendar={ OnCalendar=*-*-* 03:30:00 ; next_elapse=n/a }

Id=backup.service
Description=Nightly backup
LoadState=loaded
ActiveState=failed
SubState=failed
Result=exit-code
ExecMainStatus=1
Type=oneshot
"""

FAILING_JOURNAL = (
    "2026-09-18T03:30:01+0200 h systemd[1]: Starting backup.service - Nightly backup...\n"
    "2026-09-18T03:30:09+0200 h systemd[1]: backup.service: Main process exited, "
    "code=exited, status=1/FAILURE\n"
    "2026-09-18T03:30:09+0200 h systemd[1]: backup.service: Failed with result 'exit-code'.\n"
    "2026-09-18T03:30:09+0200 h systemd[1]: Failed to start backup.service - Nightly backup.\n"
)


def _mixed_argv(tmp_path, *extra: str) -> list[str]:
    """A crontab that reconciles with nothing, next to a timer that really failed."""
    crontab, log = _system_capture(tmp_path)
    show = tmp_path / "show.txt"
    show.write_text(FAILING_SHOW)
    journal = tmp_path / "journal.log"
    journal.write_text(FAILING_JOURNAL)
    return _scan_argv(
        crontab, log,
        "--systemctl-show", str(show), "--log-file", str(journal),
        "--crontab-format", "user", *extra,
    )


def test_a_real_failure_next_to_an_unmatched_crontab_still_exits_one(tmp_path, capsys):
    """Exit 3 means "do not act on this report", so it may not swallow an outage.

    The warning disowns the crontab entries it could not reconcile and nothing
    else; ``backup.service`` failed on the host, which no amount of crontab
    misreading can invent.  Coming back as 3 would tell a monitoring check to
    treat a real failure as a scan problem and not page.
    """
    code = cli.main(_mixed_argv(tmp_path, "--format", "json"))

    captured = capsys.readouterr()
    assert code == cli.EXIT_PROBLEMS
    # The warning is not suppressed by the promotion - only the exit code moves.
    assert "cron-postmortem: no-runs-matched:" in captured.err
    report = json.loads(captured.out)
    assert [warning["code"] for warning in report["warnings"]] == ["no-runs-matched"]
    assert report["summary"]["failure"] >= 1
    assert {
        finding["job_id"] for finding in report["findings"] if finding["kind"] == "failure"
    } == {"systemd:backup.timer"}


def test_only_the_unmatched_crontab_findings_are_disowned(tmp_path):
    """The property the exit code is decided on, checked directly."""
    crontab, log = _system_capture(tmp_path)
    show = tmp_path / "show.txt"
    show.write_text(FAILING_SHOW)
    journal = tmp_path / "journal.log"
    journal.write_text(FAILING_JOURNAL)

    result = scan(ScanOptions(
        crontab_paths=[crontab], show_paths=[show], log_paths=[log, journal],
        crontab_format="user", now=NOW,
        since=datetime(2026, 9, 18, 3, 0, 0),
        until=datetime(2026, 9, 18, 4, 0, 0),
    ))

    assert codes(result) == ["no-runs-matched"]
    assert result.problems > 1
    assert {finding.job_id for finding in result.standing_findings} == {
        "systemd:backup.timer"
    }


def test_exit_zero_mutes_the_failure_but_not_the_broken_scan(tmp_path, capsys):
    """The flag mutes findings; the warning is not a finding, so 3 comes back."""
    code = cli.main(_mixed_argv(tmp_path, "--exit-zero"))

    capsys.readouterr()
    assert code == cli.EXIT_NO_MATCH


def test_the_misdetected_system_crontab_now_scans_clean(tmp_path, capsys):
    """Acceptance criterion 3's regression: the fixture that started the issue.

    ``web01.crontab`` is not ``/etc/crontab`` and is not under ``/etc/cron.d``,
    so v0.1 read a system crontab as a per-user one, matched none of its 60
    runs and reported every one of them missed - exit 1, indistinguishable from
    a job that really had stopped.  Detecting the format from the content, the
    same file under the same name matches all 60 and finds nothing wrong.
    """
    crontab, log = _system_capture(tmp_path)

    code = cli.main(_scan_argv(crontab, log, "--format", "json"))

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert code == cli.EXIT_OK
    assert captured.err == ""
    assert report["warnings"] == []
    assert report["summary"]["missed"] == 0
    assert [job["user"] for job in report["jobs"]] == ["root"]
    assert [job["command"] for job in report["jobs"]] == ["/usr/local/bin/poll.sh"]
    assert report["summary"]["runs"] == 60


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


def test_a_year_wide_span_from_a_handful_of_lines_is_a_warning(tmp_path):
    """A rollover that is plausible line by line but not as a whole.

    A ~300-day backwards step is accepted as a new year, so an old archive
    glued onto a current log still dates a year out.  Rather than enumerate a
    year of occurrences in silence, the scan says the dates are not to be
    trusted and moves the exit code.
    """
    crontab = tmp_path / "root"
    crontab.write_text("0 3 * * * /bin/daily\n")
    log = tmp_path / "syslog"
    log.write_text(
        "".join(
            f"{month} 05 03:00:01 web01 CRON[1]: (root) CMD (/bin/daily)\n"
            for month in ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul",
                          "Aug", "Nov"]
        )
        + "Jan 05 03:00:01 web01 CRON[2]: (root) CMD (/bin/daily)\n"
    )

    result = scan(ScanOptions(crontab_paths=[crontab], log_paths=[log], now=NOW))

    assert codes(result) == ["implausible-log-dates"]
    warning = result.warnings[0]
    assert warning.usage_error is False
    assert str(log) in warning.message
    assert "365 days" in warning.message
    assert result.alerts is True


def test_the_implausible_date_warning_is_not_a_usage_error(tmp_path):
    """Exit 1, not 2: the input is odd, the arguments are not contradictory."""
    crontab = tmp_path / "root"
    crontab.write_text("0 3 * * * /bin/daily\n")
    log = tmp_path / "syslog"
    log.write_text(
        "".join(
            f"{month} 05 03:00:01 web01 CRON[1]: (root) CMD (/bin/daily)\n"
            for month in ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul",
                          "Aug", "Nov"]
        )
        + "Jan 05 03:00:01 web01 CRON[2]: (root) CMD (/bin/daily)\n"
    )

    code = cli.main([
        "scan", "--crontab", str(crontab), "--log-file", str(log),
        "--now", NOW_ARG, "--format", "json",
    ])

    assert code == 1


# --- a format that was guessed, in a scan that otherwise matched fine ----------

# One crontab whose entries settle nothing between them (a plain word in front
# of a path reads as a user column and as a command with a file argument), next
# to one that the log agrees with line for line.  "no-runs-matched" cannot see
# this: something did match, so the scan looks like an ordinary outage report.
GUESSABLE = "*/1 * * * * deploy /opt/app/tick\n"
MATCHING = "* * * * * /usr/local/bin/poll.sh\n"


def _mixed_crontabs(tmp_path) -> tuple[Path, Path, Path, Path]:
    guessed = tmp_path / "webjobs"
    guessed.write_text(GUESSABLE)
    matching = tmp_path / "root"
    matching.write_text(MATCHING)
    matching_log = tmp_path / "syslog-root"
    matching_log.write_text(busy_cron_log("root"))
    other_log = tmp_path / "syslog-deploy"
    other_log.write_text(busy_cron_log("deploy", command="/opt/app/tick"))
    return guessed, matching, matching_log, other_log


def _mixed_options(tmp_path, **kwargs) -> ScanOptions:
    guessed, matching, matching_log, other_log = _mixed_crontabs(tmp_path)
    return ScanOptions(
        crontab_paths=[matching, guessed],
        log_paths=[matching_log, other_log],
        since=datetime(2026, 9, 18, 3, 0, 0),
        until=datetime(2026, 9, 18, 4, 0, 0),
        now=NOW,
        **kwargs,
    )


def test_a_guessed_format_that_matched_nothing_is_a_warning_not_a_diagnostic(tmp_path):
    """The misread crontab next to a healthy one.

    Its sixty missed runs read exactly like an outage, and the only trace of the
    guess used to be a diagnostic buried in the report: no stderr line, no
    warnings entry, nothing a monitoring check could act on.
    """
    result = scan(_mixed_options(tmp_path))

    assert codes(result) == ["crontab-format-guessed"]
    assert result.alerts is True
    assert result.usage_error is False
    message = result.warnings[0].message
    assert str(tmp_path / "webjobs") in message
    assert "did not settle that between them" in message
    assert "--crontab-format system" in message
    # The healthy crontab is untouched by it.
    matched = [report for report in result.job_reports if report.runs]
    assert [report.job.command for report in matched] == ["/usr/local/bin/poll.sh"]


def test_naming_the_format_answers_the_warning(tmp_path):
    """Nothing was guessed, so nothing is warned about - and the runs match."""
    guessed = tmp_path / "webjobs"
    guessed.write_text(GUESSABLE)
    log = tmp_path / "syslog"
    log.write_text(busy_cron_log("deploy", command="/opt/app/tick"))

    result = scan(ScanOptions(
        crontab_paths=[guessed], log_paths=[log], now=NOW,
        crontab_format="system",
        since=datetime(2026, 9, 18, 3, 0, 0),
        until=datetime(2026, 9, 18, 4, 0, 0),
    ))

    assert codes(result) == []
    assert result.problems == 0
    assert [
        (report.job.user, report.job.command, len(report.runs))
        for report in result.job_reports
    ] == [("deploy", "/opt/app/tick", 60)]


def test_a_guessed_format_whose_entries_did_match_says_nothing(tmp_path):
    """The guess is only suspect once it has cost the scan its matches.

    ``backup /data`` settles nothing either, but here the log says exactly that
    - the reading was right, and a warning would be noise on a clean scan.
    """
    crontab_file = tmp_path / "web01.crontab"
    crontab_file.write_text("* * * * * backup /data\n")
    log = tmp_path / "syslog"
    log.write_text(busy_cron_log("root", command="backup /data"))

    result = scan(ScanOptions(
        crontab_paths=[crontab_file], log_paths=[log], now=NOW,
        since=datetime(2026, 9, 18, 3, 0, 0),
        until=datetime(2026, 9, 18, 4, 0, 0),
    ))

    assert len(result.job_reports[0].runs) == 60
    assert (codes(result), result.problems) == ([], 0)


def test_a_scan_that_reconciled_nothing_at_all_keeps_its_own_warning(tmp_path):
    """One warning per scan, and the all-or-nothing one outranks this."""
    guessed = tmp_path / "webjobs"
    guessed.write_text(GUESSABLE)
    log = tmp_path / "syslog"
    log.write_text(busy_cron_log("deploy", command="/opt/app/tick"))

    result = scan(ScanOptions(
        crontab_paths=[guessed], log_paths=[log], now=NOW,
        since=datetime(2026, 9, 18, 3, 0, 0),
        until=datetime(2026, 9, 18, 4, 0, 0),
    ))

    assert codes(result) == ["no-runs-matched"]


def test_the_cli_prints_the_guessed_format_warning_and_exits_one(tmp_path, capsys):
    guessed, matching, matching_log, other_log = _mixed_crontabs(tmp_path)

    code = cli.main([
        "scan",
        "--crontab", str(matching), "--crontab", str(guessed),
        "--log-file", str(matching_log), "--log-file", str(other_log),
        "--since", "2026-09-18T03:00:00", "--until", "2026-09-18T04:00:00",
        "--now", NOW_ARG, "--format", "json",
    ])

    captured = capsys.readouterr()
    # A real report with a warning on it: 1, not the 3 a scan that reconciled
    # nothing gets.
    assert code == cli.EXIT_PROBLEMS
    assert "cron-postmortem: crontab-format-guessed:" in captured.err
    report = json.loads(captured.out)
    assert [warning["code"] for warning in report["warnings"]] == [
        "crontab-format-guessed"
    ]
    assert report["summary"]["warnings"] == 1


def test_the_warning_names_the_format_the_entries_were_read_in(tmp_path):
    """``--crontab-user`` overrules the vote, so the vote is not what to report.

    These two entries read as system format on repetition alone and the option
    overruled that, which is why every command still carries ``deploy`` in front
    of it - the reading the missed runs come from.  The warning used to name the
    vote instead: "read as a system-format crontab ... pass --crontab-format
    user", which is the format already in effect, while the reading that cost the
    scan its matches went unsaid.
    """
    overruled = tmp_path / "webjobs"
    overruled.write_text(
        "*/1 * * * * deploy /opt/app/tick\n*/2 * * * * deploy /opt/app/other\n"
    )
    matching = tmp_path / "root"
    matching.write_text(MATCHING)
    log = tmp_path / "syslog"
    log.write_text(busy_cron_log("alice"))

    result = scan(ScanOptions(
        crontab_paths=[matching, overruled], log_paths=[log], now=NOW,
        crontab_user="alice",
        since=datetime(2026, 9, 18, 3, 0, 0),
        until=datetime(2026, 9, 18, 4, 0, 0),
    ))

    assert codes(result) == ["crontab-format-guessed"]
    message = result.warnings[0].message
    assert "read as a user-format crontab because --crontab-user was given" in message
    assert "--crontab-format system if it is wrong" in message
    # The format that was applied, and the one that was not, are not swapped.
    assert "read as a system-format crontab" not in message
    assert "--crontab-format user" not in message
    # ... and it is the reading the unmatched entries actually came from.
    unmatched = [report for report in result.job_reports if not report.runs]
    assert [report.job.command for report in unmatched] == [
        "deploy /opt/app/tick", "deploy /opt/app/other"
    ]

# --- a log with no cron lines at all is the same broken comparison --------------

# A journal collected with "journalctl -u backup.service": every line parses, and
# cron's own lines were never in it.  Nothing goes unmatched, because nothing is
# there to match - and every occurrence of every crontab entry comes back missed.
UNIT_ONLY_JOURNAL = (
    "2026-09-18T03:00:01+0200 h systemd[1]: Starting backup.service - Nightly backup...\n"
    "2026-09-18T03:10:09+0200 h systemd[1]: Finished backup.service - Nightly backup.\n"
    "2026-09-18T03:50:09+0200 h systemd[1]: Finished backup.service - Nightly backup.\n"
)


def _no_cron_lines(tmp_path) -> tuple[Path, Path]:
    crontab = tmp_path / "root"
    crontab.write_text("*/10 * * * * /usr/local/bin/poll.sh\n")
    log = tmp_path / "journal.log"
    log.write_text(UNIT_ONLY_JOURNAL)
    return crontab, log


def test_a_log_without_one_cron_line_is_the_same_warning(tmp_path):
    """Acceptance criterion 2 without a single cron run to point at.

    The warning used to hang on the unmatched runs, so a log that carries no
    ``CMD`` line at all - the shape a unit-filtered journal or the wrong log file
    has, and the likeliest way to get zero matches - produced the full page of
    invented missed runs with nothing said about it.
    """
    crontab_file, log = _no_cron_lines(tmp_path)

    result = scan(ScanOptions(
        crontab_paths=[crontab_file], log_paths=[log], now=NOW,
        since=datetime(2026, 9, 18, 3, 0, 0),
        until=datetime(2026, 9, 18, 4, 0, 0),
    ))

    assert codes(result) == ["no-runs-matched"]
    assert result.alerts is True
    assert result.usage_error is False
    # The findings are there, and all of them are disowned by the warning.
    assert result.problems > 0
    assert result.standing_findings == []
    message = result.warnings[0].message
    assert "3 log line(s) were understood but not one of them is a cron run" in message
    assert "1 crontab entry had nothing to be compared against" in message
    # Nothing was unmatched, so the sentence about unmatched runs is left off.
    assert "matched no known crontab entry" not in message
    assert message.endswith("filtering by unit.")


def test_the_cli_exits_three_on_a_log_with_no_cron_lines(tmp_path, capsys):
    crontab_file, log = _no_cron_lines(tmp_path)

    code = cli.main([
        "scan", "--crontab", str(crontab_file), "--log-file", str(log),
        "--now", NOW_ARG,
        "--since", "2026-09-18T03:00:00", "--until", "2026-09-18T04:00:00",
        "--format", "json",
    ])

    captured = capsys.readouterr()
    assert code == cli.EXIT_NO_MATCH
    assert "cron-postmortem: no-runs-matched:" in captured.err
    report = json.loads(captured.out)
    assert [warning["code"] for warning in report["warnings"]] == ["no-runs-matched"]
    assert report["summary"]["runs"] == 0
    assert report["summary"]["missed"] > 0


def test_a_log_with_no_cron_lines_and_no_cron_entries_says_nothing(tmp_path):
    """Only timers were scanned, so there was no cron comparison to break."""
    show = tmp_path / "show.txt"
    show.write_text(TIMER_SLACK_SHOW)
    log = tmp_path / "journal.log"
    log.write_text(UNIT_ONLY_JOURNAL)

    result = scan(ScanOptions(
        show_paths=[show], log_paths=[log], now=NOW,
        since=datetime(2026, 9, 18, 3, 0, 0),
        until=datetime(2026, 9, 18, 4, 0, 0),
    ))

    assert "no-runs-matched" not in codes(result)


def test_a_log_nothing_parsed_from_is_not_charged_twice(tmp_path):
    """``no-log-lines`` already covers a log that was not understood at all.

    Both warnings describe the same scan there, and only one of them is about
    something the caller can fix, so the specific one is left to say it.
    """
    crontab_file = tmp_path / "root"
    crontab_file.write_text("*/10 * * * * /usr/local/bin/poll.sh\n")
    log = tmp_path / "access.log"
    log.write_text(FOREIGN_LOG)

    result = scan(ScanOptions(
        crontab_paths=[crontab_file], log_paths=[log], now=NOW,
        since=datetime(2026, 9, 18, 3, 0, 0),
        until=datetime(2026, 9, 18, 4, 0, 0),
    ))

    assert codes(result) == ["no-log-lines"]
