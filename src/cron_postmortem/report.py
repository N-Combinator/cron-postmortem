"""Rendering a :class:`~cron_postmortem.scanner.ScanResult` as JSON or Markdown."""

from __future__ import annotations

import json
import re

from .model import FAILURE, MISSED, OVERLAP
from .scanner import ScanResult

_HEADINGS = {
    MISSED: "Missed runs",
    OVERLAP: "Overlapping runs",
    FAILURE: "Failures",
}

_BACKTICKS = re.compile(r"`+")


def _code(value: object) -> str:
    """A code span that survives the backticks in a cron command.

    ``0 3 * * * echo `date` >> /log`` would otherwise close the span early and
    spill the rest of the command into the table as markup.  The fence is one
    backtick longer than the longest run inside the text, and content touching a
    backtick is padded, exactly as CommonMark prescribes.
    """
    text = str(value)
    if not text:
        return ""
    fence = "`" * (max((len(run) for run in _BACKTICKS.findall(text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _cell(value: object) -> str:
    """Escape a value for a Markdown table cell.

    A raw ``|`` ends the cell — even inside a code span, which is where cron
    commands put theirs — and a newline ends the row, so a single piped command
    shears the whole table apart.  ``\\|`` is the one escape GFM honours here.
    """
    return str(value).replace("|", r"\|").replace("\r", " ").replace("\n", " ")


def _code_cell(value: object) -> str:
    return _cell(_code(value))


def to_json(result: ScanResult, indent: int | None = 2) -> str:
    return json.dumps(result.as_dict(), indent=indent, sort_keys=False)


def to_markdown(result: ScanResult) -> str:
    counts = result.counts()
    lines: list[str] = []
    lines.append("# cron-postmortem report")
    lines.append("")
    lines.append(
        f"Window `{result.window_start.isoformat(sep=' ')}` → "
        f"`{result.window_end.isoformat(sep=' ')}` "
        f"(tolerance {int(result.tolerance)}s), generated "
        f"{result.generated_at.isoformat(sep=' ', timespec='seconds')}."
    )
    lines.append("")
    lines.append("| Jobs | Runs | Missed | Overlaps | Failures |")
    lines.append("| ---: | ---: | -----: | -------: | -------: |")
    lines.append(
        f"| {len(result.job_reports)} "
        f"| {sum(len(report.runs) for report in result.job_reports)} "
        f"| {counts[MISSED]} | {counts[OVERLAP]} | {counts[FAILURE]} |"
    )
    lines.append("")
    lines.append(
        f"Log lines read {result.lines_total}, understood {result.lines_parsed}."
    )
    lines.append("")

    if result.warnings:
        lines.append(f"## Warnings ({len(result.warnings)})")
        lines.append("")
        for warning in result.warnings:
            lines.append(f"- {_code(warning.code)} — {warning.message}")
        lines.append("")

    if not result.findings and not result.warnings:
        lines.append("No problems found.")
        lines.append("")
    elif not result.findings:
        lines.append("No job problems found, but the scan is not conclusive.")
        lines.append("")
    else:
        for kind in (FAILURE, MISSED, OVERLAP):
            group = [finding for finding in result.findings if finding.kind == kind]
            if not group:
                continue
            lines.append(f"## {_HEADINGS[kind]} ({len(group)})")
            lines.append("")
            for finding in group:
                lines.append(f"- {_code(finding.job_id)} — {finding.message}")
            lines.append("")

    lines.append("## Jobs")
    lines.append("")
    lines.append("| Job | Source | Schedule | Expected | Observed | Problems |")
    lines.append("| --- | ------ | -------- | -------: | -------: | -------: |")
    for report in sorted(result.job_reports, key=lambda item: item.job.id):
        schedule = report.job.schedule if report.schedule_ok else f"{report.job.schedule} (?)"
        lines.append(
            f"| {_code_cell(report.job.id)} | {_cell(report.job.source)} "
            f"| {_code_cell(schedule)} "
            f"| {report.expected} | {len(report.runs)} | {len(report.findings)} |"
        )
    lines.append("")

    if result.diagnostics:
        lines.append(f"## Diagnostics ({len(result.diagnostics)})")
        lines.append("")
        for diagnostic in result.diagnostics:
            prefix = f"{_code(diagnostic.job_id)}: " if diagnostic.job_id else ""
            lines.append(f"- {prefix}{diagnostic.message}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
