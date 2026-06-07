from datetime import datetime, time
from statistics import median
from typing import Any, ClassVar

from candle import SymbolState
from config import Settings
from env_vars import EnvSpec, float_env, int_env
from market_hours import MARKET_TZ
from models import Bar, ExitDecision, Signal
from strategies.base import Strategy


MARKET_OPEN = time(9, 30)


class LiquidityScalperStrategy(Strategy):
    """Liquidity-first scalping based on intraday dollar flow and snapback structure."""

    name = "liquidity_scalper"
    requires_plan = False
    env_specs: ClassVar[tuple[EnvSpec, ...]] = (
        ("liquidity_scalper_start_minute", "LIQUIDITY_SCALPER_START_MINUTE", int_env, 0),
        ("liquidity_scalper_end_minute", "LIQUIDITY_SCALPER_END_MINUTE", int_env, 30),
        ("liquidity_scalper_afternoon_start_minute", "LIQUIDITY_SCALPER_AFTERNOON_START_MINUTE", int_env, 270),
        ("liquidity_scalper_afternoon_end_minute", "LIQUIDITY_SCALPER_AFTERNOON_END_MINUTE", int_env, 360),
        ("liquidity_scalper_min_bar_dollar_volume", "LIQUIDITY_SCALPER_MIN_BAR_DOLLAR_VOLUME", float_env, 3_000_000.0),
        ("liquidity_scalper_min_session_dollar_volume", "LIQUIDITY_SCALPER_MIN_SESSION_DOLLAR_VOLUME", float_env, 30_000_000.0),
        ("liquidity_scalper_min_volume_ratio", "LIQUIDITY_SCALPER_MIN_VOLUME_RATIO", float_env, 2.0),
        ("liquidity_scalper_min_range_pct", "LIQUIDITY_SCALPER_MIN_RANGE_PCT", float_env, 0.015),
        ("liquidity_scalper_flush_lookback_bars", "LIQUIDITY_SCALPER_FLUSH_LOOKBACK_BARS", int_env, 3),
        ("liquidity_scalper_flush_drop_pct", "LIQUIDITY_SCALPER_FLUSH_DROP_PCT", float_env, 0.025),
        ("liquidity_scalper_reclaim_pct", "LIQUIDITY_SCALPER_RECLAIM_PCT", float_env, 0.003),
        ("liquidity_scalper_breakout_lookback_bars", "LIQUIDITY_SCALPER_BREAKOUT_LOOKBACK_BARS", int_env, 5),
        ("liquidity_scalper_breakout_buffer_pct", "LIQUIDITY_SCALPER_BREAKOUT_BUFFER_PCT", float_env, 0.0005),
        ("liquidity_scalper_max_spread_bps", "LIQUIDITY_SCALPER_MAX_SPREAD_BPS", float_env, 12.0),
        ("liquidity_scalper_min_hold_seconds", "LIQUIDITY_SCALPER_MIN_HOLD_SECONDS", int_env, 2),
        ("liquidity_scalper_quick_profit_pct", "LIQUIDITY_SCALPER_QUICK_PROFIT_PCT", float_env, 0.004),
        ("liquidity_scalper_trailing_pullback_pct", "LIQUIDITY_SCALPER_TRAILING_PULLBACK_PCT", float_env, 0.0025),
        ("liquidity_scalper_stall_seconds", "LIQUIDITY_SCALPER_STALL_SECONDS", int_env, 20),
        ("liquidity_scalper_stall_loss_pct", "LIQUIDITY_SCALPER_STALL_LOSS_PCT", float_env, 0.0005),
        ("liquidity_scalper_stop_loss_pct", "LIQUIDITY_SCALPER_STOP_LOSS_PCT", float_env, 0.003),
        (
            "liquidity_scalper_max_trades_per_symbol_per_session",
            "LIQUIDITY_SCALPER_MAX_TRADES_PER_SYMBOL_PER_SESSION",
            int_env,
            6,
        ),
        ("liquidity_scalper_symbol_loss_lock_count", "LIQUIDITY_SCALPER_SYMBOL_LOSS_LOCK_COUNT", int_env, 2),
    )
    diagnostic_loggers: ClassVar[tuple[str, ...]] = ("strategies.liquidity_scalper",)

    @classmethod
    def runtime_settings_section(cls, settings: Any) -> dict[str, Any] | None:
        if cls.name not in settings.strategy_names:
            return None
        return {
            "start_minute": settings.liquidity_scalper_start_minute,
            "end_minute": settings.liquidity_scalper_end_minute,
            "afternoon_start_minute": settings.liquidity_scalper_afternoon_start_minute,
            "afternoon_end_minute": settings.liquidity_scalper_afternoon_end_minute,
            "min_bar_dollar_volume": settings.liquidity_scalper_min_bar_dollar_volume,
            "min_session_dollar_volume": settings.liquidity_scalper_min_session_dollar_volume,
            "min_volume_ratio": settings.liquidity_scalper_min_volume_ratio,
            "min_range_pct": settings.liquidity_scalper_min_range_pct,
            "flush_drop_pct": settings.liquidity_scalper_flush_drop_pct,
            "reclaim_pct": settings.liquidity_scalper_reclaim_pct,
            "max_spread_bps": settings.liquidity_scalper_max_spread_bps,
            "quick_profit_pct": settings.liquidity_scalper_quick_profit_pct,
            "trailing_pullback_pct": settings.liquidity_scalper_trailing_pullback_pct,
            "stall_seconds": settings.liquidity_scalper_stall_seconds,
            "stop_loss_pct": settings.liquidity_scalper_stop_loss_pct,
            "max_trades_per_symbol_per_session": settings.liquidity_scalper_max_trades_per_symbol_per_session,
            "symbol_loss_lock_count": settings.liquidity_scalper_symbol_loss_lock_count,
        }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_tz = MARKET_TZ

    def evaluate(self, state: SymbolState) -> Signal | None:
        if state.last_event_kind != "bar":
            return None
        if not self.is_symbol_allowed(state.symbol):
            return None
        if not self._within_entry_window(state.last_event_ms):
            return None

        bars = self._session_bars(state)
        if len(bars) < max(4, self.settings.liquidity_scalper_breakout_lookback_bars + 1):
            return None

        last = bars[-1]
        quote = state.quote
        spread_bps = quote.spread_bps if quote is not None else None
        if spread_bps is not None and spread_bps > self.settings.liquidity_scalper_max_spread_bps:
            return None

        bar_dollar_volume = last.close * last.volume
        if bar_dollar_volume < self.settings.liquidity_scalper_min_bar_dollar_volume:
            return None
        session_dollar_volume = sum(bar.close * bar.volume for bar in bars)
        if session_dollar_volume < self.settings.liquidity_scalper_min_session_dollar_volume:
            return None

        session_range_pct = self._range_pct(bars)
        if session_range_pct < self.settings.liquidity_scalper_min_range_pct:
            return None

        volume_ratio = self._volume_ratio(bars)
        if volume_ratio < self.settings.liquidity_scalper_min_volume_ratio:
            return None

        setup = self._flush_reclaim_setup(bars) or self._liquidity_breakout_setup(bars)
        if setup is None:
            return None

        entry_price = quote.ask if quote is not None and quote.ask > 0 else last.close
        stop_price = entry_price * (1.0 - self.settings.liquidity_scalper_stop_loss_pct)
        reason = (
            f"{setup}; bar_dv ${bar_dollar_volume:,.0f}, session_dv ${session_dollar_volume:,.0f}, "
            f"range {session_range_pct:.2%}, volume {volume_ratio:.1f}x"
        )
        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=entry_price,
            timestamp_ms=last.end_ms,
            change_pct=(last.close - bars[-2].close) / bars[-2].close if bars[-2].close > 0 else 0.0,
            volume_ratio=volume_ratio,
            spread_bps=spread_bps,
            reason=reason,
            stop_price=stop_price,
        )

    def should_exit(self, state: SymbolState, position) -> ExitDecision | None:
        if state.last_event_kind not in {"quote", "bar"} or position.strategy != self.name:
            return None

        price = state.last_price
        if price is None or position.entry_price <= 0:
            return None

        event_ms = state.last_event_ms or position.entry_ms
        age_seconds = (event_ms - position.entry_ms) / 1000
        pnl_pct = (price - position.entry_price) / position.entry_price

        if pnl_pct <= -self.settings.liquidity_scalper_stop_loss_pct:
            return ExitDecision("scalper stop loss")
        if age_seconds < self.settings.liquidity_scalper_min_hold_seconds:
            return None

        if pnl_pct <= -self.settings.liquidity_scalper_stall_loss_pct:
            return ExitDecision("not immediately working")

        if pnl_pct >= self.settings.liquidity_scalper_quick_profit_pct:
            return ExitDecision("quick scalp profit")

        if position.max_price > position.entry_price:
            pullback_pct = (position.max_price - price) / position.max_price
            if pullback_pct >= self.settings.liquidity_scalper_trailing_pullback_pct:
                return ExitDecision("scalp pullback")

        if age_seconds >= self.settings.liquidity_scalper_stall_seconds and pnl_pct <= 0:
            return ExitDecision("scalp stall")

        return None

    def exit_activation_delay_seconds(self, position) -> int:
        return 0

    def use_fixed_target_exit(self, position) -> bool:
        return False

    def _flush_reclaim_setup(self, bars: list[Bar]) -> str | None:
        lookback = max(2, self.settings.liquidity_scalper_flush_lookback_bars)
        if len(bars) < lookback + 1:
            return None
        recent = bars[-(lookback + 1) :]
        start_high = max(bar.high for bar in recent[:-1])
        flush_low = min(bar.low for bar in recent)
        if start_high <= 0:
            return None
        drop_pct = (start_high - flush_low) / start_high
        if drop_pct < self.settings.liquidity_scalper_flush_drop_pct:
            return None
        reclaim_pct = (recent[-1].close - flush_low) / flush_low if flush_low > 0 else 0.0
        if reclaim_pct < self.settings.liquidity_scalper_reclaim_pct:
            return None
        if recent[-1].close <= recent[-1].open:
            return None
        return f"flush_reclaim drop {drop_pct:.2%}, reclaim {reclaim_pct:.2%}"

    def _liquidity_breakout_setup(self, bars: list[Bar]) -> str | None:
        lookback = max(2, self.settings.liquidity_scalper_breakout_lookback_bars)
        if len(bars) < lookback + 1:
            return None
        prior = bars[-(lookback + 1) : -1]
        recent_high = max(bar.high for bar in prior)
        breakout_level = recent_high * (1.0 + self.settings.liquidity_scalper_breakout_buffer_pct)
        last = bars[-1]
        if last.close < breakout_level:
            return None
        if last.close <= last.open:
            return None
        return f"liquidity_breakout above {recent_high:.2f}"

    def _session_bars(self, state: SymbolState) -> list[Bar]:
        if not state.last_event_ms:
            return []
        current = datetime.fromtimestamp(state.last_event_ms / 1000, tz=self.market_tz)
        out = []
        for bar in state.bars:
            start = datetime.fromtimestamp(bar.start_ms / 1000, tz=self.market_tz)
            if start.date() == current.date() and start.time() >= MARKET_OPEN:
                out.append(bar)
        return out

    def _within_entry_window(self, timestamp_ms: int | None) -> bool:
        if timestamp_ms is None:
            return False
        current = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.market_tz)
        minutes = current.hour * 60 + current.minute
        market_open = MARKET_OPEN.hour * 60 + MARKET_OPEN.minute
        elapsed = minutes - market_open
        morning = self.settings.liquidity_scalper_start_minute <= elapsed <= self.settings.liquidity_scalper_end_minute
        afternoon = (
            self.settings.liquidity_scalper_afternoon_start_minute
            <= elapsed
            <= self.settings.liquidity_scalper_afternoon_end_minute
        )
        return morning or afternoon

    @staticmethod
    def _volume_ratio(bars: list[Bar]) -> float:
        if len(bars) < 2:
            return 0.0
        baseline = median([bar.volume for bar in bars[:-1] if bar.volume > 0] or [0.0])
        return bars[-1].volume / baseline if baseline > 0 else 0.0

    @staticmethod
    def _range_pct(bars: list[Bar]) -> float:
        if not bars:
            return 0.0
        high = max(bar.high for bar in bars)
        low = min(bar.low for bar in bars)
        return (high - low) / bars[-1].close if bars[-1].close > 0 else 0.0
