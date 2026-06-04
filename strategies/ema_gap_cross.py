from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
import logging
from typing import Any, ClassVar

from candle import SymbolState
from config import Settings
from env_vars import EnvSpec, bool_env, float_env, int_env
from market_hours import MARKET_TZ
from models import ExitDecision, Signal
from modules.indicator_history import continuous_indicator_bars
from strategy_selectors.select_gap_and_go import latest_valid_quote
from strategies.base import Strategy
from strategies.macd_early_impulse import _ema_series


LOG = logging.getLogger(__name__)
MARKET_OPEN = time(9, 30)
_EMA_FAST = 5
_EMA_MID = 10
_EMA_SLOW = 20
_MIN_WARMUP_BARS = 25


@dataclass
class _EGCPositionState:
    entry_ms: int
    ema20_peak: float | None = None


def ema_crossed_above(fast: list[float], slow: list[float], *, index: int = -1) -> bool:
    """True when fast crosses above slow between index-1 and index."""
    if len(fast) < 2 or len(slow) < 2 or len(fast) != len(slow):
        return False
    i = index if index >= 0 else len(fast) + index
    if i < 1:
        return False
    return fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]


def recent_ema_cross_above(
    fast: list[float],
    slow: list[float],
    *,
    lookback: int = 5,
) -> tuple[bool, int | None]:
    """True when fast crossed above slow within the last `lookback` bars; returns bars since cross."""
    if len(fast) < 2 or len(slow) < 2 or len(fast) != len(slow) or lookback <= 0:
        return False, None
    start = max(1, len(fast) - lookback)
    bars_since: int | None = None
    for index in range(start, len(fast)):
        if fast[index - 1] <= slow[index - 1] and fast[index] > slow[index]:
            candidate = len(fast) - 1 - index
            if bars_since is None or candidate < bars_since:
                bars_since = candidate
    if bars_since is None:
        return False, None
    return True, bars_since


def ema_crossed_below(fast: list[float], slow: list[float], *, index: int = -1) -> bool:
    """True when fast crosses below slow between index-1 and index."""
    if len(fast) < 2 or len(slow) < 2 or len(fast) != len(slow):
        return False
    i = index if index >= 0 else len(fast) + index
    if i < 1:
        return False
    return fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]


def ema20_first_decline_from_peak(ema20: list[float], peak: float | None) -> tuple[bool, float | None]:
    """Return (declined, updated_peak) when EMA20 drops below its running peak."""
    if len(ema20) < 2:
        return False, peak
    current = ema20[-1]
    updated_peak = current if peak is None else max(peak, current)
    if peak is None:
        return False, updated_peak
    if current < updated_peak:
        return True, updated_peak
    return False, updated_peak


