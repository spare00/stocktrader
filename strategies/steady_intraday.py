from __future__ import annotations

from dataclasses import dataclass
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


@dataclass
class _PositionState:
    """Track per-symbol position state for grace period and pyramid adds."""
    entry_ms: int
    bars_since_partial: int = 0
    last_processed_bar_end_ms: int | None = None
    pyramid_tranche: int = 0
    reference_entry: float = 0.0
    reference_stop: float = 0.0
    reference_r: float = 0.0
    last_add_price: float = 0.0
    last_add_ms: int = 0
    pyramid_adds_blocked: bool = False


class SteadyIntradayStrategy(Strategy):
    """VWAP/EMA trend-following day strategy with ATR risk and same-day exits.

    Entry Logic:
    - Bullish EMA9 > EMA20 > EMA50 stack with rising EMA20 and VWAP
    - ATR in acceptable range (0.18% - 1.5%)
    - Not too extended from VWAP (< 2.5%) or EMA20 (< 1.2%)
    - Triggers: pullback reclaim (previous <= EMA9, current > previous high & EMA9)
                OR opening range breakout continuation
    - Volume confirmation on trigger

    Pyramid (Livermore-style add-to-winners, optional):
    - Scout entry uses first tranche size (default 25% of max position budget)
    - Adds on strength at configured R levels (default 0.75R, 1.25R from scout entry)
    - Requires trend health (EMA stack, VWAP), volume, and bullish bar on add
    - Blocks further adds after partial exit; optional add-stop after tranche 2+

    Exit Logic:
    - Partial at 1R (default 50%), or 1.5R when pyramid tranche 2+ is filled
    - Target at 2R (R measured from scout entry / initial stop)
    - Runner pullback: 0.9% from peak after partial (with grace period)
    - EMA9 breakdown: N bars below EMA9 (uses per-bar EMA values)
    - VWAP loss: price drops below session VWAP
    - Stall: 25+ minutes with < 0.35R AND showing weakness (price < EMA9 or declining)
    """

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
        ("steady_intraday_extended_entry_open_pct", "STEADY_INTRADAY_EXTENDED_ENTRY_OPEN_PCT", float_env, 0.04),
        (
            "steady_intraday_deep_extended_entry_open_pct",
            "STEADY_INTRADAY_DEEP_EXTENDED_ENTRY_OPEN_PCT",
            float_env,
            0.06,
        ),
        (
            "steady_intraday_extended_entry_size_multiplier",
            "STEADY_INTRADAY_EXTENDED_ENTRY_SIZE_MULTIPLIER",
            float_env,
            0.35,
        ),
        (
            "steady_intraday_deep_extended_entry_size_multiplier",
            "STEADY_INTRADAY_DEEP_EXTENDED_ENTRY_SIZE_MULTIPLIER",
            float_env,
            0.35,
        ),
        ("steady_intraday_stop_atr_multiple", "STEADY_INTRADAY_STOP_ATR_MULTIPLE", float_env, 1.1),
        ("steady_intraday_stop_buffer_pct", "STEADY_INTRADAY_STOP_BUFFER_PCT", float_env, 0.0008),
        ("steady_intraday_min_r_pct", "STEADY_INTRADAY_MIN_R_PCT", float_env, 0.0025),
        ("steady_intraday_max_r_pct", "STEADY_INTRADAY_MAX_R_PCT", float_env, 0.012),
        ("steady_intraday_partial_r", "STEADY_INTRADAY_PARTIAL_R", float_env, 1.0),
        ("steady_intraday_partial_size", "STEADY_INTRADAY_PARTIAL_SIZE", float_env, 0.5),
        ("steady_intraday_target_r", "STEADY_INTRADAY_TARGET_R", float_env, 2.0),
        ("steady_intraday_runner_pullback_pct", "STEADY_INTRADAY_RUNNER_PULLBACK_PCT", float_env, 0.009),
        ("steady_intraday_runner_pullback_grace_bars", "STEADY_INTRADAY_RUNNER_PULLBACK_GRACE_BARS", int_env, 2),
        ("steady_intraday_breakdown_bars", "STEADY_INTRADAY_BREAKDOWN_BARS", int_env, 2),
        ("steady_intraday_stall_minutes", "STEADY_INTRADAY_STALL_MINUTES", int_env, 25),
        ("steady_intraday_stall_min_r", "STEADY_INTRADAY_STALL_MIN_R", float_env, 0.35),
        ("steady_intraday_stall_require_weakness", "STEADY_INTRADAY_STALL_REQUIRE_WEAKNESS", bool_env, True),
        ("steady_intraday_reclaim_tolerance_pct", "STEADY_INTRADAY_RECLAIM_TOLERANCE_PCT", float_env, 0.002),
        ("steady_intraday_support_tolerance_pct", "STEADY_INTRADAY_SUPPORT_TOLERANCE_PCT", float_env, 0.003),
        ("steady_intraday_ema_lookback_bars", "STEADY_INTRADAY_EMA_LOOKBACK_BARS", int_env, 3),
        ("steady_intraday_vwap_lookback_bars", "STEADY_INTRADAY_VWAP_LOOKBACK_BARS", int_env, 5),
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
        ("steady_intraday_pyramid_enabled", "STEADY_INTRADAY_PYRAMID_ENABLED", bool_env, True),
        ("steady_intraday_pyramid_min_add_seconds", "STEADY_INTRADAY_PYRAMID_MIN_ADD_SECONDS", int_env, 60),
        ("steady_intraday_pyramid_add_volume_ratio", "STEADY_INTRADAY_PYRAMID_ADD_VOLUME_RATIO", float_env, 1.15),
        ("steady_intraday_pyramid_add_stop_pct", "STEADY_INTRADAY_PYRAMID_ADD_STOP_PCT", float_env, 0.01),
        ("steady_intraday_pyramid_partial_r", "STEADY_INTRADAY_PYRAMID_PARTIAL_R", float_env, 1.5),
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
            "extended_entry_open_pct": settings.steady_intraday_extended_entry_open_pct,
            "deep_extended_entry_open_pct": settings.steady_intraday_deep_extended_entry_open_pct,
            "extended_entry_size_multiplier": settings.steady_intraday_extended_entry_size_multiplier,
            "deep_extended_entry_size_multiplier": settings.steady_intraday_deep_extended_entry_size_multiplier,
            "stop_atr_multiple": settings.steady_intraday_stop_atr_multiple,
            "stop_buffer_pct": settings.steady_intraday_stop_buffer_pct,
            "min_r_pct": settings.steady_intraday_min_r_pct,
            "max_r_pct": settings.steady_intraday_max_r_pct,
            "partial_r": settings.steady_intraday_partial_r,
            "target_r": settings.steady_intraday_target_r,
            "runner_pullback_pct": settings.steady_intraday_runner_pullback_pct,
            "runner_pullback_grace_bars": settings.steady_intraday_runner_pullback_grace_bars,
            "breakdown_bars": settings.steady_intraday_breakdown_bars,
            "stall_minutes": settings.steady_intraday_stall_minutes,
            "stall_min_r": settings.steady_intraday_stall_min_r,
            "stall_require_weakness": bool(settings.steady_intraday_stall_require_weakness),
            "reclaim_tolerance_pct": settings.steady_intraday_reclaim_tolerance_pct,
            "support_tolerance_pct": settings.steady_intraday_support_tolerance_pct,
            "ema_lookback_bars": settings.steady_intraday_ema_lookback_bars,
            "vwap_lookback_bars": settings.steady_intraday_vwap_lookback_bars,
            "position_size_multiplier": settings.steady_intraday_position_size_multiplier,
            "max_trades_per_symbol_per_session": settings.steady_intraday_max_trades_per_symbol_per_session,
            "symbol_loss_lock_count": settings.steady_intraday_symbol_loss_lock_count,
            "pyramid_enabled": bool(settings.steady_intraday_pyramid_enabled),
            "pyramid_tranche_sizes": list(settings.steady_intraday_pyramid_tranche_sizes),
            "pyramid_add_r_levels": list(settings.steady_intraday_pyramid_add_r_levels),
            "pyramid_min_add_seconds": settings.steady_intraday_pyramid_min_add_seconds,
            "pyramid_add_volume_ratio": settings.steady_intraday_pyramid_add_volume_ratio,
            "pyramid_add_stop_pct": settings.steady_intraday_pyramid_add_stop_pct,
            "pyramid_partial_r": settings.steady_intraday_pyramid_partial_r,
        }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_tz = MARKET_TZ
        self._last_reject_log_ms: dict[tuple[str, str], int] = {}
        self._position_states: dict[str, _PositionState] = {}

    def evaluate(self, state: SymbolState) -> Signal | None:
        if not self.is_symbol_allowed(state.symbol):
            return None

        symbol = state.symbol.strip().upper()
        if self._pyramid_enabled():
            pos_state = self._position_states.get(symbol)
            if pos_state and pos_state.pyramid_tranche > 0:
                if (
                    pos_state.pyramid_tranche < self._max_pyramid_tranches()
                    and not pos_state.pyramid_adds_blocked
                ):
                    add_signal = self._evaluate_pyramid_add(state, pos_state)
                    if add_signal is not None:
                        return add_signal
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
        ema_fast_series = self._ema_series(closes, self.settings.steady_intraday_ema_fast)
        ema_mid_series = self._ema_series(closes, self.settings.steady_intraday_ema_mid)
        ema_slow_series = self._ema_series(closes, self.settings.steady_intraday_ema_slow)
        if ema_fast_series is None or ema_mid_series is None or ema_slow_series is None:
            return self._reject(state, "history", "insufficient EMA history")

        ema_fast = ema_fast_series[-1]
        ema_mid = ema_mid_series[-1]
        ema_slow = ema_slow_series[-1]

        # Check EMA20 rising by comparing to N bars ago (configurable lookback)
        ema_lookback = max(1, self.settings.steady_intraday_ema_lookback_bars)
        if len(ema_mid_series) <= ema_lookback:
            return self._reject(state, "history", "insufficient EMA history for rising check")
        prev_ema_mid = ema_mid_series[-1 - ema_lookback]

        # Calculate session VWAP series for rising check
        vwap_lookback = max(1, self.settings.steady_intraday_vwap_lookback_bars)
        if len(bars) <= vwap_lookback:
            return self._reject(state, "history", "insufficient bars for VWAP rising check")
        session_vwap = self._session_vwap(bars)
        prev_vwap = self._session_vwap(bars[:-vwap_lookback])
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

        entry_open_pct = (entry - bars[0].open) / bars[0].open if bars[0].open else 0.0

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
        if (
            entry_open_pct > self.settings.steady_intraday_extended_entry_open_pct
            and trigger != "pullback_reclaim"
        ):
            return self._reject(
                state,
                "extension",
                (
                    f"extended from open entry_open={entry_open_pct:.2%} "
                    f"needs pullback reclaim not {trigger}"
                ),
            )

        stop_price = self._stop_price(bars, entry, ema_mid, session_vwap, atr)
        r_pct = (entry - stop_price) / entry if entry > 0 else 0.0
        if r_pct < self.settings.steady_intraday_min_r_pct:
            return self._reject(state, "risk", "R too small")
        if r_pct > self.settings.steady_intraday_max_r_pct:
            return self._reject(state, "risk", "R too wide")

        position_size_multiplier = self.settings.steady_intraday_position_size_multiplier
        if entry_open_pct > self.settings.steady_intraday_extended_entry_open_pct:
            position_size_multiplier *= self.settings.steady_intraday_extended_entry_size_multiplier
        if entry_open_pct > self.settings.steady_intraday_deep_extended_entry_open_pct:
            position_size_multiplier *= self.settings.steady_intraday_deep_extended_entry_size_multiplier

        if self._pyramid_enabled():
            tranche_sizes = self._pyramid_tranche_sizes()
            position_size_multiplier *= tranche_sizes[0]
            self._begin_pyramid_state(symbol, entry, stop_price, latest.end_ms)

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
            change_pct=entry_open_pct,
            volume_ratio=volume_ratio,
            spread_bps=spread_bps,
            reason=reason,
            stop_price=stop_price,
            session_open_price=bars[0].open,
            entry_open_pct=entry_open_pct,
            position_size_multiplier=position_size_multiplier,
        )

    def on_entry_fill(self, fill) -> None:
        """Track position state for grace period and pyramid reference prices."""
        if getattr(fill, "strategy", "") != self.name:
            return
        symbol = str(getattr(fill, "symbol", "")).strip().upper()
        timestamp_ms = getattr(fill, "timestamp_ms", None)
        fill_price = float(getattr(fill, "price", 0) or 0)
        if not symbol or timestamp_ms is None or fill_price <= 0:
            return

        pos_state = self._position_states.get(symbol)
        if pos_state is None:
            self._position_states[symbol] = _PositionState(entry_ms=int(timestamp_ms))
            pos_state = self._position_states[symbol]

        reason = str(getattr(fill, "reason", ""))
        if self._pyramid_enabled() and "pyramid_tranche_" in reason:
            pos_state.last_add_price = fill_price
            pos_state.last_add_ms = int(timestamp_ms)
            return

        pos_state.entry_ms = int(timestamp_ms)
        if self._pyramid_enabled():
            pos_state.last_add_price = fill_price
            pos_state.last_add_ms = int(timestamp_ms)
            if pos_state.reference_entry <= 0:
                pos_state.reference_entry = fill_price

    def on_exit_fill(self, fill) -> None:
        if getattr(fill, "strategy", "") != self.name:
            return
        symbol = str(getattr(fill, "symbol", "")).strip().upper()
        if not symbol:
            return
        if getattr(fill, "exit_stage", "") == "partial":
            pos_state = self._position_states.get(symbol)
            if pos_state is not None:
                pos_state.pyramid_adds_blocked = True
                pos_state.bars_since_partial = 0
            return
        self._clear_position_state(symbol)

    def should_exit(self, state: SymbolState, position) -> ExitDecision | None:
        if state.last_event_kind not in {"quote", "bar"} or position.strategy != self.name:
            return None

        price = state.last_price
        if price is None:
            return None

        symbol = position.symbol.strip().upper()
        pos_state = self._position_states.get(symbol)
        initial_stop = position.initial_stop_price or position.stop_price
        if pos_state is not None and pos_state.reference_stop > 0:
            initial_stop = pos_state.reference_stop
        reference_entry = (
            pos_state.reference_entry if pos_state is not None and pos_state.reference_entry > 0 else position.entry_price
        )
        r_initial = reference_entry - initial_stop
        if r_initial <= 0:
            return None

        if (
            self._pyramid_enabled()
            and pos_state is not None
            and pos_state.pyramid_tranche >= 2
            and pos_state.last_add_price > 0
        ):
            add_stop = pos_state.last_add_price * (1 - self.settings.steady_intraday_pyramid_add_stop_pct)
            if price < add_stop:
                self._clear_position_state(symbol)
                return ExitDecision("pyramid add stop")

        pyramid_blocks_partial = (
            self._pyramid_enabled()
            and pos_state is not None
            and pos_state.pyramid_tranche > 0
            and pos_state.pyramid_tranche < 2
        )
        partial_r = self.settings.steady_intraday_partial_r
        if self._pyramid_enabled() and pos_state is not None and pos_state.pyramid_tranche >= 2:
            partial_r = self.settings.steady_intraday_pyramid_partial_r

        # Partial exit at target R-multiple
        if not position.partial_exit_taken and position.shares > 1 and not pyramid_blocks_partial:
            partial_level = reference_entry + r_initial * partial_r
            if price >= partial_level:
                fraction = min(1.0, max(0.0, self.settings.steady_intraday_partial_size))
                shares = max(1, min(position.shares - 1, int(position.shares * fraction)))
                return ExitDecision(f"partial {partial_r:.1f}R", shares=shares, mark_partial=True)

        # Full exit at target R-multiple
        target_level = reference_entry + r_initial * self.settings.steady_intraday_target_r
        if price >= target_level:
            return ExitDecision(f"target {self.settings.steady_intraday_target_r:.1f}R")

        # Runner pullback exit (with grace period after partial)
        if position.partial_exit_taken:
            pos_state = self._position_states.get(symbol)
            grace_bars = max(0, self.settings.steady_intraday_runner_pullback_grace_bars)

            # Sync bar count on bar events
            if state.last_event_kind == "bar" and pos_state is not None:
                self._sync_position_bar_state(state, pos_state)

            # Apply grace period before runner pullback can fire
            can_exit_runner = pos_state is None or pos_state.bars_since_partial > grace_bars

            if can_exit_runner:
                peak = position.max_price if position.max_price > 0 else position.entry_price
                if peak > 0 and price <= peak * (1 - self.settings.steady_intraday_runner_pullback_pct):
                    self._clear_position_state(symbol)
                    return ExitDecision("runner pullback")

        # Structural exits: EMA9 breakdown and VWAP loss
        bars = self._regular_bars(state)
        if len(bars) >= max(3, self.settings.steady_intraday_ema_fast + 2):
            closes = [bar.close for bar in bars]
            ema_fast_series = self._ema_series(closes, self.settings.steady_intraday_ema_fast)
            session_vwap = self._session_vwap(bars)

            # EMA9 breakdown: use per-bar EMA values, not final value
            breakdown_bars = max(1, self.settings.steady_intraday_breakdown_bars)
            if ema_fast_series and len(bars) >= breakdown_bars and len(ema_fast_series) == len(bars):
                if self._last_n_closes_below_ema_series(bars, ema_fast_series, breakdown_bars):
                    symbol = position.symbol.strip().upper()
                    self._clear_position_state(symbol)
                    return ExitDecision("EMA fast breakdown")

            # VWAP loss
            if session_vwap and price < session_vwap:
                symbol = position.symbol.strip().upper()
                self._clear_position_state(symbol)
                return ExitDecision("lost VWAP")

        # Stall exit (with optional weakness requirement)
        event_ms = state.last_event_ms or position.entry_ms
        age_minutes = (event_ms - position.entry_ms) / 60_000
        current_r = (price - reference_entry) / r_initial

        if (
            age_minutes >= self.settings.steady_intraday_stall_minutes
            and current_r < self.settings.steady_intraday_stall_min_r
        ):
            # Optional: require weakness signal (price below EMA9 or declining from peak)
            if self.settings.steady_intraday_stall_require_weakness:
                bars = self._regular_bars(state)
                if len(bars) >= max(3, self.settings.steady_intraday_ema_fast + 2):
                    closes = [bar.close for bar in bars]
                    ema_fast_series = self._ema_series(closes, self.settings.steady_intraday_ema_fast)
                    if ema_fast_series:
                        ema_fast = ema_fast_series[-1]
                        peak = position.max_price if position.max_price > 0 else position.entry_price
                        is_weak = price < ema_fast or (peak > 0 and price < peak * 0.998)
                        if is_weak:
                            symbol = position.symbol.strip().upper()
                            self._clear_position_state(symbol)
                            return ExitDecision("stalled (weak)")
            else:
                # Stall without weakness check
                symbol = position.symbol.strip().upper()
                self._clear_position_state(symbol)
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
        """Detect entry trigger: pullback reclaim or ORB continuation.

        Pullback reclaim: previous bar at/below EMA9, current bar closes above both
        previous high and EMA9, held support at EMA20/VWAP, bullish close near high.

        ORB continuation: breaks above opening range high with bullish structure.
        """
        latest = bars[-1]
        previous = bars[-2]

        # Use configurable tolerances instead of hardcoded values
        reclaim_tolerance = 1.0 + max(0.0, self.settings.steady_intraday_reclaim_tolerance_pct)
        support_tolerance = 1.0 - max(0.0, self.settings.steady_intraday_support_tolerance_pct)

        reclaimed_fast = previous.close <= ema_fast * reclaim_tolerance and latest.close > max(previous.high, ema_fast)
        held_mid = latest.low >= min(ema_mid, session_vwap) * support_tolerance
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
                and previous.close <= opening_high * reclaim_tolerance
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
        """Calculate final EMA value. For series, use _ema_series()."""
        if period <= 0 or len(values) < period:
            return None
        alpha = 2 / (period + 1)
        ema = sum(values[:period]) / period
        for value in values[period:]:
            ema = (value * alpha) + (ema * (1 - alpha))
        return ema

    @staticmethod
    def _ema_series(values: list[float], period: int) -> list[float] | None:
        """Calculate full EMA series for all values."""
        if period <= 0 or len(values) < period:
            return None
        alpha = 2 / (period + 1)
        series = []
        ema = sum(values[:period]) / period
        # Add initial SMA value for first period bars
        for _ in range(period):
            series.append(ema)
        # Calculate EMA for remaining values
        for value in values[period:]:
            ema = (value * alpha) + (ema * (1 - alpha))
            series.append(ema)
        return series

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
        """Check if last N bar closes are below a fixed level."""
        if len(bars) < n:
            return False
        return all(bar.close < level for bar in bars[-n:])

    @staticmethod
    def _last_n_closes_below_ema_series(bars, ema_series: list[float], n: int) -> bool:
        """Check if last N bar closes are below their corresponding EMA values.

        This correctly compares each bar's close to its EMA value, not to the final EMA.
        """
        if len(bars) < n or len(ema_series) < n or len(bars) != len(ema_series):
            return False
        for i in range(-n, 0):
            if bars[i].close >= ema_series[i]:
                return False
        return True

    def _sync_position_bar_state(self, state: SymbolState, pos_state: _PositionState) -> None:
        """Update bars_since_partial counter on new bar completion."""
        if not state.bars:
            return
        bar_end_ms = state.bars[-1].end_ms
        if bar_end_ms == pos_state.last_processed_bar_end_ms:
            return
        pos_state.last_processed_bar_end_ms = bar_end_ms
        pos_state.bars_since_partial += 1

    def _clear_position_state(self, symbol: str) -> None:
        """Clear position state on exit."""
        self._position_states.pop(symbol.strip().upper(), None)

    def _pyramid_enabled(self) -> bool:
        return bool(self.settings.steady_intraday_pyramid_enabled)

    def _pyramid_tranche_sizes(self) -> list[float]:
        sizes = list(self.settings.steady_intraday_pyramid_tranche_sizes)
        if not sizes:
            return [0.25, 0.35, 0.40]
        return sizes

    def _pyramid_add_r_levels(self) -> list[float]:
        levels = list(self.settings.steady_intraday_pyramid_add_r_levels)
        if not levels:
            return [0.75, 1.25]
        return levels

    def _max_pyramid_tranches(self) -> int:
        return len(self._pyramid_tranche_sizes())

    def _begin_pyramid_state(self, symbol: str, entry: float, stop_price: float, timestamp_ms: int) -> None:
        reference_r = entry - stop_price
        if reference_r <= 0:
            reference_r = entry * self.settings.steady_intraday_min_r_pct
        self._position_states[symbol] = _PositionState(
            entry_ms=timestamp_ms,
            pyramid_tranche=1,
            reference_entry=entry,
            reference_stop=stop_price,
            reference_r=reference_r,
            last_add_price=entry,
            last_add_ms=timestamp_ms,
        )

    def _evaluate_pyramid_add(self, state: SymbolState, pos_state: _PositionState) -> Signal | None:
        if state.last_event_kind != "bar":
            return None
        if should_flatten_before_close(state.last_event_ms, self.settings.flatten_before_close_minutes + 5):
            return None
        if not self._within_entry_window(state.last_event_ms):
            return None

        bars = self._regular_bars(state)
        min_bars = max(self.settings.steady_intraday_min_bars, self.settings.steady_intraday_ema_slow + 5)
        if len(bars) < min_bars:
            return None

        latest = bars[-1]
        entry = latest.close
        if entry < self.settings.steady_intraday_min_price:
            return None

        spread_bps = state.quote.spread_bps if state.quote else None
        if spread_bps is not None and spread_bps > self.settings.steady_intraday_max_spread_bps:
            return None

        closes = [bar.close for bar in bars]
        ema_fast_series = self._ema_series(closes, self.settings.steady_intraday_ema_fast)
        ema_mid_series = self._ema_series(closes, self.settings.steady_intraday_ema_mid)
        ema_slow_series = self._ema_series(closes, self.settings.steady_intraday_ema_slow)
        if ema_fast_series is None or ema_mid_series is None or ema_slow_series is None:
            return None

        ema_fast = ema_fast_series[-1]
        ema_mid = ema_mid_series[-1]
        ema_slow = ema_slow_series[-1]
        session_vwap = self._session_vwap(bars)
        if session_vwap is None:
            return None

        if not (ema_fast > ema_mid > ema_slow):
            return None
        if entry <= session_vwap * (1 + self.settings.steady_intraday_vwap_buffer_pct):
            return None

        add_r_levels = self._pyramid_add_r_levels()
        level_idx = pos_state.pyramid_tranche - 1
        if level_idx < 0 or level_idx >= len(add_r_levels):
            return None
        if pos_state.reference_r <= 0:
            return None

        trigger_price = pos_state.reference_entry + add_r_levels[level_idx] * pos_state.reference_r
        if entry < trigger_price:
            return None

        min_add_ms = max(0, self.settings.steady_intraday_pyramid_min_add_seconds) * 1000
        if state.last_event_ms - pos_state.last_add_ms < min_add_ms:
            return None

        volume_ratio = self._volume_ratio(bars)
        if volume_ratio < self.settings.steady_intraday_pyramid_add_volume_ratio:
            return None
        if not self._close_near_high(latest):
            return None

        tranche_sizes = self._pyramid_tranche_sizes()
        next_tranche_idx = pos_state.pyramid_tranche
        if next_tranche_idx >= len(tranche_sizes):
            return None

        add_size_pct = tranche_sizes[next_tranche_idx]
        pos_state.pyramid_tranche += 1
        pos_state.last_add_ms = state.last_event_ms
        pos_state.last_add_price = entry

        stop_price = pos_state.reference_stop
        reason = (
            f"steady_intraday pyramid_tranche_{pos_state.pyramid_tranche}: "
            f"add at {entry:.2f} >= trigger {trigger_price:.2f} "
            f"({add_r_levels[level_idx]:.2f}R), vol {volume_ratio:.2f}x"
        )
        LOG.info(
            "Steady intraday pyramid add %s tranche %d/%d at %.2f (trigger %.2f)",
            state.symbol,
            pos_state.pyramid_tranche,
            self._max_pyramid_tranches(),
            entry,
            trigger_price,
        )
        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=entry,
            timestamp_ms=latest.end_ms,
            change_pct=0.0,
            volume_ratio=volume_ratio,
            spread_bps=spread_bps,
            reason=reason,
            stop_price=stop_price,
            position_size_multiplier=add_size_pct * self.settings.steady_intraday_position_size_multiplier,
            allow_add_to_position=True,
        )

    def _reject(self, state: SymbolState, code: str, detail: str) -> None:
        timestamp_ms = state.last_event_ms or 0
        key = (state.symbol, code)
        last_log_ms = self._last_reject_log_ms.get(key, -30_000)
        if timestamp_ms - last_log_ms >= 30_000:
            self._last_reject_log_ms[key] = timestamp_ms
            LOG.debug("No steady_intraday entry %s [%s]: %s", state.symbol, code, detail)
            self.record_signal_block(state, code, detail)
        return None
