from collections import deque
from datetime import datetime
import logging
from statistics import median

from candle import SymbolState
from config import Settings
from market_hours import MARKET_TZ
from models import ExitDecision, Signal
from strategies.base import Strategy


LOG = logging.getLogger(__name__)


class OpeningImpulseStrategy(Strategy):
    name = "opening_impulse"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_tz = MARKET_TZ
        self._last_reject_log_ms: dict[tuple[str, str], int] = {}

    def evaluate(self, state: SymbolState) -> Signal | None:
        if state.last_event_kind != "quote":
            return None

        if not self._within_trading_window(state.last_event_ms):
            return self._reject(state, "window", "outside opening impulse entry window")

        quotes = self._recent_quotes(state, self.settings.opening_impulse_window_seconds)
        if len(quotes) < self.settings.opening_impulse_min_quotes:
            return self._reject(
                state,
                "quotes",
                f"quotes {len(quotes)} < {self.settings.opening_impulse_min_quotes}",
            )

        first = quotes[0]
        last = quotes[-1]
        quote_change_pct = (last.mid - first.mid) / first.mid
        volume_ratio = self._volume_ratio(state)
        bar_impulse = None
        signal_change_pct = quote_change_pct
        signal_reason = ""

        if quote_change_pct >= self.settings.opening_impulse_change_pct:
            signal_reason = (
                f"opening impulse {quote_change_pct:.3%} over "
                f"{(last.timestamp_ms - first.timestamp_ms) / 1000:.0f}s, "
                f"volume {volume_ratio:.1f}x baseline"
            )
        else:
            bar_impulse = self._bar_impulse(state)
            if bar_impulse is None:
                return self._reject(
                    state,
                    "change",
                    f"change {quote_change_pct:.3%} < {self.settings.opening_impulse_change_pct:.3%}",
                )
            signal_change_pct, volume_ratio, signal_reason = bar_impulse

        if signal_change_pct > self.settings.opening_impulse_skip_extended_pct:
            return self._reject(
                state,
                "extended",
                f"change {signal_change_pct:.3%} > {self.settings.opening_impulse_skip_extended_pct:.3%}",
            )

        spread_bps = last.spread_bps
        if spread_bps > self.settings.opening_impulse_max_spread_bps:
            return self._reject(
                state,
                "spread",
                f"spread {spread_bps:.2f}bps > {self.settings.opening_impulse_max_spread_bps:.2f}bps",
            )

        if min(last.bid_size, last.ask_size) < self.settings.opening_impulse_min_quote_size:
            return self._reject(
                state,
                "quote_size",
                f"quote size {min(last.bid_size, last.ask_size)} < {self.settings.opening_impulse_min_quote_size}",
            )

        if bar_impulse is None:
            negative_steps = self._negative_steps(quotes)
            if negative_steps > self.settings.opening_impulse_max_negative_steps:
                return self._reject(
                    state,
                    "velocity",
                    f"negative quote steps {negative_steps} > {self.settings.opening_impulse_max_negative_steps}",
                )

            recent_high = max(quote.mid for quote in quotes)
            if last.mid < recent_high * (1 - self.settings.opening_impulse_retrace_from_high_pct):
                retrace_pct = (recent_high - last.mid) / recent_high
                return self._reject(
                    state,
                    "retrace",
                    f"retrace {retrace_pct:.3%} > {self.settings.opening_impulse_retrace_from_high_pct:.3%}",
                )

            if volume_ratio < self.settings.opening_impulse_volume_ratio:
                return self._reject(
                    state,
                    "volume",
                    f"volume {volume_ratio:.2f}x < {self.settings.opening_impulse_volume_ratio:.2f}x",
                )

        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=last.ask,
            timestamp_ms=last.timestamp_ms,
            change_pct=signal_change_pct,
            volume_ratio=volume_ratio,
            spread_bps=spread_bps,
            reason=signal_reason,
        )

    def should_exit(self, state: SymbolState, position) -> ExitDecision | None:
        if state.last_event_kind != "quote" or position.strategy != self.name:
            return None

        quotes = self._recent_quotes(state, self.settings.opening_impulse_exit_window_seconds)
        if len(quotes) < self.settings.opening_impulse_exit_min_quotes:
            return None

        latest = quotes[-1]
        age_seconds = ((state.last_event_ms or latest.timestamp_ms) - position.entry_ms) / 1000
        if (
            age_seconds >= self.settings.opening_impulse_min_hold_seconds
            and latest.mid <= position.entry_price * (1 + self.settings.opening_impulse_stall_buffer_pct)
        ):
            return ExitDecision("momentum stall")

        recent_high = max(quote.mid for quote in quotes)
        if latest.mid < recent_high * (1 - self.settings.opening_impulse_retrace_from_high_pct):
            return ExitDecision("retrace from local high")

        recent_changes = [quotes[index].mid - quotes[index - 1].mid for index in range(1, len(quotes))]
        negative_steps = sum(1 for change in recent_changes if change < 0)
        if negative_steps >= self.settings.opening_impulse_exit_negative_steps:
            return ExitDecision("momentum fade")

        return None

    def _reject(self, state: SymbolState, code: str, detail: str) -> None:
        timestamp_ms = state.last_event_ms or 0
        key = (state.symbol, code)
        last_log_ms = self._last_reject_log_ms.get(key, -10_000)
        if timestamp_ms - last_log_ms >= 10_000:
            self._last_reject_log_ms[key] = timestamp_ms
            LOG.debug("No opening_impulse entry %s: %s", state.symbol, detail)
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

    @staticmethod
    def _negative_steps(quotes: list) -> int:
        negative_steps = 0
        for index in range(1, len(quotes)):
            if quotes[index].mid < quotes[index - 1].mid:
                negative_steps += 1
        return negative_steps

    def _bar_impulse(self, state: SymbolState) -> tuple[float, float, str] | None:
        if not self.settings.opening_impulse_bar_confirmation:
            return None

        window = max(2, self.settings.opening_impulse_bar_window)
        bars = list(state.bars)[-window:]
        if len(bars) < window:
            return None

        start_price = bars[0].open or bars[0].close
        end_price = bars[-1].close
        if start_price <= 0:
            return None

        change_pct = (end_price - start_price) / start_price
        if change_pct < self.settings.opening_impulse_bar_change_pct:
            return None

        rising_bars = 0
        for index, current in enumerate(bars):
            previous_close = bars[index - 1].close if index > 0 else current.open
            if current.close >= current.open or current.close > previous_close:
                rising_bars += 1
        if rising_bars < self.settings.opening_impulse_bar_min_rising:
            return None

        volume_ratio = self._volume_ratio(state)
        if volume_ratio < self.settings.opening_impulse_bar_volume_ratio:
            return None

        elapsed_seconds = max(60, (bars[-1].end_ms - bars[0].start_ms) / 1000)
        reason = (
            f"opening bar impulse {change_pct:.3%} over {elapsed_seconds:.0f}s, "
            f"{rising_bars}/{len(bars)} rising bars, volume {volume_ratio:.1f}x baseline"
        )
        return change_pct, volume_ratio, reason

    @staticmethod
    def _volume_ratio(state: SymbolState) -> float:
        if len(state.bars) < 2:
            return 0.0
        latest_volume = state.bars[-1].volume
        baseline = median([bar.volume for bar in list(state.bars)[:-1] if bar.volume > 0] or [1])
        return latest_volume / baseline if baseline else 0.0
