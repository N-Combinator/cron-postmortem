"""Ties the parsers and the detectors together into one scan."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__, calendarspec, cronspec, systemd
from . import crontab as crontab_mod
from .detect import build_unit_runs, detect_failures, detect_missed, detect_overlaps
from .logs import LogScan, scan_lines
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
        paths.extend(crontab_mod.discover_crontab_files())
    for path in paths:
        found, problems = crontab_mod.load_crontab_file(
            path, options.crontab_format, options.crontab_user
        )
        jobs.extend(found)
        sources["crontabs"].append(str(path))
        diagnostics.extend(Diagnostic(None, problem, str(path)) for problem in problems)

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
        text, problems = systemd.live_journal(
            ["-t", "CRON", "-t", "cron", "-t", "crond"], since, until
        )
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
    """Pick the analysis window, never claiming coverage the log does not have."""
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
                f"requested window ends {end.isoformat(sep=' ')} but the log only "
                f"covers up to {covered_end.isoformat(sep=' ')}; window clamped",
            )
        )
        end = covered_end
    if end < start:
        end = start
    return start, end


def _match_cron_runs(
    jobs: list[Job], scan_data: LogScan, diagnostics: list[Diagnostic]
) -> dict[str, list[Run]]:
    """Attach observed ``CMD`` lines to the crontab entry that produced them."""
    by_key: dict[tuple[str, str], list[Job]] = {}
    for job in jobs:
        if job.source != CRON or job.command is None:
            continue
        by_key.setdefault((job.user or "", job.command), []).append(job)

    runs: dict[str, list[Run]] = {}
    unmatched: list[str] = []
    for observed in scan_data.cron_runs:
        key = (observed.user, crontab_mod.normalize_command(observed.command))
        candidates = by_key.get(key)
        if not candidates:
            unmatched.append(f"({observed.user}) {observed.command}")
            continue
        # Duplicate crontab entries share a key; the first one owns the run.
        job = candidates[0]
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
        findings.extend(detect_failures(job, runs, state))
    return JobReport(
        job=job,
        schedule_ok=schedule_ok,
        expected=len(occurrences),
        runs=runs,
        findings=findings,
    )
