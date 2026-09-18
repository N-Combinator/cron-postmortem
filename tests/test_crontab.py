from __future__ import annotations

from pathlib import Path

from cron_postmortem import crontab

SYSTEM = """\
SHELL=/bin/sh
MAILTO=ops@example.com

# comment
0 3 * * * root /usr/local/bin/backup.sh
@daily www-data /usr/local/bin/rotate.sh --keep 7
17 * * * *
"""

USER = """\
MAILTO=""
*/5 * * * * /usr/local/bin/ping.sh   >/dev/null 2>&1
@hourly /usr/local/bin/sync.sh
0 3 * * * /usr/local/bin/ping.sh >/dev/null 2>&1
"""


def test_system_format_reads_the_user_column():
    jobs, problems = crontab.parse_crontab(SYSTEM, "/etc/crontab", system_format=True)
    assert [(job.user, job.command, job.schedule) for job in jobs] == [
        ("root", "/usr/local/bin/backup.sh", "0 3 * * *"),
        ("www-data", "/usr/local/bin/rotate.sh --keep 7", "@daily"),
    ]
    assert len(problems) == 1 and "expected 7 fields" in problems[0]


def test_user_format_attributes_the_default_user_and_normalises_whitespace():
    jobs, problems = crontab.parse_crontab(USER, "/var/spool/cron/crontabs/root", False, "root")
    assert problems == []
    assert {job.user for job in jobs} == {"root"}
    assert jobs[0].command == "/usr/local/bin/ping.sh >/dev/null 2>&1"


def test_duplicate_entries_get_distinct_ids():
    jobs, _ = crontab.parse_crontab(USER, "u", False, "root")
    ids = [job.id for job in jobs]
    assert len(set(ids)) == len(ids)
    assert ids[-1].endswith("#2")


def test_origin_carries_the_line_number():
    jobs, _ = crontab.parse_crontab(SYSTEM, "/etc/crontab", system_format=True)
    assert jobs[0].origin == "/etc/crontab:5"


def test_format_detection_follows_the_path():
    assert crontab.is_system_format(Path("/etc/crontab"))
    assert crontab.is_system_format(Path("/etc/cron.d/php"))
    assert not crontab.is_system_format(Path("/var/spool/cron/crontabs/root"))
    assert not crontab.is_system_format(Path("/home/alice/my-crontab"))


def test_default_user_comes_from_the_filename():
    assert crontab.default_user_for(Path("/var/spool/cron/crontabs/www-data")) == "www-data"
    assert crontab.default_user_for(Path("/tmp/Some File.txt")) == "root"


def test_load_from_disk_autodetects(fixtures):
    system, problems = crontab.load_crontab_file(fixtures / "etc" / "crontab")
    assert problems == []
    assert {job.user for job in system} == {"root", "www-data"}

    user, problems = crontab.load_crontab_file(fixtures / "spool" / "root")
    assert problems == []
    assert [job.command for job in user] == ["/usr/local/bin/heartbeat.sh"]
    assert user[0].user == "root"


def test_unreadable_file_is_a_problem_not_a_crash(tmp_path):
    jobs, problems = crontab.load_crontab_file(tmp_path / "nope")
    assert jobs == []
    assert len(problems) == 1


def test_unknown_macro_is_reported():
    _, problems = crontab.parse_crontab("@sometimes /bin/true", "u", False, "root")
    assert "unknown schedule macro" in problems[0]


def test_discovery_reports_unreadable_directories_instead_of_raising(monkeypatch, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    real_iterdir = Path.iterdir

    def guarded(self):
        if self == spool:
            raise PermissionError(13, "Permission denied")
        return real_iterdir(self)

    monkeypatch.setattr(crontab, "SYSTEM_CRONTAB", tmp_path / "absent")
    monkeypatch.setattr(crontab, "CRON_D_DIRS", ())
    monkeypatch.setattr(crontab, "SPOOL_DIRS", (spool, tmp_path / "never-existed"))
    monkeypatch.setattr(Path, "iterdir", guarded)

    found, problems = crontab.discover_crontab_files()
    assert found == []
    assert len(problems) == 1
    assert "cannot list" in problems[0]


def test_discovery_collects_readable_files(monkeypatch, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "root").write_text("0 3 * * * /bin/true\n")
    (spool / "root.dpkg-old").write_text("0 4 * * * /bin/stale\n")
    monkeypatch.setattr(crontab, "SYSTEM_CRONTAB", tmp_path / "absent")
    monkeypatch.setattr(crontab, "CRON_D_DIRS", ())
    monkeypatch.setattr(crontab, "SPOOL_DIRS", (spool,))

    found, problems = crontab.discover_crontab_files()
    assert found == [spool / "root"]
    assert problems == []
