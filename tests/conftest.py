from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# Every fixture describes the morning of 2026-09-18; pinning "now" keeps reports
# byte-for-byte reproducible and keeps syslog year inference deterministic.
NOW = datetime(2026, 9, 18, 12, 0, 0)


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture
def now() -> datetime:
    return NOW


# One command, running every minute for an hour, as the user named below: the
# shape of a capture where the crontab and the log do line up.  Each run is
# bracketed by the pam_unix session pair that gives it an end, on its own pid,
# because that is the only thing plain cron logs an end in.
def busy_cron_log(user: str, command: str = "/usr/local/bin/poll.sh") -> str:
    lines = []
    for minute in range(60):
        stamp = f"Sep 18 03:{minute:02d}:01"
        end = f"Sep 18 03:{minute:02d}:06"
        pid = 1000 + minute * 2
        lines.append(
            f"{stamp} h CRON[{pid}]: pam_unix(cron:session): "
            f"session opened for user {user}"
        )
        lines.append(f"{stamp} h CRON[{pid + 1}]: ({user}) CMD ({command})")
        lines.append(
            f"{end} h CRON[{pid}]: pam_unix(cron:session): "
            f"session closed for user {user}"
        )
    return "\n".join(lines) + "\n"
