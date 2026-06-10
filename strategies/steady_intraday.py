from __future__ import annotations

from datetime import datetime, time
import logging
from statistics import mean
from typing import Any, ClassVar

from candle import SymbolState
from config import Settings
from env_vars import EnvSpec, bool_env, float_env, int_env
from market_hours import MARKET_TZ, should_flatten_before_close
from models import ExitDecision, Signal
from strategies.base import Strategy


LOG = logging.getLogger(__name__)
MARKET_OPEN = time(9, 30)


class SteadyIntradayStrategy(Strategy):
    """VWAP/EMA trend-following day strategy with ATR risk and same-day exits."""

    name = "steady_intraday"
    env_specs: ClassVar[tuple[EnvSpec, ...]] = (
        ("steady_intraday_start_minute", "STEADY_INTRADAY_START_MINUTE", int_env, 15),
        ("steady_intraday_end_minute", "STEADY_INTRADAY_END_MINUTE", int_env, 330),
        ("steady_intraday_min_bars", "STEADY_INTRADAY_MIN_BARS", int_env, 55),
        ("steady_intraday_orb_minutes", "STEADY_INTRADAY_ORB_MINUTES", int_env, 15),
        ("steady_intraday_ema_fast", "STEADY_INTRADAY_EMA_FAST", int_env, 9),
        ("steady_intraday_ema_mid", "STEADY_INTRADAY_EMA_MID", int_env, 20),
        ("steady_intraday_ema_slow", "STEADY_INTRADAY_EMA_SLOW", int_env, 50),
        ("steady_intraday_atr_period", "STEADY_INTRADAY_ATR_PERIOD", int_env, 14),
        ("steady_intraday_min_atr_pct", "STEADY_INTRADAY_MIN_ATR_PCT", float_env, 0.0018),
        ("steady_intraday_max_atr_pct", "STEADY_INTRADAY_MAX_ATR_PCT", float_env, 0.015),
        ("steady_intraday_min_range_pct", "STEADY_INTRADAY_MIN_RANGE_PCT", float_env, 0.006),
        ("steady_intraday_min_volume_ratio", "STEADY_INTRADAY_MIN_VOLUME_RATIO", float_env, 1.15),
        ("steady_intraday_breakout_volume_ratio", "STEADY_INTRADAY_BREAKOUT_VOLUME_RATIO", float_env, 1.35),
        ("steady_intraday_max_spread_bps", "STEADY_INTRADAY_MAX_SPREAD_BPS", float_env, 12.0),
        ("steady_intraday_min_price", "STEADY_INTRADAY_MIN_PRICE", float_env, 5.0),
        ("steady_intraday_vwap_buffer_pct", "STEADY_INTRADAY_VWAP_BUFFER_PCT", float_env, 0.0005),
        ("steady_intraday_max_vwap_extension_pct", "STEADY_INTRADAY_MAX_VWAP_EXTENSION_PCT", float_env, 0.025),
        ("steady_intraday_max_ema_extension_pct", "STEADY_INTRADAY_MAX_EMA_EXTENSION_PCT", float_env, 0.012),
        ("steady_intraday_stop_atr_multiple", "STEADY_INTRADAY_STOP_ATR_MULTIPLE", float_env, 1.1),
        ("steady_intraday_stop_buffer_pct", "STEADY_INTRADAY_STOP_BUFFER_PCT", float_env, 0.0008),
        ("steady_intraday_min_r_pct", "STEADY_INTRADAY_MIN_R_PCT", float_env, 0.0025),
        ("steady_intraday_max_r_pct", "STEADY_INTRADAY_MAX_R_PCT", float_env, 0.012),
        ("steady_intraday_partial_r", "STEADY_INTRADAY_PARTIAL_R", float_env, 1.0),
        ("steady_intraday_partial_size", "STEADY_INTRADAY_PARTIAL_SIZE", float_env, 0.5),
        ("steady_intraday_target_r", "STEADY_INTRADAY_TARGET_R", float_env, 2.0),
        ("steady_intraday_runner_pullback_pct", "STEADY_INTRADAY_RUNNER_PULLBACK_PCT", float_env, 0.009),
        ("steady_intraday_breakdown_bars", "STEADY_INTRADAY_BREAKDOWN_BARS", int_env, 2),
        ("steady_intraday_stall_minutes", "STEADY_INTRADAY_STALL_MINUTES", int_env, 25),
        ("steady_intraday_stall_min_r", "STEADY_INTRADAY_STALL_MIN_R", float_env, 0.35),
        ("steady_intraday_position_size_multiplier", "STEADY_INTRADAY_POSITION_SIZE_MULTIPLIER", float_env, 1.0),
        (
            "steady_intraday_max_trades_per_symbol_per_session",
            "STEADY_INTRADAY_MAX_TRADES_PER_SYMBOL_PER_SESSION",
            int_env,
            2,
        ),
        ("steady_intraday_symbol_loss_lock_count", "STEADY_INTRADAY_SYMBOL_LOSS_LOCK_COUNT", int_env, 2),
        ("steady_intraday_allow_orb_breakout", "STEADY_INTRADAY_ALLOW_ORB_BREAKOUT", bool_env, True),
        ("steady_intraday_allow_pullback_reclaim", "STEADY_INTRADAY_ALLOW_PULLBACK_RECLAIM", bool_env, True),
    )
    diagnostic_loggers: ClassVar[tuple[str, ...]] = ("strategies.steady_intraday",)
    selector_command: ClassVar[str] = ".venv/bin/python strategy_selectors/select_steady_intraday.py --top 12"

    @classmethod
    def runtime_settings_section(cls, settings: Any) -> dict[str, Any] | None:
        if cls.name not in settings.strategy_names:
            return None
        return {
            "start_minute": settings.steady_intraday_start_minute,
            "end_minute": settings.steady_intraday_end_minute,
            "min_bars": settings.steady_intraday_min_bars,
            "orb_minutes": settings.steady_intraday_orb_minutes,
            "ema_fast": settings.steady_intraday_ema_fast,
            "ema_mid": settings.steady_intraday_ema_mid,
            "ema_slow": settings.steady_intraday_ema_slow,
            "atr_period": settings.steady_intraday_atr_period,
            "min_atr_pct": settings.steady_intraday_min_atr_pct,
            "max_atr_pct": settings.steady_intraday_max_atr_pct,
            "min_range_pct": settings.steady_intraday_min_range_pct,
            "min_volume_ratio": settings.steady_intraday_min_volume_ratio,
            "breakout_volume_ratio": settings.steady_intraday_breakout_volume_ratio,
            "max_spread_bps": settings.steady_intraday_max_spread_bps,
            "min_price": settings.steady_intraday_min_price,
            "vwap_buffer_pct": settings.steady_intraday_vwap_buffer_pct,
            "max_vwap_extension_pct": settings.steady_intraday_max_vwap_extension_pct,
            "max_ema_extension_pct": settings.steady_intraday_max_ema_extension_pct,
            "stop_atr_multiple": settings.steady_intraday_stop_atr_multiple,
            "min_r_pct": settings.steady_intraday_min_r_pct,
            "max_r_pct": settings.steady_intraday_max_r_pct,
            "partial_r": settings.steady_intraday_partial_r,
            "target_r": settings.steady_intraday_target_r,
            "runner_pullback_pct": settings.steady_intraday_runner_pullback_pct,
            "position_size_multiplier": settings.steady_intraday_position_size_multiplier,
            "max_trades_per_symbol_per_session": settings.steady_intraday_max_trades_per_symbol_per_session,
            "symbol_loss_lock_count": settings.steady_intraday_symbol_loss_lock_count,
        }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_tz = MARKET_TZ
        self._last_reject_log_ms: dict[tuple[str, str], int] = {}

    def evaluate(self, state: SymbolState) -> Signal | None:
        if not self.is_symbol_allowed(state.symbol):
            return None
        if state.last_event_kind != "bar":
            return None
        if not self._within_entry_window(state.last_event_ms):
            return None
        if should_flatten_before_close(state.last_event_ms, self.settings.flatten_before_close_minutes + 5):
            return self._reject(state, "eod", "entry disabled near close")

        bars = self._regular_bars(state)
        min_bars = max(self.settings.steady_intraday_min_bars, self.settings.steady_intraday_ema_slow + 5)
        if len(bars) < min_bars:
            return self._reject(state, "history", "insufficient regular-session bars")

        latest = bars[-1]
        entry = latest.close
        if entry < self.settings.steady_intraday_min_price:
            return self._reject(state, "price", "price below minimum")

        spread_bps = state.quote.spread_bps if state.quote else None
        if spread_bps is not None and spread_bps > self.settings.steady_intraday_max_spread_bps:
            return self._reject(state, "spread", f"spread {spread_bps:.2f}bps too wide")

        closes = [bar.close for bar in bars]
        ema_fast = self._ema(closes, self.settings.steady_intraday_ema_fast)
        ema_mid = self._ema(closes, self.settings.steady_intraday_ema_mid)
        ema_slow = self._ema(closes, self.settings.steady_intraday_ema_slow)
        prev_ema_mid = self._ema(closes[:-3], self.settings.steady_intraday_ema_mid)
        if None in {ema_fast, ema_mid, ema_slow, prev_ema_mid}:
            return self._reject(state, "history", "insufficient EMA history")

        session_vwap = self._session_vwap(bars)
        prev_vwap = self._session_vwap(bars[:-5])
        if session_vwap is None or prev_vwap is None:
            return self._reject(state, "vwap", "missing session VWAP")

        atr = self._atr(bars, self.settings.steady_intraday_atr_period)
        if atr is None or atr <= 0:
            return self._reject(state, "atr", "missing ATR")
        atr_pct = atr / entry
        if atr_pct < self.settings.steady_intraday_min_atr_pct:
            return self._reject(state, "atr", "ATR too low")
        if atr_pct > self.settings.steady_intraday_max_atr_pct:
            return self._reject(state, "atr", "ATR too high")

        recent_range_pct = self._range_pct(bars[-20:])
        if recent_range_pct < self.settings.steady_intraday_min_range_pct:
            return self._reject(state, "range", "recent range too compressed")

        if not (ema_fast > ema_mid > ema_slow):
            return self._reject(state, "trend", "EMA stack not bullish")
        if ema_mid <= prev_ema_mid:
            return self._reject(state, "trend", "EMA20 not rising")
        if entry <= session_vwap * (1 + self.settings.steady_intraday_vwap_buffer_pct):
            return self._reject(state, "vwap", "price not above VWAP")
        if session_vwap <= prev_vwap:
            return self._reject(state, "vwap", "VWAP not rising")

        vwap_extension_pct = (entry - session_vwap) / session_vwap
        ema_extension_pct = (entry - ema_mid) / ema_mid
        if vwap_extension_pct > self.settings.steady_intraday_max_vwap_extension_pct:
            return self._reject(state, "extension", "too extended from VWAP")
        if ema_extension_pct > self.settings.steady_intraday_max_ema_extension_pct:
            return self._reject(state, "extension", "too extended from EMA20")

        volume_ratio = self._volume_ratio(bars)
        trigger = self._entry_trigger(bars, entry, ema_fast, ema_mid, session_vwap, volume_ratio)
        if trigger is None:
            return self._reject(state, "trigger", "no pullback reclaim or ORB continuation")

        stop_price = self._stop_price(bars, entry, ema_mid, session_vwap, atr)
        r_pct = (entry - stop_price) / entry if entry > 0 else 0.0
        if r_pct < self.settings.steady_intraday_min_r_pct:
            return self._reject(state, "risk", "R too small")
        if r_pct > self.settings.steady_intraday_max_r_pct:
            return self._reject(state, "risk", "R too wide")

        reason = (
            f"steady_intraday {trigger}: EMA stack {ema_fast:.2f}>{ema_mid:.2f}>{ema_slow:.2f}, "
            f"VWAP {session_vwap:.2f}, ATR {atr_pct:.2%}, R {r_pct:.2%}, vol {volume_ratio:.2f}x"
        )
        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=entry,
            timestamp_ms=latest.end_ms,
            change_pct=(entry - bars[0].open) / bars[0].open if bars[0].open else 0.0,
            volume_ratio=volume_ratio,
            spread_bps=spread_bps,
            reason=reason,
            stop_price=stop_price,
            session_open_price=bars[0].open,
            entry_open_pct=(entry - bars[0].open) / bars[0].open if bars[0].open else None,
            position_size_multiplier=self.settings.steady_intraday_position_size_multiplier,
        )

    def should_exit(self, state: SymbolState, position) -> ExitDecision | None:
        if state.last_event_kind not in {"quote", "bar"} or position.strategy != self.name:
            return None

        price = state.last_price
        if price is None:
            return None

        initial_stop = position.initial_stop_price or position.stop_price
        r_initial = position.entry_price - initial_stop
        if r_initial <= 0:
            return None

        if not position.partial_exit_taken and position.shares > 1:
            partial_level = position.entry_price + r_initial * self.settings.steady_intraday_partial_r
            if price >= partial_level:
                fraction = min(1.0, max(0.0, self.settings.steady_intraday_partial_size))
                shares = max(1, min(position.shares - 1, int(position.shares * fraction)))
                return ExitDecision(f"partial {self.settings.steady_intraday_partial_r:.1f}R", shares=shares, mark_partial=True)

        target_level = position.entry_price + r_initial * self.settings.steady_intraday_target_r
        if price >= target_level:
            return ExitDecision(f"target {self.settings.steady_intraday_target_r:.1f}R")

        if position.partial_exit_taken:
            peak = position.max_price if position.max_price > 0 else position.entry_price
            if peak > 0 and price <= peak * (1 - self.settings.steady_intraday_runner_pullback_pct):
                return ExitDecision("runner pullback")

        bars = self._regular_bars(state)
        if len(bars) >= max(3, self.settings.steady_intraday_ema_fast + 2):
            closes = [bar.close for bar in bars]
            ema_fast = self._ema(closes, self.settings.steady_intraday_ema_fast)
            session_vwap = self._session_vwap(bars)
            breakdown_bars = max(1, self.settings.steady_intraday_breakdown_bars)
            if ema_fast and self._last_n_closes_below(bars, ema_fast, breakdown_bars):
                return ExitDecision("EMA fast breakdown")
            if session_vwap and price < session_vwap:
                return ExitDecision("lost VWAP")

        event_ms = state.last_event_ms or position.entry_ms
        age_minutes = (event_ms - position.entry_ms) / 60_000
        current_r = (price - position.entry_price) / r_initial
        if (
            age_minutes >= self.settings.steady_intraday_stall_minutes
            and current_r < self.settings.steady_intraday_stall_min_r
        ):
            return ExitDecision("stalled")

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
        return self.settings.steady_intraday_start_minute <= elapsed <= self.settings.steady_intraday_end_minute

    def _entry_trigger(
        self,
        bars,
        entry: float,
        ema_fast: float,
        ema_mid: float,
        session_vwap: float,
        volume_ratio: float,
    ) -> str | None:
        latest = bars[-1]
        previous = bars[-2]
        reclaimed_fast = previous.close <= ema_fast * 1.002 and latest.close > max(previous.high, ema_fast)
        held_mid = latest.low >= min(ema_mid, session_vwap) * 0.997
        bullish_close = latest.close > latest.open and self._close_near_high(latest)
        if (
            self.settings.steady_intraday_allow_pullback_reclaim
            and reclaimed_fast
            and held_mid
            and bullish_close
            and volume_ratio >= self.settings.steady_intraday_min_volume_ratio
        ):
            return "pullback_reclaim"

        if self.settings.steady_intraday_allow_orb_breakout:
            opening_high = self._opening_range_high(bars)
            if (
                opening_high is not None
                and latest.close > opening_high
                and previous.close <= opening_high * 1.002
                and bullish_close
                and volume_ratio >= self.settings.steady_intraday_breakout_volume_ratio
            ):
                return "orb_continuation"

        return None

    def _stop_price(self, bars, entry: float, ema_mid: float, session_vwap: float, atr: float) -> float:
        swing_low = min(bar.low for bar in bars[-6:])
        structure_stop = min(swing_low, ema_mid, session_vwap)
        atr_stop = entry - atr * self.settings.steady_intraday_stop_atr_multiple
        raw_stop = min(structure_stop, atr_stop)
        return raw_stop * (1 - self.settings.steady_intraday_stop_buffer_pct)

    def _regular_bars(self, state: SymbolState):
        return [bar for bar in state.bars if self._is_regular_bar(bar)]

    def _is_regular_bar(self, bar) -> bool:
        current = datetime.fromtimestamp(bar.start_ms / 1000, tz=self.market_tz)
        if current.weekday() >= 5:
            return False
        minute = current.hour * 60 + current.minute
        return (9 * 60 + 30) <= minute < (16 * 60)

    @staticmethod
    def _session_vwap(bars) -> float | None:
        total_volume = sum(bar.volume for bar in bars if bar.volume > 0)
        if total_volume <= 0:
            return None
        total_value = sum(bar.vwap * bar.volume for bar in bars if bar.volume > 0)
        return total_value / total_volume if total_value > 0 else None

    def _opening_range_high(self, bars) -> float | None:
        cutoff = self.settings.steady_intraday_orb_minutes
        opening = []
        for bar in bars:
            current = datetime.fromtimestamp(bar.start_ms / 1000, tz=self.market_tz)
            elapsed = (current.hour * 60 + current.minute) - (MARKET_OPEN.hour * 60 + MARKET_OPEN.minute)
            if 0 <= elapsed < cutoff:
                opening.append(bar)
        return max((bar.high for bar in opening), default=None)

    @staticmethod
    def _ema(values: list[float], period: int) -> float | None:
        if period <= 0 or len(values) < period:
            return None
        alpha = 2 / (period + 1)
        ema = sum(values[:period]) / period
        for value in values[period:]:
            ema = (value * alpha) + (ema * (1 - alpha))
        return ema

    @staticmethod
    def _atr(bars, period: int) -> float | None:
        if period <= 0 or len(bars) < period + 1:
            return None
        true_ranges = []
        tail = bars[-(period + 1) :]
        for previous, current in zip(tail, tail[1:]):
            true_ranges.append(
                max(
                    current.high - current.low,
                    abs(current.high - previous.close),
                    abs(current.low - previous.close),
                )
            )
        return mean(true_ranges) if true_ranges else None

    @staticmethod
    def _volume_ratio(bars) -> float:
        if len(bars) < 12:
            return 1.0
        latest_volume = bars[-1].volume
        baseline = [bar.volume for bar in bars[-11:-1] if bar.volume > 0]
        denominator = mean(baseline) if baseline else latest_volume or 1
        return latest_volume / denominator if denominator else 1.0

    @staticmethod
    def _range_pct(bars) -> float:
        if not bars:
            return 0.0
        low = min(bar.low for bar in bars)
        high = max(bar.high for bar in bars)
        return (high - low) / low if low > 0 else 0.0

    @staticmethod
    def _close_near_high(bar) -> bool:
        rng = bar.high - bar.low
        if rng <= 0:
            return bar.close >= bar.open
        return (bar.high - bar.close) / rng <= 0.35

    @staticmethod
    def _last_n_closes_below(bars, level: float, n: int) -> bool:
        if len(bars) < n:
            return False
        return all(bar.close < level for bar in bars[-n:])

    def _reject(self, state: SymbolState, code: str, detail: str) -> None:
        timestamp_ms = state.last_event_ms or 0
        key = (state.symbol, code)
        last_log_ms = self._last_reject_log_ms.get(key, -30_000)
        if timestamp_ms - last_log_ms >= 30_000:
            self._last_reject_log_ms[key] = timestamp_ms
            LOG.debug("No steady_intraday entry %s [%s]: %s", state.symbol, code, detail)
        return None
