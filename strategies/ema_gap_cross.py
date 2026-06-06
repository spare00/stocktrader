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
from strategy_selectors.select_gap_and_go import latest_valid_quote, regular_bars
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
    bars_since_entry: int = 0
    last_processed_bar_end_ms: int | None = None


def ema_crossed_below(fast: list[float], slow: list[float], *, index: int = -1) -> bool:
    """True when fast crosses below slow between index-1 and index."""
    if len(fast) < 2 or len(slow) < 2 or len(fast) != len(slow):
        return False
    i = index if index >= 0 else len(fast) + index
    if i < 1:
        return False
    return fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]


def recent_ema_cross_below(
    fast: list[float],
    slow: list[float],
    *,
    lookback: int = 8,
    before_index: int | None = None,
) -> tuple[bool, int | None]:
    """True when fast crossed below slow within `lookback` bars ending before `before_index`."""
    if len(fast) < 2 or len(slow) < 2 or len(fast) != len(slow) or lookback <= 0:
        return False, None
    end = len(fast) - 1 if before_index is None else before_index - 1
    if end < 1:
        return False, None
    start = max(1, end - lookback + 1)
    for index in range(end, start - 1, -1):
        if ema_crossed_below(fast, slow, index=index):
            return True, index
    return False, None


def ema_rising_for_bars(series: list[float], bars: int) -> bool:
    """True when `series` has not declined over the last `bars` bar-to-bar steps."""
    if bars <= 1:
        return len(series) >= 2 and series[-1] >= series[-2]
    if len(series) < bars + 1:
        return False
    return all(series[index] >= series[index - 1] for index in range(-bars, 0))


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


def ema_crossed_above_after_bars_below(
    fast: list[float],
    slow: list[float],
    *,
    bars_below: int = 2,
    index: int = -1,
) -> bool:
    """True when fast crosses above slow right after staying at/below slow for `bars_below` bars."""
    if not ema_crossed_above(fast, slow, index=index):
        return False
    if bars_below <= 0:
        return True
    i = index if index >= 0 else len(fast) + index
    if i < bars_below:
        return False
    return all(fast[i - 1 - offset] <= slow[i - 1 - offset] for offset in range(bars_below))


def find_golden_cross_after_below(
    fast: list[float],
    slow: list[float],
    *,
    bars_below: int = 2,
    lookback: int = 3,
) -> tuple[bool, int | None, int | None]:
    """Return (found, bars_since_cross, cross_index) for the freshest qualifying golden cross."""
    if len(fast) < 2 or len(slow) < 2 or len(fast) != len(slow):
        return False, None, None
    if fast[-1] <= slow[-1]:
        return False, None, None
    window = max(1, lookback)
    start = max(1, len(fast) - window)
    best_bars_since: int | None = None
    best_index: int | None = None
    for index in range(len(fast) - 1, start - 1, -1):
        if not ema_crossed_above_after_bars_below(fast, slow, bars_below=bars_below, index=index):
            continue
        bars_since = len(fast) - 1 - index
        if best_bars_since is None or bars_since < best_bars_since:
            best_bars_since = bars_since
            best_index = index
    if best_bars_since is None or best_index is None:
        return False, None, None
    return True, best_bars_since, best_index


