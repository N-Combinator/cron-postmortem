"""Reading crontab files into :class:`~cron_postmortem.model.Job` objects."""

from __future__ import annotations

import os
import re
from collections import Counter
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
# A username may legally contain a dot, but wherever this tool has to *guess*
# whether a word is a name - the name of a collected file, a word sitting where
# the user column would be - a dot reads as an extension far more often than as
# an account, so a dotted name is not believed on its own.
_PLAIN_NAME_RE = re.compile(r"^[a-z_][a-z0-9_\-]*\$?$")
# A dot followed by one of these is an extension and nothing else: no
# distribution ships an account called backup.sh or monitor.py.
_SCRIPT_SUFFIX_RE = re.compile(r"\.(sh|bash|ksh|zsh|py|pyc|pl|rb|php|js|ts|awk|exp|jar)$")
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
# Accounts a distribution creates that are not also commands.  A name from this
# list in field 6 settles the format on its own; a name that is both an account
# and a plausible command (backup, mysql, git, sync, mail, list, man, news, lp)
# is deliberately absent, because on those the word decides nothing.
_SYSTEM_ACCOUNTS = frozenset({
    "root", "daemon", "bin", "sys", "adm", "nobody", "www-data", "http",
    "apache", "httpd", "nginx", "postgres", "postfix", "syslog", "messagebus",
    "uucp", "proxy", "gnats", "irc", "sshd", "ftp", "tomcat", "jenkins",
    "munin", "nagios", "zabbix", "prometheus", "grafana", "redis", "mongodb",
    "rabbitmq", "elasticsearch", "influxdb", "oracle", "ubuntu", "ec2-user",
    "systemd-timesync",
})

# How one entry reads.  The two system verdicts differ in what they rest on: a
# name only an account could be, or a name that merely *could* be one sitting in
# front of a path, which is the shape of a per-user entry running a local
# command with a file argument (``backup.sh /data``) just as much as it is the
# shape of a system entry.
_SYSTEM_VOTE = "system"
_SYSTEM_IF_REPEATED = "system-if-repeated"
_USER_VOTE = "user"
_ABSTAIN = "abstain"

SYSTEM_FORMAT = "system"
USER_FORMAT = "user"


@dataclass(frozen=True)
class DiscoveredCrontab:
    """A crontab found where cron itself reads it, and what that location says.

    Discovery is the one case where the path is not a guess.  ``/etc/crontab``
    and ``/etc/cron.d/*`` are read by cron with a user column and a spool file
    is read as the crontab of the user it is named after - that is cron's own
    rule, not an inference about a capture, so :func:`detect_format` has nothing
    to add there and can only get it wrong (a drop-in whose one entry is
    ``*/1 * * * * deploy /opt/app/tick`` says nothing a single line can settle).
    A file the caller names is a capture and goes on being read by content.
    """

    path: Path
    format: str
    filename_is_owner: bool


