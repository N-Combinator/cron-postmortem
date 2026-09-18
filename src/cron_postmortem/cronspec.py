"""Expansion of classic five-field cron expressions into concrete occurrences.

Deliberately dependency-free: the whole point of this tool is to be droppable onto a
box that already has a problem, so it only uses the standard library.  The semantics
follow Vixie cron, including the day-of-month / day-of-week OR rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
DOW_NAMES = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}

MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

# Schedules that exist but can never be predicted from a calendar.
UNPREDICTABLE_MACROS = {"@reboot"}

_FIELD_RE = re.compile(r"^(?P<range>[^/]+)(?:/(?P<step>\d+))?$")


class CronParseError(ValueError):
    """Raised when a cron expression cannot be understood."""


@dataclass(frozen=True)
class CronSchedule:
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    dom_restricted: bool
    dow_restricted: bool

    def matches_day(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        dom_ok = day.day in self.days_of_month
        # Python: Monday == 0; cron: Sunday == 0.
        dow_ok = ((day.weekday() + 1) % 7) in self.days_of_week
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        if self.dom_restricted:
            return dom_ok
        if self.dow_restricted:
            return dow_ok
        return True

    def occurrences(self, start: datetime, end: datetime) -> list[datetime]:
        """Every firing time in the closed interval [start, end]."""
        if end < start:
            return []
        out: list[datetime] = []
        hours = sorted(self.hours)
        minutes = sorted(self.minutes)
        day = start.date()
        last_day = end.date()
        while day <= last_day:
            if self.matches_day(day):
                for hour in hours:
                    for minute in minutes:
                        moment = datetime(day.year, day.month, day.day, hour, minute)
                        if start <= moment <= end:
                            out.append(moment)
            day += timedelta(days=1)
        return out


def _parse_value(token: str, low: int, high: int, names: dict[str, int]) -> int:
    token = token.strip().lower()
    if token in names:
        return names[token]
    if not token.isdigit():
        raise CronParseError(f"not a number: {token!r}")
    value = int(token)
    if not low <= value <= high:
        raise CronParseError(f"value {value} out of range {low}-{high}")
    return value


def _parse_field(
    field: str, low: int, high: int, names: dict[str, int] | None = None
) -> tuple[frozenset[int], bool]:
    """Return the matching values and whether the field is restricted (not ``*``)."""
    names = names or {}
    field = field.strip()
    if not field:
        raise CronParseError("empty field")
    values: set[int] = set()
    restricted = False
    for part in field.split(","):
        part = part.strip()
        match = _FIELD_RE.match(part)
        if not match:
            raise CronParseError(f"cannot parse field element {part!r}")
        body = match.group("range").strip()
        step = int(match.group("step")) if match.group("step") else 1
        if step < 1:
            raise CronParseError(f"step must be >= 1 in {part!r}")
        if body == "*":
            first, last = low, high
        elif "-" in body:
            head, _, tail = body.partition("-")
            first = _parse_value(head, low, high, names)
            last = _parse_value(tail, low, high, names)
            restricted = True
        else:
            first = _parse_value(body, low, high, names)
            # ``5/10`` means "from 5, every 10" - a bare ``5`` is a single value.
            last = high if match.group("step") else first
            restricted = True
        if last < first:
            raise CronParseError(f"inverted range in {part!r}")
        values.update(range(first, last + 1, step))
    if not values:
        raise CronParseError(f"field {field!r} matches nothing")
    return frozenset(values), restricted


def parse(expression: str) -> CronSchedule:
    """Parse a five-field cron expression or a supported ``@macro``."""
    text = expression.strip()
    if not text:
        raise CronParseError("empty expression")
    if text.startswith("@"):
        macro = text.lower()
        if macro in UNPREDICTABLE_MACROS:
            raise CronParseError(f"{macro} has no calendar schedule")
        if macro not in MACROS:
            raise CronParseError(f"unknown macro {macro}")
        text = MACROS[macro]
    fields = text.split()
    if len(fields) != 5:
        raise CronParseError(f"expected 5 fields, got {len(fields)}: {expression!r}")
    minutes, _ = _parse_field(fields[0], 0, 59)
    hours, _ = _parse_field(fields[1], 0, 23)
    doms, dom_restricted = _parse_field(fields[2], 1, 31)
    months, _ = _parse_field(fields[3], 1, 12, MONTH_NAMES)
    raw_dows, dow_restricted = _parse_field(fields[4], 0, 7, DOW_NAMES)
    dows = frozenset(0 if d == 7 else d for d in raw_dows)
    return CronSchedule(
        minutes=minutes,
        hours=hours,
        days_of_month=doms,
        months=months,
        days_of_week=dows,
        dom_restricted=dom_restricted,
        dow_restricted=dow_restricted,
    )
