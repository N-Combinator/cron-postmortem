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


def test_format_is_detected_from_the_content_not_the_path():
    """The line shape decides, and the same text decides the same way anywhere."""
    assert crontab.detect_format(SYSTEM).system_format
    assert not crontab.detect_format(USER).system_format


def test_the_user_column_is_recognised_by_what_follows_it():
    def detected(line: str) -> str:
        return crontab.detect_format(line + "\n").name

    # Field 6 is a user name and field 7 opens a command of its own.
    assert detected("*/5 * * * * root /usr/bin/x") == "system"
    assert detected("17 * * * * www-data /usr/local/bin/rotate.sh") == "system"
    assert detected("09,39 * * * * root [ -x /usr/lib/php/sessionclean ] && x") == "system"
    assert detected("0 3 * * * backup flock -n /tmp/l /usr/bin/dump") == "system"
    assert detected("@daily www-data /usr/local/bin/rotate.sh --keep 7") == "system"

    # The command starts at field 6.
    assert detected("*/5 * * * * /usr/bin/x") == "user"
    assert detected("*/5 * * * * mysqlcheck --all-databases") == "user"
    assert detected("*/5 * * * * poll.sh >/dev/null 2>&1") == "user"
    assert detected("*/5 * * * * backup") == "user"
    assert detected("0 3 * * * run-parts /etc/cron.hourly") == "user"
    assert detected("@hourly /usr/local/bin/sync.sh") == "user"


def test_a_file_that_says_nothing_either_way_is_read_as_a_user_crontab():
    """Deterministic and documented: the tie goes to the per-user format.

    ``backup archive`` is a stock account followed by a plausible script and a
    plausible command followed by a plausible argument; nothing in the line can
    tell them apart, so the file falls back to the format ``crontab -l`` emits.
    """
    detection = crontab.detect_format("*/5 * * * * backup archive\n")
    assert (detection.system_format, detection.undecided) == (False, 1)
    assert not detection.unanimous


def test_the_majority_of_entries_decides_the_whole_file():
    """cron applies one format per file, so one odd line cannot split it."""
    mixed = (
        "0 1 * * * root /usr/bin/a\n"
        "0 2 * * * root /usr/bin/b\n"
        "0 3 * * * /usr/bin/c\n"
    )
    detection = crontab.detect_format(mixed)
    assert detection.system_format
    assert (detection.system_votes, detection.user_votes) == (2, 1)
    assert not detection.unanimous


def test_a_file_whose_entries_disagree_says_so(tmp_path):
    """One entry each way is a tie, so the file falls back to user format loudly."""
    collected = tmp_path / "root"
    collected.write_text("0 1 * * * root /usr/bin/a\n0 3 * * * /usr/bin/c\n")

    _, problems = crontab.load_crontab_file(collected)

    assert len(problems) == 1
    assert "read as a user-format crontab" in problems[0]
    assert "1 look system-format, 1 user-format" in problems[0]
    assert "--crontab-format" in problems[0]


def test_an_unambiguous_file_is_read_without_comment(tmp_path):
    collected = tmp_path / "root"
    collected.write_text("0 1 * * * root /usr/bin/a\n0 3 * * * root /usr/bin/b\n")

    jobs, problems = crontab.load_crontab_file(collected)

    assert [job.command for job in jobs] == ["/usr/bin/a", "/usr/bin/b"]
    assert problems == []


def test_a_path_after_field_6_does_not_make_field_6_a_user():
    """The shape that reads both ways, and the one that does not.

    ``backup.sh /data`` is a per-user entry running a script on a directory just
    as much as it is a system entry running ``/data`` as ``backup.sh``, and
    every per-user crontab is full of commands with file arguments.  Deciding
    system format off one such line moved the command into the user column and
    left the log unmatchable - the exact reason the format is detected at all.
    """

    def detection(line: str):
        return crontab.detect_format(line + "\n")

    # A script extension settles it: no distribution ships that account.
    for line in ("0 3 * * * backup.sh /data", "*/5 * * * * monitor.py /etc/m.conf"):
        assert (detection(line).name, detection(line).user_votes) == ("user", 1)

    # A bare word in front of a path could be either, so it decides nothing.
    for line in ("*/10 * * * * backup /data", "0 3 * * * report /var/log/x.log"):
        read = detection(line)
        assert (read.name, read.system_votes, read.undecided) == ("user", 0, 1)


