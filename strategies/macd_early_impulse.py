from __future__ import annotations

from datetime import datetime, time
import json
import logging
from pathlib import Path
from statistics import median
from typing import Any, ClassVar

from candle import SymbolState
from config import Settings
from env_vars import EnvSpec, bool_env, float_env, int_env
from market_hours import MARKET_TZ
from models import ExitDecision, Signal
from strategy_selectors.select_gap_and_go import latest_valid_quote, regular_bars
from strategies.base import Strategy
from modules.indicator_history import continuous_indicator_bars


LOG = logging.getLogger(__name__)
MARKET_OPEN = time(9, 30)
PREMARKET_OPEN = time(4, 0)

# MACD uses continuous real bars for warmup; entry structure only needs a few regular-session bars.
_MIN_REGULAR_BARS = 3
_NEAR_HIGH_TOLERANCE_PCT = 0.003
_RECENT_HIGH_LOOKBACK = 15
_RECLAIM_HIGH_LOOKBACK = 10
_OVEREXTEND_MAX_PCT = 0.003
_VWAP_EXTENSION_MAX_PCT = 0.025
_EMA_TREND_PERIOD = 12
_RUNNER_PLAN_TOP_RANK = 12
_RUNNER_SESSION_RANGE_PCT = 0.012
_RUNNER_SESSION_TOP_ZONE = 0.60
_RUNNER_MIN_VOLUME_RATIO = 0.95
_RUNNER_HIST_THRESHOLD = 0.00035
_RUNNER_OVEREXTEND_MAX_PCT = 0.008
_RUNNER_VWAP_EXTENSION_MAX_PCT = 0.04
_RUNNER_STOP_LOOKBACK = 6
_RUNNER_STOP_BUFFER_PCT = 0.001
_RUNNER_STOP_MIN_PCT = 0.0055
_RUNNER_STOP_MAX_PCT = 0.012
_RUNNER_EARLY_LOSS_CUT_SECONDS = 180
_RUNNER_EARLY_LOSS_CUT_PCT = 0.0055
_RUNNER_MIN_HOLD_SECONDS = 120
_RUNNER_TRAIL_ACTIVATION_PCT = 0.006
_RUNNER_TRAIL_STOP_PCT = 0.006
_RUNNER_MOMENTUM_EXIT_MIN_PROFIT_PCT = 0.006
_RUNNER_TREND_HOLD_BUFFER_PCT = 0.0015
_RUNNER_WEAK_HIST_RATIO = 0.65


def _ema_series(values: list[float], period: int) -> list[float]:
    """Standard recursive EMA (TradingView-style seed from first bar)."""
    if not values or period <= 0:
        return []
    alpha = 2.0 / (period + 1)
    out: list[float] = []
    ema = values[0]
    for v in values:
        ema = alpha * v + (1.0 - alpha) * ema
        out.append(ema)
    return out


