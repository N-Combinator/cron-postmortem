"""Reading crontab files into :class:`~cron_postmortem.model.Job` objects."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
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

# A command begins here rather than continuing the previous word.
_COMMAND_PATH_RE = re.compile(r"^[/~]")
# Words that look like a user name but are commands, and that no distribution
# ships as an account: seeing one where the user column would be says the file
# has no user column.  Names that are both a command and a stock account
# (mysql, git, sync, mail, backup, list, man, news, lp) are deliberately absent
# - they decide nothing, and the other entries in the file vote instead.
_COMMAND_WORDS = frozenset({
    "bash", "cd", "chronic", "curl", "docker", "echo", "env", "exec", "find",
    "flock", "ionice", "logrotate", "make", "mysqldump", "nice", "node", "npm",
    "perl", "pg_dump", "php", "printf", "psql", "python", "python2", "python3",
    "rsync", "ruby", "run-parts", "sh", "sleep", "source", "sudo", "systemctl",
    "tar", "test", "timeout", "umask", "wget", "xargs",
})
# A word in the command position that continues the previous word rather than
# starting a command of its own.
_SHELL_OPERATORS = frozenset({"&&", "||", "|", ";", "&", ">", ">>", "<", "2>&1"})

SYSTEM_FORMAT = "system"
USER_FORMAT = "user"


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


@dataclass(frozen=True)
class FormatDetection:
    """How :func:`detect_format` read a crontab, and how sure it is.

    The counts are kept so a caller can say *why* it chose a format; a file
    whose entries disagree, or that says nothing either way, is worth a word in
    the report because the choice decides every command the scan compares.
    """

    system_format: bool
    system_votes: int = 0
    user_votes: int = 0
    undecided: int = 0

    @property
    def entries(self) -> int:
        return self.system_votes + self.user_votes + self.undecided

    @property
    def unanimous(self) -> bool:
        """Every entry that had an opinion agreed, and none abstained."""
        return self.undecided == 0 and not (self.system_votes and self.user_votes)

    @property
    def name(self) -> str:
        return SYSTEM_FORMAT if self.system_format else USER_FORMAT


def detect_format(text: str) -> FormatDetection:
    """Decide from the CONTENT whether a crontab carries a user column.

    The path says nothing reliable.  ``/etc/crontab`` and ``/etc/cron.d/*`` do
    carry a user column on a live host, but the files people actually hand this
    tool are captures - ``web01.crontab``, ``crontab.txt``, ``etc-crontab`` -
    and a system crontab read as a user crontab turns ``root /usr/bin/x`` into a
    command no log line can ever say, so every occurrence comes back missed.
    That is not a cosmetic slip: it is a wrong answer that looks like a finding.

    So every entry votes on what sits in field 6 (field 2 after an ``@macro``),
    and the majority decides for the whole file - cron applies one format to a
    file, not one per line.  An entry votes *user* when field 6 cannot be a user
    name, when nothing follows it, when it is a command no distribution ships as
    an account, or when the word after it is an option or a shell operator and
    so belongs to it.  It votes *system* when field 6 is a plausible user name
    and what follows opens a command of its own - an absolute path, ``[``, or a
    known command word - with ``root`` taken as a user name outright.

    Entries that fit neither shape (``*/5 * * * * backup archive``, where
    ``backup`` is both a stock account and a plausible script) abstain.  A tie,
    including a file where every entry abstained, resolves to **user format**:
    it is what ``crontab -l`` emits and what the spool holds, so it is the
    format a collected crontab most often is.  The caller is told whenever the
    entries were not unanimous, and ``--crontab-format`` settles it by hand.
    """
    system_votes = user_votes = undecided = 0
    for line in _entry_lines(text):
        rest = _fields_after_schedule(line)
        if rest is None:
            continue
        vote = _vote(rest)
        if vote is True:
            system_votes += 1
        elif vote is False:
            user_votes += 1
        else:
            undecided += 1
    return FormatDetection(
        system_format=system_votes > user_votes,
        system_votes=system_votes,
        user_votes=user_votes,
        undecided=undecided,
    )


def _entry_lines(text: str) -> list[str]:
    """The lines of a crontab that schedule something, stripped of the rest."""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or _ENV_RE.match(line):
            continue
        lines.append(line)
    return lines


def _fields_after_schedule(line: str) -> list[str] | None:
    """The fields following the schedule, or ``None`` if this is not an entry.

    A line too short to carry a command in *either* format - a truncated
    ``17 * * * *``, a bare ``@daily`` - is not an entry that could have been
    read the wrong way, it is a broken line, and :func:`parse_crontab` reports
    it as one.  Such a line is left out of the tally entirely rather than
    counted as an abstention, so that a file of nothing but broken lines is not
    also accused of being ambiguous about its format.
    """
    fields = line.split()
    if line.startswith("@"):
        rest = fields[1:]
    elif len(fields) >= 5:
        rest = fields[5:]
    else:
        return None
    return rest or None


def _vote(rest: list[str]) -> bool | None:
    """``True`` for system format, ``False`` for user format, ``None`` to abstain."""
    candidate_user, after = rest[0], rest[1] if len(rest) > 1 else None
    if not _USERNAME_RE.match(candidate_user):
        return False
    if after is None or candidate_user in _COMMAND_WORDS:
        return False
    if after.startswith("-") or after in _SHELL_OPERATORS:
        return False
    if candidate_user == "root":
        # No distribution ships a command called root, and root owns most of the
        # entries anyone collects.
        return True
    if _COMMAND_PATH_RE.match(after) or after.startswith("[") or after in _COMMAND_WORDS:
        return True
    return None


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
    detection: FormatDetection | None = None
    if format_override == "auto":
        detection = detect_format(text)
        system_format = detection.system_format
    else:
        system_format = format_override == SYSTEM_FORMAT
    user = user_override or default_user_for(path, trust_filename=trust_filename)
    jobs, problems = parse_crontab(text, str(path), system_format, user)
    if detection is not None and detection.entries and not detection.unanimous:
        # The format decides where the command starts, so every comparison
        # against the log hangs on it; a file whose entries disagree, or that
        # says nothing either way, must not have the guess made silently.
        problems.append(
            f"{path}: read as a {detection.name}-format crontab, but its "
            f"entries do not agree ({detection.system_votes} look system-format, "
            f"{detection.user_votes} user-format, {detection.undecided} could be "
            "either); pass --crontab-format if that is wrong"
        )
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
