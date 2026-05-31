from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


MARKET_TZ = ZoneInfo("America/New_York")
REGULAR_OPEN_MINUTE = (9 * 60) + 30
REGULAR_CLOSE_MINUTE = 16 * 60


def trading_day_key(timestamp_ms: int) -> str:
    """US/Eastern calendar date for daily risk and journal buckets."""
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=MARKET_TZ).date().isoformat()


def is_regular_market_time(timestamp_ms: int | None) -> bool:
    if timestamp_ms is None:
        return False

    current = datetime.fromtimestamp(timestamp_ms / 1000, tz=MARKET_TZ)
    if current.weekday() >= 5:
        return False

    minute = (current.hour * 60) + current.minute
    return REGULAR_OPEN_MINUTE <= minute < REGULAR_CLOSE_MINUTE


def minutes_until_regular_close(timestamp_ms: int | None) -> int | None:
    if timestamp_ms is None or not is_regular_market_time(timestamp_ms):
        return None

    current = datetime.fromtimestamp(timestamp_ms / 1000, tz=MARKET_TZ)
    minute = (current.hour * 60) + current.minute
    return REGULAR_CLOSE_MINUTE - minute


def should_flatten_before_close(timestamp_ms: int | None, threshold_minutes: int) -> bool:
    if threshold_minutes <= 0:
        return False

    minutes_remaining = minutes_until_regular_close(timestamp_ms)
    return minutes_remaining is not None and minutes_remaining <= threshold_minutes
