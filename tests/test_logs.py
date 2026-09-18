from __future__ import annotations

from datetime import datetime

from cron_postmortem import logs

from .conftest import NOW

SYSLOG = """\
Sep 18 03:00:01 web01 CRON[10010]: pam_unix(cron:session): session opened for user root by (uid=0)
Sep 18 03:00:01 web01 CRON[10011]: (root) CMD (/usr/local/bin/backup.sh)
Sep 18 03:00:01 web01 CRON[10012]: pam_unix(cron:session): session opened for user root by (uid=0)
Sep 18 03:00:01 web01 CRON[10013]: (root) CMD (/usr/local/bin/sync.sh)
Sep 18 03:00:02 web01 CRON[10012]: pam_unix(cron:session): session closed for user root
Sep 18 03:41:12 web01 CRON[10010]: pam_unix(cron:session): session closed for user root
"""


def scan(text, **kwargs):
    kwargs.setdefault("reference", NOW)
    return logs.scan_lines(text.splitlines(), "fixture", **kwargs)


def test_cmd_lines_are_paired_with_their_own_pam_session():
    result = scan(SYSLOG)
    by_command = {run.command: run for run in result.cron_runs}
    assert by_command["/usr/local/bin/backup.sh"].end == datetime(2026, 9, 18, 3, 41, 12)
    assert by_command["/usr/local/bin/sync.sh"].end == datetime(2026, 9, 18, 3, 0, 2)


def test_runs_without_a_session_have_no_end():
    result = scan("Sep 18 03:00:01 web01 CRON[99]: (root) CMD (/bin/true)\n")
    assert len(result.cron_runs) == 1
    assert result.cron_runs[0].end is None


def test_noise_lines_are_ignored():
    text = (
        "Sep 18 03:00:01 web01 CRON[1]: (CRON) INFO (pidfile fd = 3)\n"
        "Sep 18 03:00:01 web01 CRON[2]: (root) CMDOUT (some output)\n"
        "Sep 18 03:00:01 web01 sshd[3]: Accepted publickey for root\n"
        "this line has no timestamp at all\n"
    )
    result = scan(text)
    assert result.cron_runs == []
    assert result.unit_events == []


def test_iso_and_short_full_timestamps():
    text = (
        "2026-09-18T03:00:01+0200 web01 CRON[1]: (root) CMD (/bin/a)\n"
        "2026-09-18 03:00:02 web01 CRON[2]: (root) CMD (/bin/b)\n"
        "Fri 2026-09-18 03:00:03 CEST web01 CRON[3]: (root) CMD (/bin/c)\n"
    )
    result = scan(text)
    assert [run.start.second for run in result.cron_runs] == [1, 2, 3]
    assert [run.command for run in result.cron_runs] == ["/bin/a", "/bin/b", "/bin/c"]


def test_journal_header_lines_are_skipped():
    text = "-- Journal begins at Tue 2026-09-01 00:00:03 CEST. --\n" + SYSLOG
    assert len(scan(text).cron_runs) == 2


def test_syslog_year_rolls_over_at_new_year():
    text = (
        "Dec 31 23:59:01 web01 CRON[1]: (root) CMD (/bin/old)\n"
        "Jan 01 00:01:01 web01 CRON[2]: (root) CMD (/bin/new)\n"
    )
    result = scan(text, reference=datetime(2027, 1, 2, 9, 0))
    assert [run.start.year for run in result.cron_runs] == [2026, 2027]


def test_one_out_of_order_line_is_not_read_as_a_new_year():
    """A small backwards step is a line out of order, not a rollover.

    Reading it as one dates every line before it a year out; that line then
    becomes the scan's first timestamp, the window is clamped to it, and a
    year of occurrences comes back missed.  Centrally aggregated syslog whose
    hosts disagree across midnight, a backwards NTP step and a rotation glued
    together with ``cat`` all produce exactly this.
    """
    text = (
        "Sep 18 01:00:01 web01 CRON[100]: (root) CMD (/bin/hourly)\n"
        "Sep 17 23:59:59 web01 CRON[105]: (root) CMD (/bin/other)\n"
        "Sep 18 02:00:01 web01 CRON[110]: (root) CMD (/bin/hourly)\n"
    )
    result = scan(text)
    assert [run.start for run in result.cron_runs] == [
        datetime(2026, 9, 17, 23, 59, 59),
        datetime(2026, 9, 18, 1, 0, 1),
        datetime(2026, 9, 18, 2, 0, 1),
    ]
    assert result.first_timestamp == datetime(2026, 9, 17, 23, 59, 59)


