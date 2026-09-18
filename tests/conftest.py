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
