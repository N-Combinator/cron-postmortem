"""Ties the parsers and the detectors together into one scan."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__, calendarspec, cronspec, systemd
from . import crontab as crontab_mod
from .detect import build_unit_runs, detect_failures, detect_missed, detect_overlaps
from .logs import LogScan, journal_cron_identifiers, scan_lines
from .model import CRON, FAILURE, MISSED, OVERLAP, SYSTEMD, Diagnostic, Finding, Job, Run

DEFAULT_WINDOW = timedelta(hours=24)
DEFAULT_TOLERANCE = 120.0
UNMATCHED_EXAMPLES = 5


@dataclass
class ScanOptions:
    crontab_paths: list[Path] = field(default_factory=list)
    show_paths: list[Path] = field(default_factory=list)
    log_paths: list[Path] = field(default_factory=list)
    crontab_format: str = "auto"
    crontab_user: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    tolerance: float = DEFAULT_TOLERANCE
    discover: bool = False
    use_journal: bool = False
    ignore: frozenset[str] = frozenset()
    now: datetime = field(default_factory=datetime.now)


@dataclass
class JobReport:
    job: Job
    schedule_ok: bool
    expected: int
    runs: list[Run]
    findings: list[Finding]


@dataclass
class ScanResult:
    generated_at: datetime
    window_start: datetime
    window_end: datetime
    tolerance: float
    job_reports: list[JobReport]
    findings: list[Finding]
    diagnostics: list[Diagnostic]
    sources: dict[str, list[str]]

    @property
    def problems(self) -> int:
        return len(self.findings)

    def counts(self) -> dict[str, int]:
        counts = {MISSED: 0, OVERLAP: 0, FAILURE: 0}
        for finding in self.findings:
            counts[finding.kind] = counts.get(finding.kind, 0) + 1
        return counts

    def as_dict(self) -> dict:
        counts = self.counts()
        return {
            "tool": "cron-postmortem",
            "version": __version__,
            "generated_at": self.generated_at.isoformat(),
            "window": {
                "start": self.window_start.isoformat(),
                "end": self.window_end.isoformat(),
                "tolerance_seconds": self.tolerance,
            },
            "summary": {
                "jobs": len(self.job_reports),
                "runs": sum(len(report.runs) for report in self.job_reports),
                "missed": counts[MISSED],
                "overlap": counts[OVERLAP],
                "failure": counts[FAILURE],
                "problems": self.problems,
                "diagnostics": len(self.diagnostics),
            },
            "sources": self.sources,
            "jobs": [
                {
                    **report.job.as_dict(),
                    "schedule_understood": report.schedule_ok,
                    "expected_runs": report.expected,
                    "observed_runs": [run.as_dict() for run in report.runs],
                    "findings": [finding.as_dict() for finding in report.findings],
                }
                for report in self.job_reports
            ],
            "findings": [finding.as_dict() for finding in self.findings],
            "diagnostics": [diag.as_dict() for diag in self.diagnostics],
        }


def scan(options: ScanOptions) -> ScanResult:
    diagnostics: list[Diagnostic] = []
    sources: dict[str, list[str]] = {"crontabs": [], "systemctl_show": [], "logs": []}

    jobs, unit_states = _collect_jobs(options, diagnostics, sources)
    states_by_id = {state.unit: state for state in unit_states}
    descriptions = systemd.description_map(unit_states)

    log_text, log_origins = _collect_logs(options, jobs, diagnostics)
    sources["logs"] = log_origins
    scan_data = scan_lines(
        log_text.splitlines(),
        origin=", ".join(log_origins) or "journal",
        reference=options.now,
        description_to_unit=descriptions,
    )
    for message in sorted(scan_data.unresolved_systemd_messages):
        diagnostics.append(
            Diagnostic(
                job_id=None,
                message=(
                    f"could not map journal message {message!r} to a unit; pass "
                    "--systemctl-show so unit descriptions can be resolved"
                ),
            )
        )

    window_start, window_end = _resolve_window(options, scan_data, diagnostics)

    cron_runs_by_job = _match_cron_runs(jobs, scan_data, diagnostics)
    events_by_unit: dict[str, list] = {}
    for event in scan_data.unit_events:
        events_by_unit.setdefault(event.unit, []).append(event)

    job_reports: list[JobReport] = []
    findings: list[Finding] = []
    for job in jobs:
        report = _analyse_job(
            job=job,
            options=options,
            window_start=window_start,
            window_end=window_end,
            cron_runs=cron_runs_by_job.get(job.id, []),
            unit_events=events_by_unit.get(job.unit or "", []),
            state=states_by_id.get(job.unit or ""),
            timer_state=states_by_id.get(job.timer or ""),
            diagnostics=diagnostics,
        )
        job_reports.append(report)
        findings.extend(report.findings)

    findings.sort(key=lambda item: (item.when or window_start, item.kind, item.job_id))
    return ScanResult(
        generated_at=options.now,
        window_start=window_start,
        window_end=window_end,
        tolerance=options.tolerance,
        job_reports=job_reports,
        findings=findings,
        diagnostics=diagnostics,
        sources=sources,
    )


def _collect_jobs(
    options: ScanOptions, diagnostics: list[Diagnostic], sources: dict[str, list[str]]
) -> tuple[list[Job], list[systemd.UnitState]]:
    jobs: list[Job] = []
    paths = list(options.crontab_paths)
    if options.discover:
        discovered, problems = crontab_mod.discover_crontab_files()
        paths.extend(discovered)
        diagnostics.extend(Diagnostic(None, problem) for problem in problems)
    for path in paths:
        found, problems = crontab_mod.load_crontab_file(
            path, options.crontab_format, options.crontab_user
        )
        jobs.extend(found)
        sources["crontabs"].append(str(path))
        diagnostics.extend(Diagnostic(None, problem, str(path)) for problem in problems)
    jobs = _merge_cron_duplicates(jobs)

    unit_states: list[systemd.UnitState] = []
    for path in options.show_paths:
        states, problems = systemd.load_show_file(path)
        unit_states.extend(states)
        sources["systemctl_show"].append(str(path))
        diagnostics.extend(Diagnostic(None, problem, str(path)) for problem in problems)
    if options.discover:
        names, problems = systemd.live_timer_units()
        diagnostics.extend(Diagnostic(None, problem) for problem in problems)
        if names:
            services = [name[: -len(".timer")] + ".service" for name in names]
            states, problems = systemd.live_show(sorted(set(names) | set(services)))
            unit_states.extend(states)
            sources["systemctl_show"].append("systemctl show (live)")
            diagnostics.extend(Diagnostic(None, problem) for problem in problems)

    timer_jobs, problems = systemd.timer_jobs(unit_states, "systemctl show")
    jobs.extend(timer_jobs)
    diagnostics.extend(Diagnostic(None, problem) for problem in problems)
    return jobs, unit_states


def _merge_cron_duplicates(jobs: list[Job]) -> list[Job]:
    """Fold crontab entries running the same command as the same user into one job.

    A ``CMD`` log line carries only the user and the command, so two crontab lines
    that schedule the same command are indistinguishable in the log and cannot be
    told apart after the fact.  They become one job whose occurrences are the union
    of both schedules — the same treatment a timer with several ``OnCalendar=``
    lines gets.  Keeping them separate would hand every observed run to the first
    entry and report all of the second entry's occurrences as missed.
    """
    merged: list[Job] = []
    position_of: dict[tuple[str, str], int] = {}
    for job in jobs:
        if job.source != CRON or job.command is None:
            merged.append(job)
            continue
        key = (job.user or "", job.command)
        position = position_of.get(key)
        if position is None:
            position_of[key] = len(merged)
            merged.append(job)
            continue
        first = merged[position]
        schedules = first.schedule_list + tuple(
            expression
            for expression in job.schedule_list
            if expression not in first.schedule_list
        )
        merged[position] = replace(
            first,
            schedule=" ; ".join(schedules),
            schedules=schedules,
            origin=f"{first.origin}, {job.origin}",
        )
    return merged


def _collect_logs(
    options: ScanOptions, jobs: list[Job], diagnostics: list[Diagnostic]
) -> tuple[str, list[str]]:
    chunks: list[str] = []
    origins: list[str] = []
    for path in options.log_paths:
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            diagnostics.append(
                Diagnostic(None, f"cannot read log file ({exc.strerror or exc})", str(path))
            )
            continue
        origins.append(str(path))
    if options.use_journal:
        since = options.since or options.now - DEFAULT_WINDOW
        until = options.until or options.now
        matchers = [
            argument
            for identifier in journal_cron_identifiers()
            for argument in ("-t", identifier)
        ]
        text, problems = systemd.live_journal(matchers, since, until)
        chunks.append(text)
        diagnostics.extend(Diagnostic(None, problem) for problem in problems)
        units = sorted({job.unit for job in jobs if job.source == SYSTEMD and job.unit})
        if units:
            matchers: list[str] = []
            for unit in units:
                matchers.extend(["-u", unit])
            text, problems = systemd.live_journal(matchers, since, until)
            chunks.append(text)
            diagnostics.extend(Diagnostic(None, problem) for problem in problems)
        origins.append("journalctl")
    return "\n".join(chunks), origins


def _resolve_window(
    options: ScanOptions, scan_data: LogScan, diagnostics: list[Diagnostic]
) -> tuple[datetime, datetime]:
    """Pick the analysis window.

    The *start* is clamped to the log's span: runs from before the log begins are
    unknowable (rotation, truncation) and reporting them all as missed is noise.
    The *end* is never clamped.  Silence at the end of the window is the outage
    this tool exists to catch — a dead cron daemon, a box that went down, broken
    logging — so those occurrences must be reported as missed and move the exit
    code, not be defined away.  Without ``--until`` and without the journal the
    end still *defaults* to the last log line, which keeps an offline scan of a
    stand-alone log file reproducible.
    """
    if options.since is not None:
        start = options.since
    elif scan_data.first_timestamp is not None:
        start = scan_data.first_timestamp
    else:
        start = options.now - DEFAULT_WINDOW
    if options.until is not None:
        end = options.until
    elif scan_data.last_timestamp is not None and not options.use_journal:
        end = scan_data.last_timestamp
    else:
        end = options.now

    covered_start, covered_end = scan_data.first_timestamp, scan_data.last_timestamp
    if covered_start is not None and start < covered_start:
        diagnostics.append(
            Diagnostic(
                None,
                f"requested window starts {start.isoformat(sep=' ')} but the log only "
                f"covers from {covered_start.isoformat(sep=' ')}; window clamped",
            )
        )
        start = covered_start
    if covered_end is not None and end > covered_end:
        diagnostics.append(
            Diagnostic(
                None,
                f"the log's last entry is {covered_end.isoformat(sep=' ')} but the "
                f"window runs to {end.isoformat(sep=' ')}; occurrences after the last "
                "entry are reported as missed — a silent tail is usually one outage, "
                "not one failure per occurrence",
            )
        )
    if end < start:
        end = start
    return start, end


def _match_cron_runs(
    jobs: list[Job], scan_data: LogScan, diagnostics: list[Diagnostic]
) -> dict[str, list[Run]]:
    """Attach observed ``CMD`` lines to the crontab entry that produced them."""
    by_key: dict[tuple[str, str], Job] = {}
    for job in jobs:
        if job.source != CRON or job.command is None:
            continue
        # Entries sharing a key were already folded into one job by
        # _merge_cron_duplicates; setdefault only guards against a stray caller.
        by_key.setdefault((job.user or "", job.command), job)

    runs: dict[str, list[Run]] = {}
    unmatched: list[str] = []
    for observed in scan_data.cron_runs:
        key = (observed.user, crontab_mod.normalize_command(observed.command))
        job = by_key.get(key)
        if job is None:
            unmatched.append(f"({observed.user}) {observed.command}")
            continue
        runs.setdefault(job.id, []).append(
            Run(
                job_id=job.id,
                start=observed.start,
                end=observed.end,
                pid=observed.pid,
            )
        )
    if unmatched:
        unique = sorted(set(unmatched))
        examples = ", ".join(unique[:UNMATCHED_EXAMPLES])
        diagnostics.append(
            Diagnostic(
                None,
                f"{len(unmatched)} cron run(s) in the log matched no known crontab "
                f"entry ({len(unique)} distinct): {examples}"
                + (" ..." if len(unique) > UNMATCHED_EXAMPLES else ""),
            )
        )
    return runs


def _runs_in_window(
    runs: list[Run], window_start: datetime, window_end: datetime, tolerance: float
) -> list[Run]:
    """Keep the runs the window is about, for every detector alike.

    ``--since``/``--until`` used to bound only the expected occurrences, so a
    narrow window still reported overlaps and failures from the whole log file —
    two different windows in one report.  The lower edge is widened by the
    tolerance because a run that started just before ``--since`` is exactly the
    run that answers the first occurrence inside it; missed detection therefore
    sees the same runs it did before.
    """
    earliest = window_start - timedelta(seconds=tolerance)
    return [run for run in runs if earliest <= run.start <= window_end]


def _state_in_window(
    job: Job,
    state: systemd.UnitState | None,
    window_start: datetime,
    window_end: datetime,
    diagnostics: list[Diagnostic],
) -> systemd.UnitState | None:
    """Drop a ``systemctl show`` failure that happened outside the window.

    ``systemctl show`` reports the unit's state *now*, which may be the fallout
    of a run from last month.  Counting that as a finding inside a ``--since 1h``
    scan puts a problem in the report that the window says nothing about, so it
    is demoted to a diagnostic (D-c33b95: only findings move the exit code).
    Without a usable ``ExecMainExitTimestamp`` the failure cannot be dated and is
    kept, since a currently-failed unit is the more useful report.
    """
    if state is None or not state.is_failed:
        return state
    exited_at = systemd.parse_timestamp(state.get("ExecMainExitTimestamp"))
    if exited_at is None or window_start <= exited_at <= window_end:
        return state
    diagnostics.append(
        Diagnostic(
            job.id,
            f"unit {state.unit} is Result={state.result or 'unknown'} from "
            f"{exited_at.isoformat(sep=' ')}, which is outside the analysed window; "
            "widen --since/--until to include it",
            job.origin,
        )
    )
    return None


def _analyse_job(
    job: Job,
    options: ScanOptions,
    window_start: datetime,
    window_end: datetime,
    cron_runs: list[Run],
    unit_events: list,
    state: systemd.UnitState | None,
    timer_state: systemd.UnitState | None,
    diagnostics: list[Diagnostic],
) -> JobReport:
    tolerance = options.tolerance
    if job.source == SYSTEMD and timer_state is not None:
        tolerance += systemd.timer_tolerance(timer_state)

    if job.source == CRON:
        runs = sorted(cron_runs, key=lambda run: run.start)
    else:
        runs = build_unit_runs(job.id, unit_events)
    runs = _runs_in_window(runs, window_start, window_end, tolerance)

    # An occurrence is only judged once its tolerance has fully elapsed inside the
    # window; otherwise the very last scheduled run is always "missed".
    deadline = window_end - timedelta(seconds=tolerance)
    parse = cronspec.parse if job.source == CRON else calendarspec.parse
    moments: set[datetime] = set()
    schedule_ok = True
    for expression in job.schedule_list:
        try:
            moments.update(parse(expression).occurrences(window_start, deadline))
        except (cronspec.CronParseError, calendarspec.CalendarParseError) as exc:
            schedule_ok = False
            diagnostics.append(
                Diagnostic(
                    job.id, f"schedule {expression!r} not analysable: {exc}", job.origin
                )
            )
    occurrences = sorted(moments)

    findings: list[Finding] = []
    if MISSED not in options.ignore:
        # An unparsable expression contributes no occurrences, so a partially
        # understood schedule is under-checked rather than falsely flagged.
        findings.extend(detect_missed(job, occurrences, runs, tolerance))
    if OVERLAP not in options.ignore:
        findings.extend(detect_overlaps(job, runs))
        if job.source == CRON and len(runs) > 1 and all(run.end is None for run in runs):
            diagnostics.append(
                Diagnostic(
                    job.id,
                    "no pam_unix(cron:session) lines for this job, so run durations are "
                    "unknown and overlaps cannot be detected",
                    job.origin,
                )
            )
    if FAILURE not in options.ignore:
        findings.extend(
            detect_failures(job, runs, _state_in_window(
                job, state, window_start, window_end, diagnostics
            ))
        )
    return JobReport(
        job=job,
        schedule_ok=schedule_ok,
        expected=len(occurrences),
        runs=runs,
        findings=findings,
    )
