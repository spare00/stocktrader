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
from scripts.select_gap_and_go import latest_valid_quote, regular_bars
from strategies.base import Strategy
from strategies.macd_early_impulse import _ema_series


LOG = logging.getLogger(__name__)
MARKET_OPEN = time(9, 30)


class StochMACDReversalStrategy(Strategy):
    """1-minute STOCH/MACD reversal entry modeled from ready/buy/sell chart stages."""

    name = "stoch_macd_reversal"
    requires_plan: ClassVar[bool] = False
    env_specs: ClassVar[tuple[EnvSpec, ...]] = (
        ("stoch_macd_start_minute", "STOCH_MACD_START_MINUTE", int_env, 0),
        ("stoch_macd_end_minute", "STOCH_MACD_END_MINUTE", int_env, 360),
        ("stoch_macd_min_bars", "STOCH_MACD_MIN_BARS", int_env, 35),
        ("stoch_macd_ema_period", "STOCH_MACD_EMA_PERIOD", int_env, 5),
        ("stoch_macd_supertrend_enabled", "STOCH_MACD_SUPERTREND_ENABLED", bool_env, True),
        ("stoch_macd_supertrend_period", "STOCH_MACD_SUPERTREND_PERIOD", int_env, 7),
        ("stoch_macd_supertrend_multiplier", "STOCH_MACD_SUPERTREND_MULTIPLIER", float_env, 3.0),
        ("stoch_macd_min_volume_ratio", "STOCH_MACD_MIN_VOLUME_RATIO", float_env, 0.65),
        ("stoch_macd_max_spread_bps", "STOCH_MACD_MAX_SPREAD_BPS", float_env, 18.0),
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
    )
    diagnostic_loggers: ClassVar[tuple[str, ...]] = ("strategies.stoch_macd_reversal",)
    selector_command: ClassVar[str] = ".venv/bin/python scripts/select_stoch_macd_reversal.py --top 12"

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
            "supertrend_period": settings.stoch_macd_supertrend_period,
            "supertrend_multiplier": settings.stoch_macd_supertrend_multiplier,
            "min_volume_ratio": settings.stoch_macd_min_volume_ratio,
            "max_spread_bps": settings.stoch_macd_max_spread_bps,
            "stop_loss_pct": settings.stoch_macd_stop_loss_pct,
            "target_profit_pct": settings.stoch_macd_target_profit_pct,
            "trailing_activation_pct": settings.stoch_macd_trailing_activation_pct,
            "trailing_stop_pct": settings.stoch_macd_trailing_stop_pct,
            "min_hold_seconds": settings.stoch_macd_min_hold_seconds,
            "max_trades_per_symbol_per_session": settings.stoch_macd_max_trades_per_symbol_per_session,
            "symbol_loss_lock_count": settings.stoch_macd_symbol_loss_lock_count,
        }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_tz = MARKET_TZ
        self._last_reject_log_ms: dict[tuple[str, str], int] = {}

    def evaluate(self, state: SymbolState) -> Signal | None:
        if state.last_event_kind not in {"quote", "bar"}:
            return None
        if state.symbol not in self.settings.symbols:
            return self._reject(state, "symbol", "symbol not in strategy universe")
        if not self._within_entry_window(state.last_event_ms):
            return None

        last = latest_valid_quote(state)
        if last is None:
            return self._reject(state, "quote", "invalid or missing latest quote")
        if last.spread_bps > self.settings.stoch_macd_max_spread_bps:
            return self._reject(state, "spread", f"spread {last.spread_bps:.1f}bps too wide")

        rb = regular_bars(state)
        if len(rb) < self.settings.stoch_macd_min_bars:
            return self._reject(state, "bars", f"need >= {self.settings.stoch_macd_min_bars} bars, have {len(rb)}")

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

        ccc = macd_line[-1]
        macd_signal = signal_line[-1]
        if ccc <= macd_signal or ccc < 0:
            return self._reject(state, "macd", f"CCC not bullish ccc={ccc:.4f} signal={macd_signal:.4f}")

        supertrend = self._compute_supertrend(
            rb,
            self.settings.stoch_macd_supertrend_period,
            self.settings.stoch_macd_supertrend_multiplier,
        )
        ema_fast = self._fast_ema(rb, self.settings.stoch_macd_ema_period)
        if self.settings.stoch_macd_supertrend_enabled:
            if supertrend is None or ema_fast is None:
                return self._reject(state, "supertrend", "could not compute EMA/SuperTrend")
            supertrend_value, supertrend_bullish = supertrend
            if not supertrend_bullish or ema_fast <= supertrend_value:
                return self._reject(
                    state,
                    "supertrend_bearish",
                    f"EMA{self.settings.stoch_macd_ema_period} <= SuperTrend ema={ema_fast:.2f} line={supertrend_value:.2f}",
                )

        vol_r = self._volume_ratio(state)
        if vol_r < self.settings.stoch_macd_min_volume_ratio:
            return self._reject(state, "volume", f"volume ratio {vol_r:.2f} too low")

        stop_price = last.ask * (1.0 - self.settings.stoch_macd_stop_loss_pct)
        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=last.ask,
            timestamp_ms=last.timestamp_ms,
            change_pct=hist[-1],
            volume_ratio=vol_r,
            spread_bps=last.spread_bps,
            reason=(
                "stoch_macd_reversal confirmed trend "
                f"ema{self.settings.stoch_macd_ema_period}={ema_fast if ema_fast is not None else 0.0:.2f} "
                f"ccc={ccc:.4f} signal={macd_signal:.4f} k={k_now:.1f} d={d_now:.1f}"
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

        if pnl_pct >= self.settings.stoch_macd_target_profit_pct:
            return ExitDecision("target profit")
        if pnl_pct <= -self.settings.stoch_macd_stop_loss_pct:
            return ExitDecision("stop loss")
        if age_seconds < self.settings.stoch_macd_min_hold_seconds:
            return None

        rb = regular_bars(state)
        if len(rb) < self.settings.stoch_macd_min_bars:
            return None
        stoch = self._compute_stoch(state)
        macd = self._compute_macd(state)
        supertrend = self._compute_supertrend(
            rb,
            self.settings.stoch_macd_supertrend_period,
            self.settings.stoch_macd_supertrend_multiplier,
        )
        ema_fast = self._fast_ema(rb, self.settings.stoch_macd_ema_period)
        if stoch is not None and macd is not None and supertrend is not None and ema_fast is not None:
            k_values, d_values = stoch
            macd_line, signal_line, _ = macd
            supertrend_value, _ = supertrend
            indicator_sell = (
                ema_fast < supertrend_value
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

    @staticmethod
    def _compute_macd(state: SymbolState) -> tuple[list[float], list[float], list[float]] | None:
        rb = regular_bars(state)
        if len(rb) < 26:
            return None
        closes = [float(bar.close) for bar in rb]
        if any(close <= 0 for close in closes):
            return None
        ema12 = _ema_series(closes, 12)
        ema26 = _ema_series(closes, 26)
        macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
        signal_line = _ema_series(macd_line, 9)
        hist = [m - s for m, s in zip(macd_line, signal_line)]
        return macd_line, signal_line, hist

    @staticmethod
    def _compute_stoch(state: SymbolState, k_period: int = 14, d_period: int = 3, smooth_k: int = 3) -> tuple[list[float], list[float]] | None:
        rb = regular_bars(state)
        if len(rb) < k_period + smooth_k + d_period:
            return None

        raw_k: list[float] = []
        for index in range(k_period - 1, len(rb)):
            window = rb[index - k_period + 1 : index + 1]
            high = max(bar.high for bar in window)
            low = min(bar.low for bar in window)
            if high <= low:
                raw_k.append(50.0)
            else:
                raw_k.append(((rb[index].close - low) / (high - low)) * 100.0)

        k_values = StochMACDReversalStrategy._sma(raw_k, smooth_k)
        d_values = StochMACDReversalStrategy._sma(k_values, d_period)
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

    @staticmethod
    def _volume_ratio(state: SymbolState) -> float:
        rb = regular_bars(state)
        if len(rb) < 2:
            return 0.0
        baseline = median([bar.volume for bar in rb[:-1] if bar.volume > 0] or [0.0])
        return rb[-1].volume / baseline if baseline > 0 else 0.0

    def _reject(self, state: SymbolState, code: str, detail: str) -> None:
        timestamp_ms = state.last_event_ms or 0
        key = (state.symbol, code)
        last_log_ms = self._last_reject_log_ms.get(key, -10_000)
        if timestamp_ms - last_log_ms >= 10_000:
            self._last_reject_log_ms[key] = timestamp_ms
            LOG.debug("No stoch_macd_reversal entry %s [%s]: %s", state.symbol, code, detail)
        return None
