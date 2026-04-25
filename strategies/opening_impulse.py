from collections import deque
from datetime import datetime
from statistics import median

from candle import SymbolState
from config import Settings
from market_hours import MARKET_TZ
from models import ExitDecision, Signal
from strategies.base import Strategy


class OpeningImpulseStrategy(Strategy):
    name = "opening_impulse"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_tz = MARKET_TZ

    def evaluate(self, state: SymbolState) -> Signal | None:
        if state.last_event_kind != "quote":
            return None

        if not self._within_trading_window(state.last_event_ms):
            return None

        quotes = self._recent_quotes(state, self.settings.opening_impulse_window_seconds)
        if len(quotes) < self.settings.opening_impulse_min_quotes:
            return None

        first = quotes[0]
        last = quotes[-1]
        change_pct = (last.mid - first.mid) / first.mid
        if change_pct < self.settings.opening_impulse_change_pct:
            return None

        if change_pct > self.settings.opening_impulse_skip_extended_pct:
            return None

        spread_bps = last.spread_bps
        if spread_bps > self.settings.opening_impulse_max_spread_bps:
            return None

        if min(last.bid_size, last.ask_size) < self.settings.opening_impulse_min_quote_size:
            return None

        if not self._velocity_positive(quotes):
            return None

        recent_high = max(quote.mid for quote in quotes)
        if last.mid < recent_high * (1 - self.settings.opening_impulse_retrace_from_high_pct):
            return None

        volume_ratio = self._volume_ratio(state)
        if volume_ratio < self.settings.opening_impulse_volume_ratio:
            return None

        elapsed_seconds = (last.timestamp_ms - first.timestamp_ms) / 1000
        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=last.ask,
            timestamp_ms=last.timestamp_ms,
            change_pct=change_pct,
            volume_ratio=volume_ratio,
            spread_bps=spread_bps,
            reason=(
                f"opening impulse {change_pct:.3%} over {elapsed_seconds:.0f}s, "
                f"volume {volume_ratio:.1f}x baseline"
            ),
        )

    def should_exit(self, state: SymbolState, position) -> ExitDecision | None:
        if state.last_event_kind != "quote" or position.strategy != self.name:
            return None

        quotes = self._recent_quotes(state, self.settings.opening_impulse_exit_window_seconds)
        if len(quotes) < self.settings.opening_impulse_exit_min_quotes:
            return None

        latest = quotes[-1]
        if latest.mid <= position.entry_price * (1 + self.settings.opening_impulse_stall_buffer_pct):
            return ExitDecision("momentum stall")

        recent_high = max(quote.mid for quote in quotes)
        if latest.mid < recent_high * (1 - self.settings.opening_impulse_retrace_from_high_pct):
            return ExitDecision("retrace from local high")

        recent_changes = [quotes[index].mid - quotes[index - 1].mid for index in range(1, len(quotes))]
        negative_steps = sum(1 for change in recent_changes if change < 0)
        if negative_steps >= self.settings.opening_impulse_exit_negative_steps:
            return ExitDecision("momentum fade")

        return None

    def _within_trading_window(self, timestamp_ms: int | None) -> bool:
        if timestamp_ms is None:
            return False
        current = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.market_tz)
        minutes = current.hour * 60 + current.minute
        market_open = 9 * 60 + 30
        elapsed = minutes - market_open
        return self.settings.opening_impulse_start_minute <= elapsed <= self.settings.opening_impulse_end_minute

    @staticmethod
    def _recent_quotes(state: SymbolState, window_seconds: int) -> list:
        if not state.quotes:
            return []
        latest_ms = state.quotes[-1].timestamp_ms
        threshold = latest_ms - (window_seconds * 1000)
        return [quote for quote in state.quotes if quote.timestamp_ms >= threshold]

    def _velocity_positive(self, quotes: list) -> bool:
        rises = 0
        for index in range(1, len(quotes)):
            if quotes[index].mid >= quotes[index - 1].mid:
                rises += 1
        return rises >= max(len(quotes) - self.settings.opening_impulse_max_negative_steps, 1)

    @staticmethod
    def _volume_ratio(state: SymbolState) -> float:
        if len(state.bars) < 2:
            return 0.0
        latest_volume = state.bars[-1].volume
        baseline = median([bar.volume for bar in list(state.bars)[:-1] if bar.volume > 0] or [1])
        return latest_volume / baseline if baseline else 0.0
