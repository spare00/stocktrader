from __future__ import annotations

from datetime import datetime, time
from statistics import median
from typing import Any, ClassVar

from candle import SymbolState
from config import Settings
from env_vars import EnvSpec, bool_env, float_env, int_env
from market_hours import MARKET_TZ
from models import ExitDecision, Signal
from scripts.select_gap_and_go import latest_valid_quote
from strategies.base import Strategy


MARKET_OPEN = time(9, 30)


class NewsImpulseStrategy(Strategy):
    name = "news_impulse"
    selector_command: ClassVar[str] = ".venv/bin/python scripts/select_news_impulse.py --top 12"
    env_specs: ClassVar[tuple[EnvSpec, ...]] = (
        ("news_impulse_enabled", "NEWS_IMPULSE_ENABLED", bool_env, True),
        ("news_impulse_start_minute", "NEWS_IMPULSE_START_MINUTE", int_env, 0),
        ("news_impulse_end_minute", "NEWS_IMPULSE_END_MINUTE", int_env, 120),
        ("news_impulse_change_pct", "NEWS_IMPULSE_CHANGE_PCT", float_env, 0.003),
        ("news_impulse_min_volume_ratio", "NEWS_IMPULSE_MIN_VOLUME_RATIO", float_env, 1.5),
        (
            "news_impulse_max_move_since_event_pct",
            "NEWS_IMPULSE_MAX_MOVE_SINCE_EVENT_PCT",
            float_env,
            0.015,
        ),
        ("news_impulse_max_hold_seconds", "NEWS_IMPULSE_MAX_HOLD_SECONDS", int_env, 60),
        ("news_impulse_trailing_pullback_pct", "NEWS_IMPULSE_TRAILING_PULLBACK_PCT", float_env, 0.003),
        ("news_impulse_stop_loss_pct", "NEWS_IMPULSE_STOP_LOSS_PCT", float_env, 0.004),
        ("news_impulse_position_size_multiplier", "NEWS_IMPULSE_POSITION_SIZE_MULTIPLIER", float_env, 0.5),
    )
    diagnostic_loggers: ClassVar[tuple[str, ...]] = ("strategies.news_impulse",)

    @classmethod
    def runtime_settings_section(cls, settings: Any) -> dict[str, Any] | None:
        if cls.name not in settings.strategy_names:
            return None
        return {
            "enabled": settings.news_impulse_enabled,
            "start_minute": settings.news_impulse_start_minute,
            "end_minute": settings.news_impulse_end_minute,
            "change_pct": settings.news_impulse_change_pct,
            "min_volume_ratio": settings.news_impulse_min_volume_ratio,
            "max_move_since_event_pct": settings.news_impulse_max_move_since_event_pct,
            "max_hold_seconds": settings.news_impulse_max_hold_seconds,
            "trailing_pullback_pct": settings.news_impulse_trailing_pullback_pct,
            "stop_loss_pct": settings.news_impulse_stop_loss_pct,
            "position_size_multiplier": settings.news_impulse_position_size_multiplier,
        }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_tz = MARKET_TZ

    def evaluate(self, state: SymbolState) -> Signal | None:
        if not self.settings.news_impulse_enabled:
            return None
        if state.last_event_kind not in {"quote", "bar"}:
            return None
        if not self._within_entry_window(state.last_event_ms):
            return None
        if not state.is_high_impact_news:
            return None
        if not self._has_recent_news(state):
            return None
        now_ms = state.last_event_ms
        if now_ms is None or (now_ms - state.last_news_ms) > 60_000:
            return None

        last = latest_valid_quote(state)
        if last is None:
            return None

        move_since_news = (last.ask - state.last_news_price) / state.last_news_price
        if move_since_news > self.settings.news_impulse_max_move_since_event_pct:
            return None

        momentum = self._short_term_change_pct(state)
        if momentum < self.settings.news_impulse_change_pct:
            return None

        volume_ratio = self._volume_ratio(state)
        if volume_ratio < self.settings.news_impulse_min_volume_ratio:
            return None

        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=last.ask,
            timestamp_ms=last.timestamp_ms,
            change_pct=momentum,
            volume_ratio=volume_ratio,
            spread_bps=last.spread_bps,
            reason="news impulse early entry",
            stop_price=last.ask * (1 - self.settings.news_impulse_stop_loss_pct),
            position_size_multiplier=self.settings.news_impulse_position_size_multiplier,
        )

    def should_exit(self, state: SymbolState, position) -> ExitDecision | None:
        if position.strategy != self.name:
            return None

        price = state.last_price
        if price is None:
            return None

        now_ms = state.last_event_ms or (state.quote.timestamp_ms if state.quote else position.entry_ms)
        age_seconds = (now_ms - position.entry_ms) / 1000
        if age_seconds > self.settings.news_impulse_max_hold_seconds:
            return ExitDecision("news max hold")

        if position.max_price > 0:
            pullback_pct = (position.max_price - price) / position.max_price
            if pullback_pct >= self.settings.news_impulse_trailing_pullback_pct:
                return ExitDecision("news trailing stop")

        if position.entry_price > 0:
            loss_pct = (price - position.entry_price) / position.entry_price
            if loss_pct <= -self.settings.news_impulse_stop_loss_pct:
                return ExitDecision("news stop loss")

        return None

    def use_fixed_target_exit(self, position) -> bool:
        return False

    def _within_entry_window(self, timestamp_ms: int | None) -> bool:
        if timestamp_ms is None:
            return False
        current = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.market_tz)
        minutes = current.hour * 60 + current.minute
        market_open_minutes = (MARKET_OPEN.hour * 60) + MARKET_OPEN.minute
        elapsed = minutes - market_open_minutes
        return self.settings.news_impulse_start_minute <= elapsed <= self.settings.news_impulse_end_minute

    @staticmethod
    def _short_term_change_pct(state: SymbolState, lookback_quotes: int = 6) -> float:
        quotes = list(state.quotes)
        if len(quotes) < 2:
            return 0.0
        start = quotes[-min(len(quotes), lookback_quotes)]
        end = quotes[-1]
        if start.mid <= 0:
            return 0.0
        return (end.mid - start.mid) / start.mid

    @staticmethod
    def _volume_ratio(state: SymbolState) -> float:
        if len(state.bars) < 2:
            return 0.0
        latest_volume = state.bars[-1].volume
        baseline = median([bar.volume for bar in list(state.bars)[:-1] if bar.volume > 0] or [1])
        return latest_volume / baseline if baseline else 0.0

    @staticmethod
    def _has_recent_news(state: SymbolState) -> bool:
        return bool(state.last_news_ms is not None and state.last_news_price is not None and state.last_news_price > 0)