def test_a_backwards_step_of_a_few_weeks_is_not_a_new_year():
    text = (
        "Sep 18 03:00:01 web01 CRON[1]: (root) CMD (/bin/a)\n"
        "Aug 20 03:00:01 web01 CRON[2]: (root) CMD (/bin/b)\n"
    )
    result = scan(text)
    assert {run.start.year for run in result.cron_runs} == {2026}


def test_syslog_year_is_shifted_back_when_it_would_be_in_the_future():
    text = "Dec 31 23:59:01 web01 CRON[1]: (root) CMD (/bin/old)\n"
    result = scan(text, reference=datetime(2026, 9, 18, 12, 0))
    assert result.cron_runs[0].start.year == 2025


def test_systemd_unit_prefixed_lines():
    text = (
        "2026-09-18T04:00:03+0200 h systemd[1]: Starting backup.service - Nightly backup...\n"
        "2026-09-18T04:00:15+0200 h systemd[1]: backup.service: "
        "Main process exited, code=exited, status=1/FAILURE\n"
        "2026-09-18T04:00:15+0200 h systemd[1]: backup.service: Failed with result 'exit-code'.\n"
    )
    events = scan(text).unit_events
    assert [(event.unit, event.kind) for event in events] == [
        ("backup.service", "start"), ("backup.service", "fail"), ("backup.service", "fail"),
    ]
    assert events[1].exit_code == 1
    assert events[2].result == "exit-code"


def test_starting_and_started_are_different_events():
    # "Starting X..." begins the run; "Started X." only reports that its
    # start-up finished.  One run, not two.
    text = (
        "2026-09-18T04:00:03+0200 h systemd[1]: Starting backup.service - Nightly backup...\n"
        "2026-09-18T04:00:04+0200 h systemd[1]: Started backup.service - Nightly backup.\n"
        "2026-09-18T04:00:15+0200 h systemd[1]: backup.service: Deactivated successfully.\n"
    )
    assert [event.kind for event in scan(text).unit_events] == [
        "start", "started", "finish",
    ]


def test_killed_by_signal_is_not_reported_as_an_exit_code():
    text = (
        "2026-09-18T04:00:15+0200 h systemd[1]: backup.service: "
        "Main process exited, code=killed, status=9/KILL\n"
    )
    event = scan(text).unit_events[0]
    assert event.exit_code is None
    assert event.result == "killed/9"


def test_succeeded_line_closes_a_run_with_exit_zero():
    text = "2026-09-18T04:00:15+0200 h systemd[1]: backup.service: Succeeded.\n"
    event = scan(text).unit_events[0]
    assert (event.kind, event.exit_code, event.result) == ("finish", 0, "success")


def test_old_style_description_lines_need_the_description_map():
    text = (
        "2026-09-18T03:30:00+0200 h systemd[1]: Starting Certbot renewal...\n"
        "2026-09-18T03:30:20+0200 h systemd[1]: Finished Certbot renewal.\n"
    )
    unresolved = scan(text)
    assert unresolved.unit_events == []
    assert unresolved.unresolved_systemd_messages == {"Certbot renewal"}

    resolved = scan(text, description_to_unit={"Certbot renewal": "certbot.service"})
    assert [event.kind for event in resolved.unit_events] == ["start", "finish"]
    assert {event.unit for event in resolved.unit_events} == {"certbot.service"}


def test_leap_day_syslog_line_is_dated_not_crashed():
    # Year-less syslog is held in a placeholder year until the real one is known;
    # that placeholder must be a leap year or Feb 29 is unrepresentable.
    text = "Feb 29 03:00:01 web01 CRON[10011]: (root) CMD (/bin/true)\n"
    result = scan(text, reference=datetime(2024, 3, 1, 12, 0))
    assert [run.start for run in result.cron_runs] == [datetime(2024, 2, 29, 3, 0, 1)]


def test_leap_day_falls_back_to_the_28th_in_a_non_leap_year():
    text = "Feb 29 03:00:01 web01 CRON[10011]: (root) CMD (/bin/true)\n"
    result = scan(text, reference=datetime(2026, 3, 1, 12, 0))
    assert [run.start for run in result.cron_runs] == [datetime(2026, 2, 28, 3, 0, 1)]