def ema_fast_below_slow_for_bars(fast: list[float], slow: list[float], bars: int) -> bool:
    """True when fast has stayed below slow for the last `bars` completed values."""
    if bars <= 0 or len(fast) < bars or len(slow) < bars:
        return False
    return all(fast[index] < slow[index] for index in range(-bars, 0))


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
        ("egc_max_spread_bps", "EGC_MAX_SPREAD_BPS", float_env, 12.0),
        ("egc_min_gap_pct", "EGC_MIN_GAP_PCT", float_env, 0.0003),
        ("egc_partial_size", "EGC_PARTIAL_SIZE", float_env, 0.5),
        ("egc_min_hold_seconds", "EGC_MIN_HOLD_SECONDS", int_env, 180),
        ("egc_partial_grace_bars", "EGC_PARTIAL_GRACE_BARS", int_env, 3),
        ("egc_entry_below_bars", "EGC_ENTRY_BELOW_BARS", int_env, 2),
        ("egc_cross_lookback_bars", "EGC_CROSS_LOOKBACK_BARS", int_env, 3),
        ("egc_require_ema20_rising", "EGC_REQUIRE_EMA20_RISING", bool_env, True),
        ("egc_min_ema20_rise_bars", "EGC_MIN_EMA20_RISE_BARS", int_env, 2),
        ("egc_block_recent_death_cross_bars", "EGC_BLOCK_RECENT_DEATH_CROSS_BARS", int_env, 8),
        ("egc_require_above_vwap", "EGC_REQUIRE_ABOVE_VWAP", bool_env, True),
        ("egc_vwap_buffer_pct", "EGC_VWAP_BUFFER_PCT", float_env, 0.0),
        ("egc_require_bullish_cross_bar", "EGC_REQUIRE_BULLISH_CROSS_BAR", bool_env, True),
        ("egc_block_open_minutes", "EGC_BLOCK_OPEN_MINUTES", int_env, 0),
        ("egc_max_bars_since_cross", "EGC_MAX_BARS_SINCE_CROSS", int_env, 1),
        ("egc_stale_cross_min_gap_pct", "EGC_STALE_CROSS_MIN_GAP_PCT", float_env, 0.0008),
        ("egc_require_bullish_entry_bar", "EGC_REQUIRE_BULLISH_ENTRY_BAR", bool_env, True),
        ("egc_require_ema5_rising", "EGC_REQUIRE_EMA5_RISING", bool_env, True),
        ("egc_post_stop_cooldown_seconds", "EGC_POST_STOP_COOLDOWN_SECONDS", int_env, 600),
        ("egc_death_cross_confirm_bars", "EGC_DEATH_CROSS_CONFIRM_BARS", int_env, 2),
        ("egc_stop_lookback_bars", "EGC_STOP_LOOKBACK_BARS", int_env, 6),
        ("egc_stop_buffer_pct", "EGC_STOP_BUFFER_PCT", float_env, 0.001),
        ("egc_stop_loss_pct", "EGC_STOP_LOSS_PCT", float_env, 0.004),
        ("egc_require_ema_stack", "EGC_REQUIRE_EMA_STACK", bool_env, True),
        ("egc_max_trades_per_symbol_per_session", "EGC_MAX_TRADES_PER_SYMBOL_PER_SESSION", int_env, 3),
        ("egc_symbol_loss_lock_count", "EGC_SYMBOL_LOSS_LOCK_COUNT", int_env, 2),
        ("egc_respect_consecutive_loss_limits", "EGC_RESPECT_CONSECUTIVE_LOSS_LIMITS", bool_env, True),
        ("egc_max_hold_seconds", "EGC_MAX_HOLD_SECONDS", int_env, 0),
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
            "min_gap_pct": settings.egc_min_gap_pct,
            "partial_size": settings.egc_partial_size,
            "min_hold_seconds": settings.egc_min_hold_seconds,
            "partial_grace_bars": settings.egc_partial_grace_bars,
            "entry_below_bars": settings.egc_entry_below_bars,
            "cross_lookback_bars": settings.egc_cross_lookback_bars,
            "require_ema20_rising": bool(settings.egc_require_ema20_rising),
            "min_ema20_rise_bars": settings.egc_min_ema20_rise_bars,
            "block_recent_death_cross_bars": settings.egc_block_recent_death_cross_bars,
            "require_above_vwap": bool(settings.egc_require_above_vwap),
            "vwap_buffer_pct": settings.egc_vwap_buffer_pct,
            "require_bullish_cross_bar": bool(settings.egc_require_bullish_cross_bar),
            "block_open_minutes": settings.egc_block_open_minutes,
            "max_bars_since_cross": settings.egc_max_bars_since_cross,
            "stale_cross_min_gap_pct": settings.egc_stale_cross_min_gap_pct,
            "require_bullish_entry_bar": bool(settings.egc_require_bullish_entry_bar),
            "require_ema5_rising": bool(settings.egc_require_ema5_rising),
            "post_stop_cooldown_seconds": settings.egc_post_stop_cooldown_seconds,
            "death_cross_confirm_bars": settings.egc_death_cross_confirm_bars,
            "stop_lookback_bars": settings.egc_stop_lookback_bars,
            "stop_buffer_pct": settings.egc_stop_buffer_pct,
            "stop_loss_pct": settings.egc_stop_loss_pct,
            "require_ema_stack": bool(settings.egc_require_ema_stack),
            "max_trades_per_symbol_per_session": settings.egc_max_trades_per_symbol_per_session,
            "symbol_loss_lock_count": settings.egc_symbol_loss_lock_count,
            "respect_consecutive_loss_limits": bool(settings.egc_respect_consecutive_loss_limits),
            "max_hold_seconds": settings.egc_max_hold_seconds,
        }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_tz = MARKET_TZ
        self._last_reject_log_ms: dict[tuple[str, str], int] = {}
        self._position_states: dict[str, _EGCPositionState] = {}
        self._last_signaled_cross_index: dict[str, int] = {}
        self._last_stop_ms: dict[str, int] = {}

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
        below_bars = max(0, self.settings.egc_entry_below_bars)
        lookback = max(1, self.settings.egc_cross_lookback_bars)
        found, bars_since, cross_index = find_golden_cross_after_below(
            ema5,
            ema20,
            bars_below=below_bars,
            lookback=lookback,
        )
        if not found or cross_index is None:
            return self._reject(
                state,
                "cross",
                (
                    f"no EMA5 golden cross within {lookback} bars after "
                    f"{below_bars} bars at/below EMA20 "
                    f"({ema5[-2]:.4f}<={ema20[-2]:.4f} -> {ema5[-1]:.4f}>{ema20[-1]:.4f})"
                ),
            )

        symbol = state.symbol.strip().upper()
        if self._last_signaled_cross_index.get(symbol) == cross_index:
            return self._reject(state, "cross", f"golden cross at bar {cross_index} already signaled")

        gap = ema5[-1] - ema20[-1]
        gap_pct = gap / ema20[-1] if ema20[-1] > 0 else 0.0

        if self._bad_entry_veto(
            state,
            symbol=symbol,
            indicator_bars=indicator_bars,
            ema5=ema5,
            ema20=ema20,
            cross_index=cross_index,
            bars_since=bars_since,
            gap_pct=gap_pct,
            ask=last.ask,
        ):
            return None

        if gap_pct < self.settings.egc_min_gap_pct:
            return self._reject(
                state,
                "gap",
                f"EMA gap {gap_pct:.4%} < min {self.settings.egc_min_gap_pct:.4%}",
            )

        if self.settings.egc_require_ema_stack and not (ema5[-1] > ema10[-1] > ema20[-1]):
            return self._reject(
                state,
                "stack",
                (
                    f"EMA stack not bullish ({ema5[-1]:.4f} > {ema10[-1]:.4f} > {ema20[-1]:.4f} required)"
                ),
            )

        stop_price = self._entry_stop_price(indicator_bars, last.ask)
        self._last_signaled_cross_index[symbol] = cross_index
        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=last.ask,
            timestamp_ms=state.last_event_ms,
            change_pct=gap_pct,
            volume_ratio=0.0,
            spread_bps=last.spread_bps,
            reason=(
                f"ema_gap_cross golden: EMA5 {ema5[-1]:.4f} > EMA10 {ema10[-1]:.4f} > EMA20 {ema20[-1]:.4f}, "
                f"gap {gap_pct:.4%}, cross {bars_since} bar(s) ago"
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

    def exit_activation_delay_seconds(self, position) -> int:
        return max(0, self.settings.egc_min_hold_seconds)

    def delay_stop_loss_until_exit_activation(self, position) -> bool:
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

        pnl_pct = (price - position.entry_price) / position.entry_price
        if pnl_pct <= -self.settings.egc_stop_loss_pct:
            self._last_stop_ms[symbol] = int(event_ms)
            self._clear_position_state(symbol)
            return ExitDecision("stop loss")

        event_ms = state.last_event_ms or (quote.timestamp_ms if quote else position.entry_ms)
        age_seconds = (event_ms - position.entry_ms) / 1000
        if age_seconds < self.settings.egc_min_hold_seconds:
            return None

        confirm_bars = max(1, self.settings.egc_death_cross_confirm_bars)
        if ema_fast_below_slow_for_bars(ema5, ema20, confirm_bars):
            self._clear_position_state(symbol)
            return ExitDecision(f"EMA5 below EMA20 for {confirm_bars} bars")

        if state.last_event_kind == "bar" and pos_state is not None:
            self._sync_position_bar_state(state, pos_state)
            grace_bars = max(0, self.settings.egc_partial_grace_bars)
            if pos_state.bars_since_entry <= grace_bars:
                return None

            declined, updated_peak = ema20_first_decline_from_peak(ema20, pos_state.ema20_peak)
            pos_state.ema20_peak = updated_peak
            if (
                declined
                and not position.partial_exit_taken
                and position.shares > 1
            ):
                return self._partial_exit_decision(position, reason="EMA20 first decline from peak")

        return None

    def _sync_position_bar_state(self, state: SymbolState, pos_state: _EGCPositionState) -> None:
        if not state.bars:
            return
        bar_end_ms = state.bars[-1].end_ms
        if bar_end_ms == pos_state.last_processed_bar_end_ms:
            return
        pos_state.last_processed_bar_end_ms = bar_end_ms
        pos_state.bars_since_entry += 1

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

    def _session_vwap(self, state: SymbolState) -> float | None:
        session_bars = regular_bars(state)
        total_volume = sum(bar.volume for bar in session_bars if bar.volume > 0)
        if total_volume <= 0:
            return None
        total_value = sum(bar.vwap * bar.volume for bar in session_bars if bar.volume > 0)
        return total_value / total_volume if total_value > 0 else None

    def _minutes_since_open(self, timestamp_ms: int | None) -> int | None:
        if timestamp_ms is None:
            return None
        current = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.market_tz)
        minutes = current.hour * 60 + current.minute
        market_open = MARKET_OPEN.hour * 60 + MARKET_OPEN.minute
        return minutes - market_open

    def _bad_entry_veto(
        self,
        state: SymbolState,
        *,
        symbol: str,
        indicator_bars: list,
        ema5: list[float],
        ema20: list[float],
        cross_index: int,
        bars_since: int | None,
        gap_pct: float,
        ask: float,
    ) -> bool:
        cooldown = max(0, self.settings.egc_post_stop_cooldown_seconds)
        if cooldown > 0:
            last_stop_ms = self._last_stop_ms.get(symbol)
            now_ms = state.last_event_ms or 0
            if last_stop_ms is not None and now_ms - last_stop_ms < cooldown * 1000:
                self._reject(
                    state,
                    "stop_cooldown",
                    f"recent stop loss {int((now_ms - last_stop_ms) / 1000)}s ago < {cooldown}s",
                )
                return True

        max_cross_age = max(0, self.settings.egc_max_bars_since_cross)
        if bars_since is not None and bars_since > max_cross_age:
            self._reject(
                state,
                "stale_cross",
                f"golden cross {bars_since} bar(s) ago > max {max_cross_age}",
            )
            return True

        if bars_since is not None and bars_since >= 1:
            stale_gap = max(0.0, self.settings.egc_stale_cross_min_gap_pct)
            if stale_gap > 0 and gap_pct < stale_gap:
                self._reject(
                    state,
                    "stale_cross",
                    f"stale cross gap {gap_pct:.4%} < min {stale_gap:.4%}",
                )
                return True

        block_open = max(0, self.settings.egc_block_open_minutes)
        if block_open > 0:
            elapsed = self._minutes_since_open(state.last_event_ms)
            if elapsed is not None and elapsed < block_open:
                self._reject(
                    state,
                    "open_chop",
                    f"within first {block_open} minutes after open ({elapsed}m elapsed)",
                )
                return True

        if self.settings.egc_require_ema20_rising:
            rise_bars = max(1, self.settings.egc_min_ema20_rise_bars)
            if not ema_rising_for_bars(ema20, rise_bars):
                self._reject(
                    state,
                    "trend",
                    f"EMA20 not rising for {rise_bars} bars ({ema20[-rise_bars]:.4f} -> {ema20[-1]:.4f})",
                )
                return True

        death_lookback = max(0, self.settings.egc_block_recent_death_cross_bars)
        if death_lookback > 0:
            whipsaw, death_index = recent_ema_cross_below(
                ema5,
                ema20,
                lookback=death_lookback,
                before_index=cross_index,
            )
            if whipsaw and death_index is not None:
                self._reject(
                    state,
                    "whipsaw",
                    f"death cross {cross_index - death_index} bar(s) before golden cross",
                )
                return True

        if self.settings.egc_require_above_vwap:
            session_vwap = self._session_vwap(state)
            if session_vwap is None:
                self._reject(state, "vwap", "missing session VWAP")
                return True
            min_price = session_vwap * (1.0 + max(0.0, self.settings.egc_vwap_buffer_pct))
            if ask < min_price:
                self._reject(
                    state,
                    "vwap",
                    f"price below VWAP ask={ask:.2f} vwap={session_vwap:.2f}",
                )
                return True

        if self.settings.egc_require_bullish_cross_bar:
            if cross_index < 0 or cross_index >= len(indicator_bars):
                self._reject(state, "cross_bar", "cross bar unavailable")
                return True
            cross_bar = indicator_bars[cross_index]
            if float(cross_bar.close) <= float(cross_bar.open):
                self._reject(
                    state,
                    "cross_bar",
                    (
                        f"golden-cross bar not bullish "
                        f"(open={float(cross_bar.open):.4f} close={float(cross_bar.close):.4f})"
                    ),
                )
                return True

        if self.settings.egc_require_ema5_rising and len(ema5) >= 2 and ema5[-1] <= ema5[-2]:
            self._reject(
                state,
                "momentum",
                f"EMA5 not rising ({ema5[-2]:.4f} -> {ema5[-1]:.4f})",
            )
            return True

        if self.settings.egc_require_bullish_entry_bar and indicator_bars:
            entry_bar = indicator_bars[-1]
            if float(entry_bar.close) <= float(entry_bar.open):
                self._reject(
                    state,
                    "entry_bar",
                    (
                        f"entry bar not bullish "
                        f"(open={float(entry_bar.open):.4f} close={float(entry_bar.close):.4f})"
                    ),
                )
                return True
        return False

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