def test_the_same_word_in_field_6_of_two_entries_is_a_user_column():
    """What a user column does and what a list of commands does not."""
    detection = crontab.detect_format(
        "0 3 * * * deploy /opt/a.sh\n0 4 * * * deploy /opt/b.sh\n"
    )

    assert detection.system_format
    assert (detection.system_votes, detection.repeated_name) == (2, "deploy")
    # Believed, but by the file rather than by any entry: still worth saying.
    assert detection.rests_on_repetition


def test_different_words_in_field_6_are_just_different_commands():
    detection = crontab.detect_format(
        "0 3 * * * report /var/log/a.log\n0 4 * * * cleanup /var/tmp\n"
    )

    assert (detection.system_format, detection.undecided) == (False, 2)


def test_one_account_settles_the_entries_that_could_go_either_way():
    """``root`` proves the file has a user column; its neighbour fills it."""
    detection = crontab.detect_format(
        "0 1 * * * root /usr/bin/a\n0 3 * * * deploy /opt/deploy.sh\n"
    )

    assert (detection.system_format, detection.system_votes) == (True, 2)
    assert not detection.rests_on_repetition


def test_a_user_column_taken_on_repetition_alone_says_so(tmp_path):
    collected = tmp_path / "web01.crontab"
    collected.write_text("0 3 * * * deploy /opt/a.sh\n0 4 * * * deploy /opt/b.sh\n")

    jobs, problems = crontab.load_crontab_file(collected)

    assert [(job.user, job.command) for job in jobs] == [
        ("deploy", "/opt/a.sh"),
        ("deploy", "/opt/b.sh"),
    ]
    assert len(problems) == 1
    assert "'deploy' sits in the user column" in problems[0]
    assert "not a name this tool knows as an account" in problems[0]


def test_naming_the_user_settles_a_format_no_entry_backs_up(tmp_path):
    """``--crontab-user`` answers a question only a user-format file leaves open.

    Asking who runs these entries says the entries do not name a user, so it
    outranks a system reading that rests on nothing but the same word turning up
    twice - and the operator is told what their option decided.
    """
    collected = tmp_path / "web01.crontab"
    collected.write_text("0 3 * * * deploy /opt/a.sh\n0 4 * * * deploy /opt/b.sh\n")

    jobs, problems = crontab.load_crontab_file(collected, user_override="alice")

    assert [(job.user, job.command) for job in jobs] == [
        ("alice", "deploy /opt/a.sh"),
        ("alice", "deploy /opt/b.sh"),
    ]
    assert len(problems) == 1
    assert "--crontab-user was given" in problems[0]
    assert "--crontab-format system" in problems[0]


def test_naming_the_user_does_not_overrule_entries_that_name_an_account(tmp_path):
    """Here the file answers the question, and the answer that was not used is
    reported rather than silently dropped."""
    collected = tmp_path / "web01.crontab"
    collected.write_text("0 3 * * * root /usr/bin/a\n")

    jobs, problems = crontab.load_crontab_file(collected, user_override="alice")

    assert [(job.user, job.command) for job in jobs] == [("root", "/usr/bin/a")]
    assert len(problems) == 1
    assert "--crontab-user 'alice' was not applied" in problems[0]


def test_lines_that_are_broken_in_either_format_do_not_vote():
    """A truncated entry says nothing about the file it sits in."""
    detection = crontab.detect_format("17 * * * *\n* * * *\n@daily\n")
    assert detection.entries == 0
    assert detection.unanimous


def test_the_format_override_still_wins_over_the_content(tmp_path):
    collected = tmp_path / "root"
    collected.write_text("*/5 * * * * root /usr/bin/x\n")

    jobs, problems = crontab.load_crontab_file(collected, format_override="user")

    assert [(job.user, job.command) for job in jobs] == [("root", "root /usr/bin/x")]
    # Forced by hand, so the scan does not second-guess the content.
    assert problems == []


def test_default_user_comes_from_the_filename():
    spool = Path("/var/spool/cron/crontabs/www-data")
    assert crontab.default_user_for(spool, trust_filename=True) == "www-data"
    # A username may contain a dot, and in the spool the name is the owner.
    assert crontab.default_user_for(
        Path("/var/spool/cron/crontabs/john.doe"), trust_filename=True
    ) == "john.doe"
    assert crontab.default_user_for(Path("/tmp/Some File.txt")) == "root"


