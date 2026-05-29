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
PREMARKET_OPEN = time(4, 0)
MARKET_CLOSE = time(16, 0)

_AO_FAST = 5
_AO_SLOW = 34
_HH_PERIOD = 20
_MIN_WARMUP_BARS = 40


@dataclass(frozen=True)
class BPSeries:
    scores: list[float | None]
    momentums: list[float]
    avg_momentums: list[float]


@dataclass(frozen=True)
class BPBarDetails:
    score: float
    prev_score: float | None
    momentum: float
    avg_momentum: float
    prev_avg_momentum: float | None
    macd_above_signal: bool
    macd_positive: bool
    ao_positive: bool
    ema5_above_ema20: bool
    breakout_high: bool

    def is_green(self, *, threshold: float = 65.0) -> bool:
        return self.avg_momentum >= threshold

    def avg_momentum_rising(self) -> bool:
        return self.prev_avg_momentum is not None and self.avg_momentum > self.prev_avg_momentum


@dataclass
class _BPPositionState:
    entry_ms: int
    bars_since_entry: int = 0
    declines_without_rise: int = 0
    last_processed_bar_end_ms: int | None = None
    below_trend_hold_bar_end_ms: int | None = None


def _compute_ao_values(medians: list[float], fast: int = _AO_FAST, slow: int = _AO_SLOW) -> list[float | None]:
    if fast <= 0 or slow <= 0 or fast >= slow:
        return [None] * len(medians)
    out: list[float | None] = [None] * len(medians)
    for index in range(slow - 1, len(medians)):
        fast_window = medians[index - fast + 1 : index + 1]
        slow_window = medians[index - slow + 1 : index + 1]
        out[index] = (sum(fast_window) / fast) - (sum(slow_window) / slow)
    return out


