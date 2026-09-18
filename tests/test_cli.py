from __future__ import annotations

import json
from datetime import datetime

import pytest

from cron_postmortem import cli

from .conftest import FIXTURES

NOW_ARG = "2026-09-18T12:00:00"

CRON_ARGS = [
    "scan",
    "--crontab", str(FIXTURES / "etc" / "crontab"),
    "--crontab", str(FIXTURES / "spool" / "root"),
    "--log-file", str(FIXTURES / "syslog-cron.log"),
    "--now", NOW_ARG,
]
SYSTEMD_ARGS = [
    "scan",
    "--systemctl-show", str(FIXTURES / "systemctl-show.txt"),
    "--log-file", str(FIXTURES / "journal-systemd.log"),
    "--now", NOW_ARG,
]


def test_problems_give_a_non_zero_exit(capsys):
    assert cli.main([*CRON_ARGS, "--format", "markdown"]) == cli.EXIT_PROBLEMS
    out = capsys.readouterr().out
    assert "## Missed runs (1)" in out
    assert "## Overlapping runs (1)" in out


def test_exit_zero_overrides_the_failure_exit(capsys):
    assert cli.main([*CRON_ARGS, "--exit-zero"]) == cli.EXIT_OK
    capsys.readouterr()


def test_a_clean_scan_exits_zero(tmp_path, capsys):
    crontab = tmp_path / "root"
    crontab.write_text("0 3 * * * /bin/true\n")
    log = tmp_path / "syslog"
    log.write_text("Sep 18 03:00:01 h CRON[2]: (root) CMD (/bin/true)\n")
    code = cli.main([
        "scan", "--crontab", str(crontab), "--log-file", str(log), "--now", NOW_ARG,
    ])
    assert code == cli.EXIT_OK
    assert "No problems found." in capsys.readouterr().out


def test_json_output(capsys):
    cli.main([*SYSTEMD_ARGS, "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["failure"] == 1
    assert payload["version"]


def test_both_formats_emit_json_then_markdown(capsys):
    cli.main([*SYSTEMD_ARGS, "--format", "both"])
    out = capsys.readouterr().out
    assert out.lstrip().startswith("{")
    assert "# cron-postmortem report" in out
    json.loads(out[: out.index("# cron-postmortem report")])


def test_output_file(tmp_path, capsys):
    target = tmp_path / "report.json"
    cli.main([*SYSTEMD_ARGS, "--format", "json", "--output", str(target)])
    assert capsys.readouterr().out == ""
    assert json.loads(target.read_text())["summary"]["problems"] == 3


def test_ignore_suppresses_a_kind(capsys):
    assert cli.main([*CRON_ARGS, "--ignore", "missed", "--ignore", "overlap"]) == cli.EXIT_OK
    assert "No problems found." in capsys.readouterr().out


def test_tolerance_can_absorb_a_late_run(tmp_path, capsys):
    crontab = tmp_path / "root"
    crontab.write_text("0 3 * * * /bin/true\n")
    log = tmp_path / "syslog"
    log.write_text(
        "Sep 18 02:00:00 h CRON[1]: (root) CMD (/bin/other)\n"
        "Sep 18 03:09:00 h CRON[2]: (root) CMD (/bin/true)\n"
        "Sep 18 04:00:00 h CRON[3]: (root) CMD (/bin/other)\n"
    )
    args = ["scan", "--crontab", str(crontab), "--log-file", str(log), "--now", NOW_ARG]
    assert cli.main(args) == cli.EXIT_PROBLEMS
    capsys.readouterr()
    assert cli.main([*args, "--tolerance", "600"]) == cli.EXIT_OK
    capsys.readouterr()


def test_missing_input_file_is_a_usage_error(capsys):
    code = cli.main(["scan", "--log-file", "/nonexistent/syslog", "--now", NOW_ARG])
    assert code == cli.EXIT_USAGE
    assert "no such file" in capsys.readouterr().err


def test_unwritable_output_is_a_usage_error(tmp_path, capsys):
    target = tmp_path / "missing-dir" / "report.md"
    assert cli.main([*SYSTEMD_ARGS, "--output", str(target)]) == cli.EXIT_USAGE
    assert "cannot write" in capsys.readouterr().err


def test_offline_inputs_never_reach_the_live_system(monkeypatch, capsys):
    def explode(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("the offline path must not shell out")

    monkeypatch.setattr("cron_postmortem.systemd._run", explode)
    assert cli.main([*CRON_ARGS]) == cli.EXIT_PROBLEMS
    capsys.readouterr()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-09-18T03:00:00", datetime(2026, 9, 18, 3, 0)),
        ("2026-09-18 03:00:00", datetime(2026, 9, 18, 3, 0)),
        ("2026-09-18T03:00:00+02:00", datetime(2026, 9, 18, 3, 0)),
        ("now", datetime(2026, 9, 18, 12, 0)),
        ("24h", datetime(2026, 9, 17, 12, 0)),
        ("-24h", datetime(2026, 9, 17, 12, 0)),
        ("2d", datetime(2026, 9, 16, 12, 0)),
        ("90m", datetime(2026, 9, 18, 10, 30)),
        ("1d 2h 30m", datetime(2026, 9, 17, 9, 30)),
    ],
)
def test_time_arguments(text, expected):
    assert cli.parse_when(text, datetime(2026, 9, 18, 12, 0)) == expected


@pytest.mark.parametrize("text", ["", "yesterday", "5 fortnights", "0"])
def test_bad_time_arguments(text):
    with pytest.raises(cli.TimeArgError):
        cli.parse_when(text, datetime(2026, 9, 18, 12, 0))


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "cron-postmortem" in capsys.readouterr().out
