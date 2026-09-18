"""The three detectors: missed runs, overlapping runs, systemd unit failures."""

from __future__ import annotations

from datetime import datetime, timedelta

from .logs import UnitEvent
from .model import (
    ERROR,
    FAILURE,
    MISSED,
    OVERLAP,
    SYSTEMD,
    WARNING,
    Finding,
    Job,
    Run,
)
from .systemd import UnitState, parse_timestamp

# A trailing event that only adds detail (``Failed with result ...`` right after
# ``Main process exited ...``) belongs to the run that just ended.
EVENT_MERGE_WINDOW = timedelta(seconds=5)


def build_unit_runs(job_id: str, events: list[UnitEvent]) -> list[Run]:
    """Pair systemd start/finish/fail events into runs.

    ``Starting X...`` opens a run and ``Started X.`` reports that its start-up
    finished, so a unit logging both (anything that is not ``Type=simple``) still
    gets one run.  A ``Started`` with no activation waiting for it does open a
    run: a ``Type=simple`` unit is logged with ``Started`` alone.

    Several terminal lines describe one ending (``Main process exited ...`` then
    ``Failed with result ...`` then ``Failed to start ...``), so a terminal event
    that lands right after a run closed enriches that run instead of inventing a
    new one.  Otherwise terminal events close open runs oldest-first, which keeps
    concurrent runs of the same unit distinguishable.
    """
    runs: list[Run] = []
    open_runs: list[Run] = []
    activating: list[Run] = []  # opened by "Starting", not yet confirmed by "Started"
    last_closed: Run | None = None
    for event in sorted(events, key=lambda item: item.timestamp):
        if event.kind == "start":
            started = Run(job_id=job_id, start=event.timestamp)
            runs.append(started)
            open_runs.append(started)
            activating.append(started)
            continue
        if event.kind == "started":
            if activating:
                activating.pop(0)
                continue
            simple = Run(job_id=job_id, start=event.timestamp)
            runs.append(simple)
            open_runs.append(simple)
            continue
        if (
            last_closed is not None
            and last_closed.end is not None
            and event.timestamp - last_closed.end <= EVENT_MERGE_WINDOW
            and (not open_runs or _adds_detail(last_closed, event))
        ):
            _close(last_closed, event)
            continue
        if open_runs:
            closing = open_runs.pop(0)
            # It may have ended without ever reporting a finished start-up.
            activating = [run for run in activating if run is not closing]
            _close(closing, event)
            last_closed = closing
            continue
        # A terminal event whose start fell outside the log window.
        orphan = Run(job_id=job_id, start=event.timestamp)
        _close(orphan, event)
        runs.append(orphan)
        last_closed = orphan
    return runs


def _adds_detail(run: Run, event: UnitEvent) -> bool:
    """True when the event only refines what we already know about ``run``."""
    if event.exit_code is not None:
        return run.exit_code is None
    if event.result is not None:
        return run.result in (None, "success")
    return True


def _close(run: Run, event: UnitEvent) -> None:
    run.end = event.timestamp
    if event.exit_code is not None and run.exit_code is None:
        run.exit_code = event.exit_code
    if event.result is not None and run.result in (None, "success"):
        run.result = event.result
    elif run.result is None and event.kind == "finish" and run.exit_code in (None, 0):
        run.result = "success"


def run_failed(run: Run) -> bool:
    """Whether a reconstructed run failed, by systemd's verdict where we have one.

    ``Result=`` / ``Deactivated successfully`` is systemd's own judgement and
    already accounts for ``SuccessExitStatus=``, so it outranks the raw exit
    status; the status decides only when the journal gave us no verdict.
    """
    if run.result:
        return run.result != "success"
    return run.exit_code is not None and run.exit_code != 0


def detect_missed(
    job: Job,
    occurrences: list[datetime],
    runs: list[Run],
    tolerance: float,
) -> list[Finding]:
    """Occurrences with no run starting within ``tolerance`` seconds."""
    slack = timedelta(seconds=tolerance)
    available = sorted(runs, key=lambda run: run.start)
    claimed: set[int] = set()
    findings: list[Finding] = []
    for expected in sorted(occurrences):
        best: int | None = None
        best_delta: timedelta | None = None
        for index, run in enumerate(available):
            if index in claimed:
                continue
            delta = abs(run.start - expected)
            if delta > slack:
                continue
            if best_delta is None or delta < best_delta:
                best, best_delta = index, delta
        if best is None:
            findings.append(
                Finding(
                    kind=MISSED,
                    severity=ERROR,
                    job_id=job.id,
                    source=job.source,
                    message=(
                        f"scheduled for {expected.isoformat(sep=' ')} but no run started "
                        f"within {int(tolerance)}s"
                    ),
                    when=expected,
                    details={"expected": expected.isoformat(), "tolerance_seconds": tolerance},
                )
            )
        else:
            claimed.add(best)
    return findings


