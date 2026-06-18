from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

from candle import SymbolState
from market_hours import MARKET_TZ
from models import Bar


MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


@dataclass(frozen=True)
class OpeningSessionMemory:
    sessions: int
    long_repeat_count: int
    short_repeat_count: int
    long_fade_count: int
    short_bounce_count: int
    latest_opening_return_pct: float | None
    latest_close_from_open_pct: float | None

    def has_long_repeat(self, min_repeat_days: int) -> bool:
        return self.long_repeat_count >= max(1, min_repeat_days)

    def has_short_repeat(self, min_repeat_days: int) -> bool:
        return self.short_repeat_count >= max(1, min_repeat_days)

    def long_score(self) -> float:
        return self.long_repeat_count + (self.long_fade_count * 0.5) - (self.short_repeat_count * 0.75)

    def short_score(self) -> float:
        return self.short_repeat_count + (self.short_bounce_count * 0.5) - (self.long_repeat_count * 0.75)

    def summary(self) -> str:
        latest = ""
        if self.latest_opening_return_pct is not None:
            latest = f" latest_open={self.latest_opening_return_pct:.2%}"
        return (
            f"opening_memory long={self.long_repeat_count} fade={self.long_fade_count} "
            f"short={self.short_repeat_count} bounce={self.short_bounce_count}{latest}"
        )


@dataclass(frozen=True)
class _SessionMetrics:
    session_date: date
    open: float
    opening_high: float
    opening_low: float
    opening_close: float
    close: float

    @property
    def opening_return_pct(self) -> float:
        return (self.opening_close - self.open) / self.open if self.open > 0 else 0.0

    @property
    def opening_high_pct(self) -> float:
        return (self.opening_high - self.open) / self.open if self.open > 0 else 0.0

    @property
    def opening_low_pct(self) -> float:
        return (self.opening_low - self.open) / self.open if self.open > 0 else 0.0

    @property
    def close_from_open_pct(self) -> float:
        return (self.close - self.open) / self.open if self.open > 0 else 0.0

    @property
    def fade_from_opening_high_pct(self) -> float:
        if self.opening_high <= 0:
            return 0.0
        return (self.opening_high - self.close) / self.opening_high

    @property
    def bounce_from_opening_low_pct(self) -> float:
        if self.opening_low <= 0:
            return 0.0
        return (self.close - self.opening_low) / self.opening_low


def opening_session_memory(
    state: SymbolState,
    *,
    lookback_days: int,
    opening_minutes: int,
    min_impulse_pct: float,
    fade_pct: float,
    max_close_loss_pct: float,
    as_of_ms: int | None = None,
) -> OpeningSessionMemory:
    as_of_ms = as_of_ms or state.last_event_ms
    current_date = None
    if as_of_ms is not None:
        current_date = datetime.fromtimestamp(as_of_ms / 1000, tz=MARKET_TZ).date()

    sessions = _regular_session_metrics(
        list(state.bars),
        opening_minutes=max(1, opening_minutes),
        current_date=current_date,
    )
    sessions = sessions[-max(1, lookback_days) :]

    long_repeat_count = 0
    short_repeat_count = 0
    long_fade_count = 0
    short_bounce_count = 0
    latest_opening_return = None
    latest_close_from_open = None
    for item in sessions:
        if item.opening_return_pct >= min_impulse_pct or item.opening_high_pct >= min_impulse_pct:
            long_repeat_count += 1
            if item.fade_from_opening_high_pct >= fade_pct and item.close_from_open_pct >= -max_close_loss_pct:
                long_fade_count += 1
        if item.opening_return_pct <= -min_impulse_pct or item.opening_low_pct <= -min_impulse_pct:
            short_repeat_count += 1
            if item.bounce_from_opening_low_pct >= fade_pct and item.close_from_open_pct <= max_close_loss_pct:
                short_bounce_count += 1
        latest_opening_return = item.opening_return_pct
        latest_close_from_open = item.close_from_open_pct

    return OpeningSessionMemory(
        sessions=len(sessions),
        long_repeat_count=long_repeat_count,
        short_repeat_count=short_repeat_count,
        long_fade_count=long_fade_count,
        short_bounce_count=short_bounce_count,
        latest_opening_return_pct=latest_opening_return,
        latest_close_from_open_pct=latest_close_from_open,
    )


def _regular_session_metrics(
    bars: list[Bar],
    *,
    opening_minutes: int,
    current_date: date | None,
) -> list[_SessionMetrics]:
    by_day: dict[date, list[Bar]] = {}
    for bar in bars:
        start = datetime.fromtimestamp(bar.start_ms / 1000, tz=MARKET_TZ)
        if start.time() < MARKET_OPEN or start.time() >= MARKET_CLOSE:
            continue
        if current_date is not None and start.date() >= current_date:
            continue
        by_day.setdefault(start.date(), []).append(bar)

    out: list[_SessionMetrics] = []
    for session_date in sorted(by_day):
        day_bars = sorted(by_day[session_date], key=lambda item: item.start_ms)
        opening_bars = [
            bar
            for bar in day_bars
            if _minutes_from_open(bar.end_ms) is not None and 0 < _minutes_from_open(bar.end_ms) <= opening_minutes
        ]
        if not day_bars or not opening_bars:
            continue
        session_open = day_bars[0].open
        if session_open <= 0:
            continue
        out.append(
            _SessionMetrics(
                session_date=session_date,
                open=session_open,
                opening_high=max(bar.high for bar in opening_bars),
                opening_low=min(bar.low for bar in opening_bars),
                opening_close=opening_bars[-1].close,
                close=day_bars[-1].close,
            )
        )
    return out


def _minutes_from_open(timestamp_ms: int) -> int | None:
    current = datetime.fromtimestamp(timestamp_ms / 1000, tz=MARKET_TZ)
    if current.time() < MARKET_OPEN:
        return None
    return (current.hour * 60 + current.minute) - (MARKET_OPEN.hour * 60 + MARKET_OPEN.minute)
