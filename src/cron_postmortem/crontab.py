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
# A username may legally contain a dot, but a collected file is far more often
# named after the capture than after its owner, so off the spool a dot reads as
# an extension and the name is not believed.
_COLLECTED_NAME_RE = re.compile(r"^[a-z_][a-z0-9_\-]*\$?$")
# Names a collected crontab is given when it is named after the thing it is
# rather than after the user who owns it.
_GENERIC_NAMES = frozenset({"cron", "crontab", "crontabs", "cronjobs", "jobs", "tab"})
# run-parts drop-in files whose names cron itself ignores.
_IGNORED_NAME_RE = re.compile(r"(\.(dpkg|rpm)[^.]*|~|\.bak|\.swp)$")


def normalize_command(command: str) -> str:
    """Collapse whitespace so a crontab entry can be compared to a log line."""
    return " ".join(command.split())


def default_user_for(path: Path, *, trust_filename: bool = False) -> str:
    """User to attribute a user-format crontab to, from its filename.

    In a spool directory the filename *is* the owner's name - that is how cron
    itself decides who runs the entries - so discovery passes
    ``trust_filename=True`` and the name is taken as given.

    A path named on the command line is a file somebody collected, and it is
    usually called after the capture rather than after its owner
    (``web01.crontab``, ``root.txt``, ``crontab``).  Attributing those entries
    to a user that does not exist is not a cosmetic slip: the log only ever says
    ``(user) CMD``, so no observed run can match and every occurrence is
    reported as missed.  Off the spool the name is therefore believed only when
    it is a bare plausible username, and cron's own default of ``root`` - the
    owner of most crontabs anyone bothers to collect - is used otherwise.
    ``--crontab-user`` overrides both.
    """
    name = path.name
    if not _USERNAME_RE.match(name):
        return "root"
    if trust_filename:
        return name
    if not _COLLECTED_NAME_RE.match(name) or name in _GENERIC_NAMES:
        return "root"
    return name


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
            # cronie's CRON_TZ= makes every entry below it fire in another zone.
            # Schedules here are compared to log timestamps as local wall-clock
            # time, the same refusal calendarspec makes for OnCalendar=, so say
            # that the entries below this line are being read in local time.
            if line.split("=", 1)[0].strip().upper() == "CRON_TZ":
                problems.append(
                    f"{origin}:{lineno}: {line.split('=', 1)[0].strip()}= is not "
                    "applied; the entries below it are analysed in local time"
                )
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
    path: Path,
    format_override: str = "auto",
    user_override: str | None = None,
    *,
    trust_filename: bool = False,
) -> tuple[list[Job], list[str]]:
    """Read one crontab file from disk.

    ``trust_filename`` says the file was found in a spool directory, where the
    name is the owner's; see :func:`default_user_for`.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], [f"{path}: cannot read ({exc.strerror or exc})"]
    if format_override == "auto":
        system_format = is_system_format(path)
    else:
        system_format = format_override == "system"
    user = user_override or default_user_for(path, trust_filename=trust_filename)
    jobs, problems = parse_crontab(text, str(path), system_format, user)
    if jobs and not system_format and user_override is None and path.name != user:
        # Say which user the entries were attributed to whenever the filename
        # was not taken at face value: the whole match against the log hangs on
        # it, and getting it wrong looks exactly like a job that never ran.
        problems.append(
            f"{path}: user-format crontab whose filename is not a username; "
            f"entries attributed to {user!r} - pass --crontab-user if they "
            "belong to somebody else"
        )
    return jobs, problems


def discover_crontab_files() -> tuple[list[Path], list[str]]:
    """Every crontab file on this host that we are allowed to read.

    Spool directories are mode 0700 root, so a non-root scan legitimately cannot
    list them; that is reported as a problem rather than raised, so the rest of
    the scan still produces a report.
    """
    found: list[Path] = []
    problems: list[str] = []
    if SYSTEM_CRONTAB.is_file():
        found.append(SYSTEM_CRONTAB)
    for directory in (*CRON_D_DIRS, *SPOOL_DIRS):
        try:
            entries = sorted(directory.iterdir())
        except FileNotFoundError:
            continue
        except OSError as exc:
            problems.append(
                f"{directory}: cannot list ({exc.strerror or exc}); "
                "run as root or pass --crontab to include it"
            )
            continue
        for entry in entries:
            if not entry.is_file() or _IGNORED_NAME_RE.search(entry.name):
                continue
            if not os.access(entry, os.R_OK):
                problems.append(f"{entry}: not readable; run as root to include it")
                continue
            found.append(entry)
    return found, problems