@dataclass(frozen=True)
class CrontabRead:
    """One crontab file as this tool read it.

    ``detection`` is the vote :func:`detect_format` took, or ``None`` when the
    format did not have to be worked out from the content - the caller passed
    ``--crontab-format`` or the file was discovered where cron's own rule
    applies.  The scanner keeps it because a format nobody vouched for is the
    first suspect when the entries then match nothing in the log.
    """

    jobs: list[Job]
    problems: list[str]
    detection: FormatDetection | None = None


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
    if not _PLAIN_NAME_RE.match(name) or name in _GENERIC_NAMES:
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
    repeated_votes: int = 0
    repeated_name: str | None = None

    @property
    def entries(self) -> int:
        return self.system_votes + self.user_votes + self.undecided

    @property
    def unanimous(self) -> bool:
        """Every entry that had an opinion agreed, and none abstained."""
        return self.undecided == 0 and not (self.system_votes and self.user_votes)

    @property
    def rests_on_repetition(self) -> bool:
        """System format read in with no entry that says so on its own.

        No entry names an account: the user column was read into the first word
        after the schedule only because the same word is sitting there in more
        than one entry.  That is enough to prefer system format to a coin flip,
        and not enough to overrule a caller who said otherwise.
        """
        return self.system_format and self.system_votes == self.repeated_votes

    @property
    def guessed(self) -> bool:
        """The entries did not settle the format between them.

        Either they disagreed, or some of them said nothing, or the user column
        was read in on repetition alone.  The format decides where every command
        starts, so a scan whose comparison then comes up empty has a likelier
        explanation than an outage; :mod:`~cron_postmortem.scanner` says so.
        """
        return not self.unanimous or self.rests_on_repetition

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
    name, when it carries a script extension, when nothing follows it, when it
    is a command no distribution ships as an account, or when the word after it
    is an option or a shell operator and so belongs to it.  It votes *system*
    when field 6 is a stock account (``root``, ``www-data``, ``postgres``, …) or
    when the word after it can only be the start of a command of its own - a
    known command word or ``[``.

    Field 6 in front of a *path* is the case that cannot be read off one line:
    ``0 3 * * * backup /data`` is a system entry running ``/data`` as ``backup``
    and a per-user entry running ``backup`` on ``/data``, and the same is true of
    every local command with a file argument.  Such an entry therefore decides
    nothing by itself; it is believed only once the file corroborates it -
    another entry names an account outright, or the very same word is sitting in
    field 6 of a second entry, which is what a user column does and what a list
    of different commands does not.

    Everything else abstains (``*/5 * * * * backup archive``: ``backup`` is both
    a stock account and a plausible script, and nothing in the line can tell them
    apart).  A tie, including a file where every entry abstained, resolves to
    **user format**: it is what ``crontab -l`` emits and what the spool holds, so
    it is the format a collected crontab most often is.  The caller is told
    whenever the entries were not unanimous, and ``--crontab-format`` settles it
    by hand.
    """
    votes: list[str] = []
    repeatable: list[str] = []
    for line in _entry_lines(text):
        rest = _fields_after_schedule(line)
        if rest is None:
            continue
        vote = _vote(rest)
        votes.append(vote)
        if vote == _SYSTEM_IF_REPEATED:
            repeatable.append(rest[0])
    system_votes = votes.count(_SYSTEM_VOTE)
    user_votes = votes.count(_USER_VOTE)
    undecided = votes.count(_ABSTAIN)
    believed, name = _corroborated(repeatable, named_an_account=system_votes > 0)
    return FormatDetection(
        system_format=system_votes + believed > user_votes,
        system_votes=system_votes + believed,
        user_votes=user_votes,
        undecided=undecided + len(repeatable) - believed,
        repeated_votes=believed,
        repeated_name=name,
    )


def _corroborated(
    candidates: list[str], *, named_an_account: bool
) -> tuple[int, str | None]:
    """How many "could be a user column" entries the rest of the file backs up."""
    if not candidates:
        return 0, None
    if named_an_account:
        # Another entry names an account outright, so the file has a user column
        # and these entries are filling it.
        return len(candidates), None
    counts = Counter(candidates)
    name, count = counts.most_common(1)[0]
    if count < 2:
        return 0, None
    return sum(n for n in counts.values() if n > 1), name


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


def _vote(rest: list[str]) -> str:
    """How one entry reads: see :func:`detect_format` for the four verdicts."""
    candidate_user, after = rest[0], rest[1] if len(rest) > 1 else None
    if not _USERNAME_RE.match(candidate_user):
        return _USER_VOTE
    if after is None or candidate_user in _COMMAND_WORDS:
        return _USER_VOTE
    if _SCRIPT_SUFFIX_RE.search(candidate_user):
        # backup.sh, monitor.py: an account nobody has, a command everybody
        # writes, and the file argument that follows it says nothing.
        return _USER_VOTE
    if after.startswith("-") or after in _SHELL_OPERATORS:
        return _USER_VOTE
    if candidate_user in _SYSTEM_ACCOUNTS:
        # A name a distribution ships and no distribution ships a command for.
        return _SYSTEM_VOTE
    if not _PLAIN_NAME_RE.match(candidate_user):
        # A dot that is not a known extension: john.doe is a username, run.me is
        # a script, and this line cannot say which.
        return _ABSTAIN
    if after.startswith("[") or after in _COMMAND_WORDS:
        # Nothing runs `backup` with `flock` or `[` as its first argument, so
        # the command starts after field 6 and field 6 is the user column.
        return _SYSTEM_VOTE
    if _COMMAND_PATH_RE.match(after):
        # A path here is equally the command a system entry runs and the file a
        # per-user entry runs its command on; the file has to break the tie.
        return _SYSTEM_IF_REPEATED
    return _ABSTAIN


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
) -> CrontabRead:
    """Read one crontab file from disk.

    ``format_override`` is ``"auto"`` for a file whose format nothing but its
    content can say; discovery passes the format cron itself uses for the
    location (see :class:`DiscoveredCrontab`), as does ``--crontab-format``.
    ``trust_filename`` says the file was found in a spool directory, where the
    name is the owner's; see :func:`default_user_for`.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return CrontabRead([], [f"{path}: cannot read ({exc.strerror or exc})"])
    detection: FormatDetection | None = None
    overruled_by_user = False
    if format_override == "auto":
        detection = detect_format(text)
        system_format = detection.system_format
        if system_format and user_override is not None and detection.rests_on_repetition:
            # --crontab-user says who runs the entries, which is a question only
            # a user-format file leaves open, so asking it is also a statement
            # about the format - and it outranks a system reading that no entry
            # in the file backs up on its own.
            system_format = False
            overruled_by_user = True
    else:
        system_format = format_override == SYSTEM_FORMAT
    user = user_override or default_user_for(path, trust_filename=trust_filename)
    jobs, problems = parse_crontab(text, str(path), system_format, user)
    # The format decides where the command starts, so every comparison against
    # the log hangs on it; a file whose entries disagree, that says nothing
    # either way, or whose user column is a word this tool cannot vouch for,
    # must not have the guess made silently.
    if detection is not None and detection.entries:
        if overruled_by_user:
            problems.append(
                f"{path}: read as a user-format crontab because --crontab-user "
                f"was given; its entries would otherwise have been read as "
                f"system-format with {detection.repeated_name!r} as the user "
                "column - pass --crontab-format system if that is what it is"
            )
        elif detection.rests_on_repetition:
            problems.append(
                f"{path}: read as a system-format crontab because "
                f"{detection.repeated_name!r} sits in the user column of "
                f"{detection.repeated_votes} entries, but it is not a name this "
                "tool knows as an account; pass --crontab-format user if it is a "
                "command"
            )
        elif not detection.unanimous:
            problems.append(
                f"{path}: read as a {detection.name}-format crontab, but its "
                f"entries do not agree ({detection.system_votes} look system-format, "
                f"{detection.user_votes} user-format, {detection.undecided} could be "
                "either); pass --crontab-format if that is wrong"
            )
    if system_format and user_override is not None:
        # In this format the entries name their own user, so nothing was done
        # with the one that was asked for; say so rather than let the caller
        # believe the scan is looking for their user in the log.
        problems.append(
            f"{path}: --crontab-user {user_override!r} was not applied; the file "
            "is read as a system-format crontab, whose entries name the user "
            "themselves - pass --crontab-format user if that is wrong"
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
    return CrontabRead(jobs, problems, detection)


def discover_crontab_files() -> tuple[list[DiscoveredCrontab], list[str]]:
    """Every crontab file on this host that we are allowed to read.

    Spool directories are mode 0700 root, so a non-root scan legitimately cannot
    list them; that is reported as a problem rather than raised, so the rest of
    the scan still produces a report.
    """
    found: list[DiscoveredCrontab] = []
    problems: list[str] = []
    if SYSTEM_CRONTAB.is_file():
        found.append(DiscoveredCrontab(SYSTEM_CRONTAB, SYSTEM_FORMAT, False))
    directories = [(directory, SYSTEM_FORMAT, False) for directory in CRON_D_DIRS]
    directories += [(directory, USER_FORMAT, True) for directory in SPOOL_DIRS]
    for directory, file_format, filename_is_owner in directories:
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
            found.append(DiscoveredCrontab(entry, file_format, filename_is_owner))
    return found, problems
