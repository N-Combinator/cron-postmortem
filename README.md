# cron-postmortem

CLI that reconstructs cron/systemd-timer job health post-factum from existing logs
(missed runs, overlaps, real failures for systemd timers).

No agent, no wrapper, no daemon. You point it at the crontabs and the logs a box
*already* has, and it tells you which scheduled runs never happened, which ones ran on
top of each other, and which systemd units actually failed. It exits non-zero when it
finds something, so it drops straight into a monitoring check.

- **MISSED** — the schedule says a run was due, no run started within the tolerance.
- **OVERLAP** — a run started while the previous run of the same job was still going.
- **FAILURE** — a systemd unit run that systemd itself calls a failure: `Result=` other
  than `success`, or `ActiveState=failed`.

It also refuses to report success when it checked nothing. A scan that found no
schedules, or that understood not one line of the log it was given, prints a
**warning** and exits non-zero: "no problems" and "nothing was looked at" have to be
different answers to a monitoring check.

Zero runtime dependencies, Python 3.10+, Linux.

## Install

```console
$ pip install cron-postmortem
```

From a checkout:

```console
$ pip install -e ".[dev]"
```

## Usage

Live mode reads this host's crontabs, its systemd timers, and `journalctl`:

```console
$ cron-postmortem scan --since 24h
```

Offline mode reads files you collected somewhere else — this is also how the test suite
runs, so a scan is fully reproducible without touching a live system:

```console
$ cron-postmortem scan \
    --crontab /etc/crontab \
    --crontab /var/spool/cron/crontabs/root \
    --systemctl-show captured-systemctl-show.txt \
    --log-file /var/log/syslog \
    --format both
```

Any of `--crontab`, `--systemctl-show` and `--log-file` switches the scan to offline
mode; add `--discover` or `--journal` to mix in live sources.

Collect the offline inputs like this:

```console
$ systemctl list-units --type=timer --all --no-legend --plain \
    | awk '{print $1; sub(/\.timer$/, ".service", $1); print $1}' \
    | xargs systemctl show --no-pager > captured-systemctl-show.txt
$ journalctl --since "24 hours ago" -o short-iso -t CRON -t cron -t crond > cron.log
$ journalctl --since "24 hours ago" -o short-iso -u backup-db.service >> cron.log
```

### Options

| Option | Meaning |
| --- | --- |
| `--crontab PATH` | Crontab file to analyse (repeatable). Format is detected from the path; a user-format file is attributed to `root` unless the filename is a bare username. |
| `--crontab-format {auto,user,system}` | Force whether crontabs carry a user column. |
| `--crontab-user USER` | User to attribute user-format entries to, overriding the filename. |
| `--systemctl-show PATH` | Captured `systemctl show <units>` output (repeatable). |
| `--log-file PATH` | syslog or `journalctl` output (repeatable). |
| `--journal` / `--discover` | Read logs / schedules from this host. |
| `--since`, `--until` | ISO timestamp or offset (`24h`, `2d 6h`, `now`). |
| `--tolerance SECONDS` | How late a run may start before it counts as missed (default 120). |
| `--now TIME` | Pin "current time" for reproducible reports. |
| `--format {json,markdown,both}` | Report format (default `markdown`). |
| `--output PATH` | Write the report to a file instead of stdout. |
| `--ignore {missed,overlap,failure}` | Suppress a finding kind (repeatable). |
| `--exit-zero` | Always exit 0, even when problems are found. |

### Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | Nothing wrong and the scan was conclusive (or `--exit-zero`). |
| `1` | At least one missed run, overlap or failure — or a warning that the scan could not check what it was asked to. |
| `2` | Usage error: unreadable input, unwritable output, or arguments that leave nothing to scan. |

### Warnings

A warning means the report is not conclusive. It is listed under `## Warnings` in the
Markdown report, appears in `warnings` in the JSON, and counts towards exit code 1 —
`--exit-zero` silences the exit code, not the warning.

