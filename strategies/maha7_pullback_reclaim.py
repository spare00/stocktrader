from __future__ import annotations

from datetime import datetime, time
import logging
from statistics import mean

from candle import SymbolState
from config import Settings
from market_hours import MARKET_TZ
from models import ExitDecision, Signal
from strategies.base import Strategy


LOG = logging.getLogger(__name__)
MARKET_OPEN = time(9, 30)


class Maha7PullbackReclaimStrategy(Strategy):
    # MAHA7 refined:
    # - confirmation-based pullback strategy
    # - no early entry after crossover
    # - avoid RSI neutral zone
    # - enforce minimum hold to avoid noise exits
    # - reduce overtrading via per-symbol cap
    name = "maha7_pullback_reclaim"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_tz = MARKET_TZ
        self._last_reject_log_ms: dict[tuple[str, str], int] = {}

    def evaluate(self, state: SymbolState) -> Signal | None:
        if state.last_event_kind != "bar":
            return None
        if not self._within_entry_window(state.last_event_ms):
            return self._reject(state, "window", "outside 10:00-14:30 ET entry window")

        bars = self._regular_bars(state)
        period = self.settings.maha7_pullback_reclaim_rsi_period
        if len(bars) < max(24, period + 12):
            return self._reject(state, "history", "insufficient bar history")

        closes = [bar.close for bar in bars]
        ma7 = self._sma(closes, 7)
        ma20 = self._sma(closes, 20)
        prev_ma7 = self._sma(closes[:-1], 7)
        prev_ma20 = self._sma(closes[:-1], 20)
        if None in {ma7, ma20, prev_ma7, prev_ma20}:
            return self._reject(state, "history", "insufficient moving-average history")

        latest = bars[-1]
        latest_rsi = self._rsi(closes, period)
        previous_rsi = self._rsi(closes[:-1], period)
        if latest_rsi is None or previous_rsi is None:
            return self._reject(state, "history", "insufficient RSI history")

        ma7_slope_pct = (ma7 - prev_ma7) / prev_ma7 if prev_ma7 else 0.0
        ma20_slope_pct = (ma20 - prev_ma20) / prev_ma20 if prev_ma20 else 0.0
        if ma7 <= ma20:
            return self._reject(state, "trend", "MA7 is not above MA20")
        if ma7_slope_pct <= 0:
            return self._reject(state, "trend", "MA7 slope is not positive")
        bars_since_crossover = self._bars_since_ma7_cross_above_ma20(closes)
        min_trend_bars = self.settings.maha7_pullback_reclaim_trend_min_bars
        if bars_since_crossover is None or bars_since_crossover < min_trend_bars:
            return self._reject(state, "trend", f"MA7/MA20 trend has not stabilized for {min_trend_bars} bars")
        vwap = self._session_vwap(bars)
        vwap_distance = (latest.close - vwap) / vwap if vwap else 0.0
        if vwap_distance < self.settings.maha7_pullback_reclaim_vwap_min_distance_pct:
            return self._reject(state, "trend", "price is not far enough above VWAP")
        if not self._strong_uptrend(bars):
            return self._reject(state, "structure", "no strong higher-high structure")

        flat_threshold = self.settings.maha7_pullback_reclaim_flat_slope_pct
        if abs(ma7_slope_pct) <= flat_threshold and abs(ma20_slope_pct) <= flat_threshold:
            return self._reject(state, "flat_ma", "flat MA7/MA20")
        if 45 < latest_rsi < 55:
            return self._reject(state, "rsi_chop", "RSI is in neutral 45-55 zone")
        if self._rsi_consolidated(closes, period):
            return self._reject(state, "rsi_chop", "RSI stayed between 45 and 55 too long")
        if not self._volume_confirmed(bars):
            return self._reject(state, "volume", "trigger volume below 10-bar baseline")

        if not self._recent_pullback_to_ma7(bars):
            return self._reject(state, "pullback", "no close within MA7 pullback distance")
        if not self._recent_rsi_pullback(closes, period):
            return self._reject(state, "pullback", "RSI did not dip below 55 while staying above 45")
        rsi_min_bars = self.settings.maha7_pullback_reclaim_rsi_above_min_bars
        if not self._rsi_above_duration(closes, period, 55, rsi_min_bars):
            return self._reject(state, "trigger", f"RSI has not held above 55 for {rsi_min_bars} bars")
        if not self._recent_rsi_cross_above(closes, period, 55, rsi_min_bars + 1):
            return self._reject(state, "trigger", "RSI did not recently reclaim 55")
        if latest.close <= latest.open:
            return self._reject(state, "trigger", "trigger candle is not bullish")
        if not self._body_expanded(bars):
            return self._reject(state, "trigger", "trigger body did not exceed prior 3-body average")

        swing_low = self._previous_swing_low(bars)
        if swing_low is None or swing_low >= latest.close:
            return self._reject(state, "risk", "invalid previous swing low")

        risk = latest.close - swing_low
        volume_ratio = latest.volume / mean([bar.volume for bar in bars[-4:-1] if bar.volume > 0] or [latest.volume or 1])
        reason = (
            f"maha7 pullback reclaim: MA7 {ma7:.2f} > MA20 {ma20:.2f}, "
            f"RSI {previous_rsi:.1f}->{latest_rsi:.1f}, VWAP gap {vwap_distance:.2%}, "
            f"stop {swing_low:.2f}, risk {risk:.2f}"
        )
        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=latest.close,
            timestamp_ms=latest.end_ms,
            change_pct=(latest.close - bars[0].open) / bars[0].open if bars[0].open else 0.0,
            volume_ratio=volume_ratio,
            spread_bps=state.quote.spread_bps if state.quote else None,
            reason=reason,
            stop_price=swing_low,
        )

    def should_exit(self, state: SymbolState, position) -> ExitDecision | None:
        if state.last_event_kind not in {"quote", "bar"} or position.strategy != self.name:
            return None

        price = state.last_price
        if price is None:
            return None

        risk = position.entry_price - position.stop_price
        if risk <= 0:
            return None

        if not position.partial_exit_taken and price >= position.entry_price + (risk * self.settings.maha7_pullback_reclaim_partial_r):
            shares = max(1, position.shares // 2)
            return ExitDecision("partial 0.5R", shares=shares, mark_partial=True)

        if price >= position.entry_price + (risk * self.settings.maha7_pullback_reclaim_target_r):
            return ExitDecision(f"target {self.settings.maha7_pullback_reclaim_target_r:.1f}R")

        elapsed_seconds = (state.last_event_ms - position.entry_ms) / 1000 if state.last_event_ms else 0
        if elapsed_seconds < self.settings.maha7_pullback_reclaim_min_hold_seconds:
            return None

        bars = self._regular_bars(state)
        if len(bars) < max(8, self.settings.maha7_pullback_reclaim_rsi_period + 1):
            return None

        closes = [bar.close for bar in bars]
        ma7 = self._sma(closes, 7)
        rsi = self._rsi(closes, self.settings.maha7_pullback_reclaim_rsi_period)
        latest_close = bars[-1].close
        if ma7 is not None and latest_close < ma7:
            return ExitDecision("close below MA7")
        if rsi is not None and rsi < 50:
            return ExitDecision("RSI below 50")

        return None

    def use_fixed_target_exit(self, position) -> bool:
        return False

    def _within_entry_window(self, timestamp_ms: int | None) -> bool:
        if timestamp_ms is None:
            return False
        current = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.market_tz)
        minutes = current.hour * 60 + current.minute
        market_open = MARKET_OPEN.hour * 60 + MARKET_OPEN.minute
        elapsed = minutes - market_open
        min_elapsed = max(
            self.settings.maha7_pullback_reclaim_start_minute,
            self.settings.maha7_pullback_reclaim_min_minutes_after_opening_impulse,
        )
        return min_elapsed <= elapsed <= self.settings.maha7_pullback_reclaim_end_minute

    def _regular_bars(self, state: SymbolState):
        return [
            bar
            for bar in state.bars
            if datetime.fromtimestamp(bar.end_ms / 1000, tz=self.market_tz).time() >= MARKET_OPEN
        ]

    @staticmethod
    def _sma(values: list[float], window: int) -> float | None:
        if len(values) < window:
            return None
        return mean(values[-window:])

    @staticmethod
    def _rsi(closes: list[float], period: int) -> float | None:
        if len(closes) <= period:
            return None
        changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
        recent = changes[-period:]
        gains = [max(change, 0.0) for change in recent]
        losses = [abs(min(change, 0.0)) for change in recent]
        average_gain = mean(gains)
        average_loss = mean(losses)
        if average_loss == 0:
            return 100.0 if average_gain > 0 else 50.0
        relative_strength = average_gain / average_loss
        return 100 - (100 / (1 + relative_strength))

    @staticmethod
    def _session_vwap(bars) -> float:
        volume = sum(bar.volume for bar in bars if bar.volume > 0)
        if volume <= 0:
            return bars[-1].vwap
        return sum(bar.vwap * bar.volume for bar in bars if bar.volume > 0) / volume

    @staticmethod
    def _strong_uptrend(bars) -> bool:
        if len(bars) < 6:
            return False
        highs = [bar.high for bar in bars[-6:]]
        return highs[-1] > highs[-2] > highs[-3]

    def _bars_since_ma7_cross_above_ma20(self, closes: list[float]) -> int | None:
        if len(closes) < 20:
            return None

        above_flags = []
        for end_index in range(20, len(closes) + 1):
            prefix = closes[:end_index]
            ma7 = self._sma(prefix, 7)
            ma20 = self._sma(prefix, 20)
            above_flags.append(bool(ma7 is not None and ma20 is not None and ma7 > ma20))

        if not above_flags or not above_flags[-1]:
            return None

        streak = 0
        for is_above in reversed(above_flags):
            if not is_above:
                break
            streak += 1

        return max(0, streak - 1)

    def _recent_pullback_to_ma7(self, bars) -> bool:
        distance_limit = self.settings.maha7_pullback_reclaim_pullback_ma7_distance_pct
        closes = [bar.close for bar in bars]
        start = max(7, len(bars) - 8)
        for index in range(start, len(bars) - 1):
            bar = bars[index]
            ma7 = self._sma(closes[: index + 1], 7)
            distance = abs(bar.close - ma7) / ma7 if ma7 else float("inf")
            if distance < distance_limit:
                return True
        return False

    @staticmethod
    def _body_expanded(bars) -> bool:
        if len(bars) < 4:
            return False
        current_body = abs(bars[-1].close - bars[-1].open)
        prior_average = mean(abs(bar.close - bar.open) for bar in bars[-4:-1])
        return current_body > prior_average

    def _rsi_consolidated(self, closes: list[float], period: int) -> bool:
        candle_count = self.settings.maha7_pullback_reclaim_consolidation_candles
        if len(closes) < period + candle_count + 1:
            return False
        recent_rsis = [self._rsi(closes[:index], period) for index in range(len(closes) - candle_count, len(closes) + 1)]
        valid_rsis = [value for value in recent_rsis if value is not None]
        return len(valid_rsis) > candle_count and all(45 <= value <= 55 for value in valid_rsis)

    def _recent_rsi_pullback(self, closes: list[float], period: int) -> bool:
        if len(closes) < period + 6:
            return False
        recent_rsis = [self._rsi(closes[:index], period) for index in range(len(closes) - 5, len(closes))]
        valid_rsis = [value for value in recent_rsis if value is not None]
        return any(45 < value < 55 for value in valid_rsis)

    def _rsi_above_duration(self, closes: list[float], period: int, threshold: float, min_bars: int) -> bool:
        count = 0
        for index in range(len(closes), 0, -1):
            rsi = self._rsi(closes[:index], period)
            if rsi is None or rsi < threshold:
                break
            count += 1
        return count >= min_bars

    def _recent_rsi_cross_above(self, closes: list[float], period: int, threshold: float, lookback_bars: int) -> bool:
        start = max(period + 1, len(closes) - lookback_bars)
        for index in range(start, len(closes) + 1):
            previous = self._rsi(closes[: index - 1], period)
            current = self._rsi(closes[:index], period)
            if previous is not None and current is not None and previous <= threshold < current:
                return True
        return False

    def _volume_confirmed(self, bars) -> bool:
        if len(bars) < 10:
            return False
        latest = bars[-1]
        volumes = [bar.volume for bar in bars[-10:] if bar.volume > 0]
        if not volumes:
            return False
        average_volume = mean(volumes)
        return latest.volume >= average_volume * self.settings.maha7_pullback_reclaim_volume_min_ratio

    @staticmethod
    def _previous_swing_low(bars) -> float | None:
        if len(bars) < 5:
            return None
        search = bars[-8:-1] if len(bars) >= 8 else bars[:-1]
        for index in range(len(search) - 2, 0, -1):
            previous_bar = search[index - 1]
            current = search[index]
            next_bar = search[index + 1]
            if current.low <= previous_bar.low and current.low <= next_bar.low:
                return current.low
        return min(bar.low for bar in search) if search else None

    def _reject(self, state: SymbolState, code: str, detail: str) -> None:
        timestamp_ms = state.last_event_ms or 0
        key = (state.symbol, code)
        last_log_ms = self._last_reject_log_ms.get(key, -10_000)
        if timestamp_ms - last_log_ms >= 10_000:
            self._last_reject_log_ms[key] = timestamp_ms
            LOG.debug("No maha7_pullback_reclaim entry %s: %s", state.symbol, detail)
        return None
