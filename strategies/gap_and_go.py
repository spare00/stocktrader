from __future__ import annotations

from datetime import datetime, time, timedelta
import logging
from typing import Any, ClassVar

from candle import SymbolState
from config import Settings
from env_vars import EnvSpec, float_env, int_env
from market_hours import MARKET_TZ
from models import ExitDecision, Signal
from strategy_selectors.select_gap_and_go import (
    latest_valid_quote,
    premarket_high_price,
    premarket_volume_ratio,
    previous_regular_bars,
    regular_bars,
    regular_open_price,
    session_date,
)
from strategies.base import Strategy


LOG = logging.getLogger(__name__)
MARKET_OPEN = time(9, 30)
PREMARKET_OPEN = time(4, 0)

# --- Breakout confirmation (disabled: selector handles screening) ---
GAP_AND_GO_CONFIRM_BREAKOUT = False
GAP_AND_GO_CONFIRM_BARS = 2


class GapAndGoStrategy(Strategy):
    name = "gap_and_go"
    env_specs: ClassVar[tuple[EnvSpec, ...]] = (
        ("gap_and_go_start_minute", "GAP_AND_GO_START_MINUTE", int_env, 0),
        ("gap_and_go_end_minute", "GAP_AND_GO_END_MINUTE", int_env, 30),
        ("gap_and_go_min_gap_pct", "GAP_AND_GO_MIN_GAP_PCT", float_env, 0.02),
        ("gap_and_go_premarket_volume_ratio", "GAP_AND_GO_PREMARKET_VOLUME_RATIO", float_env, 2.0),
        ("gap_and_go_max_spread_bps", "GAP_AND_GO_MAX_SPREAD_BPS", float_env, 10.0),
        ("gap_and_go_min_price", "GAP_AND_GO_MIN_PRICE", float_env, 5.0),
        ("gap_and_go_breakout_buffer_pct", "GAP_AND_GO_BREAKOUT_BUFFER_PCT", float_env, 0.0),
        ("gap_and_go_exit_activation_delay_seconds", "GAP_AND_GO_EXIT_ACTIVATION_DELAY_SECONDS", int_env, 15),
        ("gap_and_go_trailing_retrace_pct", "GAP_AND_GO_TRAILING_RETRACE_PCT", float_env, 0.008),
        ("gap_and_go_bar_window", "GAP_AND_GO_BAR_WINDOW", int_env, 5),
        ("gap_and_go_max_trades_per_symbol_per_session", "GAP_AND_GO_MAX_TRADES_PER_SYMBOL_PER_SESSION", int_env, 2),
        ("gap_and_go_symbol_loss_lock_count", "GAP_AND_GO_SYMBOL_LOSS_LOCK_COUNT", int_env, 2),
    )
    diagnostic_loggers: ClassVar[tuple[str, ...]] = ("strategies.gap_and_go",)
    selector_command: ClassVar[str] = ".venv/bin/python strategy_selectors/select_gap_and_go.py --top 5"

    @classmethod
    def runtime_settings_section(cls, settings: Any) -> dict[str, Any] | None:
        if cls.name not in settings.strategy_names:
            return None
        return {
            "start_minute": settings.gap_and_go_start_minute,
            "end_minute": settings.gap_and_go_end_minute,
            "min_gap_pct": settings.gap_and_go_min_gap_pct,
            "premarket_volume_ratio": settings.gap_and_go_premarket_volume_ratio,
            "max_spread_bps": settings.gap_and_go_max_spread_bps,
            "min_price": settings.gap_and_go_min_price,
            "breakout_buffer_pct": settings.gap_and_go_breakout_buffer_pct,
            "exit_activation_delay_seconds": settings.gap_and_go_exit_activation_delay_seconds,
            "trailing_retrace_pct": settings.gap_and_go_trailing_retrace_pct,
            "bar_window": settings.gap_and_go_bar_window,
            "max_trades_per_symbol_per_session": settings.gap_and_go_max_trades_per_symbol_per_session,
            "symbol_loss_lock_count": settings.gap_and_go_symbol_loss_lock_count,
        }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_tz = MARKET_TZ
        self._previous_close: dict[str, float] = {}
        self._last_reject_log_ms: dict[tuple[str, str], int] = {}

    def bootstrap_states(self, states: dict[str, SymbolState]) -> None:
        try:
            from alpaca.data.timeframe import TimeFrame

            from alpaca_client import get_bars_between, make_clients
        except Exception:
            LOG.exception("Gap-and-go bootstrap imports failed")
            return

        symbols = list(states)
        if not symbols:
            return

        now = datetime.now(tz=self.market_tz)
        start_of_day = datetime.combine(now.date(), PREMARKET_OPEN, tzinfo=self.market_tz)
        previous_start = datetime.combine((now - timedelta(days=5)).date(), time.min, tzinfo=self.market_tz)

        try:
            clients = make_clients(self.settings)
            intraday_bars = get_bars_between(clients, symbols, TimeFrame.Minute, start_of_day, now)
            daily_bars = get_bars_between(clients, symbols, TimeFrame.Day, previous_start, now + timedelta(days=1))
        except Exception:
            LOG.exception("Gap-and-go bootstrap failed to load historical context")
            return

        seeded_bars = 0
        for symbol, state in states.items():
            if not state.bars:
                for bar in intraday_bars.get(symbol, []):
                    state.add_bar(bar)
                    seeded_bars += 1

            daily_items = daily_bars.get(symbol, [])
            previous_close = None
            for bar in daily_items:
                bar_date = datetime.fromtimestamp(bar.start_ms / 1000, tz=self.market_tz).date()
                if bar_date < now.date():
                    previous_close = float(bar.close)
            if previous_close and previous_close > 0:
                self._previous_close[symbol] = previous_close

        if seeded_bars or self._previous_close:
            LOG.info(
                "Gap-and-go bootstrapped %s bars and %s previous closes",
                seeded_bars,
                len(self._previous_close),
            )

    def evaluate(self, state: SymbolState) -> Signal | None:
        if not self.is_symbol_allowed(state.symbol):
            return None
        if state.last_event_kind not in {"quote", "bar"}:
            return None
        if not self._within_entry_window(state.last_event_ms):
            return None

        last = latest_valid_quote(state)
        if last is None:
            return self._reject(state, "quote", "invalid or missing latest quote")

        prev_close = self._previous_close.get(state.symbol) or self._infer_previous_close(state)
        if not prev_close:
            return self._reject(state, "prev_close", "missing previous close")

        premarket_high = self._premarket_high(state)
        if not premarket_high or premarket_high <= 0:
            return self._reject(state, "premarket", "missing premarket high")

        premarket_volume = self._premarket_volume_ratio(state)
        gap_pct = (last.ask - prev_close) / prev_close
        if last.ask < self.settings.gap_and_go_min_price:
            return self._reject(state, "price", f"price {last.ask:.2f} below min {self.settings.gap_and_go_min_price:.2f}")
        if gap_pct < self.settings.gap_and_go_min_gap_pct:
            return self._reject(
                state,
                "gap",
                f"gap {gap_pct:.2%} below min {self.settings.gap_and_go_min_gap_pct:.2%}",
            )
        if premarket_volume < self.settings.gap_and_go_premarket_volume_ratio:
            return self._reject(
                state,
                "premarket_volume",
                f"premarket volume {premarket_volume:.2f}x below min {self.settings.gap_and_go_premarket_volume_ratio:.2f}x",
            )
        if last.spread_bps > self.settings.gap_and_go_max_spread_bps:
            return self._reject(
                state,
                "spread",
                f"spread {last.spread_bps:.1f}bps above max {self.settings.gap_and_go_max_spread_bps:.1f}bps",
            )

        buffer = self.settings.gap_and_go_breakout_buffer_pct
        breakout_level = premarket_high * (1 + buffer)
        reclaim_level = premarket_high * 0.95
        LOG.debug(
            "GNG %s ask=%.2f pm_high=%.2f breakout=%.2f reclaim=%.2f",
            state.symbol,
            last.ask,
            premarket_high,
            breakout_level,
            reclaim_level,
        )
        if last.ask >= reclaim_level:
            breakout_ok = True
            entry_type = "reclaim"
        elif last.ask >= breakout_level:
            breakout_ok = True
            entry_type = "breakout"
        else:
            breakout_ok = False
            entry_type = "none"

        # --- Change #6: Confirm breakout with N consecutive closes above premarket high ---
        if GAP_AND_GO_CONFIRM_BREAKOUT and entry_type == "breakout":
            recent_bars = self._regular_bars(state)
            last_n = recent_bars[-GAP_AND_GO_CONFIRM_BARS:] if len(recent_bars) >= GAP_AND_GO_CONFIRM_BARS else []
            if not last_n or not all(bar.close > premarket_high for bar in last_n):
                return self._reject(
                    state,
                    "confirm_breakout",
                    f"breakout not confirmed over last {GAP_AND_GO_CONFIRM_BARS} bars above {premarket_high:.2f}",
                )

        # --- Change #7: Fallback ORB entry ---
        opening_range_bars = self._opening_range_bars(state, minutes=5)
        opening_range_high = max((bar.high for bar in opening_range_bars), default=None)
        if opening_range_high and last.ask > opening_range_high:
            if not breakout_ok:
                breakout_ok = True
                entry_type = "orb"
        if not breakout_ok:
            return self._reject(
                state,
                "breakout",
                f"no breakout/reclaim: {last.ask:.2f} (premarket high {premarket_high:.2f})",
            )

        # --- Change #8: Extended reason log ---
        reason = (
            f"gap_and_go gap {gap_pct:.2%}, vol {premarket_volume:.1f}x, "
            f"premarket_high {premarket_high:.2f}, entry_type={entry_type}"
        )
        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=last.ask,
            timestamp_ms=last.timestamp_ms,
            change_pct=gap_pct,
            volume_ratio=premarket_volume,
            spread_bps=last.spread_bps,
            reason=reason,
        )

    def should_exit(self, state: SymbolState, position) -> ExitDecision | None:
        if position.strategy != self.name:
            return None

        price = state.last_price
        if price is None:
            return None

        event_ms = state.last_event_ms or (state.quote.timestamp_ms if state.quote else position.entry_ms)
        age_seconds = (event_ms - position.entry_ms) / 1000
        if age_seconds < self.exit_activation_delay_seconds(position):
            return None

        open_price = self._regular_open_price(state)
        session_vwap = self._session_vwap(state)
        if open_price and price < open_price:
            return ExitDecision("lost open")
        if session_vwap and price < session_vwap:
            return ExitDecision("lost vwap")

        recent_bars = self._regular_bars(state)[-max(3, self.settings.gap_and_go_bar_window) :]
        if len(recent_bars) >= 2:
            recent_high = max(bar.high for bar in recent_bars)
            pnl_pct = (price - position.entry_price) / position.entry_price if position.entry_price > 0 else 0.0
            if pnl_pct > 0 and price < recent_high * (1 - self.settings.gap_and_go_trailing_retrace_pct):
                return ExitDecision("trailing stop")

        return None

    def exit_activation_delay_seconds(self, position) -> int:
        return self.settings.gap_and_go_exit_activation_delay_seconds

    def _within_entry_window(self, timestamp_ms: int | None) -> bool:
        if timestamp_ms is None:
            return False
        current = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.market_tz)
        minutes = current.hour * 60 + current.minute
        market_open = (MARKET_OPEN.hour * 60) + MARKET_OPEN.minute
        elapsed = minutes - market_open
        # Entry window in minutes from regular session open.
        start = getattr(self.settings, "gap_and_go_start_minute", 0)
        end = getattr(self.settings, "gap_and_go_end_minute", 30)
        return start <= elapsed <= end

    def _session_vwap(self, state: SymbolState) -> float | None:
        session_bars = regular_bars(state)
        total_volume = sum(bar.volume for bar in session_bars if bar.volume > 0)
        if total_volume <= 0:
            return None
        total_value = sum(bar.vwap * bar.volume for bar in session_bars if bar.volume > 0)
        return total_value / total_volume if total_value > 0 else None

    def _regular_open_price(self, state: SymbolState) -> float | None:
        return regular_open_price(state)

    def _premarket_high(self, state: SymbolState) -> float | None:
        return premarket_high_price(state)

    def _premarket_volume_ratio(self, state: SymbolState) -> float:
        return premarket_volume_ratio(state)

    def _regular_bars(self, state: SymbolState):
        return regular_bars(state)

    def _previous_regular_bars(self, state: SymbolState):
        return previous_regular_bars(state)

    def _session_date(self, state: SymbolState):
        return session_date(state)

    def _opening_range_bars(self, state: SymbolState, minutes: int = 5):
        """Return bars from the first `minutes` of the regular session (for ORB)."""
        session_bars = regular_bars(state)
        cutoff_ms = None
        result = []
        for bar in session_bars:
            bar_time = datetime.fromtimestamp(bar.start_ms / 1000, tz=self.market_tz).time()
            bar_elapsed = (bar_time.hour * 60 + bar_time.minute) - (MARKET_OPEN.hour * 60 + MARKET_OPEN.minute)
            if bar_elapsed < minutes:
                result.append(bar)
        return result

    def _infer_previous_close(self, state: SymbolState) -> float | None:
        session_date = self._session_date(state)
        if session_date is None:
            return None
        previous_close = None
        for bar in state.bars:
            current = datetime.fromtimestamp(bar.start_ms / 1000, tz=self.market_tz)
            if current.date() < session_date and current.time() >= MARKET_OPEN:
                previous_close = bar.close
        return previous_close

    def _reject(self, state: SymbolState, code: str, detail: str) -> None:
        timestamp_ms = state.last_event_ms or 0
        key = (state.symbol, code)
        last_log_ms = self._last_reject_log_ms.get(key, -10_000)
        if timestamp_ms - last_log_ms >= 10_000:
            self._last_reject_log_ms[key] = timestamp_ms
            LOG.debug("No gap_and_go entry %s [%s]: %s", state.symbol, code, detail)
        return None