| Code | Raised when | Exit |
| --- | --- | ---: |
| `no-schedules` | Not one crontab entry or timer was found, so every detector had nothing to run against. | `1` |
| `no-log-lines` | No log source was given, or nothing in it parsed as a cron/systemd log line — check the format and the syslog identifier. | `1` |
| `unsupported-timezone` | An `OnCalendar=` value names a timezone (see the limitations); that timer is excluded from missed-run detection. | `1` |
| `no-runs-matched` | The log is full of cron runs and not one of them belongs to a crontab entry — usually the wrong user (see below) or a log from another host. | `1` |
| `empty-window` | The tolerance is longer than the window it applies to, so no scheduled run could be judged. | `2` / `1` |

`empty-window` is the one warning that can exit `2`. An occurrence is only judged once
its tolerance has fully elapsed, so the last moment a scan can rule on is `--until`
minus the tolerance; when that falls before `--since` every job expects nothing and the
report would otherwise read "No problems found" for a scan that checked nothing.
`--since 10m --tolerance 3600`, an `--until` older than `--since` and a log too short
for the tolerance all land here. Those are the caller's arguments contradicting each
other, so they exit `2` and `--exit-zero` does *not* silence them — that flag mutes
findings for a monitoring check, not a broken invocation. The same warning at exit `1`
means one *timer's* own slack (`AccuracySec` + `RandomizedDelaySec`, added to
`--tolerance`) outlasted an otherwise usable window: only that job went unchecked.

`no-runs-matched` is the other half of the same idea. A cron run is attributed to a
crontab entry by `(user, command)` — the log line carries nothing else — so an entry
read as belonging to the wrong user can never match, and every one of its occurrences
comes back as a missed run. That is why a user-format file passed to `--crontab` is
attributed to `root` unless its name is a bare username: a collected crontab is usually
named after the capture (`web01.crontab`, `root.txt`) rather than after its owner, and
the name is only taken as the owner for the files found in a spool directory, where cron
itself reads it that way. Pass `--crontab-user` when the entries belong to somebody
else; a diagnostic names the user that was picked whenever the filename was not used.

The summary line `Log lines read N, understood M` (`log_lines_total` /
`log_lines_parsed` in JSON) is there for the in-between case: a log source that is only
partly understood still scans, and those two numbers are how you notice.

## Example output

Against the fixtures in this repository:

```console
$ cron-postmortem scan \
    --crontab tests/fixtures/etc/crontab \
    --systemctl-show tests/fixtures/systemctl-show.txt \
    --log-file tests/fixtures/syslog-cron.log \
    --log-file tests/fixtures/journal-systemd.log
```

```markdown
# cron-postmortem report

Window `2026-09-18 02:50:00` → `2026-09-18 05:59:40` (tolerance 120s), generated 2026-09-18 12:00:00.

| Jobs | Runs | Missed | Overlaps | Failures |
| ---: | ---: | -----: | -------: | -------: |
| 7 | 17 | 2 | 2 | 1 |

Log lines read 63, understood 62.

## Failures (1)

- `systemd:backup-db.timer` — run started 2026-09-18 04:00:03 failed (exit code 1, result exit-code); seen in the journal for backup-db.service

## Missed runs (2)

- `cron:root:/usr/local/bin/sync-metrics.sh` — scheduled for 2026-09-18 04:00:00 but no run started within 120s
- `systemd:metrics-push.timer` — scheduled for 2026-09-18 04:00:00 but no run started within 180s

## Overlapping runs (2)

- `systemd:logship.timer` — run started 2026-09-18 04:30:02 while the run from 2026-09-18 03:30:00 was still going (overlap 308s)
- `cron:root:/usr/local/bin/sync-metrics.sh` — run started 2026-09-18 05:30:02 while the run from 2026-09-18 05:00:01 was still going (overlap 158s)

## Jobs

| Job | Source | Schedule | Expected | Observed | Problems |
| --- | ------ | -------- | -------: | -------: | -------: |
| `cron:root:/usr/local/bin/backup.sh` | cron | `0 3 * * *` | 1 | 1 | 0 |
| `cron:root:/usr/local/bin/sync-metrics.sh` | cron | `*/30 * * * *` | 6 | 5 | 2 |
| `cron:www-data:/usr/local/bin/rotate-cache.sh` | cron | `17 * * * *` | 3 | 3 | 0 |
| `systemd:backup-db.timer` | systemd | `*-*-* 04:00:00` | 1 | 1 | 1 |
| `systemd:certbot.timer` | systemd | `*-*-* 03:30:00` | 1 | 2 | 0 |
| `systemd:logship.timer` | systemd | `*-*-* *:30:00` | 3 | 3 | 1 |
| `systemd:metrics-push.timer` | systemd | `*-*-* *:00:00` | 3 | 2 | 1 |

## Diagnostics (2)

- boot-cleanup.timer: no OnCalendar= (monotonic timer such as OnBootSec is not schedulable from a calendar)
- 2 cron run(s) in the log matched no known crontab entry (2 distinct): (root) /usr/lib/php/sessionclean, (root) /usr/local/bin/heartbeat.sh
```

