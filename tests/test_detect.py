from __future__ import annotations

from datetime import datetime, timedelta

from cron_postmortem import detect
from cron_postmortem.logs import UnitEvent
from cron_postmortem.model import CRON, FAILURE, MISSED, OVERLAP, SYSTEMD, Job, Run
from cron_postmortem.systemd import parse_show

BASE = datetime(2026, 9, 18, 3, 0, 0)

CRON_JOB = Job(
    id="cron:root:/bin/job", source=CRON, schedule="0 * * * *", origin="x",
    user="root", command="/bin/job",
)
TIMER_JOB = Job(
    id="systemd:backup.timer", source=SYSTEMD, schedule="*-*-* 03:00:00", origin="x",
    unit="backup.service", timer="backup.timer",
)


def at(**kwargs) -> datetime:
    return BASE + timedelta(**kwargs)


def run(start: datetime, end: datetime | None = None, **kwargs) -> Run:
    return Run(job_id=CRON_JOB.id, start=start, end=end, **kwargs)


# --- missed -----------------------------------------------------------------

def test_missed_when_nothing_started_in_the_window():
    findings = detect.detect_missed(CRON_JOB, [BASE, at(hours=1)], [run(BASE)], 120)
    assert [finding.kind for finding in findings] == [MISSED]
    assert findings[0].when == at(hours=1)
    assert findings[0].details["expected"] == at(hours=1).isoformat()


def test_a_late_but_tolerated_run_is_not_missed():
    assert detect.detect_missed(CRON_JOB, [BASE], [run(at(seconds=119))], 120) == []


def test_a_run_past_the_tolerance_is_missed():
    findings = detect.detect_missed(CRON_JOB, [BASE], [run(at(seconds=121))], 120)
    assert len(findings) == 1


def test_one_run_cannot_cover_two_occurrences():
    occurrences = [BASE, at(seconds=60)]
    findings = detect.detect_missed(CRON_JOB, occurrences, [run(at(seconds=30))], 120)
    assert len(findings) == 1


def test_each_occurrence_takes_its_closest_run():
    occurrences = [BASE, at(seconds=60)]
    runs = [run(at(seconds=61)), run(at(seconds=1))]
    assert detect.detect_missed(CRON_JOB, occurrences, runs, 120) == []


def test_no_occurrences_means_nothing_to_miss():
    assert detect.detect_missed(CRON_JOB, [], [], 120) == []


# --- overlap ----------------------------------------------------------------

def test_overlap_when_the_next_run_starts_before_the_previous_ends():
    runs = [run(BASE, at(minutes=40)), run(at(minutes=30), at(minutes=35))]
    findings = detect.detect_overlaps(CRON_JOB, runs)
    assert [finding.kind for finding in findings] == [OVERLAP]
    assert findings[0].details["overlap_seconds"] == 600.0
    assert findings[0].when == at(minutes=30)


def test_back_to_back_runs_do_not_overlap():
    runs = [run(BASE, at(minutes=30)), run(at(minutes=30), at(minutes=40))]
    assert detect.detect_overlaps(CRON_JOB, runs) == []


def test_unknown_end_cannot_produce_an_overlap():
    runs = [run(BASE), run(at(minutes=1))]
    assert detect.detect_overlaps(CRON_JOB, runs) == []


def test_three_way_overlap_reports_each_consecutive_pair():
    runs = [
        run(BASE, at(minutes=50)),
        run(at(minutes=10), at(minutes=55)),
        run(at(minutes=20), at(minutes=25)),
    ]
    assert len(detect.detect_overlaps(CRON_JOB, runs)) == 2


# --- systemd failures -------------------------------------------------------

def events(*items: tuple[str, str, int, int | None, str | None]) -> list[UnitEvent]:
    return [
        UnitEvent(unit, kind, BASE + timedelta(seconds=offset), exit_code, result)
        for unit, kind, offset, exit_code, result in items
    ]


