from __future__ import annotations

from datetime import datetime, time, timedelta
import logging
from statistics import mean
from typing import Any, ClassVar

from candle import SymbolState
from config import Settings
from env_vars import EnvSpec, bool_env, float_env, int_env
from market_hours import MARKET_TZ, market_now
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


class GapAndGoStrategy(Strategy):
    """Gap-up breakout strategy with opening range fallback.

    Entry: Gap ≥2% + premarket volume ≥2x, then:
    - Breakout mode: price > premarket high (priority)
    - Reclaim mode: price ≥95% of premarket high
    - ORB fallback: price breaks above first 5 min opening range

    Quality filters: min price ($5), max spread (10 bps), optional premarket
    exhaustion check (max PM extension), R-based risk management.

    Exit: R-based stop loss (swing low or 3%); partial at 1R; lost open/VWAP;
    trailing stop (0.8% from recent high); volume collapse. Min hold 15s.
    """
    name = "gap_and_go"
    env_specs: ClassVar[tuple[EnvSpec, ...]] = (
        ("gap_and_go_start_minute", "GAP_AND_GO_START_MINUTE", int_env, 0),
        ("gap_and_go_end_minute", "GAP_AND_GO_END_MINUTE", int_env, 360),
        ("gap_and_go_min_gap_pct", "GAP_AND_GO_MIN_GAP_PCT", float_env, 0.02),
        ("gap_and_go_premarket_volume_ratio", "GAP_AND_GO_PREMARKET_VOLUME_RATIO", float_env, 2.0),
        ("gap_and_go_max_spread_bps", "GAP_AND_GO_MAX_SPREAD_BPS", float_env, 10.0),
        ("gap_and_go_min_price", "GAP_AND_GO_MIN_PRICE", float_env, 5.0),
        ("gap_and_go_breakout_buffer_pct", "GAP_AND_GO_BREAKOUT_BUFFER_PCT", float_env, 0.0),
        ("gap_and_go_reclaim_pct", "GAP_AND_GO_RECLAIM_PCT", float_env, 0.95),
        ("gap_and_go_confirm_breakout", "GAP_AND_GO_CONFIRM_BREAKOUT", bool_env, False),
        ("gap_and_go_confirm_bars", "GAP_AND_GO_CONFIRM_BARS", int_env, 2),
        ("gap_and_go_use_stop_loss", "GAP_AND_GO_USE_STOP_LOSS", bool_env, True),
        ("gap_and_go_stop_loss_pct", "GAP_AND_GO_STOP_LOSS_PCT", float_env, 0.03),
        ("gap_and_go_swing_lookback", "GAP_AND_GO_SWING_LOOKBACK", int_env, 5),
        ("gap_and_go_stop_buffer_pct", "GAP_AND_GO_STOP_BUFFER_PCT", float_env, 0.001),
        ("gap_and_go_min_r_pct", "GAP_AND_GO_MIN_R_PCT", float_env, 0.005),
        ("gap_and_go_max_r_pct", "GAP_AND_GO_MAX_R_PCT", float_env, 0.04),
        ("gap_and_go_partial_r", "GAP_AND_GO_PARTIAL_R", float_env, 1.0),
        ("gap_and_go_partial_size", "GAP_AND_GO_PARTIAL_SIZE", float_env, 0.5),
        ("gap_and_go_volume_collapse_enabled", "GAP_AND_GO_VOLUME_COLLAPSE_ENABLED", bool_env, True),
        ("gap_and_go_volume_collapse_ratio", "GAP_AND_GO_VOLUME_COLLAPSE_RATIO", float_env, 0.3),
        ("gap_and_go_volume_collapse_min_bars", "GAP_AND_GO_VOLUME_COLLAPSE_MIN_BARS", int_env, 3),
        ("gap_and_go_max_premarket_extension_pct", "GAP_AND_GO_MAX_PREMARKET_EXTENSION_PCT", float_env, 0.10),
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
            "reclaim_pct": settings.gap_and_go_reclaim_pct,
            "confirm_breakout": settings.gap_and_go_confirm_breakout,
            "confirm_bars": settings.gap_and_go_confirm_bars,
            "use_stop_loss": settings.gap_and_go_use_stop_loss,
            "stop_loss_pct": settings.gap_and_go_stop_loss_pct,
            "swing_lookback": settings.gap_and_go_swing_lookback,
            "stop_buffer_pct": settings.gap_and_go_stop_buffer_pct,
            "min_r_pct": settings.gap_and_go_min_r_pct,
            "max_r_pct": settings.gap_and_go_max_r_pct,
            "partial_r": settings.gap_and_go_partial_r,
            "partial_size": settings.gap_and_go_partial_size,
            "volume_collapse_enabled": settings.gap_and_go_volume_collapse_enabled,
            "volume_collapse_ratio": settings.gap_and_go_volume_collapse_ratio,
            "volume_collapse_min_bars": settings.gap_and_go_volume_collapse_min_bars,
            "max_premarket_extension_pct": settings.gap_and_go_max_premarket_extension_pct,
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

        now = market_now(self.settings, states)
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

        # Premarket exhaustion filter: reject if PM already ran too much
        if self.settings.gap_and_go_max_premarket_extension_pct > 0:
            pm_low = self._premarket_low(state)
            if premarket_high and pm_low and pm_low > 0:
                pm_range_pct = (premarket_high - pm_low) / pm_low
                if pm_range_pct > self.settings.gap_and_go_max_premarket_extension_pct:
                    return self._reject(
                        state,
                        "premarket_exhausted",
                        f"PM range {pm_range_pct:.2%} > max {self.settings.gap_and_go_max_premarket_extension_pct:.2%}",
                    )
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
        reclaim_level = premarket_high * self.settings.gap_and_go_reclaim_pct

        # Priority: Breakout (above PM high) > Reclaim (approaching PM high) > None
        if last.ask >= breakout_level:
            breakout_ok = True
            entry_type = "breakout"
        elif last.ask >= reclaim_level:
            breakout_ok = True
            entry_type = "reclaim"
        else:
            breakout_ok = False
            entry_type = "none"

        # Optional breakout confirmation: require N consecutive closes above PM high
        if self.settings.gap_and_go_confirm_breakout and entry_type == "breakout":
            recent_bars = self._regular_bars(state)
            confirm_bars = max(1, self.settings.gap_and_go_confirm_bars)
            last_n = recent_bars[-confirm_bars:] if len(recent_bars) >= confirm_bars else []
            if not last_n or not all(bar.close > premarket_high for bar in last_n):
                return self._reject(
                    state,
                    "confirm_breakout",
                    f"breakout not confirmed over last {confirm_bars} bars above {premarket_high:.2f}",
                )

        # Fallback ORB entry: if no premarket breakout/reclaim, check opening range
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
                f"no breakout/reclaim/ORB: ask={last.ask:.2f} pm_high={premarket_high:.2f} orb_high={opening_range_high}",
            )

        # R-based stop loss calculation
        session_bars = self._regular_bars(state)
        stop_price = None
        r_pct = 0.0

        if self.settings.gap_and_go_use_stop_loss:
            swing_lookback = max(1, self.settings.gap_and_go_swing_lookback)
            swing_low = self._recent_swing_low(session_bars, swing_lookback)
            if swing_low and swing_low < last.ask:
                buffer = max(0.0, self.settings.gap_and_go_stop_buffer_pct)
                stop_price = swing_low * (1 - buffer)
            else:
                stop_price = last.ask * (1 - self.settings.gap_and_go_stop_loss_pct)

            r_dist = last.ask - stop_price
            if r_dist <= 0:
                return self._reject(state, "risk", "invalid R (stop >= entry)")

            r_pct = r_dist / last.ask if last.ask > 0 else 0.0
            min_r = self.settings.gap_and_go_min_r_pct
            max_r = self.settings.gap_and_go_max_r_pct
            if r_pct < min_r:
                return self._reject(state, "risk", f"R too small: {r_pct:.2%} < {min_r:.2%}")
            if r_pct > max_r:
                return self._reject(state, "risk", f"R too wide: {r_pct:.2%} > {max_r:.2%}")

        # Enhanced logging
        pm_low = self._premarket_low(state)
        LOG.debug(
            "GNG %s ask=%.2f pm_high=%.2f pm_low=%.2f pm_vol=%.1fx gap=%.2%% "
            "breakout=%.2f reclaim=%.2f orb_high=%s stop=%.2f r=%.2%%",
            state.symbol,
            last.ask,
            premarket_high,
            pm_low or 0.0,
            premarket_volume,
            gap_pct * 100,
            breakout_level,
            reclaim_level,
            f"{opening_range_high:.2f}" if opening_range_high else "N/A",
            stop_price or 0.0,
            r_pct * 100,
        )

        reason = (
            f"gap_and_go gap {gap_pct:.2%}, vol {premarket_volume:.1f}x, "
            f"premarket_high {premarket_high:.2f}, entry_type={entry_type}"
        )
        if stop_price:
            reason += f", stop={stop_price:.2f} r={r_pct:.2%}"

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
            stop_price=stop_price,
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

        # R-based stop loss
        if self.settings.gap_and_go_use_stop_loss:
            stop_price = position.stop_price
            if stop_price and price <= stop_price:
                return ExitDecision("stop loss")

        # Partial exit at 1R profit
        initial_stop = getattr(position, "initial_stop_price", None)
        if initial_stop is None:
            initial_stop = position.stop_price
        r_initial = position.entry_price - initial_stop if initial_stop else 0.0
        if (
            r_initial > 0
            and not position.partial_exit_taken
            and position.shares >= 2
            and price >= position.entry_price + (r_initial * self.settings.gap_and_go_partial_r)
        ):
            frac = min(1.0, max(0.0, self.settings.gap_and_go_partial_size))
            shares = max(1, min(position.shares - 1, int(position.shares * frac)))
            return ExitDecision(f"partial {self.settings.gap_and_go_partial_r:.1f}R", shares=shares, mark_partial=True)

        # Lost open
        open_price = self._regular_open_price(state)
        if open_price and price < open_price:
            return ExitDecision("lost open")

        # Lost VWAP
        session_vwap = self._session_vwap(state)
        if session_vwap and price < session_vwap:
            return ExitDecision("lost vwap")

        # Volume collapse
        if self.settings.gap_and_go_volume_collapse_enabled:
            session_bars = self._regular_bars(state)
            min_bars = max(2, self.settings.gap_and_go_volume_collapse_min_bars)
            if len(session_bars) >= min_bars:
                recent_bars = session_bars[-min_bars:]
                if len(recent_bars) >= min_bars:
                    baseline_vols = [b.volume for b in recent_bars[:-1] if b.volume > 0]
                    if baseline_vols:
                        avg_volume = mean(baseline_vols)
                        latest_volume = recent_bars[-1].volume
                        if latest_volume < avg_volume * self.settings.gap_and_go_volume_collapse_ratio:
                            return ExitDecision("volume collapse")

        # Trailing stop (only in profit)
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
        end = getattr(self.settings, "gap_and_go_end_minute", 360)
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

    def _premarket_low(self, state: SymbolState) -> float | None:
        """Get premarket low (4am-9:30am ET)."""
        premarket_bars = [
            bar
            for bar in state.bars
            if PREMARKET_OPEN
            <= datetime.fromtimestamp(bar.start_ms / 1000, tz=self.market_tz).time()
            < MARKET_OPEN
        ]
        if not premarket_bars:
            return None
        return min(bar.low for bar in premarket_bars)

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

    @staticmethod
    def _recent_swing_low(bars, lookback: int) -> float | None:
        """Find swing low pivot in last N bars (excluding current bar)."""
        if lookback < 1 or len(bars) < lookback + 1:
            return None
        search = bars[-(lookback + 1) : -1]
        if not search:
            return None
        if len(search) < 3:
            return min(b.low for b in search)
        # Find pivot: bar whose low <= both neighbors
        for index in range(1, len(search) - 1):
            previous_bar = search[index - 1]
            current = search[index]
            next_bar = search[index + 1]
            if current.low <= previous_bar.low and current.low <= next_bar.low:
                return current.low
        return min(b.low for b in search)

    def _reject(self, state: SymbolState, code: str, detail: str) -> None:
        timestamp_ms = state.last_event_ms or 0
        key = (state.symbol, code)
        last_log_ms = self._last_reject_log_ms.get(key, -10_000)
        if timestamp_ms - last_log_ms >= 10_000:
            self._last_reject_log_ms[key] = timestamp_ms
            LOG.debug("No gap_and_go entry %s [%s]: %s", state.symbol, code, detail)
        return None