`--format json` produces the same data as a machine-readable document:

```json
{
  "tool": "cron-postmortem",
  "version": "0.1.0",
  "generated_at": "2026-09-18T12:00:00",
  "window": {
    "start": "2026-09-18T02:50:00",
    "end": "2026-09-18T05:59:40",
    "tolerance_seconds": 120.0
  },
  "summary": {
    "jobs": 7, "runs": 17, "missed": 2, "overlap": 2,
    "failure": 1, "problems": 5, "warnings": 0, "diagnostics": 2,
    "log_lines_total": 63, "log_lines_parsed": 62
  },
  "jobs": [
    {
      "id": "cron:root:/usr/local/bin/backup.sh",
      "source": "cron",
      "schedule": "0 3 * * *",
      "schedules": ["0 3 * * *"],
      "origin": "tests/fixtures/etc/crontab:7",
      "user": "root",
      "command": "/usr/local/bin/backup.sh",
      "schedule_understood": true,
      "expected_runs": 1,
      "observed_runs": [
        {
          "start": "2026-09-18T03:00:01",
          "end": "2026-09-18T03:41:12",
          "duration_seconds": 2471.0,
          "pid": 10011,
          "exit_code": null,
          "result": null
        }
      ],
      "findings": []
    }
  ],
  "findings": [ "..." ],
  "warnings": [ "..." ],
  "diagnostics": [ "..." ]
}
```

## How it works

| Question | Where the answer comes from |
| --- | --- |
| What was supposed to run? | `/etc/crontab`, `/etc/cron.d/*`, user crontabs, and `OnCalendar=` from `systemctl show <timer>`. |
| What did run? | `(user) CMD (...)` lines in syslog/journal, and `Starting`/`Started`/`Finished`/`Succeeded` lines for systemd units. `Starting X...` opens a run and `Started X.` reports that its start-up finished, so a unit logging both is still one run. |
| How long did it run? | For cron, the `pam_unix(cron:session)` open/close pair that brackets the `CMD` line. For systemd, the start and terminal lines for the unit. |
| Did two runs collide? | Every *pair* of runs of the same job that was alive at the same time, not just neighbours in start order — one run hung for three hours is reported against each of the runs that started underneath it, and the reported overlap is the time the two actually ran side by side. |
| Did it fail? | `Main process exited, code=exited, status=N`, `Failed with result '...'`, and `ActiveState` / `Result` from `systemctl show`. systemd's own verdict wins over the raw exit status, so a unit with `SuccessExitStatus=3` that exits 3 is healthy; `ExecMainStatus` decides only when the capture carries no `Result=`. |

Supported log formats: traditional syslog (`Sep 18 03:00:01 host CRON[1234]: ...`) and
the journalctl renderings `short-iso`, `short-iso-precise` and `short-full`. Traditional
syslog carries no year, so it is inferred from `--now` with December→January rollover.
That inference reads the order of the lines, so **each `--log-file` is dated on its
own** before the sources are merged: `--log-file /var/log/syslog --log-file
/var/log/syslog.1` reads a rotated pair in the usual newest-first order, and gluing the
two together would make the seam look like a rollover and push a whole file a year out.
Runs are reconstructed from the merged, time-ordered stream afterwards, so a job whose
`pam_unix` session straddles the rotation still gets its end.