def test_a_collected_crontab_is_not_attributed_to_its_capture_name():
    """The filename is the owner in the spool, not on the command line.

    A file named after the capture rather than after its owner used to invent a
    user nothing in the log can match, and every occurrence came back missed.
    """
    for name in ("hang.crontab", "web01.crontab", "root.txt", "crontab", "cron"):
        assert crontab.default_user_for(Path("/tmp") / name) == "root"
    # A bare plausible username is still believed - that is how people pass a
    # crontab copied straight out of the spool.
    assert crontab.default_user_for(Path("/tmp/www-data")) == "www-data"
    assert crontab.default_user_for(Path("/tmp/root")) == "root"


def test_an_attributed_crontab_says_which_user_it_picked(tmp_path):
    collected = tmp_path / "web01.crontab"
    collected.write_text("* * * * * /usr/local/bin/poll.sh\n")

    jobs, problems = crontab.load_crontab_file(collected)

    assert [job.user for job in jobs] == ["root"]
    assert len(problems) == 1
    assert "attributed to 'root'" in problems[0]
    assert "--crontab-user" in problems[0]


def test_an_explicit_user_silences_the_attribution_note(tmp_path):
    collected = tmp_path / "web01.crontab"
    collected.write_text("* * * * * /usr/local/bin/poll.sh\n")

    jobs, problems = crontab.load_crontab_file(collected, user_override="alice")

    assert [job.user for job in jobs] == ["alice"]
    assert problems == []


def test_load_from_disk_autodetects(fixtures):
    system, problems = crontab.load_crontab_file(fixtures / "etc" / "crontab")
    assert problems == []
    assert {job.user for job in system} == {"root", "www-data"}

    user, problems = crontab.load_crontab_file(fixtures / "spool" / "root")
    assert problems == []
    assert [job.command for job in user] == ["/usr/local/bin/heartbeat.sh"]
    assert user[0].user == "root"


def test_the_same_jobs_in_both_formats_come_out_the_same(tmp_path):
    """Acceptance criterion 1, on the two files that used to need their paths.

    The same three jobs are written once with a user column and once without,
    and each file is given the *other* format's usual name.  Both must come out
    as the same jobs, so nothing about the filename can be deciding.
    """
    system = tmp_path / "root"  # a spool name, holding a system crontab
    system.write_text(
        "0 3 * * * root /usr/local/bin/backup.sh\n"
        "*/30 * * * * root /usr/local/bin/sync-metrics.sh\n"
        "17 * * * * root /usr/local/bin/rotate-cache.sh\n"
    )
    per_user = tmp_path / "crontab"  # /etc/crontab's own name, holding a user crontab
    per_user.write_text(
        "0 3 * * * /usr/local/bin/backup.sh\n"
        "*/30 * * * * /usr/local/bin/sync-metrics.sh\n"
        "17 * * * * /usr/local/bin/rotate-cache.sh\n"
    )

    from_system, system_problems = crontab.load_crontab_file(system)
    from_user, user_problems = crontab.load_crontab_file(per_user)

    assert system_problems == []
    # Neither file's format is in doubt; the one note is about the *owner* of
    # the user-format file, whose name says nothing about who runs it.
    assert len(user_problems) == 1
    assert "attributed to 'root'" in user_problems[0]
    assert "do not agree" not in user_problems[0]
    assert [(job.user, job.command, job.schedule) for job in from_system] == [
        ("root", "/usr/local/bin/backup.sh", "0 3 * * *"),
        ("root", "/usr/local/bin/sync-metrics.sh", "*/30 * * * *"),
        ("root", "/usr/local/bin/rotate-cache.sh", "17 * * * *"),
    ]
    assert [job.id for job in from_user] == [job.id for job in from_system]


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


def test_cron_tz_is_reported_rather_than_silently_ignored():
    jobs, problems = crontab.parse_crontab(
        "CRON_TZ=Europe/Berlin\n0 3 * * * /bin/true\n", "u", False, "root"
    )
    assert len(jobs) == 1
    assert problems == [
        "u:1: CRON_TZ= is not applied; the entries below it are analysed in local time"
    ]


def test_ordinary_environment_lines_stay_quiet():
    _, problems = crontab.parse_crontab(
        "MAILTO=ops@example.com\nPATH=/usr/bin\n0 3 * * * /bin/true\n", "u", False, "root"
    )
    assert problems == []