def test_impossible_dates_are_skipped_not_fatal():
    text = (
        "Sep 99 03:00:01 web01 CRON[1]: (root) CMD (/bin/skipped)\n"
        "Feb 30 03:00:01 web01 CRON[2]: (root) CMD (/bin/skipped)\n"
        "2026-09-31 03:00:01 web01 CRON[3]: (root) CMD (/bin/skipped)\n"
        "Sep 18 03:00:01 web01 CRON[4]: (root) CMD (/bin/kept)\n"
    )
    result = scan(text)
    assert [run.command for run in result.cron_runs] == ["/bin/kept"]


def test_scan_records_the_covered_window(fixtures):
    text = (fixtures / "syslog-cron.log").read_text()
    result = scan(text)
    assert result.first_timestamp == datetime(2026, 9, 18, 2, 50, 0)
    assert result.last_timestamp == datetime(2026, 9, 18, 5, 33, 10)
    assert result.lines_total == 34


def test_every_journal_identifier_is_one_the_parser_accepts():
    # The live journal query and the parser must name the same daemons: asking
    # journalctl for fewer spellings than the parser understands makes a whole
    # family of hosts (cronie logs as CROND) report no cron runs at all.
    identifiers = logs.journal_cron_identifiers()
    assert {name.lower() for name in identifiers} == logs.CRON_IDENTS
    # journalctl -t matches the recorded spelling, so both cases are asked for.
    for name in logs.CRON_IDENTS:
        assert name in identifiers and name.upper() in identifiers


def test_a_crond_line_is_parsed_like_a_cron_one():
    result = scan(
        "Sep 18 03:00:01 rhel01 CROND[7]: pam_unix(crond:session): "
        "session opened for user root by (uid=0)\n"
        "Sep 18 03:00:01 rhel01 CROND[8]: (root) CMD (/usr/local/bin/backup.sh)\n"
        "Sep 18 03:04:00 rhel01 CROND[7]: pam_unix(crond:session): "
        "session closed for user root\n"
    )
    assert len(result.cron_runs) == 1
    run = result.cron_runs[0]
    assert run.command == "/usr/local/bin/backup.sh"
    assert run.end == datetime(2026, 9, 18, 3, 4)


# --- several sources at once --------------------------------------------------

def test_each_source_infers_its_own_year():
    """Year-less syslog is dated from the order of the lines in ONE file.

    Concatenating the sources first puts a backwards jump at the seam, which
    reads as a December -> January rollover and dates a whole file a year out.
    """
    newer = "Sep 17 03:00:01 web01 CRON[1]: (root) CMD (/bin/newer)\n"
    older = "Sep 10 03:00:01 web01 CRON[2]: (root) CMD (/bin/older)\n"

    result = logs.scan_sources(
        [("syslog", newer.splitlines()), ("syslog.1", older.splitlines())],
        reference=NOW,
    )

    dates = {run.command: run.start for run in result.cron_runs}
    assert dates["/bin/newer"] == datetime(2026, 9, 17, 3, 0, 1)
    assert dates["/bin/older"] == datetime(2026, 9, 10, 3, 0, 1)
    assert (result.lines_total, result.lines_parsed) == (2, 2)
    assert result.first_timestamp == datetime(2026, 9, 10, 3, 0, 1)


def test_sources_are_merged_in_time_order_before_events_are_built():
    """Dating is per file; pairing is over the merged stream."""
    newer = (
        "Sep 18 00:02:00 web01 CRON[10]: pam_unix(cron:session): session closed for user root\n"
    )
    older = (
        "Sep 17 23:59:00 web01 CRON[10]: pam_unix(cron:session): session opened for user root\n"
        "Sep 17 23:59:00 web01 CRON[11]: (root) CMD (/bin/midnight)\n"
    )

    result = logs.scan_sources(
        [("syslog", newer.splitlines()), ("syslog.1", older.splitlines())],
        reference=NOW,
    )

    assert len(result.cron_runs) == 1
    assert result.cron_runs[0].start == datetime(2026, 9, 17, 23, 59, 0)
    assert result.cron_runs[0].end == datetime(2026, 9, 18, 0, 2, 0)


def test_an_empty_source_list_scans_nothing():
    result = logs.scan_sources([], reference=NOW)
    assert (result.lines_total, result.lines_parsed) == (0, 0)
    assert result.first_timestamp is None
