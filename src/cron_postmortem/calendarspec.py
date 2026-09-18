"""Expansion of systemd ``OnCalendar=`` expressions into concrete occurrences.

Covers the subset that real timer units use: the shorthands, weekday filters,
``*``/lists/``a..b`` ranges and ``/step`` repetitions.  Anything outside that subset
raises :class:`CalendarParseError` so the caller can report it as a coverage gap
instead of silently pretending the timer was checked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

# systemd counts Monday as the first weekday; so does datetime.date.weekday().
WEEKDAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

SHORTHANDS = {
    "minutely": "*-*-* *:*:00",
    "hourly": "*-*-* *:00:00",
    "daily": "*-*-* 00:00:00",
    "monthly": "*-*-01 00:00:00",
    "weekly": "Mon *-*-* 00:00:00",
    "yearly": "*-01-01 00:00:00",
    "annually": "*-01-01 00:00:00",
    "quarterly": "*-01,04,07,10-01 00:00:00",
    "semiannually": "*-01,07-01 00:00:00",
}

# Enumerating "every second for a month" helps nobody and eats the process.
MAX_OCCURRENCES = 200_000

_TZ_RE = re.compile(r"^(UTC|[A-Za-z]+/[A-Za-z0-9_+\-]+)$")
_STEP_RE = re.compile(r"^(?P<range>[^/]*)(?:/(?P<step>\d+))?$")


class CalendarParseError(ValueError):
    """Raised when an ``OnCalendar=`` expression cannot be understood."""


@dataclass(frozen=True)
class CalendarSchedule:
    """``None`` for a component means "any value" (the ``*`` wildcard)."""

    weekdays: frozenset[int] | None
    years: frozenset[int] | None
    months: frozenset[int] | None
    days: frozenset[int] | None
    hours: frozenset[int]
    minutes: frozenset[int]
    seconds: frozenset[int]
    timezone: str | None = None

    def matches_day(self, day: date) -> bool:
        if self.weekdays is not None and day.weekday() not in self.weekdays:
            return False
        if self.years is not None and day.year not in self.years:
            return False
        if self.months is not None and day.month not in self.months:
            return False
        if self.days is not None and day.day not in self.days:
            return False
        return True

    def occurrences(self, start: datetime, end: datetime) -> list[datetime]:
        if end < start:
            return []
        per_day = len(self.hours) * len(self.minutes) * len(self.seconds)
        span_days = (end.date() - start.date()).days + 1
        if per_day * span_days > MAX_OCCURRENCES:
            raise CalendarParseError(
                "schedule fires too often to enumerate over the requested window"
            )
        out: list[datetime] = []
        hours = sorted(self.hours)
        minutes = sorted(self.minutes)
        seconds = sorted(self.seconds)
        day = start.date()
        last_day = end.date()
        while day <= last_day:
            if self.matches_day(day):
                for hour in hours:
                    for minute in minutes:
                        for second in seconds:
                            moment = datetime(
                                day.year, day.month, day.day, hour, minute, second
                            )
                            if start <= moment <= end:
                                out.append(moment)
            day += timedelta(days=1)
        return out


def _parse_component(
    text: str, low: int, high: int, names: dict[str, int] | None = None
) -> frozenset[int] | None:
    """Parse one ``*``/list/range/step component.  ``None`` means "any"."""
    names = names or {}
    text = text.strip()
    if not text:
        raise CalendarParseError("empty component")
    if "~" in text:
        raise CalendarParseError(f"last-day syntax is not supported: {text!r}")
    if text == "*":
        return None
    values: set[int] = set()
    wildcard_only = True
    for part in text.split(","):
        part = part.strip()
        match = _STEP_RE.match(part)
        if not match:
            raise CalendarParseError(f"cannot parse {part!r}")
        body = match.group("range").strip()
        step = int(match.group("step")) if match.group("step") else 1
        if step < 1:
            raise CalendarParseError(f"step must be >= 1 in {part!r}")
        if body == "*" or body == "":
            first, last = low, high
        elif ".." in body:
            head, _, tail = body.partition("..")
            first = _lookup(head, low, high, names)
            last = _lookup(tail, low, high, names)
            wildcard_only = False
        else:
            first = _lookup(body, low, high, names)
            last = high if match.group("step") else first
            wildcard_only = False
        if last < first:
            raise CalendarParseError(f"inverted range in {part!r}")
        values.update(range(first, last + 1, step))
    if not values:
        raise CalendarParseError(f"component {text!r} matches nothing")
    if wildcard_only and len(values) == high - low + 1:
        return None
    return frozenset(values)


def _lookup(token: str, low: int, high: int, names: dict[str, int]) -> int:
    token = token.strip().lower()
    if token in names:
        return names[token]
    if not token.isdigit():
        raise CalendarParseError(f"not a number: {token!r}")
    value = int(token)
    if not low <= value <= high:
        raise CalendarParseError(f"value {value} out of range {low}-{high}")
    return value


def _split_weekdays(token: str) -> frozenset[int]:
    """Parse ``Mon``, ``Mon..Fri``, ``Sat,Sun`` into python weekday numbers."""
    days: set[int] = set()
    for part in token.split(","):
        part = part.strip()
        if not part:
            continue
        if ".." in part:
            head, _, tail = part.partition("..")
            first = _lookup(head, 0, 6, WEEKDAY_NAMES)
            last = _lookup(tail, 0, 6, WEEKDAY_NAMES)
            # systemd wraps, e.g. Fri..Mon.
            day = first
            while True:
                days.add(day)
                if day == last:
                    break
                day = (day + 1) % 7
        else:
            days.add(_lookup(part, 0, 6, WEEKDAY_NAMES))
    if not days:
        raise CalendarParseError(f"no weekday in {token!r}")
    return frozenset(days)


def _looks_like_weekdays(token: str) -> bool:
    head = re.split(r"[.,]", token, maxsplit=1)[0].strip().lower()
    return head in WEEKDAY_NAMES


def parse(expression: str) -> CalendarSchedule:
    """Parse an ``OnCalendar=`` value."""
    text = expression.strip()
    if not text:
        raise CalendarParseError("empty expression")
    lowered = text.lower()
    if lowered in SHORTHANDS:
        text = SHORTHANDS[lowered]
    elif lowered in {"*-*-* *:*:*", "second", "secondly"}:
        raise CalendarParseError("per-second schedules are not supported")

    tokens = text.split()
    timezone = None
    if len(tokens) > 1 and _TZ_RE.match(tokens[-1]):
        timezone = tokens.pop()

    weekdays: frozenset[int] | None = None
    if tokens and _looks_like_weekdays(tokens[0]):
        weekdays = _split_weekdays(tokens.pop(0))

    date_part = "*-*-*"
    time_part = "00:00:00"
    if len(tokens) == 2:
        date_part, time_part = tokens
    elif len(tokens) == 1:
        if ":" in tokens[0]:
            time_part = tokens[0]
        else:
            date_part = tokens[0]
    elif len(tokens) > 2:
        raise CalendarParseError(f"too many components in {expression!r}")
    elif weekdays is None:
        raise CalendarParseError(f"nothing to parse in {expression!r}")

    date_fields = date_part.split("-")
    if len(date_fields) == 3:
        year_s, month_s, day_s = date_fields
    elif len(date_fields) == 2:
        year_s, (month_s, day_s) = "*", date_fields
    else:
        raise CalendarParseError(f"cannot parse date {date_part!r}")

    time_fields = time_part.split(":")
    if len(time_fields) == 3:
        hour_s, minute_s, second_s = time_fields
    elif len(time_fields) == 2:
        hour_s, minute_s = time_fields
        second_s = "0"
    else:
        raise CalendarParseError(f"cannot parse time {time_part!r}")

    hours = _parse_component(hour_s, 0, 23)
    minutes = _parse_component(minute_s, 0, 59)
    seconds = _parse_component(second_s, 0, 60)
    return CalendarSchedule(
        weekdays=weekdays,
        years=_parse_component(year_s, 1970, 2200),
        months=_parse_component(month_s, 1, 12),
        days=_parse_component(day_s, 1, 31),
        hours=frozenset(range(24)) if hours is None else hours,
        minutes=frozenset(range(60)) if minutes is None else minutes,
        seconds=frozenset(range(60)) if seconds is None else frozenset(
            s for s in seconds if s < 60
        ),
        timezone=timezone,
    )
