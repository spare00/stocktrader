from collections import deque
from dataclasses import dataclass
from datetime import datetime, time
import logging
from statistics import median
from typing import Any, ClassVar

from candle import SymbolState
from config import Settings
from env_vars import EnvSpec, bool_env, float_env, int_env
from market_hours import MARKET_TZ
from models import ExitDecision, Signal
from strategies.base import Strategy


LOG = logging.getLogger(__name__)
MARKET_OPEN = time(9, 30)


@dataclass(frozen=True)
class EntryCandidate:
    change_pct: float
    volume_ratio: float
    reason: str
    kind: str


@dataclass(frozen=True)
class OpeningRange:
    open: float
    high: float
    low: float
    midpoint: float
    volume: float
    start_ms: int
    end_ms: int


class OpeningImpulseStrategy(Strategy):
    name = "opening_impulse"
    env_specs: ClassVar[tuple[EnvSpec, ...]] = (
        ("opening_impulse_start_minute", "OPENING_IMPULSE_START_MINUTE", int_env, 0),
        ("opening_impulse_end_minute", "OPENING_IMPULSE_END_MINUTE", int_env, 150),
        ("opening_impulse_window_seconds", "OPENING_IMPULSE_WINDOW_SECONDS", int_env, 30),
        ("opening_impulse_min_quotes", "OPENING_IMPULSE_MIN_QUOTES", int_env, 10),
        ("opening_impulse_change_pct", "OPENING_IMPULSE_CHANGE_PCT", float_env, 0.009),
        ("opening_impulse_skip_extended_pct", "OPENING_IMPULSE_SKIP_EXTENDED_PCT", float_env, 0.03),
        ("opening_impulse_volume_ratio", "OPENING_IMPULSE_VOLUME_RATIO", float_env, 1.5),
        ("opening_impulse_min_quote_move_seconds", "OPENING_IMPULSE_MIN_QUOTE_MOVE_SECONDS", int_env, 20),
        ("opening_impulse_max_entry_extension_pct", "OPENING_IMPULSE_MAX_ENTRY_EXTENSION_PCT", float_env, 0.02),
        ("opening_impulse_bar_confirmation", "OPENING_IMPULSE_BAR_CONFIRMATION", bool_env, True),
        ("opening_impulse_bar_window", "OPENING_IMPULSE_BAR_WINDOW", int_env, 3),
        ("opening_impulse_bar_min_rising", "OPENING_IMPULSE_BAR_MIN_RISING", int_env, 2),
        ("opening_impulse_bar_change_pct", "OPENING_IMPULSE_BAR_CHANGE_PCT", float_env, 0.003),
        ("opening_impulse_bar_volume_ratio", "OPENING_IMPULSE_BAR_VOLUME_RATIO", float_env, 1.5),
        ("opening_impulse_range_minutes", "OPENING_IMPULSE_RANGE_MINUTES", int_env, 5),
        ("opening_impulse_enable_range_breakout", "OPENING_IMPULSE_ENABLE_RANGE_BREAKOUT", bool_env, True),
        ("opening_impulse_enable_range_reversal", "OPENING_IMPULSE_ENABLE_RANGE_REVERSAL", bool_env, True),
        ("opening_impulse_range_breakout_buffer_pct", "OPENING_IMPULSE_RANGE_BREAKOUT_BUFFER_PCT", float_env, 0.0005),
        ("opening_impulse_range_reversal_min_drop_pct", "OPENING_IMPULSE_RANGE_REVERSAL_MIN_DROP_PCT", float_env, 0.005),
        ("opening_impulse_range_reclaim_buffer_pct", "OPENING_IMPULSE_RANGE_RECLAIM_BUFFER_PCT", float_env, 0.0),
        ("opening_impulse_range_volume_ratio", "OPENING_IMPULSE_RANGE_VOLUME_RATIO", float_env, 1.2),
        ("opening_impulse_max_spread_bps", "OPENING_IMPULSE_MAX_SPREAD_BPS", float_env, 15.0),
        ("opening_impulse_min_quote_size", "OPENING_IMPULSE_MIN_QUOTE_SIZE", int_env, 25),
        ("opening_impulse_max_negative_steps", "OPENING_IMPULSE_MAX_NEGATIVE_STEPS", int_env, 1),
        ("opening_impulse_exit_window_seconds", "OPENING_IMPULSE_EXIT_WINDOW_SECONDS", int_env, 10),
        ("opening_impulse_exit_min_quotes", "OPENING_IMPULSE_EXIT_MIN_QUOTES", int_env, 4),
        ("opening_impulse_exit_negative_steps", "OPENING_IMPULSE_EXIT_NEGATIVE_STEPS", int_env, 4),
        ("opening_impulse_min_hold_seconds", "OPENING_IMPULSE_MIN_HOLD_SECONDS", int_env, 15),
        ("opening_impulse_winner_min_pnl_pct", "OPENING_IMPULSE_WINNER_MIN_PNL_PCT", float_env, 0.003),
        ("opening_impulse_early_loss_cut_pct", "OPENING_IMPULSE_EARLY_LOSS_CUT_PCT", float_env, 0.0),
        ("opening_impulse_stall_buffer_pct", "OPENING_IMPULSE_STALL_BUFFER_PCT", float_env, 0.001),
        ("opening_impulse_retrace_from_high_pct", "OPENING_IMPULSE_RETRACE_FROM_HIGH_PCT", float_env, 0.008),
        ("opening_impulse_pullback_pct", "OPENING_IMPULSE_PULLBACK_PCT", float_env, 0.005),
        ("opening_impulse_strong_volume_ratio", "OPENING_IMPULSE_STRONG_VOLUME_RATIO", float_env, 2.5),
        ("opening_impulse_strong_pullback_pct", "OPENING_IMPULSE_STRONG_PULLBACK_PCT", float_env, 0.01),
        ("opening_impulse_partial_take_profit_pct", "OPENING_IMPULSE_PARTIAL_TAKE_PROFIT_PCT", float_env, 0.008),
        ("opening_impulse_partial_take_profit_fraction", "OPENING_IMPULSE_PARTIAL_TAKE_PROFIT_FRACTION", float_env, 0.5),
        ("opening_impulse_runner_pullback_pct", "OPENING_IMPULSE_RUNNER_PULLBACK_PCT", float_env, 0.012),
        ("opening_impulse_volume_collapse_ratio", "OPENING_IMPULSE_VOLUME_COLLAPSE_RATIO", float_env, 0.5),
        ("opening_impulse_price_stall_seconds", "OPENING_IMPULSE_PRICE_STALL_SECONDS", int_env, 60),
        ("opening_impulse_news_hot_minutes", "OPENING_IMPULSE_NEWS_HOT_MINUTES", int_env, 10),
        ("opening_impulse_news_change_pct", "OPENING_IMPULSE_NEWS_CHANGE_PCT", float_env, 0.003),
        ("opening_impulse_news_min_volume_ratio", "OPENING_IMPULSE_NEWS_MIN_VOLUME_RATIO", float_env, 1.3),
        ("opening_impulse_news_tight_pullback_pct", "OPENING_IMPULSE_NEWS_TIGHT_PULLBACK_PCT", float_env, 0.003),
        ("opening_impulse_news_max_hold_seconds", "OPENING_IMPULSE_NEWS_MAX_HOLD_SECONDS", int_env, 90),
        (
            "opening_impulse_news_max_move_since_event_pct",
            "OPENING_IMPULSE_NEWS_MAX_MOVE_SINCE_EVENT_PCT",
            float_env,
            0.02,
        ),
        (
            "opening_impulse_max_trades_per_symbol_per_session",
            "OPENING_IMPULSE_MAX_TRADES_PER_SYMBOL_PER_SESSION",
            int_env,
            2,
        ),
        (
            "opening_impulse_symbol_loss_lock_count",
            "OPENING_IMPULSE_SYMBOL_LOSS_LOCK_COUNT",
            int_env,
            2,
        ),
        (
            "opening_impulse_failed_continuation_no_high_seconds",
            "OPENING_IMPULSE_FAILED_CONTINUATION_NO_HIGH_SECONDS",
            int_env,
            120,
        ),
        (
            "opening_impulse_failed_continuation_max_mfe_pct",
            "OPENING_IMPULSE_FAILED_CONTINUATION_MAX_MFE_PCT",
            float_env,
            0.004,
        ),
        (
            "opening_impulse_reentry_reclaim_lookback_bars",
            "OPENING_IMPULSE_REENTRY_RECLAIM_LOOKBACK_BARS",
            int_env,
            5,
        ),
        (
            "opening_impulse_reentry_min_volume_ratio",
            "OPENING_IMPULSE_REENTRY_MIN_VOLUME_RATIO",
            float_env,
            1.3,
        ),
    )
    diagnostic_loggers: ClassVar[tuple[str, ...]] = ("strategies.opening_impulse",)
    selector_command: ClassVar[str] = ".venv/bin/python strategy_selectors/select_opening_impulse.py --top 12"

    @classmethod
    def runtime_settings_section(cls, settings: Any) -> dict[str, Any] | None:
        if cls.name not in settings.strategy_names:
            return None
        return {
            "start_minute": settings.opening_impulse_start_minute,
            "end_minute": settings.opening_impulse_end_minute,
            "window_seconds": settings.opening_impulse_window_seconds,
            "min_quotes": settings.opening_impulse_min_quotes,
            "min_quote_move_seconds": settings.opening_impulse_min_quote_move_seconds,
            "max_entry_extension_pct": settings.opening_impulse_max_entry_extension_pct,
            "change_pct": settings.opening_impulse_change_pct,
            "skip_extended_pct": settings.opening_impulse_skip_extended_pct,
            "volume_ratio": settings.opening_impulse_volume_ratio,
            "bar_confirmation": settings.opening_impulse_bar_confirmation,
            "bar_window": settings.opening_impulse_bar_window,
            "bar_min_rising": settings.opening_impulse_bar_min_rising,
            "bar_change_pct": settings.opening_impulse_bar_change_pct,
            "bar_volume_ratio": settings.opening_impulse_bar_volume_ratio,
            "range_minutes": settings.opening_impulse_range_minutes,
            "range_breakout_enabled": settings.opening_impulse_enable_range_breakout,
            "range_reversal_enabled": settings.opening_impulse_enable_range_reversal,
            "range_breakout_buffer_pct": settings.opening_impulse_range_breakout_buffer_pct,
            "range_reversal_min_drop_pct": settings.opening_impulse_range_reversal_min_drop_pct,
            "range_reclaim_buffer_pct": settings.opening_impulse_range_reclaim_buffer_pct,
            "range_volume_ratio": settings.opening_impulse_range_volume_ratio,
            "max_spread_bps": settings.opening_impulse_max_spread_bps,
            "min_quote_size": settings.opening_impulse_min_quote_size,
            "max_negative_steps": settings.opening_impulse_max_negative_steps,
            "exit_window_seconds": settings.opening_impulse_exit_window_seconds,
            "exit_min_quotes": settings.opening_impulse_exit_min_quotes,
            "exit_negative_steps": settings.opening_impulse_exit_negative_steps,
            "min_hold_seconds": settings.opening_impulse_min_hold_seconds,
            "winner_min_pnl_pct": settings.opening_impulse_winner_min_pnl_pct,
            "early_loss_cut_pct": settings.opening_impulse_early_loss_cut_pct,
            "stall_buffer_pct": settings.opening_impulse_stall_buffer_pct,
            "retrace_from_high_pct": settings.opening_impulse_retrace_from_high_pct,
            "pullback_pct": settings.opening_impulse_pullback_pct,
            "strong_volume_ratio": settings.opening_impulse_strong_volume_ratio,
            "strong_pullback_pct": settings.opening_impulse_strong_pullback_pct,
            "partial_take_profit_pct": settings.opening_impulse_partial_take_profit_pct,
            "partial_take_profit_fraction": settings.opening_impulse_partial_take_profit_fraction,
            "runner_pullback_pct": settings.opening_impulse_runner_pullback_pct,
            "volume_collapse_ratio": settings.opening_impulse_volume_collapse_ratio,
            "price_stall_seconds": settings.opening_impulse_price_stall_seconds,
            "news_hot_minutes": settings.opening_impulse_news_hot_minutes,
            "news_change_pct": settings.opening_impulse_news_change_pct,
            "news_min_volume_ratio": settings.opening_impulse_news_min_volume_ratio,
            "news_tight_pullback_pct": settings.opening_impulse_news_tight_pullback_pct,
            "news_max_hold_seconds": settings.opening_impulse_news_max_hold_seconds,
            "news_max_move_since_event_pct": settings.opening_impulse_news_max_move_since_event_pct,
            "max_trades_per_symbol_per_session": settings.opening_impulse_max_trades_per_symbol_per_session,
            "symbol_loss_lock_count": settings.opening_impulse_symbol_loss_lock_count,
            "failed_continuation_no_high_seconds": settings.opening_impulse_failed_continuation_no_high_seconds,
            "failed_continuation_max_mfe_pct": settings.opening_impulse_failed_continuation_max_mfe_pct,
            "reentry_reclaim_lookback_bars": settings.opening_impulse_reentry_reclaim_lookback_bars,
            "reentry_min_volume_ratio": settings.opening_impulse_reentry_min_volume_ratio,
        }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_tz = MARKET_TZ
        self._last_reject_log_ms: dict[tuple[str, str], int] = {}

    def evaluate(self, state: SymbolState) -> Signal | None:
        if state.last_event_kind not in {"quote", "bar"}:
            return None

        if not self._within_trading_window(state.last_event_ms):
            return None

        last = self._latest_valid_quote(state)
        if last is None:
            return self._reject(state, "quote", "invalid or missing latest quote")

        spread_bps = last.spread_bps
        has_news = self._has_hot_news(state, last.timestamp_ms)
        if spread_bps > self.settings.opening_impulse_max_spread_bps:
            return self._reject(
                state,
                "spread",
                f"spread {spread_bps:.2f}bps > {self.settings.opening_impulse_max_spread_bps:.2f}bps",
            )

        quotes = self._recent_quotes(state, self.settings.opening_impulse_window_seconds)
        quote_change_pct = 0.0
        if len(quotes) >= self.settings.opening_impulse_min_quotes and quotes[0].mid > 0:
            quote_change_pct = (quotes[-1].mid - quotes[0].mid) / quotes[0].mid
        volume_ratio = self._volume_ratio(state)
        candidate = self._range_impulse(state, last) or self._bar_impulse(state)

        if candidate is None and quote_change_pct >= self.settings.opening_impulse_change_pct:
            first = quotes[0]
            elapsed_seconds = (last.timestamp_ms - first.timestamp_ms) / 1000
            if elapsed_seconds < self.settings.opening_impulse_min_quote_move_seconds:
                return self._reject(
                    state,
                    "quote_duration",
                    (
                        f"quote impulse duration {elapsed_seconds:.0f}s < "
                        f"{self.settings.opening_impulse_min_quote_move_seconds}s"
                    ),
                )
            if not self._higher_high_structure(state):
                return self._reject(state, "structure", "missing higher-high bar structure")
            candidate = EntryCandidate(
                change_pct=quote_change_pct,
                volume_ratio=volume_ratio,
                reason=(
                    f"opening quote impulse {quote_change_pct:.3%} over "
                    f"{elapsed_seconds:.0f}s, "
                    f"volume {volume_ratio:.1f}x baseline"
                ),
                kind="quote_impulse",
            )

        if (
            candidate is None
            and has_news
            and quote_change_pct >= self.settings.opening_impulse_news_change_pct
            and volume_ratio >= self.settings.opening_impulse_news_min_volume_ratio
        ):
            candidate = EntryCandidate(
                change_pct=quote_change_pct,
                volume_ratio=volume_ratio,
                reason=(
                    f"news_early_impulse {quote_change_pct:.3%}, "
                    f"volume {volume_ratio:.1f}x baseline"
                ),
                kind="news_early_impulse",
            )

        if candidate is None:
            quote_detail = (
                f"quotes {len(quotes)} < {self.settings.opening_impulse_min_quotes}"
                if len(quotes) < self.settings.opening_impulse_min_quotes
                else f"quote change {quote_change_pct:.3%} < {self.settings.opening_impulse_change_pct:.3%}"
            )
            return self._reject(state, "change", f"no bar/range signal and {quote_detail}")

        if candidate.volume_ratio <= 0:
            return self._reject(state, "volume", "volume ratio is zero")
        min_volume_ratio = (
            self.settings.opening_impulse_news_min_volume_ratio
            if has_news
            else self.settings.opening_impulse_volume_ratio
        )
        if candidate.volume_ratio < min_volume_ratio:
            return self._reject(
                state,
                "volume",
                f"volume {candidate.volume_ratio:.2f}x < {min_volume_ratio:.2f}x",
            )
        if not has_news and not self._higher_high_structure(state):
            return self._reject(state, "structure", "missing higher-high bar structure")
        if self._recent_failed_continuation(state) and not self._reentry_structure_reclaimed(state):
            return self._reject(state, "reentry", "failed-continuation pattern requires reclaim before re-entry")

        session_open_price = self._session_open_price(state)
        if session_open_price is None or session_open_price <= 0:
            return self._reject(state, "open", "missing regular session open")
        entry_open_pct = (last.mid - session_open_price) / session_open_price
        if entry_open_pct > self.settings.opening_impulse_max_entry_extension_pct:
            return self._reject(
                state,
                "extension",
                (
                    f"entry extension {entry_open_pct:.3%} > "
                    f"{self.settings.opening_impulse_max_entry_extension_pct:.3%}"
                ),
            )

        penalty = 0.0
        warnings = []

        if candidate.change_pct > self.settings.opening_impulse_skip_extended_pct:
            penalty += 1.0
            warnings.append(
                f"extended {candidate.change_pct:.3%} > {self.settings.opening_impulse_skip_extended_pct:.3%}"
            )

        if min(last.bid_size, last.ask_size) < self.settings.opening_impulse_min_quote_size:
            penalty += 0.5
            warnings.append(f"thin quote size {min(last.bid_size, last.ask_size)}")

        if quotes:
            negative_steps = self._negative_steps(quotes)
            if negative_steps > self.settings.opening_impulse_max_negative_steps:
                penalty += 1.0
                warnings.append(f"negative quote steps {negative_steps}")

            recent_high = max(quote.mid for quote in quotes)
            if last.mid < recent_high * (1 - self.settings.opening_impulse_retrace_from_high_pct):
                retrace_pct = (recent_high - last.mid) / recent_high
                penalty += 1.0
                warnings.append(f"quote retrace {retrace_pct:.3%}")

        reason = candidate.reason
        reason = f"{reason} | entry_vs_open {entry_open_pct:.3%}"
        if has_news:
            reason = f"{reason} | hot_news"
        if warnings:
            reason = f"{reason} | entry_warnings penalty={penalty:.1f}: {', '.join(warnings)}"

        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=last.ask,
            timestamp_ms=last.timestamp_ms,
            change_pct=candidate.change_pct,
            volume_ratio=candidate.volume_ratio,
            spread_bps=spread_bps,
            reason=reason,
            session_open_price=session_open_price,
            entry_open_pct=entry_open_pct,
        )

    def should_exit(self, state: SymbolState, position) -> ExitDecision | None:
        if state.last_event_kind not in {"quote", "bar"} or position.strategy != self.name:
            return None

        price = state.last_price
        if price is None:
            return None

        event_ms = state.last_event_ms or (state.quote.timestamp_ms if state.quote else position.entry_ms)
        age_seconds = (event_ms - position.entry_ms) / 1000
        has_news = self._has_hot_news(state, event_ms)
        if has_news and age_seconds >= self.settings.opening_impulse_news_max_hold_seconds:
            return ExitDecision("news max hold")
        if age_seconds < self.exit_activation_delay_seconds(position):
            return None

        pnl_pct = (price - position.entry_price) / position.entry_price if position.entry_price > 0 else 0.0

        if pnl_pct > 0:
            if self._confirmed_higher_high_break(state):
                return ExitDecision("higher-high break")

            pullback_pct = (position.max_price - price) / position.max_price if position.max_price > 0 else 0.0
            if position.partial_exit_taken:
                runner_limit = self.settings.opening_impulse_runner_pullback_pct
                if has_news:
                    runner_limit = min(runner_limit, self.settings.opening_impulse_news_tight_pullback_pct)
                if pullback_pct >= runner_limit:
                    return ExitDecision("runner pullback")
                return None

            if (
                position.shares >= 2
                and pnl_pct >= self.settings.opening_impulse_partial_take_profit_pct
            ):
                shares = max(1, int(position.shares * self.settings.opening_impulse_partial_take_profit_fraction))
                shares = min(position.shares - 1, shares)
                return ExitDecision("partial take profit", shares=shares, mark_partial=True)

            pullback_limit = self._pullback_limit(state, has_news=has_news)
            if pullback_pct >= pullback_limit:
                return ExitDecision("pullback from high")

            stalled_ms = event_ms - position.last_high_ts if position.last_high_ts else 0
            if stalled_ms > self.settings.opening_impulse_price_stall_seconds * 1000 and self._volume_collapsed(state):
                return ExitDecision("volume collapse stall")

        bars = list(state.bars)[-max(5, self.settings.opening_impulse_bar_window) :]
        if len(bars) >= 2:
            recent_low = min(bar.low for bar in bars[:-1])
            if pnl_pct <= 0 and price < recent_low:
                return ExitDecision("break structure")
        if self._failed_continuation_no_high(position, event_ms, pnl_pct):
            return ExitDecision("failed continuation no new highs")
        if self._failed_continuation_lower_highs(state, position):
            return ExitDecision("failed continuation lower highs")

        quotes = self._recent_quotes(state, self.settings.opening_impulse_exit_window_seconds)
        if len(quotes) < self.settings.opening_impulse_exit_min_quotes:
            if pnl_pct <= self.settings.opening_impulse_early_loss_cut_pct:
                return ExitDecision("cut loss early")
            return None

        recent_changes = [quotes[index].mid - quotes[index - 1].mid for index in range(1, len(quotes))]
        negative_steps = sum(1 for change in recent_changes if change < 0)
        if pnl_pct <= 0 and negative_steps > self.settings.opening_impulse_exit_negative_steps:
            return ExitDecision("momentum fade")

        if pnl_pct <= self.settings.opening_impulse_early_loss_cut_pct:
            return ExitDecision("cut loss early")

        return None

    def exit_activation_delay_seconds(self, position) -> int:
        return self.settings.opening_impulse_min_hold_seconds

    def use_fixed_target_exit(self, position) -> bool:
        return False

    def _reject(self, state: SymbolState, code: str, detail: str) -> None:
        timestamp_ms = state.last_event_ms or 0
        key = (state.symbol, code)
        last_log_ms = self._last_reject_log_ms.get(key, -10_000)
        if timestamp_ms - last_log_ms >= 10_000:
            self._last_reject_log_ms[key] = timestamp_ms
            LOG.debug("No opening_impulse entry %s: %s", state.symbol, detail)
        return None

    def _within_trading_window(self, timestamp_ms: int | None) -> bool:
        if timestamp_ms is None:
            return False
        current = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.market_tz)
        minutes = current.hour * 60 + current.minute
        market_open = 9 * 60 + 30
        elapsed = minutes - market_open
        return self.settings.opening_impulse_start_minute <= elapsed <= self.settings.opening_impulse_end_minute

    @staticmethod
    def _recent_quotes(state: SymbolState, window_seconds: int) -> list:
        if not state.quotes:
            return []
        latest_ms = state.quotes[-1].timestamp_ms
        threshold = latest_ms - (window_seconds * 1000)
        return [quote for quote in state.quotes if quote.timestamp_ms >= threshold]

    @staticmethod
    def _latest_valid_quote(state: SymbolState):
        quote = state.quote or (state.quotes[-1] if state.quotes else None)
        if quote is None:
            return None
        if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
            return None
        return quote

    @staticmethod
    def _negative_steps(quotes: list) -> int:
        negative_steps = 0
        for index in range(1, len(quotes)):
            if quotes[index].mid < quotes[index - 1].mid:
                negative_steps += 1
        return negative_steps

    def _bar_impulse(self, state: SymbolState) -> EntryCandidate | None:
        if not self.settings.opening_impulse_bar_confirmation:
            return None

        window = max(2, self.settings.opening_impulse_bar_window)
        bars = list(state.bars)[-window:]
        if len(bars) < window:
            return None

        start_price = bars[0].open or bars[0].close
        end_price = bars[-1].close
        if start_price <= 0:
            return None

        change_pct = (end_price - start_price) / start_price
        if change_pct < self.settings.opening_impulse_bar_change_pct:
            return None

        rising_bars = 0
        for index, current in enumerate(bars):
            previous_close = bars[index - 1].close if index > 0 else current.open
            if current.close >= current.open or current.close > previous_close:
                rising_bars += 1
        if rising_bars < self.settings.opening_impulse_bar_min_rising:
            return None
        if not self._higher_high_structure(state, window):
            return None

        volume_ratio = self._volume_ratio(state)
        if volume_ratio < self.settings.opening_impulse_bar_volume_ratio:
            return None

        elapsed_seconds = max(60, (bars[-1].end_ms - bars[0].start_ms) / 1000)
        reason = (
            f"opening bar impulse {change_pct:.3%} over {elapsed_seconds:.0f}s, "
            f"{rising_bars}/{len(bars)} rising bars, volume {volume_ratio:.1f}x baseline"
        )
        return EntryCandidate(change_pct=change_pct, volume_ratio=volume_ratio, reason=reason, kind="bar_impulse")

    def _range_impulse(self, state: SymbolState, latest_quote) -> EntryCandidate | None:
        opening_range = self._opening_range(state)
        if opening_range is None:
            return None

        latest_bar = state.bars[-1] if state.bars else None
        if latest_bar is None or latest_bar.end_ms <= opening_range.end_ms:
            return None

        volume_ratio = self._volume_ratio(state)
        if volume_ratio < self.settings.opening_impulse_range_volume_ratio:
            return None

        if not self._bar_momentum(state):
            return None

        latest_mid = latest_quote.mid
        breakout_level = opening_range.high * (1 + self.settings.opening_impulse_range_breakout_buffer_pct)
        if self.settings.opening_impulse_enable_range_breakout and latest_mid >= breakout_level and latest_bar.close >= opening_range.high:
            change_pct = (latest_mid - opening_range.high) / opening_range.high
            reason = (
                f"opening_range_breakout {change_pct:.3%} above {opening_range.high:.2f}, "
                f"volume {volume_ratio:.1f}x baseline"
            )
            return EntryCandidate(
                change_pct=change_pct,
                volume_ratio=volume_ratio,
                reason=reason,
                kind="opening_range_breakout",
            )

        opening_drop_pct = (opening_range.open - opening_range.low) / opening_range.open if opening_range.open else 0.0
        reclaim_level = opening_range.midpoint * (1 + self.settings.opening_impulse_range_reclaim_buffer_pct)
        reclaimed_midpoint = latest_mid >= reclaim_level and latest_bar.close >= opening_range.midpoint
        if (
            self.settings.opening_impulse_enable_range_reversal
            and opening_drop_pct >= self.settings.opening_impulse_range_reversal_min_drop_pct
            and reclaimed_midpoint
        ):
            change_pct = (latest_mid - opening_range.midpoint) / opening_range.midpoint if opening_range.midpoint else 0.0
            reason = (
                f"opening_range_reversal reclaim after {opening_drop_pct:.3%} flush, "
                f"volume {volume_ratio:.1f}x baseline"
            )
            return EntryCandidate(
                change_pct=change_pct,
                volume_ratio=volume_ratio,
                reason=reason,
                kind="opening_range_reversal",
            )

        return None

    def _opening_range(self, state: SymbolState) -> OpeningRange | None:
        range_bars = []
        for bar in state.bars:
            start = datetime.fromtimestamp(bar.start_ms / 1000, tz=self.market_tz)
            end = datetime.fromtimestamp(bar.end_ms / 1000, tz=self.market_tz)
            if start.time() < MARKET_OPEN:
                continue
            minutes_from_open = ((end.hour * 60 + end.minute) - (MARKET_OPEN.hour * 60 + MARKET_OPEN.minute))
            if 0 < minutes_from_open <= self.settings.opening_impulse_range_minutes:
                range_bars.append(bar)

        if not range_bars:
            return None

        high = max(bar.high for bar in range_bars)
        low = min(bar.low for bar in range_bars)
        return OpeningRange(
            open=range_bars[0].open,
            high=high,
            low=low,
            midpoint=(high + low) / 2,
            volume=sum(bar.volume for bar in range_bars),
            start_ms=range_bars[0].start_ms,
            end_ms=range_bars[-1].end_ms,
        )

    def _bar_momentum(self, state: SymbolState) -> bool:
        window = max(2, self.settings.opening_impulse_bar_window)
        bars = list(state.bars)[-window:]
        if len(bars) < window:
            return False
        rising_bars = 0
        for index, current in enumerate(bars):
            previous_close = bars[index - 1].close if index > 0 else current.open
            if current.close >= current.open or current.close > previous_close:
                rising_bars += 1
        return rising_bars >= self.settings.opening_impulse_bar_min_rising

    def _higher_high_structure(self, state: SymbolState, window: int | None = None) -> bool:
        size = max(2, window or self.settings.opening_impulse_bar_window)
        bars = list(state.bars)[-size:]
        if len(bars) < size:
            return False
        return all(bars[index].high > bars[index - 1].high for index in range(1, len(bars)))

    def _confirmed_higher_high_break(self, state: SymbolState) -> bool:
        bars = list(state.bars)[-3:]
        if len(bars) < 3:
            return False
        first_weak = bars[-2].high <= bars[-3].high
        confirmation = bars[-1].high <= bars[-2].high and bars[-1].close <= bars[-2].close
        return first_weak and confirmation

    def _pullback_limit(self, state: SymbolState, *, has_news: bool = False) -> float:
        if has_news:
            return self.settings.opening_impulse_news_tight_pullback_pct
        volume_ratio = self._volume_ratio(state)
        if volume_ratio >= self.settings.opening_impulse_strong_volume_ratio:
            return self.settings.opening_impulse_strong_pullback_pct
        return self.settings.opening_impulse_pullback_pct

    def _has_hot_news(self, state: SymbolState, timestamp_ms: int) -> bool:
        if state.last_news_ms is None:
            return False
        if state.last_news_sentiment <= 0:
            return False
        hot_window_ms = max(1, self.settings.opening_impulse_news_hot_minutes) * 60_000
        if (timestamp_ms - state.last_news_ms) > hot_window_ms:
            return False
        if state.last_news_price is None or state.last_news_price <= 0:
            return False
        current_price = state.last_price
        if current_price is None or current_price <= 0:
            return False
        move_since_news = (current_price - state.last_news_price) / state.last_news_price
        return move_since_news <= self.settings.opening_impulse_news_max_move_since_event_pct

    def _session_open_price(self, state: SymbolState) -> float | None:
        for bar in state.bars:
            end = datetime.fromtimestamp(bar.end_ms / 1000, tz=self.market_tz)
            minutes_from_open = ((end.hour * 60 + end.minute) - (MARKET_OPEN.hour * 60 + MARKET_OPEN.minute))
            if minutes_from_open > 0 and bar.open > 0:
                return bar.open
        return None

    def _volume_collapsed(self, state: SymbolState) -> bool:
        if len(state.bars) < 2:
            return False
        latest_volume = state.bars[-1].volume
        baseline = median([bar.volume for bar in list(state.bars)[:-1] if bar.volume > 0] or [1])
        return latest_volume <= baseline * self.settings.opening_impulse_volume_collapse_ratio

    def _recent_failed_continuation(self, state: SymbolState) -> bool:
        bars = list(state.bars)
        if len(bars) < 5:
            return False
        window = bars[-5:-1]
        lower_highs = window[-2].high <= window[-3].high and window[-1].high <= window[-2].high
        lower_closes = window[-2].close <= window[-3].close and window[-1].close <= window[-2].close
        return lower_highs and lower_closes

    def _reentry_structure_reclaimed(self, state: SymbolState) -> bool:
        lookback = max(3, self.settings.opening_impulse_reentry_reclaim_lookback_bars)
        bars = list(state.bars)[-lookback:]
        if len(bars) < lookback:
            return False
        reclaim_level = max(bar.high for bar in bars[:-1])
        latest = bars[-1]
        if latest.close < reclaim_level:
            return False
        return self._volume_ratio(state) >= self.settings.opening_impulse_reentry_min_volume_ratio

    def _failed_continuation_no_high(self, position, event_ms: int, pnl_pct: float) -> bool:
        if position.max_price <= position.entry_price:
            return False
        age_since_high_seconds = (event_ms - position.last_high_ts) / 1000 if position.last_high_ts else 0
        if age_since_high_seconds < self.settings.opening_impulse_failed_continuation_no_high_seconds:
            return False
        return pnl_pct <= self.settings.opening_impulse_failed_continuation_max_mfe_pct

    def _failed_continuation_lower_highs(self, state: SymbolState, position) -> bool:
        bars = list(state.bars)[-3:]
        if len(bars) < 3:
            return False
        lower_highs = bars[-1].high <= bars[-2].high <= bars[-3].high
        if not lower_highs:
            return False
        mfe_pct = (position.max_price - position.entry_price) / position.entry_price if position.entry_price > 0 else 0.0
        return mfe_pct <= self.settings.opening_impulse_failed_continuation_max_mfe_pct

    @staticmethod
    def _volume_ratio(state: SymbolState) -> float:
        if len(state.bars) < 2:
            return 0.0
        latest_volume = state.bars[-1].volume
        baseline = median([bar.volume for bar in list(state.bars)[:-1] if bar.volume > 0] or [1])
        return latest_volume / baseline if baseline else 0.0
