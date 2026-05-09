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


LOG = logging.getLogger(__name__)
MARKET_OPEN = time(9, 30)
PREMARKET_OPEN = time(4, 0)

# MACD needs warmup; minimum regular-session bars for stable histogram.
_MIN_REGULAR_BARS = 20
_NEAR_HIGH_TOLERANCE_PCT = 0.003
_RECENT_HIGH_LOOKBACK = 15
_OVEREXTEND_MAX_PCT = 0.003
_EMA_TREND_PERIOD = 12


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
            1,
        ),
        (
            "macd_early_impulse_symbol_loss_lock_count",
            "MACD_SYMBOL_LOSS_LOCK_COUNT",
            int_env,
            1,
        ),
    )
    diagnostic_loggers: ClassVar[tuple[str, ...]] = ("strategies.macd_early_impulse",)
    selector_command: ClassVar[str] = ".venv/bin/python scripts/select_macd_early_impulse.py --top 12"

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
            "skip_midday": s.macd_skip_midday,
            "min_hold_seconds": s.macd_min_hold_seconds,
            "hist_rise_bars": s.macd_hist_rise_bars,
            "require_positive_hist": s.macd_require_positive_hist,
            "momentum_exit_min_profit_pct": s.macd_momentum_exit_min_profit_pct,
            "early_loss_cut_seconds": s.macd_early_loss_cut_seconds,
            "early_loss_cut_pct": s.macd_early_loss_cut_pct,
            "max_trades_per_symbol_per_session": s.macd_early_impulse_max_trades_per_symbol_per_session,
            "symbol_loss_lock_count": s.macd_early_impulse_symbol_loss_lock_count,
        }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_tz = MARKET_TZ
        self._last_reject_log_ms: dict[tuple[str, str], int] = {}

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

        now = datetime.now(tz=self.market_tz)
        start_of_day = datetime.combine(now.date(), PREMARKET_OPEN, tzinfo=self.market_tz)

        try:
            clients = make_clients(self.settings)
            intraday_bars = get_bars_between(clients, symbols, TimeFrame.Minute, start_of_day, now)
        except Exception:
            LOG.exception("MACD early impulse bootstrap failed to load bars")
            return

        seeded = 0
        for symbol, state in states.items():
            if not state.bars:
                for bar in intraday_bars.get(symbol, []):
                    state.add_bar(bar)
                    seeded += 1
        if seeded:
            LOG.info("MACD early impulse bootstrapped %s minute bars", seeded)

    def evaluate(self, state: SymbolState) -> Signal | None:
        if state.last_event_kind not in {"quote", "bar"}:
            return None

        if state.symbol not in self.settings.symbols:
            return self._reject(state, "symbol", "symbol not in selected MACD universe")

        if not self._within_entry_window(state.last_event_ms):
            return None

        last = latest_valid_quote(state)
        if last is None:
            return self._reject(state, "quote", "invalid or missing latest quote")

        rb = self._regular_bars(state)
        if len(rb) < _MIN_REGULAR_BARS:
            return self._reject(state, "bars", f"need >= {_MIN_REGULAR_BARS} regular bars, have {len(rb)}")

        if self._is_chop(state):
            return self._reject(
                state,
                "chop",
                f"10-bar range% < {self.settings.macd_chop_range_pct:.4f}",
            )

        mins = self._minutes_since_open(state)
        if self.settings.macd_skip_midday and 60 <= mins <= 120:
            return self._reject(state, "midday", f"skip midday {mins}m since open")

        if rb[-1].close <= rb[-3].close:
            return self._reject(state, "no_speed", "close[-1] not above close[-3]")

        closes = [float(bar.close) for bar in rb]
        ema_trend = _ema_series(closes, _EMA_TREND_PERIOD)
        if not ema_trend or last.ask < ema_trend[-1]:
            return self._reject(state, "below_ema", f"price below EMA{_EMA_TREND_PERIOD}")

        macd = self._compute_macd(state)
        if macd is None:
            return self._reject(state, "macd", "could not compute MACD")
        _, _, hist = macd
        min_hist_len = max(2, self.settings.macd_hist_rise_bars + 1)
        if len(hist) < min_hist_len:
            return self._reject(state, "macd", "insufficient histogram history")

        h1 = hist[-1]
        recent_hist = hist[-min_hist_len:]
        if not self._is_rising(recent_hist):
            return self._reject(
                state,
                "weak_hist",
                f"histogram not rising enough ({','.join(f'{h:.5f}' for h in recent_hist)})",
            )
        if self.settings.macd_require_positive_hist and h1 <= 0:
            return self._reject(state, "negative_hist", f"histogram {h1:.5f} not positive")

        hist_norm = h1 / last.ask if last.ask > 0 else 0.0
        if hist_norm < self.settings.macd_hist_threshold:
            return self._reject(
                state,
                "weak_macd",
                f"hist_norm {hist_norm:.5f} too small (min {self.settings.macd_hist_threshold})",
            )

        vol_r = self._volume_ratio(state)
        if vol_r < self.settings.macd_volume_ratio:
            return self._reject(
                state,
                "volume",
                f"volume_ratio {vol_r:.2f} < {self.settings.macd_volume_ratio}",
            )

        recent_high_10 = max(bar.high for bar in rb[-10:])
        extension = (last.ask - recent_high_10) / recent_high_10 if recent_high_10 > 0 else 0.0
        if extension > _OVEREXTEND_MAX_PCT:
            return self._reject(state, "overextended", f"extension {extension:.3%} too high")

        vwap = self._session_vwap(rb)
        if vwap is not None and last.ask < vwap:
            return self._reject(state, "below_vwap", "price below vwap")

        recent_high = max(bar.high for bar in rb[-_RECENT_HIGH_LOOKBACK:])
        near_high = last.ask >= recent_high * (1.0 - _NEAR_HIGH_TOLERANCE_PCT)
        if not near_high:
            return self._reject(
                state,
                "price_structure",
                f"not near recent high ({recent_high:.2f}) while above vwap ({vwap})",
            )

        spread_bps = last.spread_bps
        change_pct = h1
        stop_price = last.ask * (1.0 - self.settings.macd_stop_loss_pct)
        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=last.ask,
            timestamp_ms=last.timestamp_ms,
            change_pct=change_pct,
            volume_ratio=vol_r,
            spread_bps=spread_bps,
            reason="macd early impulse entry",
            stop_price=stop_price,
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

        if age_seconds <= self.settings.macd_early_loss_cut_seconds and pnl_pct <= -self.settings.macd_early_loss_cut_pct:
            return ExitDecision("early loss cut")

        if pnl_pct >= self.settings.macd_target_profit_pct:
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

    def _within_entry_window(self, timestamp_ms: int | None) -> bool:
        if timestamp_ms is None:
            return False
        current = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.market_tz)
        minutes = current.hour * 60 + current.minute
        market_open = MARKET_OPEN.hour * 60 + MARKET_OPEN.minute
        elapsed = minutes - market_open
        return self.settings.macd_start_minute <= elapsed <= self.settings.macd_end_minute

    def _regular_bars(self, state: SymbolState):
        return regular_bars(state)

    def _minutes_since_open(self, state: SymbolState) -> int:
        if state.last_event_ms is None:
            return -1
        current = datetime.fromtimestamp(state.last_event_ms / 1000, tz=self.market_tz)
        minutes = current.hour * 60 + current.minute
        market_open = MARKET_OPEN.hour * 60 + MARKET_OPEN.minute
        return minutes - market_open

    def _is_chop(self, state: SymbolState) -> bool:
        bars = self._regular_bars(state)[-10:]
        if len(bars) < 10:
            return True
        hi = max(b.high for b in bars)
        lo = min(b.low for b in bars)
        if lo <= 0:
            return True
        range_pct = (hi - lo) / lo
        return range_pct < self.settings.macd_chop_range_pct

    def _compute_macd(self, state: SymbolState) -> tuple[list[float], list[float], list[float]] | None:
        rb = self._regular_bars(state)
        if len(rb) < 5:
            return None
        closes = [float(bar.close) for bar in rb]
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
    def _session_vwap(session_bars) -> float | None:
        total_volume = sum(bar.volume for bar in session_bars if bar.volume > 0)
        if total_volume <= 0:
            return None
        total_value = sum(bar.vwap * bar.volume for bar in session_bars if bar.volume > 0)
        return total_value / total_volume if total_value > 0 else None

    @staticmethod
    def _volume_ratio(state: SymbolState) -> float:
        rb = regular_bars(state)
        if len(rb) < 2:
            return 0.0
        latest_volume = rb[-1].volume
        baseline = median([bar.volume for bar in rb[:-1] if bar.volume > 0] or [0.0])
        return latest_volume / baseline if baseline > 0 else 0.0

    def _reject(self, state: SymbolState, code: str, detail: str) -> None:
        timestamp_ms = state.last_event_ms or 0
        key = (state.symbol, code)
        last_log_ms = self._last_reject_log_ms.get(key, -10_000)
        if timestamp_ms - last_log_ms >= 10_000:
            self._last_reject_log_ms[key] = timestamp_ms
            LOG.debug("No macd_early_impulse entry %s [%s]: %s", state.symbol, code, detail)
        return None
