# cron-postmortem

CLI that reconstructs cron/systemd-timer job health post-factum from existing logs
(missed runs, overlaps, real failures for systemd timers).

No agent, no wrapper, no daemon. You point it at the crontabs and the logs a box
*already* has, and it tells you which scheduled runs never happened, which ones ran on
top of each other, and which systemd units actually failed. It exits non-zero when it
finds something, so it drops straight into a monitoring check.

- **MISSED** — the schedule says a run was due, no run started within the tolerance.
- **OVERLAP** — a run started while the previous run of the same job was still going.
- **FAILURE** — a systemd unit exited non-zero, was killed, or is `ActiveState=failed`.

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
| `--crontab PATH` | Crontab file to analyse (repeatable). Format is detected from the path. |
| `--crontab-format {auto,user,system}` | Force whether crontabs carry a user column. |
| `--crontab-user USER` | User to attribute user-format entries to. |
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
| `0` | No problems found (or `--exit-zero`). |
| `1` | At least one missed run, overlap or failure. |
| `2` | Usage error: unreadable input, unwritable output. |

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

## Failures (1)

- **systemd:backup-db.timer** — run started 2026-09-18 04:00:03 failed (exit code 1, result exit-code); seen in the journal for backup-db.service

## Missed runs (2)

- **cron:root:/usr/local/bin/sync-metrics.sh** — scheduled for 2026-09-18 04:00:00 but no run started within 120s
- **systemd:metrics-push.timer** — scheduled for 2026-09-18 04:00:00 but no run started within 180s

## Overlapping runs (2)

- **systemd:logship.timer** — run started 2026-09-18 04:30:02 while the run from 2026-09-18 03:30:00 was still going (overlap 308s)
- **cron:root:/usr/local/bin/sync-metrics.sh** — run started 2026-09-18 05:30:02 while the run from 2026-09-18 05:00:01 was still going (overlap 158s)

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
    "failure": 1, "problems": 5, "diagnostics": 2
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
  "diagnostics": [ "..." ]
}
```

## How it works

| Question | Where the answer comes from |
| --- | --- |
| What was supposed to run? | `/etc/crontab`, `/etc/cron.d/*`, user crontabs, and `OnCalendar=` from `systemctl show <timer>`. |
| What did run? | `(user) CMD (...)` lines in syslog/journal, and `Starting`/`Finished`/`Succeeded` lines for systemd units. |
| How long did it run? | For cron, the `pam_unix(cron:session)` open/close pair that brackets the `CMD` line. For systemd, the start and terminal lines for the unit. |
| Did it fail? | `Main process exited, code=exited, status=N`, `Failed with result '...'`, and `ActiveState` / `Result` / `ExecMainStatus` from `systemctl show`. |

Supported log formats: traditional syslog (`Sep 18 03:00:01 host CRON[1234]: ...`) and
the journalctl renderings `short-iso`, `short-iso-precise` and `short-full`. Traditional
syslog carries no year, so it is inferred from `--now` with December→January rollover.

Timestamps are compared as **local wall-clock time**, because that is what cron and
systemd timers fire against; a UTC offset in a log line is dropped rather than converted.

If the requested window is wider than the log actually covers, it is clamped to the
log's span and a diagnostic says so — otherwise every run from before the log started
would look missed.

## Known limitations

- **Plain cron exit codes are not recoverable.** syslog does not carry them, so only
  missed/overlap detection applies to cron jobs. This is a limit of the data source, not
  a bug; use a systemd timer if you need exit-code visibility.
- Cron run durations need `pam_unix(cron:session)` lines. Without them, runs have no end
  and overlaps cannot be detected; the report says so in the diagnostics.
- `OnCalendar=` support covers the shorthands, weekday filters, lists, `a..b` ranges and
  `/step` repetitions. `~` (last-day) expressions and per-second schedules are reported
  as diagnostics rather than silently ignored.
- Monotonic timers (`OnBootSec=`, `OnUnitActiveSec=`) have no calendar, so they cannot be
  checked for missed runs.
- A timer with several `OnCalendar=` lines is one job: systemd ORs them, so the expected
  occurrences are the union and a failure is reported once, not once per line.
- `Persistent=yes` catch-up runs after a boot are reported at the time they actually ran,
  which may be well after the scheduled time.
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
