# Why this exists

cron-postmortem was not designed from a feature list. It was designed from four
posts by people who run cron on their own boxes and cannot tell whether it is
working. Each section below is one of those posts: the link, when it was written,
where, by whom, what they said, and what this tool does about it.

Every quote is copied verbatim from the source and is at most 25 words. Each one
also has a matching scenario test in
[`tests/test_scenarios.py`](../tests/test_scenarios.py), which rebuilds that
person's crontab and log and asserts the tool actually finds what they were
missing — so the claims on this page are checked by CI rather than asserted.

## 1. "It works until it doesn't and debugging failures is a pain"

- **Link:** https://www.reddit.com/r/homelab/comments/1w60v3l/best_self_hosted_too_to_automate_recurring_tasks/
- **Date:** 2026-09-03
- **Subreddit:** r/homelab
- **Author:** Tiny_Bookkeeper_3156
- **Quote:** "handling with a mix of cron jobs and bash scripts. It works until it doesn't and debugging failures is a pain."

**What cron-postmortem does about it:** it reconstructs each run's start and end
from the `pam_unix(cron:session)` pair that brackets the `CMD` line, so the
classic invisible failure — a script that hung and had the next three invocations
pile on top of it — is reported as an `OVERLAP` with the exact pair of runs and
the seconds they shared, from logs that were already on disk.

## 2. "worked until they didn't, found out 3 months later"

- **Link:** https://www.reddit.com/r/homelab/comments/1q9f4xp/built_stacksnap_because_i_got_tired_of_corrupted/
- **Date:** 2026-01-10
- **Subreddit:** r/homelab
- **Author:** Brilliant_Length_765
- **Quote:** "Bash scripts + cron jobs - worked until they didn't, found out 3 months later"

**What cron-postmortem does about it:** an occurrence the schedule promised with
no run against it in the log is reported as missed, so a job that quietly stopped
comes back as every occurrence since. The window's start is clamped to the log's
first entry — runs from before the log begins are unknowable, not missed — and
with no `--until` the end defaults to the log's last entry, so one scan of the
syslog already on disk names the first occurrence that never ran (the day the job
actually stopped) and every one after it, while the other jobs in the same
crontab come back clean: that contrast is what separates "this job died" from
"cron died". The end is *not* clamped to the log, so `--until now` checks the
silent tail past the last log line as well and counts the occurrences in it as
missed too.

## 3. "i need to manually create each cron job to check everything"

- **Link:** https://www.reddit.com/r/homelab/comments/1ow6cua/monitoring_software/
- **Date:** 2025-11-13
- **Subreddit:** r/homelab
- **Author:** Fili96
- **Quote:** "healthchecks.io... seems a bit too simple (i need to manually create each cron job to check everything...)"

**What cron-postmortem does about it:** nothing is registered per job. The
crontab is the job list, so one invocation checks every entry in it — including
the ones nobody would have thought to instrument — against the log cron already
writes. No ping URL, no wrapper, no edit to a single crontab line.

## 4. "Healthchecks — cron job monitoring" as a needed stack component

- **Link:** https://www.reddit.com/r/homelab/comments/1vaqhsf/before_after_of_my_homelab_selfhosted_stack/
- **Date:** 2026-07-30
- **Subreddit:** r/homelab
- **Author:** mr_Pepper762
- **Quote:** Lists "Healthchecks — cron job monitoring" as a needed stack component

  (The source list records this entry as a description of the post rather than as
  a quotation; the quoted fragment inside it is copied verbatim.)

**What cron-postmortem does about it:** cron job monitoring without adding a
service to the stack. It is a CLI with no server, no database and no daemon, it
reads files and exits `1` when it finds a problem and `0` when it does not, so
the monitoring you already run can call it directly.

## Sources

All four items come from the `cron-postmortem` section of the project's source
list (r/homelab, `search.rss q="cron job failed notification" restrict_sr=1
sort=relevance t=year`), each marked `VERIFIED-RSS` there. Links, dates,
subreddits, authors and quotes on this page are copied from that list unchanged.
