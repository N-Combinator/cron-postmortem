"""Reading crontab files into :class:`~cron_postmortem.model.Job` objects."""

from __future__ import annotations

import os
import re
from pathlib import Path

from .cronspec import MACROS, UNPREDICTABLE_MACROS
from .model import CRON, Job

# Places a Debian/RHEL box keeps crontabs.
SYSTEM_CRONTAB = Path("/etc/crontab")
CRON_D_DIRS = (Path("/etc/cron.d"),)
SPOOL_DIRS = (Path("/var/spool/cron/crontabs"), Path("/var/spool/cron"))

_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")
_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_.\-]*\$?$")
# run-parts drop-in files whose names cron itself ignores.
_IGNORED_NAME_RE = re.compile(r"(\.(dpkg|rpm)[^.]*|~|\.bak|\.swp)$")


def normalize_command(command: str) -> str:
    """Collapse whitespace so a crontab entry can be compared to a log line."""
    return " ".join(command.split())


def default_user_for(path: Path) -> str:
    """User to attribute a user-format crontab to, from its filename."""
    name = path.name
    if _USERNAME_RE.match(name):
        return name
    return "root"


def is_system_format(path: Path) -> bool:
    """``/etc/crontab`` and ``/etc/cron.d/*`` carry a user column; user crontabs do not."""
    resolved = Path(os.path.normpath(str(path)))
    if resolved.name == "crontab" and resolved.parent.name == "etc":
        return True
    return "cron.d" in resolved.parts


def parse_crontab(
    text: str,
    origin: str,
    system_format: bool,
    default_user: str = "root",
) -> tuple[list[Job], list[str]]:
    """Return the jobs in a crontab plus a list of human-readable parse problems."""
    jobs: list[Job] = []
    problems: list[str] = []
    seen: dict[str, int] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if _ENV_RE.match(line):
            continue
        try:
            schedule, user, command = _split_entry(line, system_format, default_user)
        except ValueError as exc:
            problems.append(f"{origin}:{lineno}: {exc}")
            continue
        if not command:
            problems.append(f"{origin}:{lineno}: entry has no command")
            continue
        command = normalize_command(command)
        job_id = f"cron:{user}:{command}"
        count = seen.get(job_id, 0) + 1
        seen[job_id] = count
        if count > 1:
            job_id = f"{job_id}#{count}"
        jobs.append(
            Job(
                id=job_id,
                source=CRON,
                schedule=schedule,
                origin=f"{origin}:{lineno}",
                user=user,
                command=command,
            )
        )
    return jobs, problems


def _split_entry(line: str, system_format: bool, default_user: str) -> tuple[str, str, str]:
    if line.startswith("@"):
        parts = line.split(None, 2 if system_format else 1)
        macro = parts[0].lower()
        if macro not in MACROS and macro not in UNPREDICTABLE_MACROS:
            raise ValueError(f"unknown schedule macro {parts[0]!r}")
        if system_format:
            if len(parts) < 3:
                raise ValueError("system crontab entry needs a user and a command")
            return macro, parts[1], parts[2]
        if len(parts) < 2:
            raise ValueError("entry has no command")
        return macro, default_user, parts[1]

    fields = line.split(None, 6 if system_format else 5)
    needed = 7 if system_format else 6
    if len(fields) < needed:
        raise ValueError(f"expected {needed} fields, got {len(fields)}")
    schedule = " ".join(fields[:5])
    if system_format:
        return schedule, fields[5], fields[6]
    return schedule, default_user, fields[5]


def load_crontab_file(
    path: Path, format_override: str = "auto", user_override: str | None = None
) -> tuple[list[Job], list[str]]:
    """Read one crontab file from disk."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], [f"{path}: cannot read ({exc.strerror or exc})"]
    if format_override == "auto":
        system_format = is_system_format(path)
    else:
        system_format = format_override == "system"
    user = user_override or default_user_for(path)
    return parse_crontab(text, str(path), system_format, user)


def discover_crontab_files() -> list[Path]:
    """Every crontab file on this host that we are allowed to read."""
    found: list[Path] = []
    if SYSTEM_CRONTAB.is_file():
        found.append(SYSTEM_CRONTAB)
    for directory in CRON_D_DIRS:
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if entry.is_file() and not _IGNORED_NAME_RE.search(entry.name):
                found.append(entry)
    for directory in SPOOL_DIRS:
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if entry.is_file() and os.access(entry, os.R_OK):
                found.append(entry)
    return found
