"""Rendering a :class:`~cron_postmortem.scanner.ScanResult` as JSON or Markdown."""

from __future__ import annotations

import json

from .model import FAILURE, MISSED, OVERLAP
from .scanner import ScanResult

_HEADINGS = {
    MISSED: "Missed runs",
    OVERLAP: "Overlapping runs",
    FAILURE: "Failures",
}


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

    if not result.findings:
        lines.append("No problems found.")
        lines.append("")
    else:
        for kind in (FAILURE, MISSED, OVERLAP):
            group = [finding for finding in result.findings if finding.kind == kind]
            if not group:
                continue
            lines.append(f"## {_HEADINGS[kind]} ({len(group)})")
            lines.append("")
            for finding in group:
                lines.append(f"- **{finding.job_id}** — {finding.message}")
            lines.append("")

    lines.append("## Jobs")
    lines.append("")
    lines.append("| Job | Source | Schedule | Expected | Observed | Problems |")
    lines.append("| --- | ------ | -------- | -------: | -------: | -------: |")
    for report in sorted(result.job_reports, key=lambda item: item.job.id):
        schedule = report.job.schedule if report.schedule_ok else f"{report.job.schedule} (?)"
        lines.append(
            f"| `{report.job.id}` | {report.job.source} | `{schedule}` "
            f"| {report.expected} | {len(report.runs)} | {len(report.findings)} |"
        )
    lines.append("")

    if result.diagnostics:
        lines.append(f"## Diagnostics ({len(result.diagnostics)})")
        lines.append("")
        for diagnostic in result.diagnostics:
            prefix = f"`{diagnostic.job_id}`: " if diagnostic.job_id else ""
            lines.append(f"- {prefix}{diagnostic.message}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