A rollover is only read into a backwards step that spans most of a year — the distance
from December 31st to January 1st. A *small* backwards step is a line written out of
order, which is ordinary: a central syslog collecting hosts whose clocks differ across
midnight, a backwards NTP correction, a VM snapshot restore. Reading one of those as a
new year would date every earlier line twelve months out, and since the window starts at
the log's first entry, one such line would fill the report with missed runs that never
happened. Lines that carry their own year (journalctl's ISO formats, and `--journal`)
are never affected by any of this.

Cron is recognised under the identifiers `cron` and `crond` in either case — Debian logs
as `CRON`, cronie as `CROND` — and `--journal` asks `journalctl -t` for all four
spellings, so the live query and the file parser cover exactly the same hosts.

Timestamps are compared as **local wall-clock time**, because that is what cron and
systemd timers fire against; a UTC offset in a log line is dropped rather than converted.
For the same reason a schedule that names another timezone is refused rather than
guessed at — see the limitations below.

If the requested window starts before the log does, the **start** is clamped to the
log's first entry and a diagnostic says so — otherwise every run from before the log
was rotated would look missed. The **end** is never clamped: silence at the end of the
window is exactly the outage this tool is for (dead cron daemon, box down, logging
broken), so those occurrences are reported as missed and move the exit code. A
diagnostic points out where the log stopped, so one silent tail reads as one outage
rather than N unrelated failures.

Without `--until` (and without `--journal`) the end still *defaults* to the log's last
entry, which keeps an offline scan of a stand-alone log file reproducible. Pass
`--until now` to check the tail as well.

The tolerance is subtracted from the window's end before anything is judged, so a
window shorter than the tolerance leaves nothing to check at all; that is refused with
the `empty-window` warning and exit code 2 rather than reported as a clean run.

The window governs all three detectors, not just the missed ones: runs that started
outside it are left out of the report, so `--since 1h` cannot surface an overlap from
yesterday. The lower edge is widened by the tolerance, because a run that started a
minute before `--since` is exactly the run that answers the first occurrence inside it.
A unit that is *currently* `failed` from a run outside the window is reported as a
diagnostic rather than a finding, so it does not move the exit code of a window it did
not happen in.

## Known limitations

- **Plain cron exit codes are not recoverable.** syslog does not carry them, so only
  missed/overlap detection applies to cron jobs. This is a limit of the data source, not
  a bug; use a systemd timer if you need exit-code visibility.
- Two crontab lines running the same command as the same user are analysed as **one**
  job with both schedules, because a `CMD` log line carries only the user and the
  command and cannot be attributed to one line rather than the other.
- Cron run durations need `pam_unix(cron:session)` lines. Without them, runs have no end
  and overlaps cannot be detected; the report says so in the diagnostics.
- `OnCalendar=` support covers the shorthands, weekday filters, lists, `a..b` ranges and
  `/step` repetitions. `~` (last-day) expressions and per-second schedules are reported
  as diagnostics rather than silently ignored.
- **Timezones in schedules are not supported.** An `OnCalendar=` value with a trailing
  zone (`*-*-* 09:00:00 Europe/Berlin`, `daily UTC`) is refused: every timestamp here is
  naive local wall-clock time, and converting the schedule would mean routing it through
  the zone of whichever machine happens to run the scan, which need not be the machine
  the log came from. That timer is excluded from missed-run detection — overlaps and
  failures are still reported for it — and the scan raises the `unsupported-timezone`
  warning and exits non-zero rather than quietly comparing against the wrong hour.
  Likewise, cronie's `CRON_TZ=` in a crontab is not applied; the entries below it are
  read as local time and a diagnostic says so.
- Monotonic timers (`OnBootSec=`, `OnUnitActiveSec=`) have no calendar, so they cannot be
  checked for missed runs.
- A timer with several `OnCalendar=` lines is one job: systemd ORs them, so the expected
  occurrences are the union and a failure is reported once, not once per line.
- `Persistent=yes` catch-up runs after a boot are reported at the time they actually ran,
  which may be well after the scheduled time.
- A schedule that fires more often than 200 000 times across the requested window (a
  per-minute cron entry with `--since` a year back) is reported as a diagnostic instead of
  being enumerated; narrow the window to check it.
- Linux only. Out of scope for v0.1: wrapping jobs, modifying schedules, push-style
  alerting.

## Development

```console
$ python -m venv .venv && . .venv/bin/activate
$ pip install -e ".[dev]"
$ ruff check .
$ pytest
```

The test suite runs entirely off the fixtures in `tests/fixtures/` and never shells out
to `systemctl` or `journalctl`.

## License

MIT — see [LICENSE](LICENSE).
