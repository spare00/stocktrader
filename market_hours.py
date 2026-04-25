from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


MARKET_TZ = ZoneInfo("America/New_York")
REGULAR_OPEN_MINUTE = (9 * 60) + 30
REGULAR_CLOSE_MINUTE = 16 * 60


def is_regular_market_time(timestamp_ms: int | None) -> bool:
    if timestamp_ms is None:
        return False

    current = datetime.fromtimestamp(timestamp_ms / 1000, tz=MARKET_TZ)
    if current.weekday() >= 5:
        return False

    minute = (current.hour * 60) + current.minute
    return REGULAR_OPEN_MINUTE <= minute < REGULAR_CLOSE_MINUTE
