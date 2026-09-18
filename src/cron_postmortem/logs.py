"""Parsing of cron and systemd log lines into observed runs.

Handles the formats you actually get from a box: traditional syslog
(``Sep 18 03:00:01 host CRON[1234]: ...``) and the journalctl renderings
``short-iso``, ``short-iso-precise`` and ``short-full``.

All timestamps are reduced to naive local wall-clock time.  That is deliberate:
both cron and systemd timers fire against the wall clock, so comparing a log
timestamp to a schedule in wall-clock terms is the correct thing to do.  Any UTC
offset present in a log line is therefore dropped, not converted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

CRON_IDENTS = {"cron", "crond", "CRON", "CROND"}
SYSTEMD_IDENTS = {"systemd"}
KNOWN_IDENTS = {i.lower() for i in CRON_IDENTS} | SYSTEMD_IDENTS

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Session open and CMD are logged by different processes; cron emits them within
# the same second, so a small window is enough to pair them.
SESSION_PAIR_WINDOW = timedelta(seconds=5)

_SYSLOG_TS = re.compile(
    r"^(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2}):(?P<second>\d{2})\s+"
)
_ISO_TS = re.compile(
    r"^(?:[A-Za-z]{3}\s+)?"
    r"(?P<date>\d{4}-\d{2}-\d{2})[T ]"
    r"(?P<time>\d{2}:\d{2}:\d{2})(?:[.,]\d+)?"
    r"(?P<offset>Z|[+-]\d{2}:?\d{2})?\s+"
)
# journalctl short-full prints a timezone abbreviation after the timestamp.
_TZ_ABBREV = re.compile(r"^(?:[A-Z]{2,5}|[+-]\d{4})\s+")

_HOSTED = re.compile(
    r"^(?P<host>\S+)\s+(?P<ident>[A-Za-z][\w.\-]*)(?:\[(?P<pid>\d+)\])?:\s?(?P<msg>.*)$"
)
_BARE = re.compile(
    r"^(?P<ident>[A-Za-z][\w.\-]*)(?:\[(?P<pid>\d+)\])?:\s?(?P<msg>.*)$"
)

_CMD_RE = re.compile(r"^\((?P<user>[^)]+)\)\s+CMD\s+\((?P<command>.*)\)\s*$")
_SESSION_RE = re.compile(
    r"pam_unix\(cron(?:d)?:session\):\s+session\s+(?P<action>opened|closed)\s+"
    r"for\s+user\s+(?P<user>[\w.\-]+)"
)
_UNIT_PREFIX_RE = re.compile(
    r"^(?P<unit>[\w@.\\:\-]+\.(?:service|timer|mount|socket|scope|path)):\s+(?P<rest>.*)$"
)
_EXIT_RE = re.compile(
    r"Main process exited,\s+code=(?P<code>\w+),\s+status=(?P<status>\d+)"
)
_RESULT_RE = re.compile(r"Failed with result '(?P<result>[^']+)'")
_UNIT_IN_VERB_RE = re.compile(r"^(?P<unit>[\w@.\\:\-]+\.service)(?:\s+-\s+.*)?$")


class LogParseError(ValueError):
    """Raised when a log file yields no usable timestamps at all."""


@dataclass
class LogLine:
    timestamp: datetime
    ident: str
    pid: int | None
    message: str
    lineno: int
    origin: str


@dataclass
class CronRun:
    """One ``CMD`` line, optionally paired with its pam session window."""

    user: str
    command: str
    start: datetime
    end: datetime | None = None
    pid: int | None = None


@dataclass
class CronSession:
    """A ``pam_unix(cron:session)`` window, used to recover run durations."""

    opened_at: datetime
    user: str
    run: CronRun | None = None


@dataclass
class UnitEvent:
    """A start/finish/failure event observed for a systemd unit."""

    unit: str
    kind: str  # "start" | "finish" | "fail"
    timestamp: datetime
    exit_code: int | None = None
    result: str | None = None


@dataclass
class LogScan:
    cron_runs: list[CronRun] = field(default_factory=list)
    unit_events: list[UnitEvent] = field(default_factory=list)
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    lines_parsed: int = 0
    lines_total: int = 0
    unresolved_systemd_messages: set[str] = field(default_factory=set)


def _strip_timestamp(line: str) -> tuple[datetime | None, str, bool]:
    """Return (timestamp, remainder, needs_year).  Year is 1900 when unknown."""
    match = _ISO_TS.match(line)
    if match:
        stamp = datetime.strptime(
            f"{match.group('date')} {match.group('time')}", "%Y-%m-%d %H:%M:%S"
        )
        rest = line[match.end():]
        rest = _TZ_ABBREV.sub("", rest)
        return stamp, rest, False
    match = _SYSLOG_TS.match(line)
    if match:
        month = MONTHS.get(match.group("mon").lower())
        if month is None:
            return None, line, False
        stamp = datetime(
            1900,
            month,
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second")),
        )
        return stamp, line[match.end():], True
    return None, line, False


def _split_body(rest: str) -> tuple[str, int | None, str] | None:
    match = _HOSTED.match(rest)
    if match and match.group("ident").lower() in KNOWN_IDENTS:
        pid = match.group("pid")
        return match.group("ident"), int(pid) if pid else None, match.group("msg")
    match = _BARE.match(rest)
    if match and match.group("ident").lower() in KNOWN_IDENTS:
        pid = match.group("pid")
        return match.group("ident"), int(pid) if pid else None, match.group("msg")
    return None


def parse_lines(
    lines: list[str], origin: str, reference: datetime | None = None
) -> list[LogLine]:
    """Turn raw log text into timestamped, identified lines.

    Traditional syslog carries no year.  Lines are assumed to be in chronological
    order: the year starts at ``reference``'s year and rolls forward on a
    backwards jump (December -> January); if that puts entries in the future the
    whole file is shifted back a year.
    """
    reference = reference or datetime.now()
    out: list[LogLine] = []
    undated: list[tuple[int, datetime, str, int | None, str]] = []
    for lineno, raw in enumerate(lines, start=1):
        text = raw.rstrip("\n")
        if not text.strip() or text.startswith("--"):
            continue
        stamp, rest, needs_year = _strip_timestamp(text)
        if stamp is None:
            continue
        body = _split_body(rest)
        if body is None:
            continue
        ident, pid, message = body
        if needs_year:
            undated.append((lineno, stamp, ident, pid, message))
        else:
            out.append(LogLine(stamp, ident, pid, message, lineno, origin))

    if undated:
        rebuilt: list[tuple[int, datetime, str, int | None, str]] = []
        rollover = 0
        previous: datetime | None = None
        for lineno, stamp, ident, pid, message in undated:
            if previous is not None and _wrapped(stamp, previous):
                rollover += 1
            previous = stamp
            dated = _with_year(stamp, reference.year + rollover)
            rebuilt.append((lineno, dated, ident, pid, message))
        newest = max(item[1] for item in rebuilt)
        shift = 0
        while (
            shift < 50
            and _with_year(newest, newest.year - shift) > reference + timedelta(days=1)
        ):
            shift += 1
        for lineno, stamp, ident, pid, message in rebuilt:
            out.append(
                LogLine(
                    _with_year(stamp, stamp.year - shift),
                    ident, pid, message, lineno, origin,
                )
            )

    out.sort(key=lambda item: (item.timestamp, item.lineno))
    return out


def _wrapped(candidate: datetime, previous: datetime) -> bool:
    """True when ``candidate`` looks like it wrapped into the next year."""
    return (candidate.month, candidate.day) < (previous.month, previous.day)


def _with_year(stamp: datetime, year: int) -> datetime:
    try:
        return stamp.replace(year=year)
    except ValueError:  # Feb 29 in a non-leap year
        return stamp.replace(year=year, day=28)


def scan_lines(
    lines: list[str],
    origin: str,
    reference: datetime | None = None,
    description_to_unit: dict[str, str] | None = None,
) -> LogScan:
    """Extract cron runs and systemd unit events from raw log text."""
    parsed = parse_lines(lines, origin, reference)
    scan = LogScan(lines_total=len(lines), lines_parsed=len(parsed))
    if parsed:
        scan.first_timestamp = parsed[0].timestamp
        scan.last_timestamp = parsed[-1].timestamp

    open_sessions: dict[int, CronSession] = {}

    for line in parsed:
        ident = line.ident.lower()
        if ident in {"cron", "crond"}:
            _handle_cron_line(line, scan, open_sessions)
        elif ident in SYSTEMD_IDENTS:
            _handle_systemd_line(line, scan, description_to_unit or {})

    scan.cron_runs.sort(key=lambda run: (run.start, run.command))
    scan.unit_events.sort(key=lambda event: (event.timestamp, event.unit))
    return scan


def _handle_cron_line(
    line: LogLine, scan: LogScan, open_sessions: dict[int, CronSession]
) -> None:
    session = _SESSION_RE.search(line.message)
    if session and line.pid is not None:
        if session.group("action") == "opened":
            open_sessions[line.pid] = CronSession(line.timestamp, session.group("user"))
        else:
            closed = open_sessions.pop(line.pid, None)
            if closed is not None and closed.run is not None and closed.run.end is None:
                closed.run.end = line.timestamp
        return

    cmd = _CMD_RE.match(line.message.strip())
    if not cmd:
        return
    run = CronRun(
        user=cmd.group("user"),
        command=cmd.group("command").strip(),
        start=line.timestamp,
        pid=line.pid,
    )
    scan.cron_runs.append(run)

    # cron logs CMD from a child process, so its pid usually differs from the pam
    # session pid.  Attach the run to the newest still-unclaimed session opened
    # for the same user just before this line.
    own = open_sessions.get(line.pid) if line.pid is not None else None
    if own is not None and own.run is None:
        own.run = run
        return
    candidates = [
        candidate
        for candidate in open_sessions.values()
        if candidate.run is None
        and candidate.user == run.user
        and candidate.opened_at <= run.start <= candidate.opened_at + SESSION_PAIR_WINDOW
    ]
    if candidates:
        max(candidates, key=lambda candidate: candidate.opened_at).run = run


def _handle_systemd_line(
    line: LogLine, scan: LogScan, description_to_unit: dict[str, str]
) -> None:
    message = line.message.strip()
    prefixed = _UNIT_PREFIX_RE.match(message)
    if prefixed:
        unit = prefixed.group("unit")
        rest = prefixed.group("rest")
        exit_match = _EXIT_RE.search(rest)
        if exit_match:
            code = exit_match.group("code")
            status = int(exit_match.group("status"))
            if code == "exited":
                # ``status`` is the exit code.
                scan.unit_events.append(
                    UnitEvent(
                        unit, "fail" if status else "finish", line.timestamp,
                        exit_code=status,
                    )
                )
            else:
                # code=killed / code=dumped: ``status`` is a signal, not an exit code.
                scan.unit_events.append(
                    UnitEvent(unit, "fail", line.timestamp, result=f"{code}/{status}")
                )
            return
        result_match = _RESULT_RE.search(rest)
        if result_match:
            scan.unit_events.append(
                UnitEvent(
                    unit, "fail", line.timestamp, result=result_match.group("result")
                )
            )
            return
        if rest.startswith("Succeeded") or rest.startswith("Deactivated successfully"):
            scan.unit_events.append(
                UnitEvent(unit, "finish", line.timestamp, exit_code=0, result="success")
            )
        return

    for verb, kind in (
        ("Starting ", "start"),
        ("Started ", "start"),
        ("Finished ", "finish"),
        ("Failed to start ", "fail"),
    ):
        if not message.startswith(verb):
            continue
        tail = message[len(verb):].strip().rstrip(".")
        unit = _resolve_unit(tail, description_to_unit)
        if unit is None:
            scan.unresolved_systemd_messages.add(tail)
            return
        scan.unit_events.append(
            UnitEvent(
                unit, kind, line.timestamp,
                result="failed-to-start" if kind == "fail" else None,
            )
        )
        return


def _resolve_unit(tail: str, description_to_unit: dict[str, str]) -> str | None:
    """systemd >= 250 logs ``unit.service - Description``; older logs only the
    description, which we map back via ``systemctl show``'s ``Description=``."""
    direct = _UNIT_IN_VERB_RE.match(tail)
    if direct:
        return direct.group("unit")
    return description_to_unit.get(tail.rstrip("."))