def test_unit_runs_pair_start_and_finish():
    runs = detect.build_unit_runs(
        TIMER_JOB.id,
        events(
            ("backup.service", "start", 0, None, None),
            ("backup.service", "finish", 30, 0, "success"),
        ),
    )
    assert len(runs) == 1
    assert runs[0].duration == 30.0
    assert not detect.run_failed(runs[0])


def test_the_three_line_failure_sequence_is_one_failed_run():
    runs = detect.build_unit_runs(
        TIMER_JOB.id,
        events(
            ("backup.service", "start", 0, None, None),
            ("backup.service", "fail", 12, 1, None),
            ("backup.service", "fail", 12, None, "exit-code"),
            ("backup.service", "fail", 12, None, "failed-to-start"),
        ),
    )
    assert len(runs) == 1
    assert (runs[0].exit_code, runs[0].result) == (1, "exit-code")
    findings = detect.detect_failures(TIMER_JOB, runs, None)
    assert [finding.kind for finding in findings] == [FAILURE]
    assert "exit code 1" in findings[0].message
    assert findings[0].details["evidence"] == "journalctl"


def test_concurrent_unit_runs_close_oldest_first():
    runs = detect.build_unit_runs(
        TIMER_JOB.id,
        events(
            ("logship.service", "start", 0, None, None),
            ("logship.service", "start", 60, None, None),
            ("logship.service", "finish", 120, 0, "success"),
            ("logship.service", "finish", 180, 0, "success"),
        ),
    )
    assert [(r.start, r.end) for r in runs] == [
        (BASE, at(seconds=120)),
        (at(seconds=60), at(seconds=180)),
    ]
    assert len(detect.detect_overlaps(TIMER_JOB, runs)) == 1


def test_a_terminal_event_without_a_start_still_yields_a_run():
    runs = detect.build_unit_runs(
        TIMER_JOB.id, events(("backup.service", "fail", 0, 3, None))
    )
    assert len(runs) == 1 and runs[0].exit_code == 3


def test_failed_unit_state_is_reported_from_systemctl_show():
    state = parse_show(
        "Id=backup.service\nActiveState=failed\nResult=exit-code\nExecMainStatus=2\n"
        "ExecMainExitTimestamp=Fri 2026-09-18 03:00:12 CEST\nNRestarts=0\n"
    )[0]
    findings = detect.detect_failures(TIMER_JOB, [], state)
    assert [finding.kind for finding in findings] == [FAILURE]
    assert findings[0].details["evidence"] == "systemctl show"
    assert findings[0].details["exit_code"] == 2


def test_the_same_failure_is_not_reported_twice():
    state = parse_show(
        "Id=backup.service\nActiveState=failed\nResult=exit-code\nExecMainStatus=1\n"
        "ExecMainExitTimestamp=Fri 2026-09-18 03:00:12 CEST\n"
    )[0]
    runs = detect.build_unit_runs(
        TIMER_JOB.id,
        events(
            ("backup.service", "start", 0, None, None),
            ("backup.service", "fail", 12, 1, "exit-code"),
        ),
    )
    assert len(detect.detect_failures(TIMER_JOB, runs, state)) == 1


def test_a_healthy_unit_produces_no_failure():
    state = parse_show(
        "Id=backup.service\nActiveState=inactive\nResult=success\nExecMainStatus=0\n"
    )[0]
    runs = detect.build_unit_runs(
        TIMER_JOB.id,
        events(
            ("backup.service", "start", 0, None, None),
            ("backup.service", "finish", 5, 0, "success"),
        ),
    )
    assert detect.detect_failures(TIMER_JOB, runs, state) == []


def test_cron_jobs_never_report_failures():
    # syslog carries no exit codes for plain cron; that is a data-source limit.
    assert detect.detect_failures(CRON_JOB, [run(BASE, at(minutes=1))], None) == []