def detect_overlaps(job: Job, runs: list[Run]) -> list[Finding]:
    """Every pair of runs of this job that were alive at the same time.

    Comparing each run only with its neighbour in start order hides the case this
    detector exists for: one run that hangs for hours is still running when the
    next three or four start, and each of those is a separate collision - another
    process on the same lock, the same table, the same output file.  Counting
    that as a single overlap reports a stuck job as no worse than two runs that
    brushed past each other.  The sweep below keeps the runs still open at each
    start, so a hung run is reported against every run it covers.

    A run whose end is unknown (plain cron with no ``pam_unix`` session lines)
    cannot be shown to have covered anything, so it opens no pair of its own; it
    can still be covered by a run whose end we do know.
    """
    findings: list[Finding] = []
    ordered = sorted(runs, key=lambda run: (run.start, run.end or run.start))
    # (run, end) for the runs not yet finished at this point, in start order.
    still_open: list[tuple[Run, datetime]] = []
    for current in ordered:
        still_open = [item for item in still_open if item[1] > current.start]
        findings.extend(
            _overlap_finding(job, previous, previous_end, current)
            for previous, previous_end in still_open
        )
        if current.end is not None:
            still_open.append((current, current.end))
    return findings


def _overlap_finding(
    job: Job, previous: Run, previous_end: datetime, current: Run
) -> Finding:
    # The time the two actually ran side by side.  Measuring to ``previous_end``
    # alone would charge a five-second run with the whole remaining hour of the
    # hung run covering it, which now happens often enough to matter.
    until = previous_end if current.end is None else min(previous_end, current.end)
    overlap = (until - current.start).total_seconds()
    return Finding(
        kind=OVERLAP,
        severity=WARNING,
        job_id=job.id,
        source=job.source,
        message=(
            f"run started {current.start.isoformat(sep=' ')} while the run from "
            f"{previous.start.isoformat(sep=' ')} was still going "
            f"(overlap {overlap:.0f}s)"
        ),
        when=current.start,
        details={
            "previous_start": previous.start.isoformat(),
            "previous_end": previous_end.isoformat(),
            "started": current.start.isoformat(),
            "overlap_seconds": overlap,
        },
    )


def detect_failures(
    job: Job, runs: list[Run], state: UnitState | None
) -> list[Finding]:
    """Real failures for a systemd unit: non-zero exits and a failed unit state.

    Plain cron cannot reach here: syslog does not carry job exit codes, which is a
    documented limitation of the data source rather than of this tool.
    """
    if job.source != SYSTEMD:
        return []
    findings: list[Finding] = []
    failed_at: set[datetime] = set()
    for run in runs:
        if not run_failed(run):
            continue
        when = run.end or run.start
        failed_at.add(when)
        detail = _describe_failure(run)
        findings.append(
            Finding(
                kind=FAILURE,
                severity=ERROR,
                job_id=job.id,
                source=job.source,
                message=(
                    f"run started {run.start.isoformat(sep=' ')} failed ({detail}); "
                    f"seen in the journal for {job.unit}"
                ),
                when=when,
                details={
                    "unit": job.unit,
                    "started": run.start.isoformat(),
                    "ended": run.end.isoformat() if run.end else None,
                    "exit_code": run.exit_code,
                    "result": run.result,
                    "evidence": "journalctl",
                },
            )
        )

    if state is not None and state.is_failed:
        exited_at = parse_timestamp(state.get("ExecMainExitTimestamp"))
        already = exited_at is not None and any(
            abs((exited_at - seen).total_seconds()) <= EVENT_MERGE_WINDOW.total_seconds()
            for seen in failed_at
        )
        if not already:
            findings.append(
                Finding(
                    kind=FAILURE,
                    severity=ERROR,
                    job_id=job.id,
                    source=job.source,
                    message=(
                        f"unit {state.unit} is "
                        f"ActiveState={state.active_state or 'unknown'} "
                        f"Result={state.result or 'unknown'} "
                        f"ExecMainStatus={_or_unknown(state.exit_status)}"
                    ),
                    when=exited_at,
                    details={
                        "unit": state.unit,
                        "active_state": state.active_state,
                        "sub_state": state.get("SubState"),
                        "result": state.result,
                        "exit_code": state.exit_status,
                        "exec_main_exit": state.get("ExecMainExitTimestamp") or None,
                        "n_restarts": state.get("NRestarts") or None,
                        "evidence": "systemctl show",
                    },
                )
            )
    return findings


def _or_unknown(value: object) -> str:
    return "unknown" if value is None else str(value)


def _describe_failure(run: Run) -> str:
    bits = []
    if run.exit_code is not None and run.exit_code != 0:
        bits.append(f"exit code {run.exit_code}")
    if run.result and run.result != "success":
        bits.append(f"result {run.result}")
    return ", ".join(bits) or "unknown reason"
