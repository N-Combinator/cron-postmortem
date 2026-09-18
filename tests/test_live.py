"""The live paths: ``scan`` with no arguments, ``--discover`` and ``--journal``.

Nothing here touches the real host either.  Crontab discovery is pointed at a
temporary tree and every subprocess goes through the single ``systemd._run``
seam, which is handed the same fixtures the offline tests use — so the code that
runs on a real box is exercised end to end, down to the argv it builds.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from cron_postmortem import cli, systemd
from cron_postmortem import crontab as crontab_mod
from cron_postmortem.model import FAILURE, MISSED, OVERLAP
from cron_postmortem.scanner import ScanOptions, scan

from .conftest import FIXTURES, NOW

NOW_ARG = "2026-09-18T12:00:00"
SINCE_ARG = "2026-09-18T02:00:00"
UNTIL_ARG = "2026-09-18T06:00:00"

TIMER_LIST = """\
backup-db.timer  loaded active waiting Nightly database backup timer
certbot.timer    loaded active waiting Run certbot twice daily
logship.timer    loaded active waiting Ship logs to archive timer
metrics-push.timer loaded active waiting Push metrics to collector timer
boot-cleanup.timer loaded active waiting Clean up after boot
"""


class FakeHost:
    """Stands in for ``systemctl`` and ``journalctl`` on a live box."""

    def __init__(self, timer_list: str = TIMER_LIST, journal_code: int = 0):
        self.calls: list[list[str]] = []
        self.timer_list = timer_list
        self.journal_code = journal_code

    def run(self, argv: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
        self.calls.append(argv)
        if argv[:2] == ["systemctl", "list-units"]:
            if not self.timer_list:
                return 1, "", "Failed to list units: Connection refused"
            return 0, self.timer_list, ""
        if argv[:2] == ["systemctl", "show"]:
            return 0, (FIXTURES / "systemctl-show.txt").read_text(), ""
        if argv[0] == "journalctl":
            if self.journal_code:
                return self.journal_code, "", "Failed to open journal"
            fixture = "syslog-cron.log" if "-t" in argv else "journal-systemd.log"
            return 0, (FIXTURES / fixture).read_text(), ""
        raise AssertionError(f"unexpected command: {argv}")  # pragma: no cover

    def argv_for(self, program: str) -> list[list[str]]:
        return [call for call in self.calls if call[0] == program]


@pytest.fixture
def host(monkeypatch) -> FakeHost:
    fake = FakeHost()
    monkeypatch.setattr(systemd, "_run", fake.run)
    return fake


@pytest.fixture
def etc(monkeypatch, tmp_path):
    """A fake /etc and /var/spool/cron for discovery to walk."""
    root = tmp_path / "host"
    (root / "etc").mkdir(parents=True)
    (root / "cron.d").mkdir()
    (root / "spool").mkdir()
    (root / "etc" / "crontab").write_text((FIXTURES / "etc" / "crontab").read_text())
    (root / "spool" / "root").write_text((FIXTURES / "spool" / "root").read_text())
    monkeypatch.setattr(crontab_mod, "SYSTEM_CRONTAB", root / "etc" / "crontab")
    monkeypatch.setattr(crontab_mod, "CRON_D_DIRS", (root / "cron.d",))
    monkeypatch.setattr(crontab_mod, "SPOOL_DIRS", (root / "spool",))
    return root


def live_options(**kwargs) -> ScanOptions:
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("discover", True)
    kwargs.setdefault("use_journal", True)
    kwargs.setdefault("since", datetime(2026, 9, 18, 2, 0))
    kwargs.setdefault("until", datetime(2026, 9, 18, 6, 0))
    return ScanOptions(**kwargs)


def test_a_discovering_scan_finds_the_same_problems_as_the_offline_one(host, etc):
    result = scan(live_options())

    assert len(result.job_reports) == 8
    # Identical to the offline scan over the same fixtures.
    assert result.counts() == {MISSED: 2, OVERLAP: 2, FAILURE: 1}
    assert result.sources["crontabs"] == [
        str(etc / "etc" / "crontab"), str(etc / "spool" / "root"),
    ]
    assert result.sources["systemctl_show"] == ["systemctl show (live)"]
    assert result.sources["logs"] == ["journalctl"]


def test_discovery_asks_systemctl_for_both_the_timers_and_their_services(host, etc):
    scan(live_options())
    show = host.argv_for("systemctl")[1]
    assert show[:2] == ["systemctl", "show"]
    assert "backup-db.timer" in show and "backup-db.service" in show
    assert "boot-cleanup.timer" in show


def test_the_window_is_passed_through_to_journalctl(host, etc):
    scan(live_options())
    cron_call, unit_call = host.argv_for("journalctl")
    for call in (cron_call, unit_call):
        assert call[call.index("--since") + 1] == "2026-09-18 02:00:00"
        assert call[call.index("--until") + 1] == "2026-09-18 06:00:00"
    assert cron_call[-6:] == ["-t", "CRON", "-t", "cron", "-t", "crond"]
    # One -u matcher per discovered unit, the timers' services.
    assert unit_call.count("-u") == 4
    assert "backup-db.service" in unit_call


def test_without_a_window_the_journal_is_asked_for_the_last_day(host, etc):
    scan(live_options(since=None, until=None))
    cron_call = host.argv_for("journalctl")[0]
    assert cron_call[cron_call.index("--since") + 1] == "2026-09-17 12:00:00"
    assert cron_call[cron_call.index("--until") + 1] == "2026-09-18 12:00:00"


def test_a_failing_systemctl_becomes_a_diagnostic_not_a_crash(monkeypatch, etc):
    fake = FakeHost(timer_list="")
    monkeypatch.setattr(systemd, "_run", fake.run)
    result = scan(live_options())
    assert any("list-units failed" in diag.message for diag in result.diagnostics)
    # The cron half of the scan still produced a report.
    assert [report.job.source for report in result.job_reports] == ["cron"] * 4


def test_a_failing_journalctl_becomes_a_diagnostic_not_a_crash(monkeypatch, etc):
    fake = FakeHost(journal_code=1)
    monkeypatch.setattr(systemd, "_run", fake.run)
    result = scan(live_options())
    assert sum("journalctl" in diag.message for diag in result.diagnostics) == 2
    # No log at all means every scheduled run is missed, which is the truth.
    assert result.counts()[MISSED] > 0


def test_an_undiscoverable_spool_directory_is_a_diagnostic(monkeypatch, host, etc):
    # A path that is not a directory fails to list the same way an unreadable
    # one does; on a real box this is /var/spool/cron/crontabs as non-root.
    blocked = etc / "spool-file"
    blocked.write_text("not a directory\n")
    monkeypatch.setattr(crontab_mod, "SPOOL_DIRS", (blocked,))
    result = scan(live_options())
    assert any("cannot list" in diag.message for diag in result.diagnostics)


def test_the_cli_with_no_sources_scans_this_host(host, etc, capsys):
    code = cli.main([
        "scan", "--since", SINCE_ARG, "--until", UNTIL_ARG, "--now", NOW_ARG,
        "--format", "json",
    ])
    assert code == cli.EXIT_PROBLEMS
    payload = json.loads(capsys.readouterr().out)
    summary = payload["summary"]
    assert (summary["jobs"], summary["runs"]) == (8, 18)
    assert (summary["missed"], summary["overlap"], summary["failure"]) == (2, 2, 1)
    assert summary["problems"] == 5
    assert payload["sources"]["logs"] == ["journalctl"]


def test_the_cli_can_mix_a_given_crontab_with_the_live_journal(host, etc, tmp_path, capsys):
    crontab = tmp_path / "root"
    crontab.write_text("50 2 * * * /usr/local/bin/heartbeat.sh\n")
    code = cli.main([
        "scan", "--crontab", str(crontab), "--journal",
        "--since", SINCE_ARG, "--until", UNTIL_ARG, "--now", NOW_ARG,
    ])
    out = capsys.readouterr().out
    # Only the crontab that was named: --crontab switches discovery off.
    assert code == cli.EXIT_OK
    assert "No problems found." in out
    assert out.count("| `cron:") == 1