class MACDEarlyImpulseStrategy(Strategy):
    name = "macd_early_impulse"
    requires_plan: ClassVar[bool] = False
    env_specs: ClassVar[tuple[EnvSpec, ...]] = (
        ("macd_start_minute", "MACD_START_MINUTE", int_env, 0),
        ("macd_end_minute", "MACD_END_MINUTE", int_env, 360),
        ("macd_hist_threshold", "MACD_HIST_THRESHOLD", float_env, 0.001),
        ("macd_volume_ratio", "MACD_VOLUME_RATIO", float_env, 1.35),
        ("macd_target_profit_pct", "MACD_TARGET_PROFIT_PCT", float_env, 0.012),
        ("macd_stop_loss_pct", "MACD_STOP_LOSS_PCT", float_env, 0.0035),
        ("macd_trailing_stop_pct", "MACD_TRAILING_STOP_PCT", float_env, 0.0045),
        ("macd_trailing_activation_pct", "MACD_TRAILING_ACTIVATION_PCT", float_env, 0.003),
        ("macd_chop_range_pct", "MACD_CHOP_RANGE_PCT", float_env, 0.0035),
        ("macd_max_spread_bps", "MACD_MAX_SPREAD_BPS", float_env, 15.0),
        ("macd_atr_period", "MACD_ATR_PERIOD", int_env, 14),
        ("macd_min_atr_pct", "MACD_MIN_ATR_PCT", float_env, 0.0015),
        ("macd_max_atr_pct", "MACD_MAX_ATR_PCT", float_env, 0.0300),
        ("macd_range_lookback_bars", "MACD_RANGE_LOOKBACK_BARS", int_env, 20),
        ("macd_min_range_pct", "MACD_MIN_RANGE_PCT", float_env, 0.0040),
        ("macd_structure_lookback_bars", "MACD_STRUCTURE_LOOKBACK_BARS", int_env, 6),
        ("macd_stop_buffer_pct", "MACD_STOP_BUFFER_PCT", float_env, 0.0008),
        ("macd_min_r_pct", "MACD_MIN_R_PCT", float_env, 0.0020),
        ("macd_max_r_pct", "MACD_MAX_R_PCT", float_env, 0.0150),
        ("macd_partial_r", "MACD_PARTIAL_R", float_env, 1.0),
        ("macd_partial_size", "MACD_PARTIAL_SIZE", float_env, 0.5),
        ("macd_target_r", "MACD_TARGET_R", float_env, 2.0),
        ("macd_runner_pullback_pct", "MACD_RUNNER_PULLBACK_PCT", float_env, 0.0090),
        ("macd_early_window_minutes", "MACD_EARLY_WINDOW_MINUTES", int_env, 60),
        ("macd_early_min_volume_ratio", "MACD_EARLY_MIN_VOLUME_RATIO", float_env, 1.50),
        (
            "macd_early_volume_average_fallback_enabled",
            "MACD_EARLY_VOLUME_AVERAGE_FALLBACK_ENABLED",
            bool_env,
            False,
        ),
        ("macd_early_volume_lookback_bars", "MACD_EARLY_VOLUME_LOOKBACK_BARS", int_env, 3),
        ("macd_early_min_avg_volume_ratio", "MACD_EARLY_MIN_AVG_VOLUME_RATIO", float_env, 1.40),
        ("macd_early_min_latest_volume_ratio", "MACD_EARLY_MIN_LATEST_VOLUME_RATIO", float_env, 0.70),
        ("macd_early_max_spread_bps", "MACD_EARLY_MAX_SPREAD_BPS", float_env, 12.0),
        ("macd_early_min_hist_norm", "MACD_EARLY_MIN_HIST_NORM", float_env, 0.0012),
        ("macd_early_max_vwap_extension_pct", "MACD_EARLY_MAX_VWAP_EXTENSION_PCT", float_env, 0.015),
        ("macd_volume_impulse_runner_volume_ratio", "MACD_VOLUME_IMPULSE_RUNNER_VOLUME_RATIO", float_env, 3.0),
        ("macd_volume_impulse_runner_hist_norm", "MACD_VOLUME_IMPULSE_RUNNER_HIST_NORM", float_env, 0.0015),
        ("macd_volume_impulse_max_spread_bps", "MACD_VOLUME_IMPULSE_MAX_SPREAD_BPS", float_env, 30.0),
        ("macd_volume_impulse_size_multiplier", "MACD_VOLUME_IMPULSE_SIZE_MULTIPLIER", float_env, 0.5),
        ("macd_skip_midday", "MACD_SKIP_MIDDAY", bool_env, False),
        ("macd_min_hold_seconds", "MACD_MIN_HOLD_SECONDS", int_env, 60),
        ("macd_hist_rise_bars", "MACD_HIST_RISE_BARS", int_env, 2),
        ("macd_require_positive_hist", "MACD_REQUIRE_POSITIVE_HIST", bool_env, True),
        ("macd_momentum_exit_min_profit_pct", "MACD_MOMENTUM_EXIT_MIN_PROFIT_PCT", float_env, 0.0015),
        ("macd_early_loss_cut_seconds", "MACD_EARLY_LOSS_CUT_SECONDS", int_env, 75),
        ("macd_early_loss_cut_pct", "MACD_EARLY_LOSS_CUT_PCT", float_env, 0.0022),
        (
            "macd_early_impulse_max_trades_per_symbol_per_session",
            "MACD_MAX_TRADES_PER_SYMBOL_PER_SESSION",
            int_env,
            2,
        ),
        (
            "macd_early_impulse_symbol_loss_lock_count",
            "MACD_SYMBOL_LOSS_LOCK_COUNT",
            int_env,
            2,
        ),
        ("macd_macd_warmup_bars", "MACD_MACD_WARMUP_BARS", int_env, 120),
        ("macd_risk_off_hist_multiplier", "MACD_RISK_OFF_HIST_MULTIPLIER", float_env, 1.35),
        ("macd_risk_off_volume_add", "MACD_RISK_OFF_VOLUME_ADD", float_env, 0.25),
        ("macd_risk_off_chop_range_multiplier", "MACD_RISK_OFF_CHOP_RANGE_MULTIPLIER", float_env, 1.15),
        ("macd_risk_off_min_range_multiplier", "MACD_RISK_OFF_MIN_RANGE_MULTIPLIER", float_env, 1.15),
        ("macd_risk_off_max_extension_multiplier", "MACD_RISK_OFF_MAX_EXTENSION_MULTIPLIER", float_env, 0.80),
        (
            "macd_risk_off_max_vwap_extension_multiplier",
            "MACD_RISK_OFF_MAX_VWAP_EXTENSION_MULTIPLIER",
            float_env,
            0.80,
        ),
        ("macd_risk_off_max_r_multiplier", "MACD_RISK_OFF_MAX_R_MULTIPLIER", float_env, 0.85),
        ("macd_risk_on_hist_multiplier", "MACD_RISK_ON_HIST_MULTIPLIER", float_env, 0.90),
        ("macd_risk_on_volume_add", "MACD_RISK_ON_VOLUME_ADD", float_env, -0.10),
        ("macd_risk_on_chop_range_multiplier", "MACD_RISK_ON_CHOP_RANGE_MULTIPLIER", float_env, 0.90),
        ("macd_risk_on_min_range_multiplier", "MACD_RISK_ON_MIN_RANGE_MULTIPLIER", float_env, 0.90),
        ("macd_risk_on_max_extension_multiplier", "MACD_RISK_ON_MAX_EXTENSION_MULTIPLIER", float_env, 1.10),
        (
            "macd_risk_on_max_vwap_extension_multiplier",
            "MACD_RISK_ON_MAX_VWAP_EXTENSION_MULTIPLIER",
            float_env,
            1.10,
        ),
        ("macd_risk_on_max_r_multiplier", "MACD_RISK_ON_MAX_R_MULTIPLIER", float_env, 1.05),
        ("macd_neutral_hist_multiplier", "MACD_NEUTRAL_HIST_MULTIPLIER", float_env, 1.10),
        ("macd_neutral_volume_add", "MACD_NEUTRAL_VOLUME_ADD", float_env, 0.08),
        ("macd_neutral_chop_range_multiplier", "MACD_NEUTRAL_CHOP_RANGE_MULTIPLIER", float_env, 1.10),
        ("macd_neutral_min_range_multiplier", "MACD_NEUTRAL_MIN_RANGE_MULTIPLIER", float_env, 1.10),
        ("macd_neutral_max_extension_multiplier", "MACD_NEUTRAL_MAX_EXTENSION_MULTIPLIER", float_env, 0.90),
        (
            "macd_neutral_max_vwap_extension_multiplier",
            "MACD_NEUTRAL_MAX_VWAP_EXTENSION_MULTIPLIER",
            float_env,
            0.90,
        ),
        ("macd_neutral_max_r_multiplier", "MACD_NEUTRAL_MAX_R_MULTIPLIER", float_env, 0.92),
    )
    diagnostic_loggers: ClassVar[tuple[str, ...]] = ("strategies.macd_early_impulse",)
    selector_command: ClassVar[str] = ".venv/bin/python strategy_selectors/select_macd_early_impulse.py --top 12"

    @classmethod
    def runtime_settings_section(cls, settings: Any) -> dict[str, Any] | None:
        if cls.name not in settings.strategy_names:
            return None
        s = settings
        return {
            "start_minute": s.macd_start_minute,
            "end_minute": s.macd_end_minute,
            "hist_norm_min": s.macd_hist_threshold,
            "volume_ratio": s.macd_volume_ratio,
            "target_profit_pct": s.macd_target_profit_pct,
            "stop_loss_pct": s.macd_stop_loss_pct,
            "trailing_stop_pct": s.macd_trailing_stop_pct,
            "trailing_activation_pct": s.macd_trailing_activation_pct,
            "chop_range_pct": s.macd_chop_range_pct,
            "max_spread_bps": s.macd_max_spread_bps,
            "atr_period": s.macd_atr_period,
            "min_atr_pct": s.macd_min_atr_pct,
            "max_atr_pct": s.macd_max_atr_pct,
            "range_lookback_bars": s.macd_range_lookback_bars,
            "min_range_pct": s.macd_min_range_pct,
            "structure_lookback_bars": s.macd_structure_lookback_bars,
            "stop_buffer_pct": s.macd_stop_buffer_pct,
            "min_r_pct": s.macd_min_r_pct,
            "max_r_pct": s.macd_max_r_pct,
            "partial_r": s.macd_partial_r,
            "partial_size": s.macd_partial_size,
            "target_r": s.macd_target_r,
            "runner_pullback_pct": s.macd_runner_pullback_pct,
            "early_window_minutes": s.macd_early_window_minutes,
            "early_min_volume_ratio": s.macd_early_min_volume_ratio,
            "early_volume_average_fallback_enabled": s.macd_early_volume_average_fallback_enabled,
            "early_volume_lookback_bars": s.macd_early_volume_lookback_bars,
            "early_min_avg_volume_ratio": s.macd_early_min_avg_volume_ratio,
            "early_min_latest_volume_ratio": s.macd_early_min_latest_volume_ratio,
            "early_max_spread_bps": s.macd_early_max_spread_bps,
            "early_min_hist_norm": s.macd_early_min_hist_norm,
            "early_max_vwap_extension_pct": s.macd_early_max_vwap_extension_pct,
            "volume_impulse_runner_volume_ratio": s.macd_volume_impulse_runner_volume_ratio,
            "volume_impulse_runner_hist_norm": s.macd_volume_impulse_runner_hist_norm,
            "volume_impulse_max_spread_bps": s.macd_volume_impulse_max_spread_bps,
            "volume_impulse_size_multiplier": s.macd_volume_impulse_size_multiplier,
            "skip_midday": s.macd_skip_midday,
            "min_hold_seconds": s.macd_min_hold_seconds,
            "hist_rise_bars": s.macd_hist_rise_bars,
            "require_positive_hist": s.macd_require_positive_hist,
            "momentum_exit_min_profit_pct": s.macd_momentum_exit_min_profit_pct,
            "early_loss_cut_seconds": s.macd_early_loss_cut_seconds,
            "early_loss_cut_pct": s.macd_early_loss_cut_pct,
            "max_trades_per_symbol_per_session": s.macd_early_impulse_max_trades_per_symbol_per_session,
            "symbol_loss_lock_count": s.macd_early_impulse_symbol_loss_lock_count,
            "macd_warmup_bars": s.macd_macd_warmup_bars,
            "risk_off": {
                "hist_multiplier": s.macd_risk_off_hist_multiplier,
                "volume_add": s.macd_risk_off_volume_add,
                "chop_range_multiplier": s.macd_risk_off_chop_range_multiplier,
                "min_range_multiplier": s.macd_risk_off_min_range_multiplier,
                "max_extension_multiplier": s.macd_risk_off_max_extension_multiplier,
                "max_vwap_extension_multiplier": s.macd_risk_off_max_vwap_extension_multiplier,
                "max_r_multiplier": s.macd_risk_off_max_r_multiplier,
            },
            "risk_on": {
                "hist_multiplier": s.macd_risk_on_hist_multiplier,
                "volume_add": s.macd_risk_on_volume_add,
                "chop_range_multiplier": s.macd_risk_on_chop_range_multiplier,
                "min_range_multiplier": s.macd_risk_on_min_range_multiplier,
                "max_extension_multiplier": s.macd_risk_on_max_extension_multiplier,
                "max_vwap_extension_multiplier": s.macd_risk_on_max_vwap_extension_multiplier,
                "max_r_multiplier": s.macd_risk_on_max_r_multiplier,
            },
            "neutral_regime": {
                "hist_multiplier": s.macd_neutral_hist_multiplier,
                "volume_add": s.macd_neutral_volume_add,
                "chop_range_multiplier": s.macd_neutral_chop_range_multiplier,
                "min_range_multiplier": s.macd_neutral_min_range_multiplier,
                "max_extension_multiplier": s.macd_neutral_max_extension_multiplier,
                "max_vwap_extension_multiplier": s.macd_neutral_max_vwap_extension_multiplier,
                "max_r_multiplier": s.macd_neutral_max_r_multiplier,
            },
        }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_tz = MARKET_TZ
        self._last_reject_log_ms: dict[tuple[str, str], int] = {}
        self._runner_plan_ranks: dict[str, int] = {}
        self._load_runner_plan_ranks()

    def bootstrap_states(self, states: dict[str, SymbolState]) -> None:
        try:
            from alpaca.data.timeframe import TimeFrame

            from alpaca_client import get_bars_between, make_clients
        except Exception:
            LOG.exception("MACD early impulse bootstrap imports failed")
            return

        symbols = list(states)
        if not symbols:
            return

        now = self._bootstrap_end_time(states)
        start_of_day = datetime.combine(now.date(), PREMARKET_OPEN, tzinfo=self.market_tz)

        try:
            clients = make_clients(self.settings)
            intraday_bars = get_bars_between(clients, symbols, TimeFrame.Minute, start_of_day, now)
        except Exception:
            LOG.exception("MACD early impulse bootstrap failed to load bars")
            return

        seeded = 0
        for symbol, state in states.items():
            if len(state.bars) >= self.settings.macd_macd_warmup_bars:
                continue
            if not state.bars:
                for bar in intraday_bars.get(symbol, []):
                    state.add_bar(bar)
                    seeded += 1
        if seeded:
            LOG.info("MACD early impulse bootstrapped %s minute bars", seeded)

    def evaluate(self, state: SymbolState) -> Signal | None:
        if state.last_event_kind not in {"quote", "bar"}:
            return None

        if not self.is_symbol_allowed(state.symbol):
            return None

        if not self._within_entry_window(state.last_event_ms):
            return None

        last = latest_valid_quote(state)
        if last is None:
            return self._reject(state, "quote", "invalid or missing latest quote")
        early_window = self._in_early_window(state.last_event_ms)
        regime_name = self._market_regime_name()
        risk_off = regime_name == "risk_off"
        risk_on = regime_name == "risk_on"
        neutral_hardening = self._neutral_market_regime_hardening()
        max_spread_bps = self.settings.macd_max_spread_bps
        if early_window:
            max_spread_bps = min(max_spread_bps, self.settings.macd_early_max_spread_bps)

        rb = self._regular_bars(state)
        if len(rb) < _MIN_REGULAR_BARS:
            return self._reject(state, "bars", f"need >= {_MIN_REGULAR_BARS} regular bars, have {len(rb)}")

        ib = continuous_indicator_bars(state, self.settings)
        if len(ib) < self.settings.macd_macd_warmup_bars:
            return self._reject(
                state,
                "macd_warmup",
                f"need >= {self.settings.macd_macd_warmup_bars} indicator bars, have {len(ib)}",
            )

        mins = self._minutes_since_open(state)
        if self.settings.macd_skip_midday and 60 <= mins <= 120:
            return self._reject(state, "midday", f"skip midday {mins}m since open")

        closes = [float(bar.close) for bar in ib]
        ema_trend = _ema_series(closes, _EMA_TREND_PERIOD)
        if not ema_trend or last.ask < ema_trend[-1]:
            return self._reject(state, "below_ema", f"price below EMA{_EMA_TREND_PERIOD}")

        vol_r = self._volume_ratio(state)
        vwap = self._session_vwap(rb)
        if vwap is not None and last.ask < vwap:
            return self._reject(state, "below_vwap", "price below vwap")
        runner_mode = self._runner_mode(state.symbol, rb, last.ask, ema_trend[-1], vwap, vol_r)

        chop_range_pct = self.settings.macd_chop_range_pct
        if risk_off:
            chop_range_pct *= max(0.0, self.settings.macd_risk_off_chop_range_multiplier)
        elif risk_on:
            chop_range_pct *= max(0.0, self.settings.macd_risk_on_chop_range_multiplier)
        elif neutral_hardening > 0:
            chop_range_pct *= 1.0 + (
                max(1.0, self.settings.macd_neutral_chop_range_multiplier) - 1.0
            ) * neutral_hardening
        if self._is_chop(state, chop_range_pct) and not runner_mode:
            return self._reject(
                state,
                "chop",
                f"10-bar range% < {chop_range_pct:.4f}",
            )

        atr = self._atr(rb, self.settings.macd_atr_period)
        if atr is not None and atr > 0:
            atr_pct = atr / last.ask if last.ask > 0 else 0.0
            if atr_pct < self.settings.macd_min_atr_pct:
                return self._reject(state, "atr", f"ATR {atr_pct:.2%} too low")
            if atr_pct > self.settings.macd_max_atr_pct:
                return self._reject(state, "atr", f"ATR {atr_pct:.2%} too high")

        range_lookback = max(1, self.settings.macd_range_lookback_bars)
        if len(rb) >= range_lookback:
            recent_range_pct = self._range_pct(rb[-range_lookback:])
            min_range_pct = self.settings.macd_min_range_pct
            if risk_off:
                min_range_pct *= max(0.0, self.settings.macd_risk_off_min_range_multiplier)
            elif risk_on:
                min_range_pct *= max(0.0, self.settings.macd_risk_on_min_range_multiplier)
            elif neutral_hardening > 0:
                min_range_pct *= 1.0 + (
                    max(1.0, self.settings.macd_neutral_min_range_multiplier) - 1.0
                ) * neutral_hardening
            if recent_range_pct < min_range_pct:
                return self._reject(state, "range", f"range {recent_range_pct:.2%} too compressed")

        speed_ok = rb[-1].close > rb[-3].close

        macd = self._compute_macd(state)
        if macd is None:
            return self._reject(state, "macd", "could not compute MACD")
        macd_line, signal_line, hist = macd
        min_hist_len = max(2, self.settings.macd_hist_rise_bars + 1)
        if len(hist) < min_hist_len:
            return self._reject(state, "macd", "insufficient histogram history")

        h1 = hist[-1]
        if macd_line[-1] <= signal_line[-1]:
            return self._reject(state, "macd_below_signal", "MACD line not above signal")
        if not runner_mode and (len(macd_line) < 2 or macd_line[-1] <= macd_line[-2]):
            return self._reject(state, "macd_slope", "MACD line not rising")
        if not self._histogram_ready(hist, min_hist_len, runner_mode):
            return self._reject(
                state,
                "hist_fade",
                f"histogram not expanding ({','.join(f'{h:.5f}' for h in hist[-min_hist_len:])})",
            )
        if self.settings.macd_require_positive_hist and h1 <= 0:
            return self._reject(state, "negative_hist", f"histogram {h1:.5f} not positive")

        hist_norm = h1 / last.ask if last.ask > 0 else 0.0
        min_hist_norm = self.settings.macd_hist_threshold
        if runner_mode:
            min_hist_norm = min(min_hist_norm, _RUNNER_HIST_THRESHOLD)
        if early_window:
            min_hist_norm = max(min_hist_norm, self.settings.macd_early_min_hist_norm)
        if risk_off:
            min_hist_norm *= max(1.0, self.settings.macd_risk_off_hist_multiplier)
        elif risk_on:
            min_hist_norm *= max(0.0, self.settings.macd_risk_on_hist_multiplier)
        elif neutral_hardening > 0:
            min_hist_norm *= 1.0 + (
                max(1.0, self.settings.macd_neutral_hist_multiplier) - 1.0
            ) * neutral_hardening
        if hist_norm < min_hist_norm:
            return self._reject(
                state,
                "weak_macd",
                f"hist_norm {hist_norm:.5f} too small (min {min_hist_norm})",
            )

        volume_impulse_runner = (
            early_window
            and not runner_mode
            and vol_r >= self.settings.macd_volume_impulse_runner_volume_ratio
            and hist_norm >= self.settings.macd_volume_impulse_runner_hist_norm
            and vwap is not None
            and last.ask >= vwap
        )
        spread_exception = False
        if last.spread_bps > max_spread_bps:
            impulse_max_spread_bps = max(max_spread_bps, self.settings.macd_volume_impulse_max_spread_bps)
            if volume_impulse_runner and last.spread_bps <= impulse_max_spread_bps:
                spread_exception = True
            else:
                return self._reject(
                    state,
                    "spread",
                    (
                        f"spread {last.spread_bps:.2f}bps too wide "
                        f"max={max_spread_bps:.2f} impulse_max={impulse_max_spread_bps:.2f}"
                    ),
                )

        min_volume_ratio = self.settings.macd_volume_ratio
        if runner_mode:
            min_volume_ratio = min(min_volume_ratio, _RUNNER_MIN_VOLUME_RATIO)
        if early_window:
            min_volume_ratio = max(min_volume_ratio, self.settings.macd_early_min_volume_ratio)
        volume_add = 0.0
        if risk_off:
            volume_add += max(0.0, self.settings.macd_risk_off_volume_add)
        elif risk_on:
            volume_add += self.settings.macd_risk_on_volume_add
        elif neutral_hardening > 0:
            volume_add += max(0.0, self.settings.macd_neutral_volume_add) * neutral_hardening
        min_volume_ratio = max(0.0, min_volume_ratio + volume_add)
        if vol_r < min_volume_ratio:
            if early_window and self.settings.macd_early_volume_average_fallback_enabled:
                recent_vol_r = self._recent_average_volume_ratio(
                    state,
                    self.settings.macd_early_volume_lookback_bars,
                )
                min_latest_vol_r = max(0.0, self.settings.macd_early_min_latest_volume_ratio)
                min_recent_vol_r = max(0.0, self.settings.macd_early_min_avg_volume_ratio + volume_add)
                if vol_r >= min_latest_vol_r and recent_vol_r is not None and recent_vol_r >= min_recent_vol_r:
                    pass
                else:
                    avg_detail = f"{recent_vol_r:.2f}" if recent_vol_r is not None else "n/a"
                    return self._reject(
                        state,
                        "volume",
                        (
                            f"volume_ratio {vol_r:.2f} < {min_volume_ratio} "
                            f"avg{self.settings.macd_early_volume_lookback_bars}={avg_detail} "
                            f"min_avg={min_recent_vol_r:.2f} min_latest={min_latest_vol_r:.2f}"
                        ),
                    )
            else:
                return self._reject(
                    state,
                    "volume",
                    f"volume_ratio {vol_r:.2f} < {min_volume_ratio}",
                )

        if not speed_ok and not self._runner_pullback_reclaim_confirmed(rb, last.ask, ema_trend[-1], vwap):
            return self._reject(state, "no_speed", "close[-1] not above close[-3]")

        recent_high_10 = max(bar.high for bar in rb[-10:])
        extension = (last.ask - recent_high_10) / recent_high_10 if recent_high_10 > 0 else 0.0
        max_extension_pct = _RUNNER_OVEREXTEND_MAX_PCT if runner_mode else _OVEREXTEND_MAX_PCT
        if risk_off:
            max_extension_pct *= max(0.0, self.settings.macd_risk_off_max_extension_multiplier)
        elif risk_on:
            max_extension_pct *= max(0.0, self.settings.macd_risk_on_max_extension_multiplier)
        elif neutral_hardening > 0:
            neutral_max_extension_multiplier = min(1.0, max(0.0, self.settings.macd_neutral_max_extension_multiplier))
            max_extension_pct *= 1.0 - ((1.0 - neutral_max_extension_multiplier) * neutral_hardening)
        if extension > max_extension_pct:
            return self._reject(state, "overextended", f"extension {extension:.3%} too high")

        if vwap is not None:
            vwap_extension = (last.ask - vwap) / vwap if vwap > 0 else 0.0
            max_vwap_extension_pct = _RUNNER_VWAP_EXTENSION_MAX_PCT if runner_mode else _VWAP_EXTENSION_MAX_PCT
            if early_window:
                max_vwap_extension_pct = min(max_vwap_extension_pct, self.settings.macd_early_max_vwap_extension_pct)
            if risk_off:
                max_vwap_extension_pct *= max(0.0, self.settings.macd_risk_off_max_vwap_extension_multiplier)
            elif risk_on:
                max_vwap_extension_pct *= max(0.0, self.settings.macd_risk_on_max_vwap_extension_multiplier)
            elif neutral_hardening > 0:
                neutral_vwap_multiplier = min(1.0, max(0.0, self.settings.macd_neutral_max_vwap_extension_multiplier))
                max_vwap_extension_pct *= 1.0 - ((1.0 - neutral_vwap_multiplier) * neutral_hardening)
            if vwap_extension > max_vwap_extension_pct:
                return self._reject(state, "vwap_overextended", f"VWAP extension {vwap_extension:.3%} too high")

        recent_high = max(bar.high for bar in rb[-_RECENT_HIGH_LOOKBACK:])
        near_high = last.ask >= recent_high * (1.0 - _NEAR_HIGH_TOLERANCE_PCT)
        structure_ok = near_high and self._price_reclaim_confirmed(rb, last.ask, vwap)
        if runner_mode and not structure_ok:
            structure_ok = self._runner_pullback_reclaim_confirmed(rb, last.ask, ema_trend[-1], vwap)
        if not structure_ok:
            return self._reject(
                state,
                "price_structure",
                f"no VWAP/high reclaim near recent high ({recent_high:.2f}) while above vwap ({vwap})",
            )

        spread_bps = last.spread_bps
        change_pct = h1
        stop_price = (
            self._runner_stop_price(rb, last.ask, ema_trend[-1], vwap)
            if runner_mode
            else self._entry_stop_price(rb, last.ask)
        )
        r_pct = (last.ask - stop_price) / last.ask if last.ask > 0 else 0.0
        max_r_pct = self.settings.macd_max_r_pct
        if risk_off:
            max_r_pct *= max(0.0, self.settings.macd_risk_off_max_r_multiplier)
        elif risk_on:
            max_r_pct *= max(0.0, self.settings.macd_risk_on_max_r_multiplier)
        elif neutral_hardening > 0:
            neutral_max_r_multiplier = min(1.0, max(0.0, self.settings.macd_neutral_max_r_multiplier))
            max_r_pct *= 1.0 - ((1.0 - neutral_max_r_multiplier) * neutral_hardening)
        if r_pct < self.settings.macd_min_r_pct:
            return self._reject(state, "risk", f"R {r_pct:.2%} too small")
        if r_pct > max_r_pct:
            return self._reject(state, "risk", f"R {r_pct:.2%} too wide")
        regime_reason = f" regime={regime_name}" if regime_name in {"risk_off", "risk_on"} else ""
        if not regime_reason and neutral_hardening > 0:
            regime_reason = f" regime=neutral_hardened:{neutral_hardening:.2f}"
        runner_reason = " runner=volume" if volume_impulse_runner else ""
        spread_reason = " spread=impulse_exception" if spread_exception else ""
        size_multiplier = 1.0
        if spread_exception:
            size_multiplier = min(1.0, max(0.0, self.settings.macd_volume_impulse_size_multiplier))
        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=last.ask,
            timestamp_ms=state.last_event_ms,
            change_pct=change_pct,
            volume_ratio=vol_r,
            spread_bps=spread_bps,
            reason=f"macd early impulse entry | R {r_pct:.2%}{regime_reason}{runner_reason}{spread_reason}",
            stop_price=stop_price,
            position_size_multiplier=size_multiplier,
            runner_mode=runner_mode or volume_impulse_runner,
        )

    def use_fixed_target_exit(self, position) -> bool:
        """Targets and most exits use strategy-specific pct in should_exit."""
        return False

    def should_exit(self, state: SymbolState, position) -> ExitDecision | None:
        if position.strategy != self.name:
            return None

        price = state.last_price
        if price is None or position.entry_price <= 0:
            return None

        pnl_pct = (price - position.entry_price) / position.entry_price
        event_ms = state.last_event_ms or (state.quote.timestamp_ms if state.quote else position.entry_ms)
        age_seconds = (event_ms - position.entry_ms) / 1000
        runner_mode = self._position_runner_mode(position, state)
        initial_stop = position.initial_stop_price or position.stop_price
        r_initial = position.entry_price - initial_stop if initial_stop else 0.0

        if r_initial > 0:
            if not position.partial_exit_taken and position.shares > 1:
                partial_level = position.entry_price + r_initial * self.settings.macd_partial_r
                if price >= partial_level:
                    fraction = min(1.0, max(0.0, self.settings.macd_partial_size))
                    shares = max(1, min(position.shares - 1, int(position.shares * fraction)))
                    return ExitDecision(f"partial {self.settings.macd_partial_r:.1f}R", shares=shares, mark_partial=True)

            target_level = position.entry_price + r_initial * self.settings.macd_target_r
            if price >= target_level and not (position.partial_exit_taken and runner_mode):
                return ExitDecision(f"target {self.settings.macd_target_r:.1f}R")

            if position.partial_exit_taken:
                peak = position.max_price if position.max_price > 0 else position.entry_price
                if peak > 0 and price <= peak * (1 - self.settings.macd_runner_pullback_pct):
                    return ExitDecision("runner pullback")

        if runner_mode:
            if age_seconds <= _RUNNER_EARLY_LOSS_CUT_SECONDS and pnl_pct <= -_RUNNER_EARLY_LOSS_CUT_PCT:
                return ExitDecision("early loss cut")

            min_hold_seconds = max(self.settings.macd_min_hold_seconds, _RUNNER_MIN_HOLD_SECONDS)
            if age_seconds < min_hold_seconds:
                return None

            macd = self._compute_macd(state)
            if macd is not None:
                _, _, hist = macd
                if (
                    len(hist) >= 3
                    and hist[-1] < hist[-2] < hist[-3]
                    and pnl_pct >= max(self.settings.macd_momentum_exit_min_profit_pct, _RUNNER_MOMENTUM_EXIT_MIN_PROFIT_PCT)
                ):
                    return ExitDecision("macd momentum fade")

            trail_high = max(position.max_price or 0.0, price)
            trail_activation_pct = max(self.settings.macd_trailing_activation_pct, _RUNNER_TRAIL_ACTIVATION_PCT)
            trail_stop_pct = max(self.settings.macd_trailing_stop_pct, _RUNNER_TRAIL_STOP_PCT)
            if trail_high >= position.entry_price * (1.0 + trail_activation_pct):
                if trail_high > 0 and price < trail_high * (1.0 - trail_stop_pct):
                    return ExitDecision("trailing stop")

            if pnl_pct <= -max(self.settings.macd_stop_loss_pct, _RUNNER_STOP_MIN_PCT):
                return ExitDecision("stop loss")
            return None

        if age_seconds <= self.settings.macd_early_loss_cut_seconds and pnl_pct <= -self.settings.macd_early_loss_cut_pct:
            return ExitDecision("early loss cut")

        if r_initial <= 0 and pnl_pct >= self.settings.macd_target_profit_pct:
            return ExitDecision("target profit")

        if age_seconds < self.settings.macd_min_hold_seconds:
            return None

        macd = self._compute_macd(state)
        if macd is not None:
            _, _, hist = macd
            if (
                len(hist) >= 2
                and hist[-1] < hist[-2]
                and pnl_pct >= self.settings.macd_momentum_exit_min_profit_pct
            ):
                return ExitDecision("macd momentum fade")

        trail_high = max(position.max_price or 0.0, price)
        if trail_high >= position.entry_price * (1.0 + self.settings.macd_trailing_activation_pct):
            if trail_high > 0 and price < trail_high * (1.0 - self.settings.macd_trailing_stop_pct):
                return ExitDecision("trailing stop")

        if pnl_pct <= -self.settings.macd_stop_loss_pct:
            return ExitDecision("stop loss")

        return None

    def allow_max_hold_exit(self, state: SymbolState, position, age_seconds: float, pnl_pct: float) -> bool:
        if getattr(position, "strategy", "") != self.name:
            return True

        macd = self._compute_macd(state)
        if macd is None:
            return True

        macd_line, signal_line, hist = macd
        if not macd_line or not signal_line or not hist:
            return True

        hist_not_fading = len(hist) < 2 or hist[-1] >= hist[-2]
        if macd_line[-1] > signal_line[-1] and hist[-1] > 0 and hist_not_fading:
            LOG.debug(
                "Max hold deferred %s [macd_early_impulse]: MACD constructive age=%.1fs pnl=%.3f%% macd=%.5f signal=%.5f hist=%.5f",
                state.symbol,
                age_seconds,
                pnl_pct * 100,
                macd_line[-1],
                signal_line[-1],
                hist[-1],
            )
            return False

        return True

    def _within_entry_window(self, timestamp_ms: int | None) -> bool:
        if timestamp_ms is None:
            return False
        current = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.market_tz)
        minutes = current.hour * 60 + current.minute
        market_open = MARKET_OPEN.hour * 60 + MARKET_OPEN.minute
        elapsed = minutes - market_open
        return self.settings.macd_start_minute <= elapsed <= self.settings.macd_end_minute

    def _in_early_window(self, timestamp_ms: int | None) -> bool:
        if timestamp_ms is None or self.settings.macd_early_window_minutes <= 0:
            return False
        current = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.market_tz)
        minutes = current.hour * 60 + current.minute
        market_open = MARKET_OPEN.hour * 60 + MARKET_OPEN.minute
        elapsed = minutes - market_open
        return 0 <= elapsed < self.settings.macd_early_window_minutes

    def _regular_bars(self, state: SymbolState):
        return regular_bars(state)

    def _minutes_since_open(self, state: SymbolState) -> int:
        if state.last_event_ms is None:
            return -1
        current = datetime.fromtimestamp(state.last_event_ms / 1000, tz=self.market_tz)
        minutes = current.hour * 60 + current.minute
        market_open = MARKET_OPEN.hour * 60 + MARKET_OPEN.minute
        return minutes - market_open

    def _market_regime_name(self) -> str:
        return getattr(getattr(self, "_market_regime", None), "name", "")

    def _bootstrap_end_time(self, states: dict[str, SymbolState]) -> datetime:
        latest_event_ms = max((state.last_event_ms or 0) for state in states.values()) if states else 0
        if latest_event_ms > 0:
            return datetime.fromtimestamp(latest_event_ms / 1000, tz=self.market_tz)
        return datetime.now(tz=self.market_tz)

    def _neutral_market_regime_hardening(self) -> float:
        regime = getattr(self, "_market_regime", None)
        if getattr(regime, "name", "") != "neutral":
            return 0.0
        risk_off_score = self.settings.market_regime_risk_off_score
        risk_on_score = self.settings.market_regime_risk_on_score
        span = max(1, risk_on_score - risk_off_score)
        score = getattr(regime, "score", 0)
        return min(1.0, max(0.0, (risk_on_score - score) / span))

    def _is_chop(self, state: SymbolState, chop_range_pct: float | None = None) -> bool:
        bars = self._regular_bars(state)[-10:]
        if len(bars) < 10:
            return False
        hi = max(b.high for b in bars)
        lo = min(b.low for b in bars)
        if lo <= 0:
            return True
        range_pct = (hi - lo) / lo
        threshold = self.settings.macd_chop_range_pct if chop_range_pct is None else chop_range_pct
        return range_pct < threshold

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
        return sum(true_ranges) / len(true_ranges) if true_ranges else None

    @staticmethod
    def _range_pct(bars) -> float:
        if not bars:
            return 0.0
        low = min(bar.low for bar in bars if bar.low > 0)
        high = max(bar.high for bar in bars if bar.high > 0)
        return (high - low) / low if low > 0 else 0.0

    def _compute_macd(self, state: SymbolState) -> tuple[list[float], list[float], list[float]] | None:
        ib = continuous_indicator_bars(state, self.settings)
        if len(ib) < self.settings.macd_macd_warmup_bars:
            return None
        closes = [float(bar.close) for bar in ib]
        if not closes or any(c <= 0 for c in closes):
            return None

        ema12 = _ema_series(closes, 12)
        ema26 = _ema_series(closes, 26)
        macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
        signal_line = _ema_series(macd_line, 9)
        hist = [m - s for m, s in zip(macd_line, signal_line)]
        return macd_line, signal_line, hist

    @staticmethod
    def _is_rising(values: list[float]) -> bool:
        return len(values) >= 2 and all(values[index] > values[index - 1] for index in range(1, len(values)))

    @staticmethod
    def _histogram_expanding(hist: list[float], min_hist_len: int) -> bool:
        if len(hist) < max(3, min_hist_len):
            return False
        recent = hist[-min_hist_len:]
        previous = hist[-(min_hist_len + 1) : -1]
        return hist[-1] > 0 and hist[-1] > hist[-2] and sum(recent) / len(recent) > sum(previous) / len(previous)

    @classmethod
    def _histogram_ready(cls, hist: list[float], min_hist_len: int, runner_mode: bool) -> bool:
        if cls._histogram_expanding(hist, min_hist_len):
            return True
        if not runner_mode or len(hist) < max(3, min_hist_len):
            return False
        h1 = hist[-1]
        h2 = hist[-2]
        h3 = hist[-3]
        return h1 > 0 and h1 >= h2 * _RUNNER_WEAK_HIST_RATIO and h2 >= h3 * _RUNNER_WEAK_HIST_RATIO

    @staticmethod
    def _price_reclaim_confirmed(session_bars, current_price: float, vwap: float | None) -> bool:
        if len(session_bars) < 2:
            return False
        latest = session_bars[-1]
        previous = session_bars[-2]
        reclaimed_vwap = vwap is not None and latest.close >= vwap and previous.close <= vwap
        prior_bars = session_bars[-(_RECLAIM_HIGH_LOOKBACK + 1) : -1]
        prior_high = max((bar.high for bar in prior_bars if bar.high > 0), default=0.0)
        reclaimed_high = prior_high > 0 and current_price >= prior_high * (1.0 - _NEAR_HIGH_TOLERANCE_PCT)
        return reclaimed_vwap or reclaimed_high

    @staticmethod
    def _session_vwap(session_bars) -> float | None:
        total_volume = sum(bar.volume for bar in session_bars if bar.volume > 0)
        if total_volume <= 0:
            return None
        total_value = sum(bar.vwap * bar.volume for bar in session_bars if bar.volume > 0)
        return total_value / total_volume if total_value > 0 else None

    def _volume_ratio(self, state: SymbolState) -> float:
        session_bars = self._regular_bars(state)
        bars = session_bars if len(session_bars) >= 2 else continuous_indicator_bars(state, self.settings)
        if len(bars) < 2:
            return 0.0
        latest_volume = bars[-1].volume
        baseline = median([bar.volume for bar in bars[:-1] if bar.volume > 0] or [0.0])
        return latest_volume / baseline if baseline > 0 else 0.0

    def _recent_average_volume_ratio(self, state: SymbolState, lookback_bars: int) -> float | None:
        lookback = max(1, lookback_bars)
        session_bars = self._regular_bars(state)
        bars = session_bars if len(session_bars) >= lookback + 2 else continuous_indicator_bars(state, self.settings)
        if len(bars) < lookback + 2:
            return None
        baseline_bars = bars[:-lookback]
        baseline = median([bar.volume for bar in baseline_bars if bar.volume > 0] or [0.0])
        if baseline <= 0:
            return None
        recent_avg = sum(bar.volume for bar in bars[-lookback:]) / lookback
        return recent_avg / baseline

    def _load_runner_plan_ranks(self) -> None:
        plan_path = Path("data") / f"{self.name}_plan.json"
        if not plan_path.exists():
            return
        try:
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            LOG.debug("Could not load runner plan ranks from %s", plan_path, exc_info=True)
            return
        ranked = payload.get("ranked") or []
        for index, item in enumerate(ranked, start=1):
            symbol = str(item.get("symbol", "")).strip().upper()
            if symbol:
                self._runner_plan_ranks[symbol] = index

    def _runner_mode(
        self,
        symbol: str,
        session_bars,
        current_price: float,
        ema_value: float,
        vwap: float | None,
        volume_ratio: float,
    ) -> bool:
        rank = self._runner_plan_ranks.get(symbol.strip().upper())
        if rank is not None and rank <= _RUNNER_PLAN_TOP_RANK:
            return True
        if len(session_bars) < 10 or current_price <= 0:
            return False
        session_high = max(bar.high for bar in session_bars if bar.high > 0)
        session_low = min(bar.low for bar in session_bars if bar.low > 0)
        if session_low <= 0 or session_high <= session_low:
            return False
        range_pct = (session_high - session_low) / session_low
        session_zone = (current_price - session_low) / (session_high - session_low)
        if range_pct < _RUNNER_SESSION_RANGE_PCT or session_zone < _RUNNER_SESSION_TOP_ZONE:
            return False
        if volume_ratio < _RUNNER_MIN_VOLUME_RATIO:
            return False
        if current_price < ema_value:
            return False
        return vwap is None or current_price >= vwap

    def _position_runner_mode(self, position, state: SymbolState) -> bool:
        if getattr(position, "runner_mode", False):
            return True
        current_price = state.last_price or position.entry_price
        rb = self._regular_bars(state)
        if len(rb) < 10:
            return position.symbol.strip().upper() in self._runner_plan_ranks
        vwap = self._session_vwap(rb)
        ib = continuous_indicator_bars(state, self.settings)
        if len(ib) < self.settings.macd_macd_warmup_bars:
            ema_value = position.entry_price
        else:
            closes = [float(bar.close) for bar in ib]
            ema_trend = _ema_series(closes, _EMA_TREND_PERIOD)
            ema_value = ema_trend[-1] if ema_trend else position.entry_price
        volume_ratio = self._volume_ratio(state)
        return self._runner_mode(position.symbol, rb, current_price, ema_value, vwap, volume_ratio)

    @staticmethod
    def _runner_pullback_reclaim_confirmed(session_bars, current_price: float, ema_value: float, vwap: float | None) -> bool:
        if len(session_bars) < 5:
            return False
        prior_bars = session_bars[-5:-1]
        latest = session_bars[-1]
        trend_floor = min(
            value
            for value in (ema_value, vwap if vwap is not None else ema_value)
            if value is not None and value > 0
        )
        pullback_low = min(bar.low for bar in prior_bars if bar.low > 0)
        if pullback_low < trend_floor * (1.0 - _RUNNER_TREND_HOLD_BUFFER_PCT):
            return False
        prior_high = max(bar.high for bar in prior_bars if bar.high > 0)
        reclaim_close = latest.close >= prior_high * (1.0 - _NEAR_HIGH_TOLERANCE_PCT)
        reclaim_price = current_price >= prior_high * (1.0 - _NEAR_HIGH_TOLERANCE_PCT)
        return reclaim_close or reclaim_price

    @staticmethod
    def _runner_stop_price(session_bars, current_price: float, ema_value: float, vwap: float | None) -> float:
        recent_bars = session_bars[-_RUNNER_STOP_LOOKBACK:]
        recent_low = min((bar.low for bar in recent_bars if bar.low > 0), default=current_price)
        anchors = [recent_low]
        if ema_value > 0:
            anchors.append(ema_value)
        if vwap is not None and vwap > 0:
            anchors.append(vwap)
        support = min(anchors)
        raw_risk_pct = (current_price - support) / current_price if current_price > 0 and support > 0 else _RUNNER_STOP_MIN_PCT
        risk_pct = min(max(raw_risk_pct + _RUNNER_STOP_BUFFER_PCT, _RUNNER_STOP_MIN_PCT), _RUNNER_STOP_MAX_PCT)
        return current_price * (1.0 - risk_pct)

    def _entry_stop_price(self, session_bars, current_price: float) -> float:
        fixed_stop = current_price * (1.0 - self.settings.macd_stop_loss_pct)
        lookback = max(1, self.settings.macd_structure_lookback_bars)
        if len(session_bars) < lookback or current_price <= 0:
            return fixed_stop
        recent_low = min((bar.low for bar in session_bars[-lookback:] if bar.low > 0), default=0.0)
        if recent_low <= 0 or recent_low >= current_price:
            return fixed_stop
        structure_stop = recent_low * (1.0 - max(0.0, self.settings.macd_stop_buffer_pct))
        return max(fixed_stop, structure_stop)

    def _reject(self, state: SymbolState, code: str, detail: str) -> None:
        timestamp_ms = state.last_event_ms or 0
        key = (state.symbol, code)
        last_log_ms = self._last_reject_log_ms.get(key, -10_000)
        if timestamp_ms - last_log_ms >= 10_000:
            self._last_reject_log_ms[key] = timestamp_ms
            LOG.debug("No macd_early_impulse entry %s [%s]: %s", state.symbol, code, detail)
        return None
