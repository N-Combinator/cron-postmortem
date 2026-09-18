"""One test per complaint that motivated this tool.

Each test rebuilds the situation its author described - the crontab they said
they had, and the log the box would have written - runs the CLI over it exactly
as a user would, and asserts the thing they could not see is reported.  The
quote and the link in every docstring come from docs/motivation.md, which is the
prose side of the same four sources.

These are end-to-end tests on purpose: they go through ``cli.main`` so that a
regression anywhere between the crontab parser and the exit code turns them red.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from cron_postmortem import cli


def session(
    host: str, day: str, opened: str, closed: str, user: str, command: str, pid: int,
) -> str:
    """The three syslog lines cron writes for one run.

    The `CMD` line comes from a child process, so its pid differs from the pam
    session's; without the session pair a run has no end and no overlap can be
    seen, which is why every scenario log below is written this way.
    """
    return (
        f"{day} {opened} {host} CRON[{pid}]: "
        f"pam_unix(cron:session): session opened for user {user} by (uid=0)\n"
        f"{day} {opened} {host} CRON[{pid + 1}]: ({user}) CMD ({command})\n"
        f"{day} {closed} {host} CRON[{pid}]: "
        f"pam_unix(cron:session): session closed for user {user}\n"
    )


def findings_of(report: dict, kind: str) -> list[dict]:
    return [finding for finding in report["findings"] if finding["kind"] == kind]


def test_a_hung_script_with_the_next_runs_piled_on_top(tmp_path, capsys):
    """Tiny_Bookkeeper_3156, r/homelab, 2026-09-03.

    "handling with a mix of cron jobs and bash scripts. It works until it
    doesn't and debugging failures is a pain."

    https://www.reddit.com/r/homelab/comments/1w60v3l/best_self_hosted_too_to_automate_recurring_tasks/

    A mix of cron entries and bash scripts, and the failure that is a pain to
    debug: sync-media.sh hangs for 43 minutes one morning and the next two
    quarter-hourly invocations start on top of it.  Nothing in the log says
    "failed" - the runs are all there - so the collision is only visible once
    each run's end is reconstructed.
    """
    crontab = tmp_path / "root"
    crontab.write_text(
        "*/15 * * * * /home/tim/scripts/sync-media.sh\n"
        "0    4 * * * /home/tim/scripts/backup-nas.sh\n"
    )
    sync = "/home/tim/scripts/sync-media.sh"
    log = tmp_path / "syslog"
    log.write_text(
        session("nas", "Sep 18", "03:00:01", "03:02:10", "root", sync, 2000)
        # The run that hangs, and the two that pile on underneath it.
        + session("nas", "Sep 18", "03:15:01", "03:58:40", "root", sync, 2010)
        + session("nas", "Sep 18", "03:30:01", "03:31:05", "root", sync, 2020)
        + session("nas", "Sep 18", "03:45:01", "03:46:02", "root", sync, 2030)
        + session("nas", "Sep 18", "04:00:01", "04:01:00", "root", sync, 2040)
        + session("nas", "Sep 18", "04:00:02", "04:20:33", "root",
                  "/home/tim/scripts/backup-nas.sh", 2050)
        + session("nas", "Sep 18", "04:15:01", "04:16:00", "root", sync, 2060)
    )

    code = cli.main([
        "scan", "--crontab", str(crontab), "--log-file", str(log),
        "--now", "2026-09-18T12:00:00", "--format", "json",
    ])
    report = json.loads(capsys.readouterr().out)

    assert code == cli.EXIT_PROBLEMS
    overlaps = findings_of(report, "overlap")
    assert [finding["job_id"] for finding in overlaps] == [f"cron:root:{sync}"] * 2
    # Both collisions are reported against the run that hung, not just the
    # neighbouring one, and each is measured as the time they really shared.
    assert [finding["when"] for finding in overlaps] == [
        "2026-09-18T03:30:01", "2026-09-18T03:45:01",
    ]
    assert [finding["details"]["previous_start"] for finding in overlaps] == [
        "2026-09-18T03:15:01", "2026-09-18T03:15:01",
    ]
    assert all(finding["details"]["overlap_seconds"] > 0 for finding in overlaps)
    # The other job in the same crontab ran cleanly and is not dragged in.
    assert report["summary"]["missed"] == 0


def test_a_job_that_stopped_three_months_ago(tmp_path, capsys):
    """Brilliant_Length_765, r/homelab, 2026-01-10.

    "Bash scripts + cron jobs - worked until they didn't, found out 3 months
    later"

    https://www.reddit.com/r/homelab/comments/1q9f4xp/built_stacksnap_because_i_got_tired_of_corrupted/

    backup-to-b2.sh runs for the last time on 2026-06-14 and is never heard from
    again; the hourly-ish prune job keeps going, so cron itself is plainly
    alive and nothing about the box looks broken.  Three months later one scan
    of the same syslog names the day the backup stopped.
    """
    crontab = tmp_path / "root"
    crontab.write_text(
        "30 2 * * * /opt/scripts/prune-snapshots.sh\n"
        "0  3 * * * /opt/scripts/backup-to-b2.sh\n"
    )
    last_good = date(2026, 6, 14)
    lines = []
    day = date(2026, 6, 13)
    while day <= date(2026, 9, 18):
        stamp = day.strftime("%b %e")
        lines.append(session("vault", stamp, "02:30:01", "02:30:47", "root",
                             "/opt/scripts/prune-snapshots.sh", 4000))
        if day <= last_good:
            lines.append(session("vault", stamp, "03:00:01", "03:08:44", "root",
                                 "/opt/scripts/backup-to-b2.sh", 3000))
        day += timedelta(days=1)
    log = tmp_path / "syslog"
    log.write_text("".join(lines))

    code = cli.main([
        "scan", "--crontab", str(crontab), "--log-file", str(log),
        "--now", "2026-09-18T12:00:00", "--format", "json",
    ])
    report = json.loads(capsys.readouterr().out)

    assert code == cli.EXIT_PROBLEMS
    missed = findings_of(report, "missed")
    # Only the backup is missing: the prune job proves cron kept running.
    assert {finding["job_id"] for finding in missed} == {
        "cron:root:/opt/scripts/backup-to-b2.sh"
    }
    # The first missed occurrence is the morning after the last successful run,
    # which is the answer the author did not have for three months.
    assert missed[0]["when"] == "2026-06-15T03:00:00"
    assert missed[-1]["when"] == "2026-09-17T03:00:00"
    # Every day in between, not just the ends.
    assert len(missed) == (date(2026, 9, 17) - date(2026, 6, 15)).days + 1


def test_every_entry_is_checked_without_instrumenting_any_of_them(tmp_path, capsys):
    """Fili96, r/homelab, 2025-11-13.

    "healthchecks.io... seems a bit too simple (i need to manually create each
    cron job to check everything...)"

    https://www.reddit.com/r/homelab/comments/1ow6cua/monitoring_software/

    Five entries, none of them registered anywhere or wrapped in a ping - the
    crontab is exactly what the box already had.  One invocation checks all
    five and finds the one that did not run, which is the per-job manual setup
    the author did not want to do.
    """
    crontab_text = (
        "*/5 * * * * /usr/local/bin/check-disk.sh\n"
        "0   2 * * * /usr/local/bin/pg-dump.sh\n"
        "0   3 * * * /usr/local/bin/restic-backup.sh\n"
        "15  * * * * /usr/local/bin/rotate-logs.sh\n"
        "0   5 * * 0 /usr/local/bin/update-containers.sh\n"
    )
    # The point of the scenario: no ping URL, no wrapper, no per-job edit.
    assert "curl" not in crontab_text and "hc-ping" not in crontab_text
    crontab = tmp_path / "root"
    crontab.write_text(crontab_text)

    lines = []
    pid = 5000
    for hour in ("01", "02", "03", "04"):
        for minute in range(0, 60, 5):
            lines.append(session("tower", "Nov 13", f"{hour}:{minute:02d}:01",
                                 f"{hour}:{minute:02d}:04", "root",
                                 "/usr/local/bin/check-disk.sh", pid))
            pid += 10
        lines.append(session("tower", "Nov 13", f"{hour}:15:01", f"{hour}:15:20",
                             "root", "/usr/local/bin/rotate-logs.sh", pid))
        pid += 10
    # 02:00 dumps the database; 03:00 should have backed it up and never fired.
    lines.append(session("tower", "Nov 13", "02:00:02", "02:11:38", "root",
                         "/usr/local/bin/pg-dump.sh", pid))
    log = tmp_path / "syslog"
    log.write_text("".join(sorted(lines)))

    code = cli.main([
        "scan", "--crontab", str(crontab), "--log-file", str(log),
        "--now", "2025-11-13T12:00:00", "--format", "json",
    ])
    report = json.loads(capsys.readouterr().out)

    assert code == cli.EXIT_PROBLEMS
    # All five entries are known to the scan, without any of them opting in.
    assert [job["id"] for job in report["jobs"]] == [
        "cron:root:/usr/local/bin/check-disk.sh",
        "cron:root:/usr/local/bin/pg-dump.sh",
        "cron:root:/usr/local/bin/restic-backup.sh",
        "cron:root:/usr/local/bin/rotate-logs.sh",
        "cron:root:/usr/local/bin/update-containers.sh",
    ]
    missed = findings_of(report, "missed")
    assert [(finding["job_id"], finding["when"]) for finding in missed] == [
        ("cron:root:/usr/local/bin/restic-backup.sh", "2025-11-13T03:00:00"),
    ]


def test_cron_monitoring_without_adding_anything_to_the_stack(tmp_path, monkeypatch, capsys):
    """mr_Pepper762, r/homelab, 2026-07-30.

    Lists "Healthchecks — cron job monitoring" as a needed stack component

    https://www.reddit.com/r/homelab/comments/1vaqhsf/before_after_of_my_homelab_selfhosted_stack/

    The stack component this replaces is a server; here the whole of it is one
    command over files that already exist.  A box with a problem exits 1, the
    same box once the job runs again exits 0, and nothing is contacted on the
    way - which is the entire contract a monitoring check needs.
    """
    def explode(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("a monitoring check must not shell out to the host")

    monkeypatch.setattr("cron_postmortem.systemd._run", explode)

    crontab = tmp_path / "root"
    crontab.write_text("0 * * * * /usr/local/bin/media-scan.sh\n")
    scan_sh = "/usr/local/bin/media-scan.sh"
    runs = {
        hour: session("plex", "Jul 30", f"{hour:02d}:00:01", f"{hour:02d}:04:{seconds}",
                      "root", scan_sh, 7000 + hour * 10)
        for hour, seconds in ((1, "12"), (2, "31"), (3, "02"), (4, "44"))
    }
    broken = tmp_path / "syslog-broken"
    # The 03:00 scan never started; everything else about the box looks normal.
    broken.write_text("".join(block for hour, block in runs.items() if hour != 3))
    fixed = tmp_path / "syslog-fixed"
    fixed.write_text("".join(runs.values()))

    args = ["scan", "--crontab", str(crontab), "--now", "2026-07-30T12:00:00"]

    assert cli.main([*args, "--log-file", str(broken), "--format", "json"]) == cli.EXIT_PROBLEMS
    report = json.loads(capsys.readouterr().out)
    assert [(finding["kind"], finding["when"]) for finding in findings_of(report, "missed")] == [
        ("missed", "2026-07-30T03:00:00"),
    ]

    # Same command, same box, job running again: silence and exit 0.
    assert cli.main([*args, "--log-file", str(fixed)]) == cli.EXIT_OK
    assert "No problems found." in capsys.readouterr().out
