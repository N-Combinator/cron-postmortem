"""Reading systemd timer/service state from ``systemctl show`` output.

Everything here works on captured text, so the live path and the offline path
(``--systemctl-show FILE``) go through exactly the same parser.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .model import SYSTEMD, Job

_ONCALENDAR_RE = re.compile(r"OnCalendar=(?P<value>[^;}]+)")
_DURATION_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[a-z]+)")
_TIMESTAMP_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2}:\d{2})"
)

_DURATION_UNITS = {
    "us": 1e-6, "usec": 1e-6, "µs": 1e-6,
    "ms": 1e-3, "msec": 1e-3,
    "s": 1.0, "sec": 1.0, "second": 1.0, "seconds": 1.0,
    "m": 60.0, "min": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hr": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "d": 86400.0, "day": 86400.0, "days": 86400.0,
    "w": 604800.0, "week": 604800.0, "weeks": 604800.0,
}


@dataclass
class UnitState:
    """The subset of ``systemctl show`` we reason about."""

    unit: str
    properties: dict[str, str] = field(default_factory=dict)

    def get(self, key: str, default: str = "") -> str:
        return self.properties.get(key, default)

    @property
    def description(self) -> str:
        return self.get("Description")

    @property
    def active_state(self) -> str:
        return self.get("ActiveState")

    @property
    def result(self) -> str:
        return self.get("Result")

    @property
    def exit_status(self) -> int | None:
        raw = self.get("ExecMainStatus")
        return int(raw) if raw.strip().lstrip("-").isdigit() else None

    @property
    def is_failed(self) -> bool:
        if self.active_state == "failed":
            return True
        if self.result and self.result not in {"success", ""}:
            return True
        status = self.exit_status
        return status is not None and status != 0


def parse_duration(text: str) -> float:
    """``30s``, ``1min 30s``, ``0``, ``infinity`` -> seconds."""
    text = text.strip().lower()
    if not text or text in {"infinity", "n/a"}:
        return 0.0
    if text.isdigit():  # bare *USec* property value
        return int(text) / 1e6
    total = 0.0
    matched = False
    for match in _DURATION_RE.finditer(text):
        factor = _DURATION_UNITS.get(match.group("unit"))
        if factor is None:
            continue
        total += float(match.group("value")) * factor
        matched = True
    return total if matched else 0.0


def parse_timestamp(text: str) -> datetime | None:
    """``Fri 2026-09-18 04:00:00 CEST`` -> naive local datetime."""
    match = _TIMESTAMP_RE.search(text or "")
    if not match:
        return None
    return datetime.strptime(
        f"{match.group('date')} {match.group('time')}", "%Y-%m-%d %H:%M:%S"
    )


def parse_show(text: str) -> list[UnitState]:
    """Parse ``systemctl show`` output, which separates units with a blank line."""
    units: list[UnitState] = []
    current: dict[str, str] = {}

    def flush() -> None:
        if current:
            units.append(UnitState(unit=current.get("Id", ""), properties=dict(current)))
        current.clear()

    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            flush()
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key == "Id" and "Id" in current:
            # Some systemd versions run blocks together without a blank line.
            flush()
        current[key] = value
    flush()
    return [unit for unit in units if unit.unit]


def timer_jobs(units: list[UnitState], origin: str) -> tuple[list[Job], list[str]]:
    """Build one job per ``.timer`` unit found in the parsed properties."""
    jobs: list[Job] = []
    problems: list[str] = []
    for unit in units:
        if not unit.unit.endswith(".timer"):
            continue
        calendars = _ONCALENDAR_RE.findall(unit.get("TimersCalendar"))
        service = unit.get("Unit") or unit.unit[: -len(".timer")] + ".service"
        if not calendars:
            problems.append(
                f"{unit.unit}: no OnCalendar= (monotonic timer such as OnBootSec is "
                "not schedulable from a calendar)"
            )
            continue
        for index, calendar in enumerate(calendars):
            calendar = calendar.strip()
            suffix = f"#{index + 1}" if len(calendars) > 1 else ""
            jobs.append(
                Job(
                    id=f"systemd:{unit.unit}{suffix}",
                    source=SYSTEMD,
                    schedule=calendar,
                    origin=origin,
                    unit=service,
                    timer=unit.unit,
                )
            )
    return jobs, problems


def timer_tolerance(unit: UnitState) -> float:
    """Extra slack a timer legitimately has before a run counts as missed."""
    accuracy = parse_duration(unit.get("AccuracyUSec") or unit.get("AccuracySec"))
    randomized = parse_duration(
        unit.get("RandomizedDelayUSec") or unit.get("RandomizedDelaySec")
    )
    return accuracy + randomized


def description_map(units: list[UnitState]) -> dict[str, str]:
    """``Description=`` -> unit id, to resolve pre-v250 ``Starting <desc>...`` lines."""
    mapping: dict[str, str] = {}
    for unit in units:
        if unit.description and unit.unit.endswith(".service"):
            mapping.setdefault(unit.description.rstrip("."), unit.unit)
    return mapping


def load_show_file(path: Path) -> tuple[list[UnitState], list[str]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], [f"{path}: cannot read ({exc.strerror or exc})"]
    return parse_show(text), []


def _run(argv: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)
    return completed.returncode, completed.stdout, completed.stderr


def live_timer_units() -> tuple[list[str], list[str]]:
    """Names of every timer unit known to the local systemd."""
    code, out, err = _run(
        ["systemctl", "list-units", "--type=timer", "--all", "--no-legend",
         "--plain", "--no-pager"]
    )
    if code != 0:
        return [], [f"systemctl list-units failed: {err.strip() or code}"]
    names = []
    for line in out.splitlines():
        fields = line.split()
        if fields and fields[0].endswith(".timer"):
            names.append(fields[0])
    return names, []


def live_show(units: list[str]) -> tuple[list[UnitState], list[str]]:
    """``systemctl show`` for the given units."""
    if not units:
        return [], []
    code, out, err = _run(["systemctl", "show", "--no-pager", *units])
    if code != 0:
        return [], [f"systemctl show failed: {err.strip() or code}"]
    return parse_show(out), []


def live_journal(argv_extra: list[str], since: datetime, until: datetime) -> tuple[str, list[str]]:
    """Fetch journal text for the given matchers over the window."""
    argv = [
        "journalctl", "--no-pager", "--output=short-iso",
        "--since", since.strftime("%Y-%m-%d %H:%M:%S"),
        "--until", until.strftime("%Y-%m-%d %H:%M:%S"),
        *argv_extra,
    ]
    code, out, err = _run(argv, timeout=120.0)
    if code != 0:
        return "", [f"{' '.join(argv)}: failed ({err.strip() or code})"]
    return out, []