def _rolling_highest(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0:
        return out
    for index in range(period - 1, len(values)):
        out[index] = max(values[index - period + 1 : index + 1])
    return out


def compute_breakout_power_series(
    bars: list,
    *,
    momentum_ema_period: int = 4,
) -> BPSeries:
    """TradingView BreakOut Power: score, raw momentum, and EMA-smoothed avg_momentum."""
    if not bars:
        return BPSeries(scores=[], momentums=[], avg_momentums=[])

    closes = [float(bar.close) for bar in bars]
    highs = [float(bar.high) for bar in bars]
    medians = [(high + float(bar.low)) / 2.0 for bar, high in zip(bars, highs)]

    ema5 = _ema_series(closes, 5)
    ema20 = _ema_series(closes, 20)
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
    signal_line = _ema_series(macd_line, 9)
    hist = [m - s for m, s in zip(macd_line, signal_line)]
    ao_values = _compute_ao_values(medians)
    hh = _rolling_highest(highs, _HH_PERIOD)

    scores: list[float | None] = [None] * len(bars)
    momentums: list[float] = [0.0] * len(bars)
    for index in range(len(bars)):
        if ao_values[index] is None:
            continue
        if index > 0 and hh[index - 1] is None:
            continue

        score = 0.0
        if macd_line[index] > signal_line[index]:
            score += 25.0
        if macd_line[index] > 0:
            score += 20.0
        if ao_values[index] > 0:
            score += 15.0
        if ema5[index] > ema20[index]:
            score += 20.0
        if index > 0 and closes[index] > hh[index - 1]:
            score += 20.0
        scores[index] = score

        if index > 0 and ao_values[index - 1] is not None:
            momentum = 0.0
            if hist[index] > hist[index - 1]:
                momentum += 50.0
            if ao_values[index] > ao_values[index - 1]:
                momentum += 50.0
            momentums[index] = momentum

    avg_momentums = _ema_series(momentums, max(1, momentum_ema_period))
    return BPSeries(scores=scores, momentums=momentums, avg_momentums=avg_momentums)


def latest_breakout_power_details(
    bars: list,
    *,
    momentum_ema_period: int = 4,
) -> BPBarDetails | None:
    """Return the latest bar's BP score, momentum, and component flags."""
    if len(bars) < 2:
        return None

    closes = [float(bar.close) for bar in bars]
    highs = [float(bar.high) for bar in bars]
    medians = [(high + float(bar.low)) / 2.0 for bar, high in zip(bars, highs)]

    ema5 = _ema_series(closes, 5)
    ema20 = _ema_series(closes, 20)
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
    signal_line = _ema_series(macd_line, 9)
    ao_values = _compute_ao_values(medians)
    hh = _rolling_highest(highs, _HH_PERIOD)

    series = compute_breakout_power_series(bars, momentum_ema_period=momentum_ema_period)
    index = len(bars) - 1
    score = series.scores[index]
    if score is None or ao_values[index] is None or hh[index - 1] is None:
        return None

    return BPBarDetails(
        score=score,
        prev_score=series.scores[index - 1],
        momentum=series.momentums[index],
        avg_momentum=series.avg_momentums[index],
        prev_avg_momentum=series.avg_momentums[index - 1] if index > 0 else None,
        macd_above_signal=macd_line[index] > signal_line[index],
        macd_positive=macd_line[index] > 0,
        ao_positive=ao_values[index] > 0,
        ema5_above_ema20=ema5[index] > ema20[index],
        breakout_high=closes[index] > hh[index - 1],
    )


def recent_breakout_power_cross(
    scores: list[float | None],
    *,
    trend_line: float = 50.0,
    lookback: int = 5,
) -> bool:
    paired = [value for value in scores if value is not None]
    if len(paired) < 2:
        return False
    tail = paired[-(lookback + 1) :]
    for previous, current in zip(tail, tail[1:]):
        if previous <= trend_line < current:
            return True
    return False


class BreakoutPowerStrategy(Strategy):
    """BreakOut Power (BP) score cross with momentum-colored entries and bar-based exits."""

    name = "breakout_power"
    requires_plan: ClassVar[bool] = False
    selector_command: ClassVar[str] = ".venv/bin/python strategy_selectors/select_breakout_power.py --top 12"
    env_specs: ClassVar[tuple[EnvSpec, ...]] = (
        ("bp_start_minute", "BP_START_MINUTE", int_env, 0),
        ("bp_end_minute", "BP_END_MINUTE", int_env, 360),
        ("bp_warmup_bars", "BP_WARMUP_BARS", int_env, _MIN_WARMUP_BARS),
        ("bp_green_threshold", "BP_GREEN_THRESHOLD", float_env, 65.0),
        ("bp_trend_line", "BP_TREND_LINE", float_env, 50.0),
        ("bp_hold_floor", "BP_HOLD_FLOOR", float_env, 45.0),
        ("bp_decline_grace_bars", "BP_DECLINE_GRACE_BARS", int_env, 2),
        ("bp_partial_size", "BP_PARTIAL_SIZE", float_env, 0.5),
        ("bp_momentum_ema_period", "BP_MOMENTUM_EMA_PERIOD", int_env, 4),
        ("bp_max_spread_bps", "BP_MAX_SPREAD_BPS", float_env, 15.0),
        ("bp_stop_lookback_bars", "BP_STOP_LOOKBACK_BARS", int_env, 6),
        ("bp_stop_buffer_pct", "BP_STOP_BUFFER_PCT", float_env, 0.001),
        ("bp_stop_loss_pct", "BP_STOP_LOSS_PCT", float_env, 0.005),
        ("bp_min_hold_seconds", "BP_MIN_HOLD_SECONDS", int_env, 0),
        ("bp_max_trades_per_symbol_per_session", "BP_MAX_TRADES_PER_SYMBOL_PER_SESSION", int_env, 3),
        ("bp_symbol_loss_lock_count", "BP_SYMBOL_LOSS_LOCK_COUNT", int_env, 1),
        ("bp_respect_consecutive_loss_limits", "BP_RESPECT_CONSECUTIVE_LOSS_LIMITS", bool_env, False),
    )
    diagnostic_loggers: ClassVar[tuple[str, ...]] = ("strategies.breakout_power",)

    @classmethod
    def runtime_settings_section(cls, settings: Any) -> dict[str, Any] | None:
        if cls.name not in settings.strategy_names:
            return None
        return {
            "start_minute": settings.bp_start_minute,
            "end_minute": settings.bp_end_minute,
            "warmup_bars": settings.bp_warmup_bars,
            "green_threshold": settings.bp_green_threshold,
            "trend_line": settings.bp_trend_line,
            "hold_floor": settings.bp_hold_floor,
            "decline_grace_bars": settings.bp_decline_grace_bars,
            "partial_size": settings.bp_partial_size,
            "momentum_ema_period": settings.bp_momentum_ema_period,
            "max_spread_bps": settings.bp_max_spread_bps,
            "stop_lookback_bars": settings.bp_stop_lookback_bars,
            "stop_buffer_pct": settings.bp_stop_buffer_pct,
            "stop_loss_pct": settings.bp_stop_loss_pct,
            "min_hold_seconds": settings.bp_min_hold_seconds,
            "max_trades_per_symbol_per_session": settings.bp_max_trades_per_symbol_per_session,
            "symbol_loss_lock_count": settings.bp_symbol_loss_lock_count,
            "respect_consecutive_loss_limits": bool(settings.bp_respect_consecutive_loss_limits),
        }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_tz = MARKET_TZ
        self._last_reject_log_ms: dict[tuple[str, str], int] = {}
        self._position_states: dict[str, _BPPositionState] = {}

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
        if last.spread_bps > self.settings.bp_max_spread_bps:
            return self._reject(state, "spread", f"spread {last.spread_bps:.1f}bps too wide")

        indicator_bars = self._indicator_bars(state)
        if len(indicator_bars) < self.settings.bp_warmup_bars:
            return self._reject(
                state,
                "warmup",
                f"need >= {self.settings.bp_warmup_bars} indicator bars, have {len(indicator_bars)}",
            )

        bp = self._compute_bp(indicator_bars)
        if len(bp.scores) < 2:
            return self._reject(state, "bp", "insufficient BP history")

        prev_score = bp.scores[-2]
        score = bp.scores[-1]
        avg_momentum = bp.avg_momentums[-1]
        if prev_score is None or score is None:
            return self._reject(state, "bp", "BP score unavailable")

        trend_line = self.settings.bp_trend_line
        green_threshold = self.settings.bp_green_threshold
        if not (prev_score <= trend_line < score):
            return self._reject(
                state,
                "bp_cross",
                f"no BP cross above {trend_line:.0f} prev={prev_score:.1f} now={score:.1f}",
            )
        if avg_momentum < green_threshold:
            return self._reject(
                state,
                "bp_color",
                f"BP not green avg_momentum={avg_momentum:.1f} need>={green_threshold:.0f}",
            )

        stop_price = self._entry_stop_price(indicator_bars, last.ask)
        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=last.ask,
            timestamp_ms=state.last_event_ms,
            change_pct=score - prev_score,
            volume_ratio=0.0,
            spread_bps=last.spread_bps,
            reason=(
                f"breakout_power cross score={score:.0f} "
                f"avg_momentum={avg_momentum:.1f} green>={green_threshold:.0f}"
            ),
            stop_price=stop_price,
        )

    def on_entry_fill(self, fill) -> None:
        if getattr(fill, "strategy", "") != self.name:
            return
        symbol = str(getattr(fill, "symbol", "")).strip().upper()
        timestamp_ms = getattr(fill, "timestamp_ms", None)
        if symbol and timestamp_ms is not None:
            self._position_states[symbol] = _BPPositionState(entry_ms=int(timestamp_ms))

    def use_fixed_target_exit(self, position) -> bool:
        return False

    def should_exit(self, state: SymbolState, position) -> ExitDecision | None:
        if position.strategy != self.name:
            return None

        quote = getattr(state, "quote", None)
        price = quote.bid if quote is not None and quote.bid > 0 else state.last_price
        if price is None or position.entry_price <= 0:
            return None

        event_ms = state.last_event_ms or (state.quote.timestamp_ms if state.quote else position.entry_ms)
        age_seconds = (event_ms - position.entry_ms) / 1000
        if age_seconds < self.settings.bp_min_hold_seconds:
            return None

        pnl_pct = (price - position.entry_price) / position.entry_price
        if pnl_pct <= -self.settings.bp_stop_loss_pct:
            self._clear_position_state(position.symbol)
            return ExitDecision("stop loss")

        indicator_bars = self._indicator_bars(state)
        bp = self._compute_bp(indicator_bars)
        if len(bp.scores) < 2:
            return None

        prev_score = bp.scores[-2]
        score = bp.scores[-1]
        if prev_score is None or score is None:
            return None

        symbol = position.symbol.strip().upper()
        pos_state = self._position_states.get(symbol)
        is_decline = score < prev_score
        is_rise = score > prev_score
        trend_line = self.settings.bp_trend_line

        if state.last_event_kind == "bar":
            self._sync_position_bar_state(position, indicator_bars, pos_state, prev_score, score, is_decline, is_rise)

        hold_floor = self.settings.bp_hold_floor
        recovery_hold = self.hold_supported(bp, trend_line=trend_line, hold_floor=hold_floor)

        below_trend_exit = self._below_trend_exit_decision(
            state,
            position,
            indicator_bars,
            pos_state,
            score,
            trend_line,
            hold_floor,
            recovery_hold,
        )
        if below_trend_exit is not None:
            return below_trend_exit
        if recovery_hold and score < trend_line:
            return None

        if (
            score >= trend_line
            and pos_state is not None
            and pos_state.declines_without_rise >= 2
        ):
            self._clear_position_state(position.symbol)
            return ExitDecision("BP double decline")

        if (
            is_decline
            and not position.partial_exit_taken
            and position.shares > 1
            and pos_state is not None
            and pos_state.bars_since_entry > self.settings.bp_decline_grace_bars
            and not (recovery_hold and score < trend_line)
        ):
            fraction = min(1.0, max(0.0, self.settings.bp_partial_size))
            shares = max(1, min(position.shares - 1, int(position.shares * fraction)))
            return ExitDecision("BP first decline partial", shares=shares, mark_partial=True)

        return None

    def exit_activation_delay_seconds(self, position) -> int:
        return max(0, self.settings.bp_min_hold_seconds)

    def _compute_bp(self, bars: list) -> BPSeries:
        return compute_breakout_power_series(
            bars,
            momentum_ema_period=self.settings.bp_momentum_ema_period,
        )

    def _below_trend_exit_decision(
        self,
        state: SymbolState,
        position,
        indicator_bars: list,
        pos_state: _BPPositionState | None,
        score: float,
        trend_line: float,
        hold_floor: float,
        recovery_hold: bool,
    ) -> ExitDecision | None:
        if score >= trend_line:
            if pos_state is not None:
                pos_state.below_trend_hold_bar_end_ms = None
            return None

        if score < hold_floor:
            self._clear_position_state(position.symbol)
            return ExitDecision(f"BP below {hold_floor:.0f}")

        if pos_state is None:
            self._clear_position_state(position.symbol)
            return ExitDecision(f"BP below {trend_line:.0f}")

        completed = [bar for bar in indicator_bars if bar.end_ms > position.entry_ms]
        latest_bar_end_ms = completed[-1].end_ms if completed else None
        deferred_bar_end_ms = pos_state.below_trend_hold_bar_end_ms

        if (
            state.last_event_kind == "bar"
            and latest_bar_end_ms is not None
            and deferred_bar_end_ms is not None
            and latest_bar_end_ms > deferred_bar_end_ms
        ):
            self._clear_position_state(position.symbol)
            return ExitDecision(f"BP failed recovery below {trend_line:.0f}")

        if recovery_hold:
            if state.last_event_kind == "bar" and latest_bar_end_ms is not None and deferred_bar_end_ms is None:
                pos_state.below_trend_hold_bar_end_ms = latest_bar_end_ms
            return None

        self._clear_position_state(position.symbol)
        return ExitDecision(f"BP below {trend_line:.0f}")

    def _sync_position_bar_state(
        self,
        position,
        indicator_bars: list,
        pos_state: _BPPositionState | None,
        prev_score: float,
        score: float,
        is_decline: bool,
        is_rise: bool,
    ) -> None:
        if pos_state is None:
            return
        completed = [bar for bar in indicator_bars if bar.end_ms > position.entry_ms]
        if not completed:
            return
        latest_bar = completed[-1]
        if pos_state.last_processed_bar_end_ms == latest_bar.end_ms:
            return

        pos_state.last_processed_bar_end_ms = latest_bar.end_ms
        pos_state.bars_since_entry = len(completed)
        if is_decline:
            pos_state.declines_without_rise += 1
        elif is_rise:
            pos_state.declines_without_rise = 0

    def _clear_position_state(self, symbol: str) -> None:
        self._position_states.pop(symbol.strip().upper(), None)

    def _indicator_bars(self, state: SymbolState) -> list:
        return continuous_indicator_bars(state, self.settings)

    def _entry_stop_price(self, bars: list, ask: float) -> float:
        lookback = max(1, self.settings.bp_stop_lookback_bars)
        tail = bars[-lookback:] if len(bars) >= lookback else bars
        swing_low = min(float(bar.low) for bar in tail) if tail else ask
        buffered = swing_low * (1.0 - max(0.0, self.settings.bp_stop_buffer_pct))
        pct_stop = ask * (1.0 - self.settings.bp_stop_loss_pct)
        return min(buffered, pct_stop)

    def _within_entry_window(self, timestamp_ms: int | None) -> bool:
        if timestamp_ms is None:
            return False
        current = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.market_tz)
        minutes = current.hour * 60 + current.minute
        market_open = MARKET_OPEN.hour * 60 + MARKET_OPEN.minute
        elapsed = minutes - market_open
        return self.settings.bp_start_minute <= elapsed <= self.settings.bp_end_minute

    def _reject(self, state: SymbolState, code: str, message: str) -> None:
        key = (state.symbol, code)
        now = state.last_event_ms or 0
        if now - self._last_reject_log_ms.get(key, 0) >= 60_000:
            LOG.debug("breakout_power reject %s [%s]: %s", state.symbol, code, message)
            self._last_reject_log_ms[key] = now
        return None

    @staticmethod
    def avg_momentum_rising(bp: BPSeries) -> bool:
        if len(bp.avg_momentums) < 2:
            return False
        return bp.avg_momentums[-1] > bp.avg_momentums[-2]

    @staticmethod
    def hold_supported(bp: BPSeries, *, trend_line: float, hold_floor: float) -> bool:
        if not bp.scores or bp.scores[-1] is None:
            return False
        score = bp.scores[-1]
        if score > trend_line:
            return True
        return score >= hold_floor and BreakoutPowerStrategy.avg_momentum_rising(bp)
