from __future__ import annotations

import json

from cron_postmortem.report import to_json, to_markdown
from cron_postmortem.scanner import ScanOptions, scan

from .conftest import FIXTURES, NOW


def build(**kwargs):
    return scan(ScanOptions(now=NOW, **kwargs))


def test_json_is_valid_and_carries_the_summary():
    result = build(
        show_paths=[FIXTURES / "systemctl-show.txt"],
        log_paths=[FIXTURES / "journal-systemd.log"],
    )
    payload = json.loads(to_json(result))
    assert payload["summary"] == {
        "jobs": 4, "runs": 8, "missed": 1, "overlap": 1, "failure": 1,
        "problems": 3, "diagnostics": 1,
    }
    assert payload["window"]["tolerance_seconds"] == 120.0


def test_markdown_lists_every_finding_kind():
    result = build(
        show_paths=[FIXTURES / "systemctl-show.txt"],
        log_paths=[FIXTURES / "journal-systemd.log"],
    )
    text = to_markdown(result)
    assert text.startswith("# cron-postmortem report")
    assert "## Failures (1)" in text
    assert "## Missed runs (1)" in text
    assert "## Overlapping runs (1)" in text
    assert "`systemd:backup-db.timer`" in text
    assert "## Diagnostics (1)" in text


def test_markdown_says_so_when_nothing_is_wrong(tmp_path):
    crontab = tmp_path / "root"
    crontab.write_text("0 3 * * * /bin/true\n")
    log = tmp_path / "syslog"
    log.write_text(
        "Sep 18 03:00:01 h CRON[1]: pam_unix(cron:session): session opened for user root\n"
        "Sep 18 03:00:01 h CRON[2]: (root) CMD (/bin/true)\n"
        "Sep 18 03:05:01 h CRON[1]: pam_unix(cron:session): session closed for user root\n"
    )
    text = to_markdown(build(crontab_paths=[crontab], log_paths=[log]))
    assert "No problems found." in text
    assert "## Failures" not in text


def test_unparsable_schedules_are_flagged_in_the_job_table(tmp_path):
    crontab = tmp_path / "root"
    crontab.write_text("@reboot /bin/true\n")
    log = tmp_path / "syslog"
    log.write_text("Sep 18 03:00:01 h CRON[2]: (root) CMD (/bin/other)\n")
    text = to_markdown(build(crontab_paths=[crontab], log_paths=[log]))
    assert "`@reboot (?)`" in text
