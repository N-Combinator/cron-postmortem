"""Ties the parsers and the detectors together into one scan."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__, calendarspec, cronspec, systemd
from . import crontab as crontab_mod
from .detect import build_unit_runs, detect_failures, detect_missed, detect_overlaps
from .logs import (
    ImplausibleDates,
    LogScan,
    journal_cron_identifiers,
    scan_sources,
)
from .model import (
    CRON,
    FAILURE,
    MISSED,
    NO_RUNS_MATCHED,
    OVERLAP,
    SYSTEMD,
    Diagnostic,
    Finding,
    Job,
    Run,
    ScanWarning,
)

DEFAULT_WINDOW = timedelta(hours=24)
DEFAULT_TOLERANCE = 120.0
UNMATCHED_EXAMPLES = 5

# A crontab this tool had to guess the format of, whose entries then matched
# nothing in a log that does carry cron runs; see _guessed_format_warnings.
CRONTAB_FORMAT_GUESSED = "crontab-format-guessed"

# What a scan that reconciled nothing has found, said the same way whether the
# missed runs it stands to invent are in the report or not.
NOTHING_MATCHED = (
    "not one cron run in the log could be attributed to a crontab entry"
)


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
    warnings: list[ScanWarning] = field(default_factory=list)
    lines_total: int = 0
    lines_parsed: int = 0

    @property
    def problems(self) -> int:
        return len(self.findings)

    @property
    def usage_error(self) -> bool:
        """Whether the scan was asked for something it could never answer.

        Separate from :attr:`alerts` because the answer is not "your jobs are
        unhealthy" but "these arguments contradict each other"; the CLI turns it
        into exit code 2 and ``--exit-zero`` does not silence it.
        """
        return any(warning.usage_error for warning in self.warnings)

    @property
    def no_runs_matched(self) -> bool:
        """Whether the schedules and the log could not be reconciled at all.

        The missed runs in such a report are an artefact of the comparison, not
        an outage, so the CLI gives it an exit code of its own (3) and does not
        let ``--exit-zero`` mute it.  Only the entries that matched nothing are
        disowned: see :attr:`standing_findings` for what still exits 1, and
        :func:`_comparison_warnings` for why a scan with no such runs in it -
        a window nothing was due in - is not warned about at all.
        """
        return any(warning.code == NO_RUNS_MATCHED for warning in self.warnings)

    @property
    def standing_findings(self) -> list[Finding]:
        """The findings :attr:`no_runs_matched` does not disown.

        That warning says one thing only: these cron entries were compared
        against the log on a key the two sides do not share, so their missed
        runs are an artefact.  It says nothing about a timer that failed or a
        cron job whose runs *did* match - those problems are real, and a scan
        that reports them has found an outage whatever else went wrong with it.
        The exit code has to reflect that, or a systemd failure sitting next to
        a misread crontab comes back as "scan problem, do not page".
        """
        artefacts = {
            report.job.id
            for report in self.job_reports
            if report.job.source == CRON and not report.runs
        }
        return [finding for finding in self.findings if finding.job_id not in artefacts]

    @property
    def alerts(self) -> bool:
        """Whether this scan should exit non-zero.

        Warnings count: a scan that understood no schedules, or no log lines,
        found no problems only because it looked at nothing, and a monitoring
        check that reads that as success is worse than useless.
        """
        return bool(self.findings or self.warnings)

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
                "warnings": len(self.warnings),
                "diagnostics": len(self.diagnostics),
                "log_lines_total": self.lines_total,
                "log_lines_parsed": self.lines_parsed,
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
            "warnings": [warning.as_dict() for warning in self.warnings],
            "diagnostics": [diag.as_dict() for diag in self.diagnostics],
        }


def scan(options: ScanOptions) -> ScanResult:
    diagnostics: list[Diagnostic] = []
    warnings: list[ScanWarning] = []
    sources: dict[str, list[str]] = {"crontabs": [], "systemctl_show": [], "logs": []}

    guessed_formats: dict[str, crontab_mod.CrontabRead] = {}
    jobs, unit_states = _collect_jobs(options, diagnostics, sources, guessed_formats)
    if not jobs:
        warnings.append(_no_schedules_warning(options, sources))
    states_by_id = {state.unit: state for state in unit_states}
    descriptions = systemd.description_map(unit_states)

    log_sources, log_origins = _collect_logs(options, jobs, diagnostics)
    sources["logs"] = log_origins
    scan_data = scan_sources(
        log_sources,
        reference=options.now,
        description_to_unit=descriptions,
    )
    if scan_data.lines_parsed == 0:
        warnings.append(_no_log_lines_warning(options, scan_data, log_origins))
    for suspect in scan_data.implausible_dates:
        warnings.append(_implausible_dates_warning(suspect))
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
    empty_window = _empty_window_warning(options, window_start, window_end)
    if empty_window is not None:
        warnings.append(empty_window)

    cron_runs_by_job, nothing_matched = _match_cron_runs(
        jobs, scan_data, diagnostics, options.crontab_user
    )
    # Both comparison warnings are claims about missed runs the comparison
    # invented, so neither can be decided before the report says whether there
    # are any; they are settled after the job loop and spliced in here, where a
    # statement about the whole scan belongs - ahead of the per-job warnings.
    comparison_warnings_at = len(warnings)

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
            warnings=warnings,
            report_empty_window=empty_window is None,
        )
        job_reports.append(report)
        findings.extend(report.findings)

    warnings[comparison_warnings_at:comparison_warnings_at] = _comparison_warnings(
        job_reports=job_reports,
        nothing_matched=nothing_matched,
        guessed_formats=guessed_formats,
        has_cron_runs=bool(scan_data.cron_runs),
        diagnostics=diagnostics,
    )

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
        warnings=warnings,
        lines_total=scan_data.lines_total,
        lines_parsed=scan_data.lines_parsed,
    )


def _no_schedules_warning(
    options: ScanOptions, sources: dict[str, list[str]]
) -> ScanWarning:
    """A scan with nothing to check must not pass for a healthy one.

    Every detector is driven by the schedules, so with none of them a report of
    "no problems" says only that nothing was looked at - a typo in ``--crontab``,
    a crontab directory the scan could not read, or a host with no timers at all
    all land here.
    """
    looked_at = [*sources["crontabs"], *sources["systemctl_show"]]
    where = ", ".join(looked_at) if looked_at else "no schedule source"
    if options.discover and not options.crontab_paths and not options.show_paths:
        hint = (
            "this host has no readable crontabs or systemd timers; run as root "
            "if the spool directory is unreadable, or pass --crontab/--systemctl-show"
        )
    else:
        hint = "check the paths passed to --crontab/--systemctl-show"
    return ScanWarning(
        "no-schedules", f"no schedules to check (looked at: {where}); {hint}"
    )


def _no_log_lines_warning(
    options: ScanOptions, scan_data: LogScan, log_origins: list[str]
) -> ScanWarning:
    """Not one log line was understood, so nothing could be observed.

    Without runs every occurrence is missed and every job looks dead, or - with
    no schedules of its own to compare against - the report comes out clean.
    Either way the answer is about the log source, not about the jobs.
    """
    where = ", ".join(log_origins)
    if not log_origins:
        return ScanWarning(
            "no-log-lines",
            "no log source: pass --log-file or --journal, otherwise no run can "
            "be observed and every scheduled run counts as missed",
        )
    if scan_data.lines_total == 0:
        detail = f"{where} produced no output"
        if options.use_journal:
            detail += " for this window"
    else:
        detail = (
            f"none of the {scan_data.lines_total} line(s) from {where} were "
            "recognised as cron or systemd log lines"
        )
    return ScanWarning(
        "no-log-lines",
        f"no log lines parsed: {detail}; check the log format and that cron logs "
        "under one of " + ", ".join(journal_cron_identifiers()),
    )


def _implausible_dates_warning(suspect: ImplausibleDates) -> ScanWarning:
    """The dates a year-less log was given are too wide to believe.

    Traditional syslog carries no year, so one is inferred from the order of the
    lines.  When that inference goes wrong the log does not complain - it simply
    comes out dated a year apart, the window is clamped to the earliest dated
    line, and every occurrence in the invented months is reported missed.  A
    span of most of a year covered by fewer lines than it has days is the cheap
    tell, so it is said out loud rather than enumerated in silence.
    """
    return ScanWarning(
        "implausible-log-dates",
        f"{suspect.origin}: {suspect.lines} year-less syslog line(s) were dated "
        f"across {suspect.span_days} days "
        f"({suspect.first.isoformat(sep=' ')} - {suspect.last.isoformat(sep=' ')}); "
        "syslog carries no year, so one is inferred from the order of the lines "
        "and a span that wide over so few lines usually means the order is not "
        "chronological (an aggregated log, a stepped clock, or files glued "
        "together) - the window starts at the earliest dated line, so any "
        "missed runs before the log really begins are not real; pass "
        "--since/--until to bound the window, or give each file as its own "
        "--log-file, oldest first",
    )


def _empty_window_warning(
    options: ScanOptions, window_start: datetime, window_end: datetime
) -> ScanWarning | None:
    """Refuse a window that the tolerance eats whole.

    An occurrence is only judged once its tolerance has fully elapsed, so the
    last moment a scan can rule on is ``window_end - tolerance``.  When the
    tolerance is longer than the window itself that deadline falls before the
    window even opens: every job gets zero expected runs, every detector has
    nothing to say and the report reads "No problems found" for a scan that
    checked nothing at all.  ``--since 10m --tolerance 3600`` and a ``--until``
    older than ``--since`` both land here, and so does a log so short that the
    start clamp leaves less than the tolerance.

    That is the caller's arguments contradicting each other rather than a
    finding about a job, so it is a usage error: exit 2, not a clean 0.
    """
    span = (window_end - window_start).total_seconds()
    if span >= options.tolerance:
        return None
    return ScanWarning(
        "empty-window",
        f"the window {window_start.isoformat(sep=' ')} - "
        f"{window_end.isoformat(sep=' ')} is {_seconds(span)} long but the "
        f"tolerance is {_seconds(options.tolerance)}, so no scheduled run could "
        "be judged and nothing was checked; widen --since/--until (the window "
        "may also have been clamped to the log's first entry) or lower "
        "--tolerance",
        usage_error=True,
    )


def _seconds(value: float) -> str:
    return f"{int(value)}s"


def _collect_jobs(
    options: ScanOptions,
    diagnostics: list[Diagnostic],
    sources: dict[str, list[str]],
    guessed_formats: dict[str, crontab_mod.CrontabRead],
) -> tuple[list[Job], list[systemd.UnitState]]:
    jobs: list[Job] = []
    # A path the caller names is a capture: neither its format nor its owner can
    # be read off the filename, so the content decides and --crontab-user says
    # who the entries belong to.  A path discovery found is a file cron itself
    # reads, and cron's rule for the location is better information than any
    # vote over the entries - /etc/cron.d/webjobs holding one
    # "*/1 * * * * deploy /opt/app/tick" is a system crontab whatever that line
    # looks like on its own.  The location names the owner too (the spool
    # filename, or the entries' own user column), so --crontab-user, which the
    # caller can only have meant for the file they named, is not applied there.
    to_read: list[tuple[crontab_mod.DiscoveredCrontab, str | None]] = [
        (
            crontab_mod.DiscoveredCrontab(path, options.crontab_format, False),
            options.crontab_user,
        )
        for path in options.crontab_paths
    ]
    if options.discover:
        discovered, problems = crontab_mod.discover_crontab_files()
        to_read.extend(
            (
                found if options.crontab_format == "auto"
                else replace(found, format=options.crontab_format),
                None,
            )
            for found in discovered
        )
        diagnostics.extend(Diagnostic(None, problem) for problem in problems)
    for source, user_override in to_read:
        read = crontab_mod.load_crontab_file(
            source.path,
            source.format,
            user_override,
            trust_filename=source.filename_is_owner,
        )
        jobs.extend(read.jobs)
        sources["crontabs"].append(str(source.path))
        diagnostics.extend(
            Diagnostic(None, problem, str(source.path)) for problem in read.problems
        )
        if read.detection is not None and read.detection.guessed:
            guessed_formats[str(source.path)] = read
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
) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """Gather the log text, keeping every source separate.

    The sources are handed to :func:`~cron_postmortem.logs.scan_sources` one by
    one rather than concatenated: each has its own chronological order, and that
    order is the only thing year-less syslog timestamps can be dated from.  Two
    journalctl queries count as two sources for the same reason - the second one
    restarts at the beginning of the window.
    """
    chunks: list[tuple[str, list[str]]] = []
    origins: list[str] = []
    for path in options.log_paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            diagnostics.append(
                Diagnostic(None, f"cannot read log file ({exc.strerror or exc})", str(path))
            )
            continue
        chunks.append((str(path), text.splitlines()))
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
        chunks.append(("journalctl (cron)", text.splitlines()))
        diagnostics.extend(Diagnostic(None, problem) for problem in problems)
        units = sorted({job.unit for job in jobs if job.source == SYSTEMD and job.unit})
        if units:
            matchers: list[str] = []
            for unit in units:
                matchers.extend(["-u", unit])
            text, problems = systemd.live_journal(matchers, since, until)
            chunks.append(("journalctl (units)", text.splitlines()))
            diagnostics.extend(Diagnostic(None, problem) for problem in problems)
        origins.append("journalctl")
    return chunks, origins


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
    jobs: list[Job],
    scan_data: LogScan,
    diagnostics: list[Diagnostic],
    crontab_user: str | None = None,
) -> tuple[dict[str, list[Run]], str | None]:
    """Attach observed ``CMD`` lines to the crontab entry that produced them.

    Returns the runs per job and, where not one pair could be made, the message
    describing why.  Whether that message is a warning or a diagnostic depends
    on what the comparison went on to invent, which is not known here: see
    :func:`_comparison_warnings`.
    """
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
    summary = ""
    if unmatched:
        unique = sorted(set(unmatched))
        examples = ", ".join(unique[:UNMATCHED_EXAMPLES]) + (
            " ..." if len(unique) > UNMATCHED_EXAMPLES else ""
        )
        summary = (
            f"{len(unmatched)} cron run(s) in the log matched no known crontab "
            f"entry ({len(unique)} distinct): {examples}"
        )
    # Entries on one side, an understood log on the other, and not one pair
    # between them.  The log needing no cron run of its own for this is the
    # point: a log whose cron lines were never collected (a journal filtered by
    # unit, or asked for an identifier cron does not use on this host) reports
    # every occurrence of every entry missed just the same, and that page is no
    # more a verdict on the jobs than the one an unmatched user column invents.
    # A log nothing at all was understood from is already its own warning, so it
    # is left to say so rather than being charged twice.
    nothing_matched = None
    if by_key and not runs and scan_data.lines_parsed:
        nothing_matched = _nothing_matched_message(
            by_key, scan_data, summary, crontab_user
        )
    elif summary:
        diagnostics.append(Diagnostic(None, summary))
    return runs, nothing_matched


def _comparison_warnings(
    job_reports: list[JobReport],
    nothing_matched: str | None,
    guessed_formats: dict[str, crontab_mod.CrontabRead],
    has_cron_runs: bool,
    diagnostics: list[Diagnostic],
) -> list[ScanWarning]:
    """Decide the warnings that are about missed runs the comparison invented.

    Both ``no-runs-matched`` and ``crontab-format-guessed`` say the same kind of
    thing - *these missed runs are an artefact of how the two sides were lined
    up, not an outage* - and neither is true of a report that has no missed runs
    in it.  A scan whose window holds no occurrence of any entry (a narrow
    incident window, a monthly job looked at over an afternoon, a crontab of
    nothing but ``@reboot``) reconciles nothing and invents nothing, and telling
    its caller that every scheduled run counts as missed, over a report saying
    ``missed: 0``, is a page for a healthy host.  So the comparison is only
    disowned once it has something to disown: at least one cron entry with no
    matched run *and* a missed run reported in its place.

    What went unreconciled is still worth saying, so where there is nothing to
    disown the same sentence is kept as a diagnostic, which does not move the
    exit code.
    """
    warnings: list[ScanWarning] = []
    invented = {
        report.job.id
        for report in job_reports
        if report.job.source == CRON
        and not report.runs
        and any(finding.kind == MISSED for finding in report.findings)
    }
    if nothing_matched is not None:
        if invented:
            warnings.append(
                ScanWarning(
                    NO_RUNS_MATCHED,
                    f"{NOTHING_MATCHED}, so every scheduled cron run counts as "
                    f"missed: {nothing_matched}",
                )
            )
        else:
            diagnostics.append(
                Diagnostic(
                    None,
                    f"{NOTHING_MATCHED}, but no missed run was reported for "
                    "the entries that went unreconciled, so no finding in this "
                    f"report rests on it: {nothing_matched}",
                )
            )
    elif has_cron_runs:
        # The all-or-nothing case is already the loudest thing this tool says;
        # this is the same mistake in one crontab out of several, which used to
        # leave nothing but a diagnostic under the missed runs it invented.
        warnings.extend(_guessed_format_warnings(job_reports, guessed_formats, invented))
    return warnings


def _nothing_matched_message(
    by_key: dict[tuple[str, str], Job],
    scan_data: LogScan,
    summary: str,
    crontab_user: str | None = None,
) -> str:
    """Crontab entries, cron runs in the log, and not one pair between them.

    Some entries never firing is a finding about those jobs; *every* entry
    missing while the log is full of cron runs is a statement about the scan
    itself, because the two sides are being compared on a key one of them does
    not use - most often the user a ``--crontab`` file was attributed to, which
    the log carries but the file does not.  Left as a diagnostic it does not
    move the exit code and is easy to lose under the missed runs it invents, so
    where it invents any it is a warning: the report is not a verdict on these
    jobs.  This builds the half of the sentence that holds either way; the
    opener and the verdict are :func:`_comparison_warnings`'.

    A log that was understood but carries no cron run *at all* is the same claim
    with one side missing - ``summary`` is empty there, because nothing went
    unmatched; there was nothing to match.  It is the commoner shape of the two:
    the log cron writes to was not the one collected, or the journal was asked
    for units and the cron lines were left out of it.  The report is the same
    full page of invented missed runs, so it is said the same way.
    """
    observed_users = sorted({observed.user for observed in scan_data.cron_runs})
    known_users = sorted({user for user, _ in by_key})
    if not scan_data.cron_runs:
        entries = len(by_key)
        cause = (
            f"{scan_data.lines_parsed} log line(s) were understood but not one of "
            f"them is a cron run, so the {entries} crontab "
            f"{'entry' if entries == 1 else 'entries'} had nothing to be compared "
            "against; check that the log is the one cron writes to and that it "
            "covers the window, and that a journal query asked for cron's own "
            f"identifier ({', '.join(journal_cron_identifiers())}) instead of "
            "filtering by unit"
        )
    elif set(observed_users).isdisjoint(known_users):
        if crontab_user is not None:
            # Telling somebody to pass the option they passed is how a report
            # gets closed as noise.  They named a user and the entries are not
            # under it, so the name was overruled: by a system-format reading,
            # which takes the user from the file, or by a second crontab the
            # option was never meant for.
            remedy = (
                f"--crontab-user {crontab_user!r} was given, so the entries it "
                "applies to were read as a system-format crontab (which names "
                "its own user) or come from another --crontab file; check the "
                "parse problems above and pass --crontab-format user if the "
                "file has no user column"
            )
        else:
            remedy = (
                "a user-format crontab is attributed to root unless its "
                "filename is the owner's name, so pass --crontab-user"
            )
        cause = (
            f"the log's cron runs belong to {_names(observed_users)} but the "
            f"crontab entries are attributed to {_names(known_users)}; {remedy}"
        )
    else:
        cause = (
            f"the users match ({_names(known_users)}) but none of the commands "
            "do; check that the crontab was read in the right format (a system "
            "crontab read as a user one puts the user column in front of every "
            "command - pass --crontab-format) and that the crontab and the log "
            "come from the same host and the same point in time"
        )
    return f"{cause}. {summary}" if summary else f"{cause}."


def _guessed_format_warnings(
    job_reports: list[JobReport],
    guessed_formats: dict[str, crontab_mod.CrontabRead],
    invented: set[str],
) -> list[ScanWarning]:
    """A crontab whose format was guessed, and whose entries then matched nothing.

    The format decides where every command starts, so reading it wrong does not
    lose a run here and there - it loses all of that file's runs and reports one
    missed occurrence in their place.  When the file's format was worked out
    from entries that did not settle it (:attr:`FormatDetection.guessed`) and
    not one of its entries can be found in a log that *is* carrying cron runs,
    the guess is the likelier explanation of the page of missed runs than an
    outage is, and the caller has to be told before they act on it.

    ``no-runs-matched`` already covers the case where this happened to the whole
    scan.  This one is for the crontab that is wrong next to one that is right,
    which that warning cannot see: the report exits 1 with a plausible-looking
    outage in it and, until now, an empty stderr.

    The format named is the one the entries were *read* in
    (:attr:`CrontabRead.format`), not the one the vote reached: where
    ``--crontab-user`` overruled a system reading, naming the vote would tell the
    caller to correct the file to the format it is already being read in and
    leave the one that actually produced the missed runs unsaid.

    ``invented`` is the entries a missed run *was* reported for; a file none of
    whose entries is in it has no missed runs to blame on the guess, so it is
    left alone - the warning's whole subject is those runs.
    """
    warnings: list[ScanWarning] = []
    matched = {report.job.id for report in job_reports if report.runs}
    for path, read in guessed_formats.items():
        entries = [
            report.job
            for report in job_reports
            if report.job.source == CRON and path in _origins(report.job)
        ]
        if not entries or any(job.id in matched for job in entries):
            continue
        if not any(job.id in invented for job in entries):
            continue
        if read.overruled_by_user:
            reading = (
                f"read as a {read.format}-format crontab because --crontab-user "
                f"was given, though its entries read as {read.other_format}-format "
                "on their own"
            )
        else:
            reading = (
                f"read as a {read.format}-format crontab, but its entries did not "
                "settle that between them"
            )
        warnings.append(
            ScanWarning(
                CRONTAB_FORMAT_GUESSED,
                f"{path}: {reading}, and not one of its "
                f"{len(entries)} entries matched a cron run in the log; the "
                "missed runs reported for them may be an artefact of that "
                f"reading rather than an outage - pass --crontab-format "
                f"{read.other_format} if it is wrong (see the parse problems in "
                "the report)",
            )
        )
    return warnings


def _origins(job: Job) -> set[str]:
    """The crontab files a job's entries came from.

    ``Job.origin`` is ``path:lineno``, and a job merged from two entries carries
    both, comma-separated (see :func:`_merge_cron_duplicates`).
    """
    return {part.rsplit(":", 1)[0] for part in job.origin.split(", ")}


def _names(users: list[str]) -> str:
    shown = ", ".join(users[:UNMATCHED_EXAMPLES])
    if len(users) > UNMATCHED_EXAMPLES:
        shown += " ..."
    return f"user(s) {shown}"


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
    warnings: list[ScanWarning],
    report_empty_window: bool = True,
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
    if deadline < window_start and report_empty_window:
        # The window is long enough in general but not for this timer: AccuracySec
        # and RandomizedDelaySec buy it more slack than the window has to give.
        # Nothing about it is checked, and it must not look checked.
        warnings.append(
            ScanWarning(
                "empty-window",
                f"{job.id}: the timer's slack of {_seconds(tolerance)} "
                f"(--tolerance plus AccuracySec/RandomizedDelaySec) is longer than "
                "the analysed window, so none of its runs could be judged; widen "
                "--since/--until",
            )
        )
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
            if isinstance(exc, calendarspec.UnsupportedTimezoneError):
                # Not just a coverage gap: the timer looks checked in the job
                # table (zero expected, zero missed) while nothing about its
                # schedule was verified, so say it out loud and exit non-zero.
                warnings.append(
                    ScanWarning(
                        "unsupported-timezone",
                        f"{job.id}: OnCalendar={expression!r} names a timezone, "
                        "which is not supported - the timer is excluded from "
                        "missed-run detection (overlaps and failures are still "
                        "reported)",
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
