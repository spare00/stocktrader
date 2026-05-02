from datetime import datetime, time
from statistics import median
from typing import Any, ClassVar

from candle import SymbolState
from config import Settings
from env_vars import EnvSpec, float_env, int_env, optional_int_env
from market_hours import MARKET_TZ
from models import Bar, Signal
from strategies.base import Strategy


MARKET_OPEN = time(9, 30)


class SpikeStrategy(Strategy):
    name = "spike"
    env_specs: ClassVar[tuple[EnvSpec, ...]] = (
        ("spike_lookback_seconds", "SPIKE_LOOKBACK_SECONDS", int_env, 5),
        ("spike_change_pct", "SPIKE_CHANGE_PCT", float_env, 0.0025),
        ("spike_start_minute", "SPIKE_START_MINUTE", optional_int_env, None),
        ("spike_end_minute", "SPIKE_END_MINUTE", optional_int_env, None),
        ("volume_ratio", "VOLUME_RATIO", float_env, 2.0),
        ("max_spread_bps", "MAX_SPREAD_BPS", float_env, 12.0),
    )
    diagnostic_loggers: ClassVar[tuple[str, ...]] = ("strategies.spike",)

    @classmethod
    def runtime_settings_section(cls, settings: Any) -> dict[str, Any] | None:
        if cls.name not in settings.strategy_names:
            return None
        return {
            "start_minute": settings.spike_start_minute,
            "end_minute": settings.spike_end_minute,
            "lookback_seconds": settings.spike_lookback_seconds,
            "change_pct": settings.spike_change_pct,
            "volume_ratio": settings.volume_ratio,
            "max_spread_bps": settings.max_spread_bps,
        }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_tz = MARKET_TZ

    def evaluate(self, state: SymbolState) -> Signal | None:
        if state.last_event_kind != "bar":
            return None

        lookback = self.settings.spike_lookback_seconds
        threshold_ms = state.bars[-1].end_ms - (lookback * 1000)
        last = state.bars[-1]
        if not self._within_entry_window(last.end_ms):
            return None

        prior_index = self._prior_index(list(state.bars), threshold_ms)
        if prior_index is None:
            return None

        bars = list(state.bars)
        prior = bars[prior_index]
        elapsed_seconds = (last.end_ms - prior.end_ms) / 1000
        if elapsed_seconds > lookback:
            return None

        change_pct = (last.close - prior.close) / prior.close
        if abs(change_pct) < self.settings.spike_change_pct:
            return None

        volume_ratio = self._volume_ratio(bars[prior_index:-1], last)
        if volume_ratio < self.settings.volume_ratio:
            return None

        spread_bps = state.quote.spread_bps if state.quote else None
        if spread_bps is not None and spread_bps > self.settings.max_spread_bps:
            return None

        side = "BUY" if change_pct > 0 else "SELL"
        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side=side,
            price=last.close,
            timestamp_ms=last.end_ms,
            change_pct=change_pct,
            volume_ratio=volume_ratio,
            spread_bps=spread_bps,
            reason=f"{elapsed_seconds:.0f}s move {change_pct:.3%}, volume {volume_ratio:.1f}x baseline",
        )

    @staticmethod
    def _volume_ratio(previous: list[Bar], last: Bar) -> float:
        baseline = median([bar.volume for bar in previous if bar.volume > 0] or [1])
        return last.volume / baseline if baseline else 0.0

    @staticmethod
    def _prior_index(bars: list[Bar], threshold_ms: int) -> int | None:
        for index in range(len(bars) - 2, -1, -1):
            if bars[index].end_ms <= threshold_ms:
                return index
        return None

    def _within_entry_window(self, timestamp_ms: int) -> bool:
        if self.settings.spike_start_minute is None and self.settings.spike_end_minute is None:
            return True

        current = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.market_tz)
        minutes = current.hour * 60 + current.minute
        market_open = (MARKET_OPEN.hour * 60) + MARKET_OPEN.minute
        elapsed = minutes - market_open

        if self.settings.spike_start_minute is not None and elapsed < self.settings.spike_start_minute:
            return False
        if self.settings.spike_end_minute is not None and elapsed > self.settings.spike_end_minute:
            return False
        return True
