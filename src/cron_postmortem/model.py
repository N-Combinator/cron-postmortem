"""Data model shared by the parsers, the detectors and the reporters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

CRON = "cron"
SYSTEMD = "systemd"

MISSED = "missed"
OVERLAP = "overlap"
FAILURE = "failure"

ERROR = "error"
WARNING = "warning"

# The one ScanWarning code the exit scheme treats specially; see ScanWarning
# and cli.EXIT_NO_MATCH.
NO_RUNS_MATCHED = "no-runs-matched"


@dataclass(frozen=True)
class Job:
    """A scheduled job we know about, independent of whether it ever ran."""

    id: str
    source: str
    schedule: str
    origin: str
    user: str | None = None
    command: str | None = None
    unit: str | None = None
    timer: str | None = None
    # A systemd timer may carry several OnCalendar= lines, which systemd ORs.
    schedules: tuple[str, ...] = ()

    @property
    def schedule_list(self) -> tuple[str, ...]:
        return self.schedules or (self.schedule,)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "schedule": self.schedule,
            "schedules": list(self.schedule_list),
            "origin": self.origin,
            "user": self.user,
            "command": self.command,
            "unit": self.unit,
            "timer": self.timer,
        }


@dataclass
class Run:
    """One observed execution of a job, as reconstructed from log lines."""

    job_id: str
    start: datetime
    end: datetime | None = None
    pid: int | None = None
    exit_code: int | None = None
    result: str | None = None

    @property
    def duration(self) -> float | None:
        if self.end is None:
            return None
        return (self.end - self.start).total_seconds()

    def as_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat() if self.end else None,
            "duration_seconds": self.duration,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "result": self.result,
        }


@dataclass
class Finding:
    """A problem worth a non-zero exit code."""

    kind: str
    severity: str
    job_id: str
    source: str
    message: str
    when: datetime | None = None
    details: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "job_id": self.job_id,
            "source": self.source,
            "message": self.message,
            "when": self.when.isoformat() if self.when else None,
            "details": self.details,
        }


@dataclass
class ScanWarning:
    """Something wrong with the scan itself rather than with a job.

    A :class:`Diagnostic` is a gap we can live with; a warning means the scan
    could not do what it was asked to do - no schedules to check, no log lines
    understood - so reporting success would be a lie.  Warnings therefore move
    the exit code, findings-style, even though they belong to no single job.

    ``usage_error`` marks the subset that is the caller's fault rather than the
    host's: arguments that contradict each other, such as a window shorter than
    the tolerance applied to it.  Those exit 2 (usage) instead of 1 (problems
    found), because there is no report to act on - the scan never ran.

    :data:`NO_RUNS_MATCHED` gets an exit code of its own (3) for the same
    reason in reverse: the scan did run, and produced a full page of missed
    runs that are not real.  It is those runs the warning is about, so a scan
    that reconciled nothing and reported none of them stays quiet.  A
    monitoring check that cannot tell that page from a genuine outage acts on
    fiction, so it must be able to tell them apart from the exit code alone.
    It disowns those entries and nothing else, so a scan that also found a real
    problem still exits 1; see
    ``ScanResult.standing_findings``.
    """

    code: str
    message: str
    usage_error: bool = False

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "usage_error": self.usage_error,
        }


@dataclass
class Diagnostic:
    """Something we could not analyse; not a job problem, a coverage gap."""

    job_id: str | None
    message: str
    origin: str | None = None

    def as_dict(self) -> dict:
        return {"job_id": self.job_id, "message": self.message, "origin": self.origin}
