from __future__ import annotations

from datetime import datetime, time
import logging
from statistics import mean, median
from typing import Any, ClassVar

from candle import SymbolState
from config import Settings
from env_vars import EnvSpec, bool_env, float_env, int_env
from market_hours import MARKET_TZ
from models import ExitDecision, Signal
from strategies.base import Strategy


LOG = logging.getLogger(__name__)
MARKET_OPEN = time(9, 30)


class Maha7Strategy(Strategy):
    """MA7/MA20 pullback/continuation strategy.

    Entry: MA7 > MA20 (rising) for min 3 bars, price > MA7, then either:
    - Pullback mode: price within 0.3% of MA7, reclaims previous high
    - Continuation mode: strong bull bar closing near high, volume ≥1.35x

    Quality filters: chop detection (tight MA spacing + compressed range),
    min 30m range, higher-low structure, chase prevention.

    Exit: -1R stop; partial at 0.5R; target at 2R (optional); runner pullback
    1.2% from peak; MA7 confirmed breakdown (2 bars below MA7 + slope ≤ 0).
    Breakeven stop after partial (if enabled).
    """
    name = "maha7"
    env_specs: ClassVar[tuple[EnvSpec, ...]] = (
        ("maha7_start_minute", "MAHA7_START_MINUTE", int_env, 30),
        ("maha7_end_minute", "MAHA7_END_MINUTE", int_env, 210),
        ("maha7_rsi_period", "MAHA7_RSI_PERIOD", int_env, 14),
        ("maha7_rsi_above_min_bars", "MAHA7_RSI_ABOVE_MIN_BARS", int_env, 2),
        ("maha7_flat_slope_pct", "MAHA7_FLAT_SLOPE_PCT", float_env, 0.0002),
        ("maha7_consolidation_candles", "MAHA7_CONSOLIDATION_CANDLES", int_env, 10),
        ("maha7_vwap_min_distance_pct", "MAHA7_VWAP_MIN_DISTANCE_PCT", float_env, 0.002),
        ("maha7_pullback_ma7_distance_pct", "MAHA7_PULLBACK_MA7_DISTANCE_PCT", float_env, 0.003),
        ("maha7_volume_min_ratio", "MAHA7_VOLUME_MIN_RATIO", float_env, 1.25),
        ("maha7_reentry_cooldown_seconds", "MAHA7_REENTRY_COOLDOWN_SECONDS", int_env, 1200),
        ("maha7_min_minutes_after_opening_impulse", "MAHA7_MIN_MINUTES_AFTER_OPENING_IMPULSE", int_env, 5),
        ("maha7_trend_min_bars", "MAHA7_TREND_MIN_BARS", int_env, 3),
        ("maha7_min_hold_seconds", "MAHA7_MIN_HOLD_SECONDS", int_env, 120),
        (
            "maha7_max_trades_per_symbol_per_session",
            "MAHA7_MAX_TRADES_PER_SYMBOL_PER_SESSION",
            int_env,
            2,
        ),
        ("maha7_symbol_loss_lock_count", "MAHA7_SYMBOL_LOSS_LOCK_COUNT", int_env, 1),
        ("maha7_early_loss_cut_seconds", "MAHA7_EARLY_LOSS_CUT_SECONDS", int_env, 120),
        ("maha7_early_loss_cut_pct", "MAHA7_EARLY_LOSS_CUT_PCT", float_env, 0.002),
        ("maha7_partial_r", "MAHA7_PARTIAL_R", float_env, 0.5),
        ("maha7_partial_size", "MAHA7_PARTIAL_SIZE", float_env, 0.5),
        ("maha7_target_r", "MAHA7_TARGET_R", float_env, 2.0),
        ("maha7_move_stop_to_entry_after_partial", "MAHA7_MOVE_STOP_TO_ENTRY_AFTER_PARTIAL", bool_env, True),
        ("maha7_hard_target_r_exit", "MAHA7_HARD_TARGET_R_EXIT", bool_env, True),
        ("maha7_trend_quality_enabled", "MAHA7_TREND_QUALITY_ENABLED", bool_env, True),
        ("maha7_min_30m_range_pct", "MAHA7_MIN_30M_RANGE_PCT", float_env, 0.01),
        ("maha7_chop_max_ma_spacing_pct", "MAHA7_CHOP_MAX_MA_SPACING_PCT", float_env, 0.002),
        ("maha7_chop_max_range_pct", "MAHA7_CHOP_MAX_RANGE_PCT", float_env, 0.007),
        ("maha7_require_higher_low", "MAHA7_REQUIRE_HIGHER_LOW", bool_env, True),
        ("maha7_allow_continuation", "MAHA7_ALLOW_CONTINUATION", bool_env, True),
        (
            "maha7_continuation_pullback_min_pct",
            "MAHA7_CONTINUATION_PULLBACK_MIN_PCT",
            float_env,
            0.003,
        ),
        (
            "maha7_continuation_pullback_max_pct",
            "MAHA7_CONTINUATION_PULLBACK_MAX_PCT",
            float_env,
            0.012,
        ),
        ("maha7_reclaim_buffer_pct", "MAHA7_RECLAIM_BUFFER_PCT", float_env, 0.0005),
        ("maha7_allow_early_trend_entry", "MAHA7_ALLOW_EARLY_TREND_ENTRY", bool_env, True),
        (
            "maha7_early_trend_max_bars_since_cross",
            "MAHA7_EARLY_TREND_MAX_BARS_SINCE_CROSS",
            int_env,
            15,
        ),
        ("maha7_runner_confirm_break_bars", "MAHA7_RUNNER_CONFIRM_BREAK_BARS", int_env, 2),
        (
            "maha7_runner_peak_pullback_pct",
            "MAHA7_RUNNER_PEAK_PULLBACK_PCT",
            float_env,
            0.012,
        ),
        ("maha7_swing_lookback", "MAHA7_SWING_LOOKBACK", int_env, 5),
        ("maha7_stop_anchor_buffer_pct", "MAHA7_STOP_ANCHOR_BUFFER_PCT", float_env, 0.001),
        ("maha7_min_r_pct", "MAHA7_MIN_R_PCT", float_env, 0.003),
        ("maha7_max_r_pct", "MAHA7_MAX_R_PCT", float_env, 0.012),
        ("maha7_continuation_volume_ratio", "MAHA7_CONTINUATION_VOLUME_RATIO", float_env, 1.35),
        ("maha7_max_chase_pct", "MAHA7_MAX_CHASE_PCT", float_env, 0.01),
        ("maha7_recent_high_lookback", "MAHA7_RECENT_HIGH_LOOKBACK", int_env, 20),
        ("maha7_momentum_green_bars", "MAHA7_MOMENTUM_GREEN_BARS", int_env, 2),
        ("maha7_disable_ma7_exit", "MAHA7_DISABLE_MA7_EXIT", bool_env, False),
        ("maha7_volume_use_median", "MAHA7_VOLUME_USE_MEDIAN", bool_env, True),
    )
    diagnostic_loggers: ClassVar[tuple[str, ...]] = ("strategies.maha7",)
    selector_command: ClassVar[str] = ".venv/bin/python strategy_selectors/select_maha7.py --top 12"

    @classmethod
    def runtime_settings_section(cls, settings: Any) -> dict[str, Any] | None:
        if cls.name not in settings.strategy_names:
            return None
        s = settings
        return {
            "start_minute": s.maha7_start_minute,
            "end_minute": s.maha7_end_minute,
            "rsi_period": s.maha7_rsi_period,
            "rsi_above_min_bars": s.maha7_rsi_above_min_bars,
            "flat_slope_pct": s.maha7_flat_slope_pct,
            "consolidation_candles": s.maha7_consolidation_candles,
            "vwap_min_distance_pct": s.maha7_vwap_min_distance_pct,
            "pullback_ma7_distance_pct": s.maha7_pullback_ma7_distance_pct,
            "volume_min_ratio": s.maha7_volume_min_ratio,
            "min_minutes_after_opening_impulse": s.maha7_min_minutes_after_opening_impulse,
            "reentry_cooldown_seconds": s.maha7_reentry_cooldown_seconds,
            "trend_min_bars": s.maha7_trend_min_bars,
            "min_hold_seconds": s.maha7_min_hold_seconds,
            "max_trades_per_symbol_per_session": s.maha7_max_trades_per_symbol_per_session,
            "symbol_loss_lock_count": s.maha7_symbol_loss_lock_count,
            "early_loss_cut_seconds": s.maha7_early_loss_cut_seconds,
            "early_loss_cut_pct": s.maha7_early_loss_cut_pct,
            "partial_r": s.maha7_partial_r,
            "partial_size": s.maha7_partial_size,
            "target_r": s.maha7_target_r,
            "move_stop_to_entry_after_partial": s.maha7_move_stop_to_entry_after_partial,
            "hard_target_r_exit": s.maha7_hard_target_r_exit,
            "trend_quality_enabled": s.maha7_trend_quality_enabled,
            "min_30m_range_pct": s.maha7_min_30m_range_pct,
            "chop_max_ma_spacing_pct": s.maha7_chop_max_ma_spacing_pct,
            "chop_max_range_pct": s.maha7_chop_max_range_pct,
            "require_higher_low": s.maha7_require_higher_low,
            "allow_continuation": s.maha7_allow_continuation,
            "continuation_pullback_min_pct": s.maha7_continuation_pullback_min_pct,
            "continuation_pullback_max_pct": s.maha7_continuation_pullback_max_pct,
            "reclaim_buffer_pct": s.maha7_reclaim_buffer_pct,
            "allow_early_trend_entry": s.maha7_allow_early_trend_entry,
            "early_trend_max_bars_since_cross": s.maha7_early_trend_max_bars_since_cross,
            "runner_confirm_break_bars": s.maha7_runner_confirm_break_bars,
            "runner_peak_pullback_pct": s.maha7_runner_peak_pullback_pct,
            "swing_lookback": s.maha7_swing_lookback,
            "stop_anchor_buffer_pct": s.maha7_stop_anchor_buffer_pct,
            "min_r_pct": s.maha7_min_r_pct,
            "max_r_pct": s.maha7_max_r_pct,
            "continuation_volume_ratio": s.maha7_continuation_volume_ratio,
            "max_chase_pct": s.maha7_max_chase_pct,
            "recent_high_lookback": s.maha7_recent_high_lookback,
            "momentum_green_bars": s.maha7_momentum_green_bars,
            "disable_ma7_exit": s.maha7_disable_ma7_exit,
            "volume_use_median": s.maha7_volume_use_median,
        }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_tz = MARKET_TZ
        self._last_reject_log_ms: dict[tuple[str, str], int] = {}

    def evaluate(self, state: SymbolState) -> Signal | None:
        if state.last_event_kind != "bar":
            return None
        if not self.is_symbol_allowed(state.symbol):
            return None
        if not self._within_entry_window(state.last_event_ms):
            return self._reject(state, "window", "outside configured entry window")

        bars = self._regular_bars(state)
        lookback_rh = max(5, self.settings.maha7_recent_high_lookback)
        if len(bars) < max(24, lookback_rh + 5):
            return self._reject(state, "history", "insufficient bar history")

        closes = [bar.close for bar in bars]

        # Calculate MA series for point-in-time comparisons (not subset recalculation)
        ma7_series = self._sma_series(closes, 7)
        ma20_series = self._sma_series(closes, 20)
        if ma7_series is None or ma20_series is None or len(ma7_series) < 2 or len(ma20_series) < 2:
            return self._reject(state, "history", "insufficient moving-average history")

        ma7 = ma7_series[-1]
        ma20 = ma20_series[-1]
        prev_ma7 = ma7_series[-2]
        prev_ma20 = ma20_series[-2]

        latest = bars[-1]
        entry_price = latest.close

        ma7_slope_pct = (ma7 - prev_ma7) / prev_ma7 if prev_ma7 > 0 else 0.0
        if ma7 <= ma20:
            return self._reject(state, "trend", "trend not aligned (MA7 not above MA20)")
        if ma7_slope_pct <= 0:
            return self._reject(state, "trend", "MA7 not rising")
        if latest.close <= ma7:
            return self._reject(state, "trend", "price is not above MA7")

        bars_since_crossover = self._bars_since_ma7_cross_above_ma20(closes)
        min_trend_bars = self.settings.maha7_trend_min_bars
        if bars_since_crossover is None or bars_since_crossover < min_trend_bars:
            return self._reject(state, "trend", f"MA7/MA20 trend has not stabilized for {min_trend_bars} bars")

        if self.settings.maha7_trend_quality_enabled:
            if self._is_choppy_market(bars, ma7, ma20, latest.close):
                return self._reject(state, "chop", "tight MA spacing and compressed range (chop filter)")
            if not self._min_30m_range_ok(bars):
                return self._reject(state, "range", "last-30m range below minimum trend quality")
            if self.settings.maha7_require_higher_low and not self._higher_low_structure(bars):
                return self._reject(state, "structure", "no higher-low structure")

        swing_lookback = max(3, self.settings.maha7_swing_lookback)
        swing_low = self._recent_swing_low(bars, swing_lookback)
        if swing_low is None or swing_low >= entry_price:
            return self._reject(state, "risk", "invalid R (swing anchor)")

        buf = self.settings.maha7_stop_anchor_buffer_pct
        stop_price = swing_low * (1.0 - buf)
        r_dist = entry_price - stop_price
        if r_dist <= 0:
            return self._reject(state, "risk", "invalid R")

        r_pct = r_dist / entry_price if entry_price > 0 else 0.0
        min_r = self.settings.maha7_min_r_pct
        max_r = self.settings.maha7_max_r_pct
        if r_pct < min_r:
            return self._reject(state, "risk", "R too small (noise)")
        if r_pct > max_r:
            return self._reject(state, "risk", "R too large (chasing)")

        pullback_max = self.settings.maha7_pullback_ma7_distance_pct
        distance_to_ma7_pct = abs(entry_price - ma7) / ma7 if ma7 > 0 else float("inf")
        pullback_ok = distance_to_ma7_pct <= pullback_max

        # Volume baseline: median (robust to outliers) or mean (sensitive to spikes)
        base_vols = [bar.volume for bar in bars[-4:-1] if bar.volume > 0]
        if self.settings.maha7_volume_use_median:
            vol_denom = median(base_vols) if base_vols else (latest.volume or 1)
        else:
            vol_denom = mean(base_vols) if base_vols else (latest.volume or 1)
        volume_ratio = latest.volume / vol_denom if vol_denom > 0 else 1.0
        cvol = self.settings.maha7_continuation_volume_ratio
        continuation_ok = (
            self.settings.maha7_allow_continuation
            and self._strong_bull_bar(latest)
            and self._close_near_high(latest)
            and volume_ratio >= cvol
        )

        if not (pullback_ok or continuation_ok):
            return self._reject(state, "trigger", "no valid pullback/continuation")

        if len(bars) < 2:
            return self._reject(state, "trigger", "no reclaim / weak structure")
        previous_high = bars[-2].high
        if latest.close < previous_high:
            return self._reject(state, "trigger", "no reclaim / weak structure")

        tail = bars[-lookback_rh:] if len(bars) >= lookback_rh else bars
        recent_high = max(b.high for b in tail)
        if recent_high > 0:
            distance_from_recent_high = (recent_high - entry_price) / recent_high
            if distance_from_recent_high < self.settings.maha7_max_chase_pct:
                return self._reject(
                    state,
                    "chase",
                    f"too close to recent high ({distance_from_recent_high:.2%} < {self.settings.maha7_max_chase_pct:.2%})",
                )

        n_green = max(1, self.settings.maha7_momentum_green_bars)
        if not self._last_n_green_bars(bars, n_green):
            return self._reject(state, "momentum", "no momentum")

        reason = (
            f"maha7 optimized entry (pullback or continuation): MA7 {ma7:.2f} > MA20 {ma20:.2f}, "
            f"R {r_dist:.2f} ({r_pct:.2%} of entry), stop {stop_price:.2f}, vol {volume_ratio:.2f}x"
        )
        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=entry_price,
            timestamp_ms=latest.end_ms,
            change_pct=(entry_price - bars[0].open) / bars[0].open if bars[0].open else 0.0,
            volume_ratio=volume_ratio,
            spread_bps=state.quote.spread_bps if state.quote else None,
            reason=reason,
            stop_price=stop_price,
        )

    def should_exit(self, state: SymbolState, position) -> ExitDecision | None:
        """MAHA7 exits (in order): -1R stop; partial at MAHA7_PARTIAL_R / MAHA7_PARTIAL_SIZE; +MAHA7_TARGET_R
        if MAHA7_HARD_TARGET_R_EXIT; runner pullback vs MAHA7_RUNNER_PEAK_PULLBACK_PCT (after partial);
        after MAHA7_MIN_HOLD_SECONDS, MA7 confirmed breakdown unless MAHA7_DISABLE_MA7_EXIT
        (MAHA7_RUNNER_CONFIRM_BREAK_BARS consecutive closes below bar SMA7 and MA7 slope <= 0). No single-bar
        MA7 or RSI-based exits. Breakeven stop after partial is in execution (MAHA7_MOVE_STOP_TO_ENTRY_AFTER_PARTIAL)."""
        if state.last_event_kind not in {"quote", "bar"} or position.strategy != self.name:
            return None

        price = state.last_price
        if price is None:
            return None

        initial_stop = getattr(position, "initial_stop_price", None)
        if initial_stop is None:
            initial_stop = position.stop_price
        r_initial = position.entry_price - initial_stop
        if r_initial <= 0:
            return None

        bars = self._regular_bars(state)
        closes = [bar.close for bar in bars] if bars else []
        skip_ma7_exit = self.settings.maha7_disable_ma7_exit

        floor_1r = position.entry_price - r_initial
        if price <= floor_1r:
            return ExitDecision("stop loss -1R")

        event_ms = state.last_event_ms or (state.quote.timestamp_ms if state.quote else position.entry_ms)
        age_seconds = (event_ms - position.entry_ms) / 1000
        pnl_pct = (price - position.entry_price) / position.entry_price
        if age_seconds <= self.settings.maha7_early_loss_cut_seconds:
            early_loss_cut_pct = max(0.0, self.settings.maha7_early_loss_cut_pct)
            if pnl_pct <= -early_loss_cut_pct:
                return ExitDecision("early loss cut")

        partial_r = self.settings.maha7_partial_r
        if (
            not position.partial_exit_taken
            and position.shares > 1
            and price >= position.entry_price + (r_initial * partial_r)
        ):
            frac = min(1.0, max(0.0, self.settings.maha7_partial_size))
            shares = max(1, min(position.shares - 1, int(position.shares * frac)))
            return ExitDecision(f"partial {partial_r}R", shares=shares, mark_partial=True)

        if self.settings.maha7_hard_target_r_exit and price >= position.entry_price + (
            r_initial * self.settings.maha7_target_r
        ):
            return ExitDecision(f"target {self.settings.maha7_target_r:.1f}R")

        elapsed_seconds = age_seconds

        if position.partial_exit_taken:
            peak = position.max_price if position.max_price > 0 else position.entry_price
            pull_pct = self.settings.maha7_runner_peak_pullback_pct
            pullback_hit = peak > 0 and price <= peak * (1 - pull_pct)
            if pullback_hit:
                return ExitDecision("runner pullback")

        if elapsed_seconds < self.settings.maha7_min_hold_seconds:
            return None

        if not skip_ma7_exit:
            # Sustained closes below point-in-time SMA7 + MA7 slope <= 0 (no single-bar whipsaw).
            breakdown_n = max(1, self.settings.maha7_runner_confirm_break_bars)
            if len(closes) >= 8 and self._last_n_bars_below_ma7(bars, breakdown_n) and self._ma7_slope_not_positive(
                closes
            ):
                return ExitDecision("MA7 confirmed breakdown")

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
            self.settings.maha7_start_minute,
            self.settings.maha7_min_minutes_after_opening_impulse,
        )
        return min_elapsed <= elapsed <= self.settings.maha7_end_minute

    def _regular_bars(self, state: SymbolState):
        return [
            bar
            for bar in state.bars
            if datetime.fromtimestamp(bar.end_ms / 1000, tz=self.market_tz).time() >= MARKET_OPEN
        ]

    @staticmethod
    def _bars_since_entry(bars, entry_ms: int) -> int:
        return sum(1 for bar in bars if bar.end_ms > entry_ms)

    def _min_30m_range_ok(self, bars) -> bool:
        tail = bars[-30:] if len(bars) >= 30 else bars
        if len(tail) < 5:
            return False
        high = max(b.high for b in tail)
        low = min(b.low for b in tail)
        mid = (high + low) / 2
        if mid <= 0:
            return False
        return (high - low) / mid >= self.settings.maha7_min_30m_range_pct

    def _is_choppy_market(self, bars, ma7: float, ma20: float, last_close: float) -> bool:
        tail = bars[-30:] if len(bars) >= 30 else bars
        if len(tail) < 5 or last_close <= 0:
            return False
        high = max(b.high for b in tail)
        low = min(b.low for b in tail)
        mid = (high + low) / 2
        range_pct = (high - low) / mid if mid > 0 else 0.0
        spacing = abs(ma7 - ma20) / last_close
        return (
            spacing <= self.settings.maha7_chop_max_ma_spacing_pct
            and range_pct < self.settings.maha7_chop_max_range_pct
        )

    @staticmethod
    def _higher_low_structure(bars) -> bool:
        if len(bars) < 6:
            return False
        lows = [bar.low for bar in bars[-6:]]
        return lows[-1] > lows[-3] and lows[-2] >= lows[-4] * 0.998



    def _ma7_slope_not_positive(self, closes: list[float]) -> bool:
        """True when MA7 slope <= 0 (flat or down): SMA7(now) <= SMA7(previous bar).

        Uses point-in-time MA7 series, not subset recalculation.
        """
        ma7_series = self._sma_series(closes, 7)
        if ma7_series is None or len(ma7_series) < 2:
            return False
        return ma7_series[-1] <= ma7_series[-2]

    def _last_n_bars_below_ma7(self, bars, n: int) -> bool:
        """Each of the last ``n`` completed bars closes below that bar's SMA7 (point-in-time)."""
        if n <= 0 or len(bars) < n + 7:
            return False
        closes_all = [b.close for b in bars]
        for k in range(n):
            idx = len(bars) - n + k
            ma = self._sma(closes_all[: idx + 1], 7)
            if ma is None or bars[idx].close >= ma:
                return False
        return True

    @staticmethod
    def _sma(values: list[float], window: int) -> float | None:
        """Calculate single SMA value (last window only)."""
        if len(values) < window:
            return None
        return mean(values[-window:])

    @staticmethod
    def _sma_series(values: list[float], window: int) -> list[float] | None:
        """Calculate full SMA series for all values (point-in-time calculation).

        Each SMA value is calculated using only the data available at that bar,
        preventing lookahead bias and enabling correct historical comparisons.
        """
        if len(values) < window:
            return None
        series = []
        for i in range(len(values)):
            if i + 1 < window:
                # Partial window for early bars (use all available data)
                series.append(mean(values[:i + 1]))
            else:
                # Full window
                series.append(mean(values[i + 1 - window : i + 1]))
        return series


    @staticmethod
    def _session_vwap(bars) -> float:
        volume = sum(bar.volume for bar in bars if bar.volume > 0)
        if volume <= 0:
            return bars[-1].vwap
        return sum(bar.vwap * bar.volume for bar in bars if bar.volume > 0) / volume


    def _bars_since_ma7_cross_above_ma20(self, closes: list[float]) -> int | None:
        """Count bars since MA7 crossed above MA20 (optimized with series calculation)."""
        if len(closes) < 20:
            return None

        # Calculate full series once instead of recalculating for each bar
        ma7_series = self._sma_series(closes, 7)
        ma20_series = self._sma_series(closes, 20)
        if ma7_series is None or ma20_series is None:
            return None

        # Compare MA7 > MA20 for bars where both are available (from bar 20 onward)
        above_flags = [ma7 > ma20 for ma7, ma20 in zip(ma7_series[20:], ma20_series[20:])]

        if not above_flags or not above_flags[-1]:
            return None

        # Count consecutive bars where MA7 > MA20
        streak = 0
        for is_above in reversed(above_flags):
            if not is_above:
                break
            streak += 1

        return max(0, streak - 1)



    @staticmethod
    def _recent_swing_low(bars, lookback: int) -> float | None:
        """Pivot (or min low) in the last ``lookback`` completed bars only (excludes current bar)."""
        if lookback < 1 or len(bars) < lookback + 1:
            return None
        search = bars[-(lookback + 1) : -1]
        if not search:
            return None
        if len(search) < 3:
            return min(b.low for b in search)
        for index in range(1, len(search) - 1):
            previous_bar = search[index - 1]
            current = search[index]
            next_bar = search[index + 1]
            if current.low <= previous_bar.low and current.low <= next_bar.low:
                return current.low
        return min(b.low for b in search)

    @staticmethod
    def _strong_bull_bar(bar) -> bool:
        rng = bar.high - bar.low
        if rng <= 0:
            return False
        body = bar.close - bar.open
        return bar.close > bar.open and body >= 0.45 * rng

    @staticmethod
    def _close_near_high(bar) -> bool:
        rng = bar.high - bar.low
        if rng <= 0:
            return True
        return (bar.high - bar.close) <= 0.25 * rng

    @staticmethod
    def _last_n_green_bars(bars, n: int) -> bool:
        if len(bars) < n or n <= 0:
            return False
        tail = bars[-n:]
        return all(b.close > b.open for b in tail)

    def _reject(self, state: SymbolState, code: str, detail: str) -> None:
        timestamp_ms = state.last_event_ms or 0
        key = (state.symbol, code)
        last_log_ms = self._last_reject_log_ms.get(key, -10_000)
        if timestamp_ms - last_log_ms >= 10_000:
            self._last_reject_log_ms[key] = timestamp_ms
            LOG.debug("No maha7 entry %s: %s", state.symbol, detail)
        return None
