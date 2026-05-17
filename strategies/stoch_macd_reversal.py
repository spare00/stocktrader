from __future__ import annotations

from datetime import datetime, time
import logging
from statistics import median
from typing import Any, ClassVar

from candle import SymbolState
from config import Settings
from env_vars import EnvSpec, bool_env, float_env, int_env
from market_hours import MARKET_TZ
from models import ExitDecision, Signal
from strategy_selectors.select_gap_and_go import latest_valid_quote
from strategies.base import Strategy
from strategies.macd_early_impulse import _ema_series


LOG = logging.getLogger(__name__)
MARKET_OPEN = time(9, 30)
PREMARKET_OPEN = time(4, 0)
MARKET_CLOSE = time(16, 0)


class StochMACDReversalStrategy(Strategy):
    """1-minute STOCH/MACD reversal entry modeled from ready/buy/sell chart stages."""

    name = "stoch_macd_reversal"
    requires_plan: ClassVar[bool] = False
    env_specs: ClassVar[tuple[EnvSpec, ...]] = (
        ("stoch_macd_start_minute", "STOCH_MACD_START_MINUTE", int_env, 0),
        ("stoch_macd_end_minute", "STOCH_MACD_END_MINUTE", int_env, 360),
        ("stoch_macd_min_bars", "STOCH_MACD_MIN_BARS", int_env, 0),
        ("stoch_macd_ema_period", "STOCH_MACD_EMA_PERIOD", int_env, 5),
        ("stoch_macd_supertrend_enabled", "STOCH_MACD_SUPERTREND_ENABLED", bool_env, True),
        ("stoch_macd_supertrend_buffer_pct", "STOCH_MACD_SUPERTREND_BUFFER_PCT", float_env, 0.0005),
        ("stoch_macd_supertrend_period", "STOCH_MACD_SUPERTREND_PERIOD", int_env, 7),
        ("stoch_macd_supertrend_multiplier", "STOCH_MACD_SUPERTREND_MULTIPLIER", float_env, 3.0),
        ("stoch_macd_min_volume_ratio", "STOCH_MACD_MIN_VOLUME_RATIO", float_env, 0.80),
        ("stoch_macd_max_spread_bps", "STOCH_MACD_MAX_SPREAD_BPS", float_env, 15.0),
        ("stoch_macd_vwap_enabled", "STOCH_MACD_VWAP_ENABLED", bool_env, True),
        ("stoch_macd_vwap_buffer_pct", "STOCH_MACD_VWAP_BUFFER_PCT", float_env, 0.0005),
        ("stoch_macd_require_vwap_rising", "STOCH_MACD_REQUIRE_VWAP_RISING", bool_env, True),
        ("stoch_macd_min_hist_norm", "STOCH_MACD_MIN_HIST_NORM", float_env, 0.00005),
        ("stoch_macd_hist_rise_bars", "STOCH_MACD_HIST_RISE_BARS", int_env, 2),
        ("stoch_macd_macd_rise_bars", "STOCH_MACD_MACD_RISE_BARS", int_env, 2),
        ("stoch_macd_stoch_cross_lookback_bars", "STOCH_MACD_STOCH_CROSS_LOOKBACK_BARS", int_env, 3),
        ("stoch_macd_max_k", "STOCH_MACD_MAX_K", float_env, 88.0),
        ("stoch_macd_overbought_min_hist_rise_norm", "STOCH_MACD_OVERBOUGHT_MIN_HIST_RISE_NORM", float_env, 0.00005),
        ("stoch_macd_atr_period", "STOCH_MACD_ATR_PERIOD", int_env, 14),
        ("stoch_macd_min_atr_pct", "STOCH_MACD_MIN_ATR_PCT", float_env, 0.0015),
        ("stoch_macd_max_atr_pct", "STOCH_MACD_MAX_ATR_PCT", float_env, 0.0300),
        ("stoch_macd_range_lookback_bars", "STOCH_MACD_RANGE_LOOKBACK_BARS", int_env, 20),
        ("stoch_macd_min_range_pct", "STOCH_MACD_MIN_RANGE_PCT", float_env, 0.0040),
        ("stoch_macd_partial_r", "STOCH_MACD_PARTIAL_R", float_env, 1.0),
        ("stoch_macd_partial_size", "STOCH_MACD_PARTIAL_SIZE", float_env, 0.5),
        ("stoch_macd_runner_pullback_pct", "STOCH_MACD_RUNNER_PULLBACK_PCT", float_env, 0.006),
        ("stoch_macd_structure_lookback_bars", "STOCH_MACD_STRUCTURE_LOOKBACK_BARS", int_env, 6),
        ("stoch_macd_min_r_pct", "STOCH_MACD_MIN_R_PCT", float_env, 0.0025),
        ("stoch_macd_max_r_pct", "STOCH_MACD_MAX_R_PCT", float_env, 0.0120),
        ("stoch_macd_early_window_minutes", "STOCH_MACD_EARLY_WINDOW_MINUTES", int_env, 60),
        ("stoch_macd_early_min_volume_ratio", "STOCH_MACD_EARLY_MIN_VOLUME_RATIO", float_env, 1.20),
        ("stoch_macd_early_max_spread_bps", "STOCH_MACD_EARLY_MAX_SPREAD_BPS", float_env, 12.0),
        ("stoch_macd_early_min_hist_norm", "STOCH_MACD_EARLY_MIN_HIST_NORM", float_env, 0.00010),
        ("stoch_macd_early_vwap_buffer_pct", "STOCH_MACD_EARLY_VWAP_BUFFER_PCT", float_env, 0.0010),
        ("stoch_macd_stop_loss_pct", "STOCH_MACD_STOP_LOSS_PCT", float_env, 0.0045),
        ("stoch_macd_target_profit_pct", "STOCH_MACD_TARGET_PROFIT_PCT", float_env, 0.012),
        ("stoch_macd_trailing_activation_pct", "STOCH_MACD_TRAILING_ACTIVATION_PCT", float_env, 0.004),
        ("stoch_macd_trailing_stop_pct", "STOCH_MACD_TRAILING_STOP_PCT", float_env, 0.005),
        ("stoch_macd_min_hold_seconds", "STOCH_MACD_MIN_HOLD_SECONDS", int_env, 30),
        (
            "stoch_macd_max_trades_per_symbol_per_session",
            "STOCH_MACD_MAX_TRADES_PER_SYMBOL_PER_SESSION",
            int_env,
            2,
        ),
        ("stoch_macd_symbol_loss_lock_count", "STOCH_MACD_SYMBOL_LOSS_LOCK_COUNT", int_env, 1),
        ("stoch_macd_macd_warmup_bars", "STOCH_MACD_MACD_WARMUP_BARS", int_env, 120),
        (
            "stoch_macd_risk_off_stoch_cross_lookback_bars",
            "STOCH_MACD_RISK_OFF_STOCH_CROSS_LOOKBACK_BARS",
            int_env,
            3,
        ),
        ("stoch_macd_risk_off_hist_multiplier", "STOCH_MACD_RISK_OFF_HIST_MULTIPLIER", float_env, 1.5),
        ("stoch_macd_risk_off_volume_add", "STOCH_MACD_RISK_OFF_VOLUME_ADD", float_env, 0.3),
        (
            "stoch_macd_risk_off_vwap_buffer_multiplier",
            "STOCH_MACD_RISK_OFF_VWAP_BUFFER_MULTIPLIER",
            float_env,
            1.5,
        ),
        ("stoch_macd_risk_off_max_r_multiplier", "STOCH_MACD_RISK_OFF_MAX_R_MULTIPLIER", float_env, 0.8),
        ("stoch_macd_neutral_hist_multiplier", "STOCH_MACD_NEUTRAL_HIST_MULTIPLIER", float_env, 1.15),
        ("stoch_macd_neutral_volume_add", "STOCH_MACD_NEUTRAL_VOLUME_ADD", float_env, 0.10),
        (
            "stoch_macd_neutral_vwap_buffer_multiplier",
            "STOCH_MACD_NEUTRAL_VWAP_BUFFER_MULTIPLIER",
            float_env,
            1.20,
        ),
        ("stoch_macd_neutral_max_r_multiplier", "STOCH_MACD_NEUTRAL_MAX_R_MULTIPLIER", float_env, 0.90),
        ("stoch_macd_reentry_fresh_enabled", "STOCH_MACD_REENTRY_FRESH_ENABLED", bool_env, True),
        (
            "stoch_macd_reentry_fresh_lookback_bars",
            "STOCH_MACD_REENTRY_FRESH_LOOKBACK_BARS",
            int_env,
            20,
        ),
        ("stoch_macd_reentry_high_buffer_pct", "STOCH_MACD_REENTRY_HIGH_BUFFER_PCT", float_env, 0.0005),
        (
            "stoch_macd_reentry_hist_rise_multiplier",
            "STOCH_MACD_REENTRY_HIST_RISE_MULTIPLIER",
            float_env,
            1.25,
        ),
        ("stoch_macd_reentry_volume_add", "STOCH_MACD_REENTRY_VOLUME_ADD", float_env, 0.25),
        (
            "stoch_macd_respect_consecutive_loss_limits",
            "STOCH_MACD_RESPECT_CONSECUTIVE_LOSS_LIMITS",
            bool_env,
            False,
        ),
    )
    diagnostic_loggers: ClassVar[tuple[str, ...]] = ("strategies.stoch_macd_reversal",)
    selector_command: ClassVar[str] = ".venv/bin/python strategy_selectors/select_stoch_macd_reversal.py --top 12"

    @classmethod
    def runtime_settings_section(cls, settings: Any) -> dict[str, Any] | None:
        if cls.name not in settings.strategy_names:
            return None
        return {
            "start_minute": settings.stoch_macd_start_minute,
            "end_minute": settings.stoch_macd_end_minute,
            "min_bars": settings.stoch_macd_min_bars,
            "ema_period": settings.stoch_macd_ema_period,
            "supertrend_enabled": settings.stoch_macd_supertrend_enabled,
            "supertrend_buffer_pct": settings.stoch_macd_supertrend_buffer_pct,
            "supertrend_period": settings.stoch_macd_supertrend_period,
            "supertrend_multiplier": settings.stoch_macd_supertrend_multiplier,
            "min_volume_ratio": settings.stoch_macd_min_volume_ratio,
            "max_spread_bps": settings.stoch_macd_max_spread_bps,
            "vwap_enabled": settings.stoch_macd_vwap_enabled,
            "vwap_buffer_pct": settings.stoch_macd_vwap_buffer_pct,
            "require_vwap_rising": settings.stoch_macd_require_vwap_rising,
            "min_hist_norm": settings.stoch_macd_min_hist_norm,
            "hist_rise_bars": settings.stoch_macd_hist_rise_bars,
            "macd_rise_bars": settings.stoch_macd_macd_rise_bars,
            "stoch_cross_lookback_bars": settings.stoch_macd_stoch_cross_lookback_bars,
            "max_k": settings.stoch_macd_max_k,
            "overbought_min_hist_rise_norm": settings.stoch_macd_overbought_min_hist_rise_norm,
            "atr_period": settings.stoch_macd_atr_period,
            "min_atr_pct": settings.stoch_macd_min_atr_pct,
            "max_atr_pct": settings.stoch_macd_max_atr_pct,
            "range_lookback_bars": settings.stoch_macd_range_lookback_bars,
            "min_range_pct": settings.stoch_macd_min_range_pct,
            "partial_r": settings.stoch_macd_partial_r,
            "partial_size": settings.stoch_macd_partial_size,
            "runner_pullback_pct": settings.stoch_macd_runner_pullback_pct,
            "structure_lookback_bars": settings.stoch_macd_structure_lookback_bars,
            "min_r_pct": settings.stoch_macd_min_r_pct,
            "max_r_pct": settings.stoch_macd_max_r_pct,
            "early_window_minutes": settings.stoch_macd_early_window_minutes,
            "early_min_volume_ratio": settings.stoch_macd_early_min_volume_ratio,
            "early_max_spread_bps": settings.stoch_macd_early_max_spread_bps,
            "early_min_hist_norm": settings.stoch_macd_early_min_hist_norm,
            "early_vwap_buffer_pct": settings.stoch_macd_early_vwap_buffer_pct,
            "stop_loss_pct": settings.stoch_macd_stop_loss_pct,
            "target_profit_pct": settings.stoch_macd_target_profit_pct,
            "trailing_activation_pct": settings.stoch_macd_trailing_activation_pct,
            "trailing_stop_pct": settings.stoch_macd_trailing_stop_pct,
            "min_hold_seconds": settings.stoch_macd_min_hold_seconds,
            "max_trades_per_symbol_per_session": settings.stoch_macd_max_trades_per_symbol_per_session,
            "symbol_loss_lock_count": settings.stoch_macd_symbol_loss_lock_count,
            "macd_warmup_bars": settings.stoch_macd_macd_warmup_bars,
            "risk_off": {
                "stoch_cross_lookback_bars": settings.stoch_macd_risk_off_stoch_cross_lookback_bars,
                "hist_multiplier": settings.stoch_macd_risk_off_hist_multiplier,
                "volume_add": settings.stoch_macd_risk_off_volume_add,
                "vwap_buffer_multiplier": settings.stoch_macd_risk_off_vwap_buffer_multiplier,
                "max_r_multiplier": settings.stoch_macd_risk_off_max_r_multiplier,
            },
            "neutral_regime": {
                "hist_multiplier": settings.stoch_macd_neutral_hist_multiplier,
                "volume_add": settings.stoch_macd_neutral_volume_add,
                "vwap_buffer_multiplier": settings.stoch_macd_neutral_vwap_buffer_multiplier,
                "max_r_multiplier": settings.stoch_macd_neutral_max_r_multiplier,
            },
            "reentry_freshness": {
                "enabled": settings.stoch_macd_reentry_fresh_enabled,
                "lookback_bars": settings.stoch_macd_reentry_fresh_lookback_bars,
                "high_buffer_pct": settings.stoch_macd_reentry_high_buffer_pct,
                "hist_rise_multiplier": settings.stoch_macd_reentry_hist_rise_multiplier,
                "volume_add": settings.stoch_macd_reentry_volume_add,
            },
            "respect_consecutive_loss_limits": settings.stoch_macd_respect_consecutive_loss_limits,
        }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_tz = MARKET_TZ
        self._last_reject_log_ms: dict[tuple[str, str], int] = {}
        self._last_entry_ms_by_symbol: dict[str, int] = {}

    def _indicator_bars(self, state: SymbolState) -> list:
        """Chronological rolling bars for indicators: no session-date filter (preload + prior days kept)."""
        bars: list = []
        for bar in state.bars:
            current = datetime.fromtimestamp(bar.start_ms / 1000, tz=MARKET_TZ)
            if self.settings.indicator_include_afterhours:
                bars.append(bar)
            else:
                if PREMARKET_OPEN <= current.time() < MARKET_CLOSE:
                    bars.append(bar)
        return bars

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
        risk_off = self._risk_off_market_regime()
        neutral_hardening = self._neutral_market_regime_hardening()
        reentry_freshness = self._requires_reentry_freshness(state)
        max_spread_bps = self.settings.stoch_macd_max_spread_bps
        if early_window:
            max_spread_bps = min(max_spread_bps, self.settings.stoch_macd_early_max_spread_bps)
        if last.spread_bps > max_spread_bps:
            return self._reject(state, "spread", f"spread {last.spread_bps:.1f}bps too wide")

        indicator_bars = self._indicator_bars(state)
        if self.settings.stoch_macd_min_bars > 0 and len(indicator_bars) < self.settings.stoch_macd_min_bars:
            return self._reject(
                state,
                "bars",
                f"need >= {self.settings.stoch_macd_min_bars} indicator bars, have {len(indicator_bars)}",
            )

        stoch = self._compute_stoch(state)
        macd = self._compute_macd(state)
        if stoch is None or macd is None:
            return self._reject(state, "indicators", "could not compute STOCH/MACD")
        k_values, d_values = stoch
        macd_line, signal_line, hist = macd
        if len(k_values) < 1 or len(hist) < 1 or len(macd_line) < 1 or len(signal_line) < 1:
            return self._reject(state, "warmup", "indicator warmup incomplete")

        k_now = k_values[-1]
        d_now = d_values[-1]
        if k_now <= d_now:
            return self._reject(
                state,
                "stoch",
                f"STOCH not bullish k={k_now:.1f} d={d_now:.1f}",
            )
        stoch_cross_lookback = max(0, self.settings.stoch_macd_stoch_cross_lookback_bars)
        if risk_off:
            stoch_cross_lookback = min(
                stoch_cross_lookback,
                max(1, self.settings.stoch_macd_risk_off_stoch_cross_lookback_bars),
            )
        if not self._stoch_cross_recent(k_values, d_values, stoch_cross_lookback):
            return self._reject(
                state,
                "stoch_timing",
                f"STOCH bullish cross is stale lookback={stoch_cross_lookback}",
            )
        if not self._last_n_rising(k_values, 1):
            return self._reject(state, "stoch_timing", f"STOCH K not rising k={k_now:.1f}")

        ccc = macd_line[-1]
        macd_signal = signal_line[-1]
        if ccc <= macd_signal:
            return self._reject(state, "macd", f"CCC not bullish ccc={ccc:.4f} signal={macd_signal:.4f}")
        hist_now = hist[-1]
        price_ref = max(last.ask, 0.01)
        hist_norm = hist_now / price_ref
        min_hist_norm = self.settings.stoch_macd_min_hist_norm
        if early_window:
            min_hist_norm = max(min_hist_norm, self.settings.stoch_macd_early_min_hist_norm)
        if risk_off:
            min_hist_norm *= max(1.0, self.settings.stoch_macd_risk_off_hist_multiplier)
        elif neutral_hardening > 0:
            min_hist_norm *= 1.0 + (
                max(1.0, self.settings.stoch_macd_neutral_hist_multiplier) - 1.0
            ) * neutral_hardening
        if hist_norm < min_hist_norm:
            return self._reject(
                state,
                "macd_strength",
                f"hist too weak hist_norm={hist_norm:.5f} min={min_hist_norm:.5f}",
            )
        if not self._last_n_rising(hist, self.settings.stoch_macd_hist_rise_bars):
            return self._reject(state, "macd_strength", "histogram not rising")
        if not self._last_n_rising(macd_line, self.settings.stoch_macd_macd_rise_bars):
            return self._reject(state, "macd_strength", "MACD line not rising")
        if k_now > self.settings.stoch_macd_max_k:
            hist_rise_norm = self._rise_over_bars(hist, self.settings.stoch_macd_hist_rise_bars) / price_ref
            if hist_rise_norm < self.settings.stoch_macd_overbought_min_hist_rise_norm:
                return self._reject(
                    state,
                    "stoch_timing",
                    f"STOCH overbought without strong MACD expansion k={k_now:.1f} hist_rise_norm={hist_rise_norm:.5f}",
                )

        supertrend = self._compute_supertrend(
            indicator_bars,
            self.settings.stoch_macd_supertrend_period,
            self.settings.stoch_macd_supertrend_multiplier,
        )
        ema_fast = self._fast_ema(indicator_bars, self.settings.stoch_macd_ema_period)
        if self.settings.stoch_macd_supertrend_enabled:
            if supertrend is None or ema_fast is None:
                return self._reject(state, "supertrend", "could not compute EMA/SuperTrend")
            supertrend_value, supertrend_bullish = supertrend
            buffer_pct = max(0.0, self.settings.stoch_macd_supertrend_buffer_pct)
            min_ema = supertrend_value * (1.0 - buffer_pct)
            if ema_fast < min_ema:
                return self._reject(
                    state,
                    "supertrend_bearish",
                    f"EMA{self.settings.stoch_macd_ema_period} <= SuperTrend ema={ema_fast:.2f} line={supertrend_value:.2f}",
                )
            if not supertrend_bullish:
                return self._reject(state, "supertrend_bearish", f"SuperTrend bearish line={supertrend_value:.2f}")

        vol_r = self._volume_ratio(state)
        min_volume_ratio = self.settings.stoch_macd_min_volume_ratio
        if early_window:
            min_volume_ratio = max(min_volume_ratio, self.settings.stoch_macd_early_min_volume_ratio)
        if risk_off:
            min_volume_ratio += max(0.0, self.settings.stoch_macd_risk_off_volume_add)
        elif neutral_hardening > 0:
            min_volume_ratio += max(0.0, self.settings.stoch_macd_neutral_volume_add) * neutral_hardening
        if reentry_freshness:
            min_volume_ratio += max(0.0, self.settings.stoch_macd_reentry_volume_add)
        if vol_r < min_volume_ratio:
            return self._reject(state, "volume", f"volume ratio {vol_r:.2f} too low min={min_volume_ratio:.2f}")

        current_session_bars = self._current_session_indicator_bars(state)
        if reentry_freshness:
            lookback = max(2, self.settings.stoch_macd_reentry_fresh_lookback_bars)
            if len(current_session_bars) >= lookback + 1:
                recent_high = max(bar.high for bar in current_session_bars[-(lookback + 1) : -1])
                required_high = recent_high * (1.0 + max(0.0, self.settings.stoch_macd_reentry_high_buffer_pct))
                if last.ask < required_high:
                    return self._reject(
                        state,
                        "reentry_freshness",
                        f"repeat entry lacks fresh high ask={last.ask:.2f} required={required_high:.2f}",
                    )
            hist_rise_norm = self._rise_over_bars(hist, self.settings.stoch_macd_hist_rise_bars) / price_ref
            min_reentry_hist_rise = self.settings.stoch_macd_overbought_min_hist_rise_norm * max(
                1.0,
                self.settings.stoch_macd_reentry_hist_rise_multiplier,
            )
            if hist_rise_norm < min_reentry_hist_rise:
                return self._reject(
                    state,
                    "reentry_freshness",
                    f"repeat entry MACD expansion weak hist_rise_norm={hist_rise_norm:.5f} min={min_reentry_hist_rise:.5f}",
                )
        atr = self._atr(current_session_bars, self.settings.stoch_macd_atr_period)
        if atr is not None:
            atr_pct = atr / last.ask if last.ask > 0 else 0.0
            if atr_pct < self.settings.stoch_macd_min_atr_pct:
                return self._reject(state, "atr", f"ATR too low atr={atr_pct:.2%}")
            if atr_pct > self.settings.stoch_macd_max_atr_pct:
                return self._reject(state, "atr", f"ATR too high atr={atr_pct:.2%}")
        range_lookback = max(1, self.settings.stoch_macd_range_lookback_bars)
        if len(current_session_bars) >= range_lookback:
            recent_range_pct = self._range_pct(current_session_bars[-range_lookback:])
            if recent_range_pct < self.settings.stoch_macd_min_range_pct:
                return self._reject(state, "range", f"recent range too compressed range={recent_range_pct:.2%}")

        if self.settings.stoch_macd_vwap_enabled:
            session_vwap = self._session_vwap(current_session_bars)
            if session_vwap is None:
                return self._reject(state, "vwap", "missing current-session VWAP")
            vwap_buffer_pct = self.settings.stoch_macd_vwap_buffer_pct
            if early_window:
                vwap_buffer_pct = max(vwap_buffer_pct, self.settings.stoch_macd_early_vwap_buffer_pct)
            if risk_off:
                vwap_buffer_pct *= max(1.0, self.settings.stoch_macd_risk_off_vwap_buffer_multiplier)
            elif neutral_hardening > 0:
                vwap_buffer_pct *= 1.0 + (
                    max(1.0, self.settings.stoch_macd_neutral_vwap_buffer_multiplier) - 1.0
                ) * neutral_hardening
            min_price = session_vwap * (1.0 + max(0.0, vwap_buffer_pct))
            if last.ask <= min_price:
                return self._reject(
                    state,
                    "vwap",
                    f"price below VWAP ask={last.ask:.2f} vwap={session_vwap:.2f}",
                )
            if self.settings.stoch_macd_require_vwap_rising and len(current_session_bars) >= 6:
                prev_vwap = self._session_vwap(current_session_bars[:-3])
                if prev_vwap is None or session_vwap <= prev_vwap:
                    return self._reject(
                        state,
                        "vwap",
                        f"VWAP not rising current={session_vwap:.2f} previous={prev_vwap or 0.0:.2f}",
                    )

        stop_price = self._entry_stop_price(current_session_bars, last.ask)
        r_pct = (last.ask - stop_price) / last.ask if last.ask > 0 else 0.0
        max_r_pct = self.settings.stoch_macd_max_r_pct
        if risk_off:
            max_r_pct *= max(0.0, self.settings.stoch_macd_risk_off_max_r_multiplier)
        elif neutral_hardening > 0:
            neutral_max_r_multiplier = min(1.0, max(0.0, self.settings.stoch_macd_neutral_max_r_multiplier))
            max_r_pct *= 1.0 - ((1.0 - neutral_max_r_multiplier) * neutral_hardening)
        if r_pct < self.settings.stoch_macd_min_r_pct:
            return self._reject(state, "risk", f"R too small r={r_pct:.2%}")
        if r_pct > max_r_pct:
            return self._reject(state, "risk", f"R too wide r={r_pct:.2%} max={max_r_pct:.2%}")
        regime_reason = " regime=risk_off" if risk_off else ""
        if not risk_off and neutral_hardening > 0:
            regime_reason = f" regime=neutral_hardened:{neutral_hardening:.2f}"
        reentry_reason = " reentry=fresh" if reentry_freshness else ""
        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=last.ask,
            timestamp_ms=state.last_event_ms,
            change_pct=hist[-1],
            volume_ratio=vol_r,
            spread_bps=last.spread_bps,
            reason=(
                "stoch_macd_reversal confirmed trend "
                f"ema{self.settings.stoch_macd_ema_period}={ema_fast if ema_fast is not None else 0.0:.2f} "
                f"ccc={ccc:.4f} signal={macd_signal:.4f} hist_norm={hist_norm:.5f} r={r_pct:.2%} "
                f"k={k_now:.1f} d={d_now:.1f} vol={vol_r:.2f}x{regime_reason}{reentry_reason}"
            ),
            stop_price=stop_price,
            position_size_multiplier=0.8,
        )

    def use_fixed_target_exit(self, position) -> bool:
        return False

    def should_exit(self, state: SymbolState, position) -> ExitDecision | None:
        if position.strategy != self.name:
            return None
        price = state.last_price
        if price is None or position.entry_price <= 0:
            return None

        event_ms = state.last_event_ms or (state.quote.timestamp_ms if state.quote else position.entry_ms)
        age_seconds = (event_ms - position.entry_ms) / 1000
        pnl_pct = (price - position.entry_price) / position.entry_price

        if age_seconds < self.settings.stoch_macd_min_hold_seconds:
            return None

        if pnl_pct <= -self.settings.stoch_macd_stop_loss_pct:
            return ExitDecision("stop loss")

        initial_stop = position.initial_stop_price or position.stop_price
        r_initial = position.entry_price - initial_stop if initial_stop else 0.0
        if r_initial <= 0:
            r_initial = position.entry_price * self.settings.stoch_macd_stop_loss_pct
        if r_initial > 0 and not position.partial_exit_taken and position.shares > 1:
            partial_level = position.entry_price + r_initial * self.settings.stoch_macd_partial_r
            if price >= partial_level:
                fraction = min(1.0, max(0.0, self.settings.stoch_macd_partial_size))
                shares = max(1, min(position.shares - 1, int(position.shares * fraction)))
                return ExitDecision(f"partial {self.settings.stoch_macd_partial_r:.1f}R", shares=shares, mark_partial=True)

        if pnl_pct >= self.settings.stoch_macd_target_profit_pct:
            return ExitDecision("target profit")
        if position.partial_exit_taken:
            peak = position.max_price if position.max_price > 0 else position.entry_price
            if peak > 0 and price <= peak * (1 - self.settings.stoch_macd_runner_pullback_pct):
                return ExitDecision("runner pullback")

        indicator_bars = self._indicator_bars(state)
        if self.settings.stoch_macd_min_bars > 0 and len(indicator_bars) < self.settings.stoch_macd_min_bars:
            return None
        stoch = self._compute_stoch(state)
        macd = self._compute_macd(state)
        supertrend = self._compute_supertrend(
            indicator_bars,
            self.settings.stoch_macd_supertrend_period,
            self.settings.stoch_macd_supertrend_multiplier,
        )
        ema_fast = self._fast_ema(indicator_bars, self.settings.stoch_macd_ema_period)
        if stoch is not None and macd is not None and supertrend is not None and ema_fast is not None:
            k_values, d_values = stoch
            macd_line, signal_line, _ = macd
            supertrend_value, supertrend_bullish = supertrend
            indicator_sell = (
                not supertrend_bullish
                and ema_fast < supertrend_value
                and macd_line[-1] < signal_line[-1]
                and k_values[-1] < d_values[-1]
            )
            if indicator_sell:
                return ExitDecision("stoch_macd indicator sell")

        trail_high = max(position.max_price or 0.0, price)
        if trail_high >= position.entry_price * (1.0 + self.settings.stoch_macd_trailing_activation_pct):
            if trail_high > 0 and price < trail_high * (1.0 - self.settings.stoch_macd_trailing_stop_pct):
                return ExitDecision("trailing stop")

        return None

    def _within_entry_window(self, timestamp_ms: int | None) -> bool:
        if timestamp_ms is None:
            return False
        current = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.market_tz)
        minutes = current.hour * 60 + current.minute
        market_open = MARKET_OPEN.hour * 60 + MARKET_OPEN.minute
        elapsed = minutes - market_open
        return self.settings.stoch_macd_start_minute <= elapsed <= self.settings.stoch_macd_end_minute

    def exit_activation_delay_seconds(self, position) -> int:
        return max(0, self.settings.stoch_macd_min_hold_seconds)

    def delay_stop_loss_until_exit_activation(self, position) -> bool:
        return True

    def _in_early_window(self, timestamp_ms: int | None) -> bool:
        if timestamp_ms is None or self.settings.stoch_macd_early_window_minutes <= 0:
            return False
        current = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.market_tz)
        minutes = current.hour * 60 + current.minute
        market_open = MARKET_OPEN.hour * 60 + MARKET_OPEN.minute
        elapsed = minutes - market_open
        return 0 <= elapsed < self.settings.stoch_macd_early_window_minutes

    def _risk_off_market_regime(self) -> bool:
        return getattr(getattr(self, "_market_regime", None), "name", "") == "risk_off"

    def _neutral_market_regime_hardening(self) -> float:
        regime = getattr(self, "_market_regime", None)
        if getattr(regime, "name", "") != "neutral":
            return 0.0
        risk_off_score = self.settings.market_regime_risk_off_score
        risk_on_score = self.settings.market_regime_risk_on_score
        span = max(1, risk_on_score - risk_off_score)
        score = getattr(regime, "score", 0)
        return min(1.0, max(0.0, (risk_on_score - score) / span))

    def on_entry_fill(self, fill) -> None:
        if getattr(fill, "strategy", "") != self.name:
            return
        symbol = str(getattr(fill, "symbol", "")).strip().upper()
        timestamp_ms = getattr(fill, "timestamp_ms", None)
        if symbol and timestamp_ms is not None:
            self._last_entry_ms_by_symbol[symbol] = int(timestamp_ms)

    def _requires_reentry_freshness(self, state: SymbolState) -> bool:
        if not self.settings.stoch_macd_reentry_fresh_enabled or state.last_event_ms is None:
            return False
        last_entry_ms = self._last_entry_ms_by_symbol.get(state.symbol.upper())
        if last_entry_ms is None or state.last_event_ms <= last_entry_ms:
            return False
        current_date = datetime.fromtimestamp(state.last_event_ms / 1000, tz=MARKET_TZ).date()
        entry_date = datetime.fromtimestamp(last_entry_ms / 1000, tz=MARKET_TZ).date()
        return current_date == entry_date

    def _current_session_indicator_bars(self, state: SymbolState) -> list:
        if state.last_event_ms is None:
            return []
        current = datetime.fromtimestamp(state.last_event_ms / 1000, tz=MARKET_TZ)
        return [
            bar
            for bar in self._indicator_bars(state)
            if datetime.fromtimestamp(bar.start_ms / 1000, tz=MARKET_TZ).date() == current.date()
        ]

    @staticmethod
    def _session_vwap(bars) -> float | None:
        total_volume = sum(bar.volume for bar in bars if bar.volume > 0)
        if total_volume <= 0:
            return None
        total_value = sum(bar.vwap * bar.volume for bar in bars if bar.volume > 0)
        return total_value / total_volume if total_value > 0 else None

    @staticmethod
    def _atr(bars, period: int) -> float | None:
        if period <= 0 or len(bars) < period + 1:
            return None
        true_ranges: list[float] = []
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
        high = max(bar.high for bar in bars)
        low = min(bar.low for bar in bars)
        close = bars[-1].close
        return (high - low) / close if close > 0 else 0.0

    def _entry_stop_price(self, current_session_bars, entry: float) -> float:
        fixed_stop = entry * (1.0 - self.settings.stoch_macd_stop_loss_pct)
        lookback = max(1, self.settings.stoch_macd_structure_lookback_bars)
        if len(current_session_bars) < max(2, lookback):
            return fixed_stop
        recent = current_session_bars[-lookback:]
        swing_low = min(bar.low for bar in recent)
        if swing_low <= 0 or swing_low >= entry:
            return fixed_stop
        return max(fixed_stop, swing_low)

    @staticmethod
    def _last_n_rising(values: list[float], count: int) -> bool:
        if count <= 0:
            return True
        if len(values) < count + 1:
            return False
        tail = values[-(count + 1) :]
        return all(current > previous for previous, current in zip(tail, tail[1:]))

    @staticmethod
    def _rise_over_bars(values: list[float], count: int) -> float:
        if count <= 0 or len(values) < count + 1:
            return 0.0
        return values[-1] - values[-(count + 1)]

    @staticmethod
    def _stoch_cross_recent(k_values: list[float], d_values: list[float], lookback: int) -> bool:
        if lookback <= 0:
            return True
        if len(k_values) < 2 or len(d_values) < 2:
            return False
        paired = list(zip(k_values, d_values))
        tail = paired[-(lookback + 1) :]
        for previous, current in zip(tail, tail[1:]):
            prev_k, prev_d = previous
            curr_k, curr_d = current
            if prev_k <= prev_d and curr_k > curr_d:
                return True
        return False

    def _compute_macd(self, state: SymbolState) -> tuple[list[float], list[float], list[float]] | None:
        bars = self._indicator_bars(state)
        if len(bars) < self.settings.stoch_macd_macd_warmup_bars:
            return None
        closes = [float(bar.close) for bar in bars]
        if any(close <= 0 for close in closes):
            return None
        ema12 = _ema_series(closes, 12)
        ema26 = _ema_series(closes, 26)
        macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
        signal_line = _ema_series(macd_line, 9)
        hist = [m - s for m, s in zip(macd_line, signal_line)]
        return macd_line, signal_line, hist

    def _compute_stoch(
        self, state: SymbolState, k_period: int = 14, d_period: int = 3, smooth_k: int = 3
    ) -> tuple[list[float], list[float]] | None:
        bars = self._indicator_bars(state)
        if len(bars) < k_period + smooth_k + d_period:
            return None

        raw_k: list[float] = []
        for index in range(k_period - 1, len(bars)):
            window = bars[index - k_period + 1 : index + 1]
            high = max(bar.high for bar in window)
            low = min(bar.low for bar in window)
            if high <= low:
                raw_k.append(50.0)
            else:
                raw_k.append(((bars[index].close - low) / (high - low)) * 100.0)

        k_values = self._sma(raw_k, smooth_k)
        d_values = self._sma(k_values, d_period)
        return k_values, d_values

    @staticmethod
    def _fast_ema(session_bars, period: int) -> float | None:
        if period <= 0 or not session_bars:
            return None
        closes = [float(bar.close) for bar in session_bars if bar.close > 0]
        values = _ema_series(closes, period)
        return values[-1] if values else None

    @staticmethod
    def _compute_supertrend(session_bars, period: int = 7, multiplier: float = 3.0) -> tuple[float, bool] | None:
        if period <= 0 or multiplier <= 0 or len(session_bars) < period + 1:
            return None

        true_ranges: list[float] = []
        for index, bar in enumerate(session_bars):
            if index == 0:
                true_ranges.append(bar.high - bar.low)
                continue
            prev_close = session_bars[index - 1].close
            true_ranges.append(
                max(
                    bar.high - bar.low,
                    abs(bar.high - prev_close),
                    abs(bar.low - prev_close),
                )
            )

        atr_values: list[float | None] = []
        for index in range(len(true_ranges)):
            if index + 1 < period:
                atr_values.append(None)
            elif index + 1 == period:
                atr_values.append(sum(true_ranges[:period]) / period)
            else:
                prev_atr = atr_values[-1]
                if prev_atr is None:
                    return None
                atr_values.append(((prev_atr * (period - 1)) + true_ranges[index]) / period)

        first_atr_index = next((index for index, value in enumerate(atr_values) if value is not None), None)
        if first_atr_index is None:
            return None

        first_bar = session_bars[first_atr_index]
        first_atr = atr_values[first_atr_index]
        if first_atr is None:
            return None
        hl2 = (first_bar.high + first_bar.low) / 2
        final_upper = hl2 + multiplier * first_atr
        final_lower = hl2 - multiplier * first_atr
        bullish = first_bar.close >= hl2
        supertrend = final_lower if bullish else final_upper

        for index in range(first_atr_index + 1, len(session_bars)):
            bar = session_bars[index]
            atr = atr_values[index]
            if atr is None:
                continue
            basic_upper = ((bar.high + bar.low) / 2) + multiplier * atr
            basic_lower = ((bar.high + bar.low) / 2) - multiplier * atr
            prev_close = session_bars[index - 1].close

            if basic_upper < final_upper or prev_close > final_upper:
                final_upper = basic_upper
            if basic_lower > final_lower or prev_close < final_lower:
                final_lower = basic_lower

            if bar.close > final_upper:
                bullish = True
            elif bar.close < final_lower:
                bullish = False
            supertrend = final_lower if bullish else final_upper

        return supertrend, bullish

    @staticmethod
    def _sma(values: list[float], period: int) -> list[float]:
        if not values or period <= 1:
            return values[:]
        out: list[float] = []
        for index in range(len(values)):
            start = max(0, index - period + 1)
            window = values[start : index + 1]
            out.append(sum(window) / len(window))
        return out

    def _volume_ratio(self, state: SymbolState) -> float:
        bars = self._indicator_bars(state)
        if len(bars) < 2:
            return 0.0
        baseline = median([bar.volume for bar in bars[:-1] if bar.volume > 0] or [0.0])
        return bars[-1].volume / baseline if baseline > 0 else 0.0

    def _reject(self, state: SymbolState, code: str, detail: str) -> None:
        timestamp_ms = state.last_event_ms or 0
        key = (state.symbol, code)
        last_log_ms = self._last_reject_log_ms.get(key, -10_000)
        if timestamp_ms - last_log_ms >= 10_000:
            self._last_reject_log_ms[key] = timestamp_ms
            LOG.debug("No stoch_macd_reversal entry %s [%s]: %s", state.symbol, code, detail)
        return None