class EmaGapCrossStrategy(Strategy):
    """EMA5/EMA10/EMA20: golden cross entry, EMA20-peak partial, death cross remainder."""

    name = "ema_gap_cross"
    requires_plan: ClassVar[bool] = False
    selector_command: ClassVar[str] = ".venv/bin/python strategy_selectors/select_ema_gap_cross.py --top 12"
    env_specs: ClassVar[tuple[EnvSpec, ...]] = (
        ("egc_start_minute", "EGC_START_MINUTE", int_env, 0),
        ("egc_end_minute", "EGC_END_MINUTE", int_env, 360),
        ("egc_warmup_bars", "EGC_WARMUP_BARS", int_env, _MIN_WARMUP_BARS),
        ("egc_max_spread_bps", "EGC_MAX_SPREAD_BPS", float_env, 8.0),
        ("egc_partial_size", "EGC_PARTIAL_SIZE", float_env, 0.5),
        ("egc_stop_lookback_bars", "EGC_STOP_LOOKBACK_BARS", int_env, 6),
        ("egc_stop_buffer_pct", "EGC_STOP_BUFFER_PCT", float_env, 0.001),
        ("egc_stop_loss_pct", "EGC_STOP_LOSS_PCT", float_env, 0.004),
        ("egc_require_ema5_above_ema10", "EGC_REQUIRE_EMA5_ABOVE_EMA10", bool_env, True),
        ("egc_max_trades_per_symbol_per_session", "EGC_MAX_TRADES_PER_SYMBOL_PER_SESSION", int_env, 2),
        ("egc_symbol_loss_lock_count", "EGC_SYMBOL_LOSS_LOCK_COUNT", int_env, 1),
        ("egc_respect_consecutive_loss_limits", "EGC_RESPECT_CONSECUTIVE_LOSS_LIMITS", bool_env, True),
    )
    diagnostic_loggers: ClassVar[tuple[str, ...]] = ("strategies.ema_gap_cross",)

    @classmethod
    def runtime_settings_section(cls, settings: Any) -> dict[str, Any] | None:
        if cls.name not in settings.strategy_names:
            return None
        return {
            "start_minute": settings.egc_start_minute,
            "end_minute": settings.egc_end_minute,
            "warmup_bars": settings.egc_warmup_bars,
            "max_spread_bps": settings.egc_max_spread_bps,
            "partial_size": settings.egc_partial_size,
            "stop_lookback_bars": settings.egc_stop_lookback_bars,
            "stop_buffer_pct": settings.egc_stop_buffer_pct,
            "stop_loss_pct": settings.egc_stop_loss_pct,
            "require_ema5_above_ema10": bool(settings.egc_require_ema5_above_ema10),
            "max_trades_per_symbol_per_session": settings.egc_max_trades_per_symbol_per_session,
            "symbol_loss_lock_count": settings.egc_symbol_loss_lock_count,
            "respect_consecutive_loss_limits": bool(settings.egc_respect_consecutive_loss_limits),
        }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_tz = MARKET_TZ
        self._last_reject_log_ms: dict[tuple[str, str], int] = {}
        self._position_states: dict[str, _EGCPositionState] = {}

    def evaluate(self, state: SymbolState) -> Signal | None:
        if state.last_event_kind != "bar":
            return None
        if not self.is_symbol_allowed(state.symbol):
            return None
        if not self._within_entry_window(state.last_event_ms):
            return None

        last = latest_valid_quote(state)
        if last is None:
            return self._reject(state, "quote", "invalid or missing latest quote")
        if last.spread_bps > self.settings.egc_max_spread_bps:
            return self._reject(state, "spread", f"spread {last.spread_bps:.1f}bps too wide")

        indicator_bars = self._indicator_bars(state)
        if len(indicator_bars) < self.settings.egc_warmup_bars:
            return self._reject(
                state,
                "warmup",
                f"need >= {self.settings.egc_warmup_bars} indicator bars, have {len(indicator_bars)}",
            )

        ema = self._ema_triple(indicator_bars)
        if ema is None:
            return self._reject(state, "ema", "insufficient EMA history")

        ema5, ema10, ema20 = ema
        if not ema_crossed_above(ema5, ema20):
            return self._reject(
                state,
                "cross",
                f"no EMA5 cross above EMA20 ({ema5[-2]:.4f}<={ema20[-2]:.4f} -> {ema5[-1]:.4f}>{ema20[-1]:.4f})",
            )

        if self.settings.egc_require_ema5_above_ema10 and ema5[-1] <= ema10[-1]:
            return self._reject(
                state,
                "ema10",
                f"EMA5 not above EMA10 ({ema5[-1]:.4f} <= {ema10[-1]:.4f})",
            )

        stop_price = self._entry_stop_price(indicator_bars, last.ask)
        gap = ema5[-1] - ema20[-1]
        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=last.ask,
            timestamp_ms=state.last_event_ms,
            change_pct=gap,
            volume_ratio=0.0,
            spread_bps=last.spread_bps,
            reason=(
                f"ema_gap_cross golden: EMA5 {ema5[-1]:.4f} > EMA20 {ema20[-1]:.4f}, "
                f"EMA10 {ema10[-1]:.4f}, gap {gap:.4f}"
            ),
            stop_price=stop_price,
        )

    def on_entry_fill(self, fill) -> None:
        if getattr(fill, "strategy", "") != self.name:
            return
        symbol = str(getattr(fill, "symbol", "")).strip().upper()
        timestamp_ms = getattr(fill, "timestamp_ms", None)
        if symbol and timestamp_ms is not None:
            self._position_states[symbol] = _EGCPositionState(entry_ms=int(timestamp_ms))

    def use_fixed_target_exit(self, position) -> bool:
        return False

    def should_exit(self, state: SymbolState, position) -> ExitDecision | None:
        if position.strategy != self.name:
            return None

        quote = getattr(state, "quote", None)
        price = quote.bid if quote is not None and quote.bid > 0 else state.last_price
        if price is None or position.entry_price <= 0:
            return None

        indicator_bars = self._indicator_bars(state)
        ema = self._ema_triple(indicator_bars)
        if ema is None:
            return None

        ema5, _ema10, ema20 = ema
        symbol = position.symbol.strip().upper()
        pos_state = self._position_states.get(symbol)

        if ema_crossed_below(ema5, ema20):
            self._clear_position_state(symbol)
            return ExitDecision("EMA5 death cross below EMA20")

        pnl_pct = (price - position.entry_price) / position.entry_price
        if pnl_pct <= -self.settings.egc_stop_loss_pct:
            self._clear_position_state(symbol)
            return ExitDecision("stop loss")

        if state.last_event_kind == "bar" and pos_state is not None:
            declined, updated_peak = ema20_first_decline_from_peak(ema20, pos_state.ema20_peak)
            pos_state.ema20_peak = updated_peak
            if (
                declined
                and not position.partial_exit_taken
                and position.shares > 1
            ):
                return self._partial_exit_decision(position, reason="EMA20 first decline from peak")

        return None

    def _ema_triple(self, bars: list) -> tuple[list[float], list[float], list[float]] | None:
        if len(bars) < _EMA_SLOW:
            return None
        closes = [float(bar.close) for bar in bars]
        return (
            _ema_series(closes, _EMA_FAST),
            _ema_series(closes, _EMA_MID),
            _ema_series(closes, _EMA_SLOW),
        )

    def _entry_stop_price(self, bars: list, entry: float) -> float:
        lookback = max(1, self.settings.egc_stop_lookback_bars)
        swing_low = min(float(bar.low) for bar in bars[-lookback:])
        pct_stop = entry * (1 - self.settings.egc_stop_loss_pct)
        raw = min(swing_low, pct_stop)
        return raw * (1 - self.settings.egc_stop_buffer_pct)

    def _partial_exit_decision(self, position, *, reason: str) -> ExitDecision:
        return ExitDecision(
            reason,
            shares=self._partial_exit_shares(position),
            mark_partial=True,
        )

    def _partial_exit_shares(self, position) -> int:
        fraction = min(1.0, max(0.0, self.settings.egc_partial_size))
        return max(1, min(position.shares - 1, int(position.shares * fraction)))

    def _indicator_bars(self, state: SymbolState) -> list:
        return continuous_indicator_bars(state, self.settings)

    def _clear_position_state(self, symbol: str) -> None:
        self._position_states.pop(symbol.strip().upper(), None)

    def _within_entry_window(self, timestamp_ms: int | None) -> bool:
        if timestamp_ms is None:
            return False
        current = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.market_tz)
        minutes = current.hour * 60 + current.minute
        market_open = MARKET_OPEN.hour * 60 + MARKET_OPEN.minute
        elapsed = minutes - market_open
        return self.settings.egc_start_minute <= elapsed <= self.settings.egc_end_minute

    def _reject(self, state: SymbolState, stage: str, detail: str) -> None:
        key = (state.symbol, stage)
        now = state.last_event_ms or 0
        if now - self._last_reject_log_ms.get(key, 0) < 60_000:
            return None
        self._last_reject_log_ms[key] = now
        LOG.debug("%s %s rejected [%s]: %s", self.name, state.symbol, stage, detail)
        return None
