"""Command line entry point."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__
from .model import FAILURE, MISSED, OVERLAP
from .report import to_json, to_markdown
from .scanner import DEFAULT_TOLERANCE, ScanOptions, scan

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_USAGE = 2
# Not one observed run could be attributed to a scheduled job: the report is
# full of missed runs that are artefacts of the comparison rather than an
# outage, so it must not come back as the same 1 a real outage does.
EXIT_NO_MATCH = 3

KINDS = (MISSED, OVERLAP, FAILURE)

_RELATIVE_RE = re.compile(
    r"^-?(?:(?P<days>\d+)d)?\s*(?P<hours>\d+h)?\s*(?P<minutes>\d+m)?\s*(?P<seconds>\d+s)?$"
)


class TimeArgError(ValueError):
    pass


def parse_when(text: str, now: datetime) -> datetime:
    """Accept an ISO timestamp or a relative offset such as ``24h`` / ``2d 6h``."""
    text = text.strip()
    if not text:
        raise TimeArgError("empty time value")
    if text.lower() == "now":
        return now
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        pass
    match = _RELATIVE_RE.match(text)
    if match and any(match.groupdict().values()):
        parts = {
            key: int(value.rstrip("dhms")) if value else 0
            for key, value in match.groupdict().items()
        }
        delta = timedelta(
            days=parts["days"],
            hours=parts["hours"],
            minutes=parts["minutes"],
            seconds=parts["seconds"],
        )
        if delta:
            return now - delta
    raise TimeArgError(
        f"cannot parse time {text!r}; use an ISO timestamp or an offset like 24h"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cron-postmortem",
        description=(
            "Reconstruct cron / systemd-timer job health from logs that already exist: "
            "missed runs, overlapping runs and real systemd failures."
        ),
    )
    parser.add_argument("--version", action="version", version=f"cron-postmortem {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="analyse schedules against logs")
    sources = scan_parser.add_argument_group("sources")
    sources.add_argument(
        "--crontab", action="append", default=[], metavar="PATH", type=Path,
        help="crontab file to analyse (repeatable)",
    )
    sources.add_argument(
        "--crontab-format", choices=("auto", "user", "system"), default="auto",
        help="whether crontab files carry a user column "
             "(default: auto, detected from the file's contents)",
    )
    sources.add_argument(
        "--crontab-user", metavar="USER", default=None,
        help="user to attribute user-format crontab entries to",
    )
    sources.add_argument(
        "--systemctl-show", action="append", default=[], metavar="PATH", type=Path,
        help="captured `systemctl show <units>` output (repeatable)",
    )
    sources.add_argument(
        "--log-file", action="append", default=[], metavar="PATH", type=Path,
        help="syslog or journalctl output to analyse (repeatable)",
    )
    sources.add_argument(
        "--journal", action="store_true",
        help="read logs from journalctl on this host",
    )
    sources.add_argument(
        "--discover", action="store_true",
        help="read this host's crontabs and systemd timers",
    )

    window = scan_parser.add_argument_group("window")
    window.add_argument(
        "--since", metavar="TIME", default=None,
        help="start of the analysis window (ISO timestamp or offset like 24h)",
    )
    window.add_argument(
        "--until", metavar="TIME", default=None,
        help="end of the analysis window (ISO timestamp, offset, or 'now')",
    )
    window.add_argument(
        "--tolerance", type=float, default=DEFAULT_TOLERANCE, metavar="SECONDS",
        help="how late a run may start before it counts as missed "
             f"(default: {int(DEFAULT_TOLERANCE)})",
    )
    window.add_argument(
        "--now", metavar="TIME", default=None,
        help="treat this ISO timestamp as the current time (for reproducible reports)",
    )

    output = scan_parser.add_argument_group("output")
    output.add_argument(
        "--format", choices=("json", "markdown", "both"), default="markdown",
        help="report format (default: markdown)",
    )
    output.add_argument(
        "--output", metavar="PATH", type=Path, default=None,
        help="write the report to a file instead of stdout",
    )
    output.add_argument(
        "--ignore", action="append", default=[], choices=KINDS, metavar="KIND",
        help=f"suppress a finding kind ({', '.join(KINDS)}); repeatable",
    )
    output.add_argument(
        "--exit-zero", action="store_true",
        help="always exit 0 when problems are found (does not silence exit 2 or 3, "
             "which say the report is not a verdict on the jobs)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        # Whole seconds keep the window in the report readable.
        clock = datetime.now().replace(microsecond=0)
        now = parse_when(args.now, clock) if args.now else clock
        since = parse_when(args.since, now) if args.since else None
        until = parse_when(args.until, now) if args.until else None
    except TimeArgError as exc:
        parser.error(str(exc))
        return EXIT_USAGE  # pragma: no cover - argparse exits

    offline = bool(args.crontab or args.systemctl_show or args.log_file)
    discover = args.discover or not offline
    use_journal = args.journal or (not offline and not args.log_file)

    if args.tolerance < 0:
        parser.error("--tolerance must not be negative")

    missing = [path for path in [*args.crontab, *args.systemctl_show, *args.log_file]
               if not path.exists()]
    if missing:
        for path in missing:
            print(f"cron-postmortem: no such file: {path}", file=sys.stderr)
        return EXIT_USAGE

    options = ScanOptions(
        crontab_paths=list(args.crontab),
        show_paths=list(args.systemctl_show),
        log_paths=list(args.log_file),
        crontab_format=args.crontab_format,
        crontab_user=args.crontab_user,
        since=since,
        until=until,
        tolerance=args.tolerance,
        discover=discover,
        use_journal=use_journal,
        ignore=frozenset(args.ignore),
        now=now,
    )
    result = scan(options)

    chunks = []
    if args.format in ("json", "both"):
        chunks.append(to_json(result))
    if args.format in ("markdown", "both"):
        chunks.append(to_markdown(result))
    text = "\n\n".join(chunk.rstrip() for chunk in chunks) + "\n"

    if args.output:
        try:
            args.output.write_text(text, encoding="utf-8")
        except OSError as exc:
            print(f"cron-postmortem: cannot write {args.output}: {exc}", file=sys.stderr)
            return EXIT_USAGE
    else:
        sys.stdout.write(text)

    # The report itself goes to stdout or to --output; a warning says the report
    # is not conclusive, which is exactly what gets lost when stdout is piped
    # into a file or a dashboard, so every warning is repeated on stderr.
    for warning in result.warnings:
        print(f"cron-postmortem: {warning.code}: {warning.message}", file=sys.stderr)

    if result.usage_error:
        # Not "your jobs are unhealthy" but "this scan could never have answered
        # the question", so it outranks --exit-zero: that flag mutes findings for
        # a monitoring check, it must not mute a broken invocation.
        return EXIT_USAGE
    if result.no_runs_matched:
        # 3 says "do not act on this report", so it may only be returned when
        # the unreconciled cron entries are the whole of it.  A systemd failure
        # in the same scan is a real outage the cron-matching problem cannot
        # have invented, and downgrading it to "scan problem" loses the page.
        # 1 wins where both apply; the warning is still printed and reported.
        if result.standing_findings and not args.exit_zero:
            return EXIT_PROBLEMS
        # Outranks --exit-zero, and for the same reason usage errors do: the
        # flag exists so a monitoring check does not page on findings, and
        # these findings are not real.  Swallowing this one is how a misread
        # crontab passes for a clean bill of health - or for an outage that
        # never happened.
        return EXIT_NO_MATCH
    if result.alerts and not args.exit_zero:
        return EXIT_PROBLEMS
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
