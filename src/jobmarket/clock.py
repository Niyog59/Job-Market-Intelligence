"""Time source. Code takes a clock instead of calling datetime.now() so tests can control
time (for example, to check retries scheduled hours apart without waiting)."""

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]


def system_clock() -> datetime:
    return datetime.now(UTC)
