from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import ceil

from config import Settings
from market_hours import is_regular_market_time, should_flatten_before_close
from models import Signal


@dataclass
class RiskDecision:
    allowed: bool
    reason: str


@dataclass
class RiskManager:
    settings: Settings
    last_trade_ms: dict[str, int] = field(default_factory=dict)
    last_trade_by_strategy_ms: dict[tuple[str, str], int] = field(default_factory=dict)
    last_failed_entry_ms: dict[str, int] = field(default_factory=dict)
    consecutive_losses: int = 0
    consecutive_losses_by_strategy: dict[str, int] = field(default_factory=dict)
    pause_until_by_strategy_ms: dict[str, int] = field(default_factory=dict)
    stopped_strategy_day_keys: set[tuple[str, str]] = field(default_factory=set)
    daily_realized_pnl: dict[str, float] = field(default_factory=dict)
    session_trade_counts: dict[tuple[str, str, str], int] = field(default_factory=dict)
    strategy_trade_timestamps: dict[tuple[str, str], list[int]] = field(default_factory=dict)
    session_symbol_loss_streaks: dict[tuple[str, str, str], int] = field(default_factory=dict)
    session_symbol_locks: set[tuple[str, str, str]] = field(default_factory=set)

    def check_entry(
        self,
        signal: Signal,
        open_symbols: set[str],
        total_pnl: float,
        open_strategy_counts: dict[str, int] | None = None,
    ) -> RiskDecision:
        if self.settings.regular_market_only and not is_regular_market_time(signal.timestamp_ms):
            return RiskDecision(False, "outside regular market hours")

        if should_flatten_before_close(signal.timestamp_ms, self.settings.flatten_before_close_minutes):
            return RiskDecision(False, "close flatten window active")

        day_key = self._day_key(signal.timestamp_ms)
        if self._respects_consecutive_loss_limits(signal.strategy):
            strategy_key = self._strategy_key(signal.strategy)
            if (day_key, strategy_key) in self.stopped_strategy_day_keys:
                return RiskDecision(False, "consecutive loss day stop active")

            pause_until_ms = self.pause_until_by_strategy_ms.get(strategy_key, 0)
            if signal.timestamp_ms < pause_until_ms:
                return RiskDecision(False, "consecutive loss pause active")

        daily_limit = self._daily_loss_limit()
        daily_pnl = self.daily_realized_pnl.get(day_key, 0.0)
        if total_pnl <= -daily_limit or daily_pnl <= -daily_limit:
            return RiskDecision(False, "daily loss limit reached")

        if signal.side != "BUY":
            return RiskDecision(False, "short entries are disabled in paper mode")

        if signal.symbol in open_symbols:
            return RiskDecision(False, "position already open")

        if len(open_symbols) >= self.settings.max_open_positions:
            return RiskDecision(False, "max open positions reached")

        strategy_max_open = self._max_open_positions_for_strategy(signal.strategy)
        if strategy_max_open > 0:
            strategy_open = (open_strategy_counts or {}).get(signal.strategy, 0)
            if strategy_open >= strategy_max_open:
                return RiskDecision(False, "max open positions for strategy reached")

        if self._strategy_burst_limit_reached(signal.strategy, day_key, signal.timestamp_ms):
            return RiskDecision(False, "strategy burst limit reached")

        opening_impulse_ms = self.last_trade_by_strategy_ms.get((signal.symbol, "opening_impulse"))
        if signal.strategy == "maha7" and opening_impulse_ms is not None:
            elapsed = (signal.timestamp_ms - opening_impulse_ms) / 1000
            cooldown_seconds = self.settings.maha7_min_minutes_after_opening_impulse * 60
            if elapsed < cooldown_seconds:
                return RiskDecision(False, "opening impulse cooldown active")

        lock_key = (day_key, signal.strategy, signal.symbol)
        if lock_key in self.session_symbol_locks:
            return RiskDecision(False, "symbol session loss lock active")

        max_symbol_trades = self._max_trades_per_symbol_for_strategy(signal.strategy)
        if max_symbol_trades > 0:
            session_key = (day_key, signal.strategy, signal.symbol)
            trade_count = self.session_trade_counts.get(session_key, 0)
            if trade_count >= max_symbol_trades:
                return RiskDecision(False, "max trades per symbol per session reached")

        last_ms = self.last_trade_ms.get(signal.symbol)
        if last_ms is not None:
            elapsed = (signal.timestamp_ms - last_ms) / 1000
            cooldown_seconds = max(
                self._trade_cooldown_seconds_for_strategy(signal.strategy),
                self._reentry_cooldown_seconds_for_strategy(signal.strategy),
            )
            if elapsed < cooldown_seconds:
                return RiskDecision(False, "symbol cooldown active")

        failed_entry_ms = self.last_failed_entry_ms.get(signal.symbol)
        if failed_entry_ms is not None and self.settings.failed_entry_cooldown_seconds > 0:
            elapsed = (signal.timestamp_ms - failed_entry_ms) / 1000
            if elapsed < self.settings.failed_entry_cooldown_seconds:
                return RiskDecision(False, "failed entry cooldown active")

        return RiskDecision(True, "accepted")

    def _reentry_cooldown_seconds_for_strategy(self, strategy: str) -> int:
        if strategy == "maha7":
            return self.settings.maha7_reentry_cooldown_seconds
        return getattr(self.settings, f"{self._settings_prefix(strategy)}_reentry_cooldown_seconds", 0)

    def _max_open_positions_for_strategy(self, strategy: str) -> int:
        return int(getattr(self.settings, f"{self._compact_settings_prefix(strategy)}_max_open_positions", 0) or 0)

    def _trade_cooldown_seconds_for_strategy(self, strategy: str) -> int:
        cooldown = int(getattr(self.settings, f"{self._compact_settings_prefix(strategy)}_trade_cooldown_seconds", 0) or 0)
        return cooldown if cooldown > 0 else self.settings.trade_cooldown_seconds

    def _strategy_burst_limit_reached(self, strategy: str, day_key: str, timestamp_ms: int) -> bool:
        max_entries = self._burst_max_entries_for_strategy(strategy)
        window_seconds = self._burst_window_seconds_for_strategy(strategy)
        if max_entries <= 0 or window_seconds <= 0:
            return False
        cutoff_ms = timestamp_ms - window_seconds * 1000
        day_strategy_key = (day_key, strategy)
        recent = [
            trade_ms
            for trade_ms in self.strategy_trade_timestamps.get(day_strategy_key, [])
            if trade_ms >= cutoff_ms
        ]
        self.strategy_trade_timestamps[day_strategy_key] = recent
        return len(recent) >= max_entries

    def _burst_max_entries_for_strategy(self, strategy: str) -> int:
        return int(getattr(self.settings, f"{self._compact_settings_prefix(strategy)}_burst_max_entries", 0) or 0)

    def _burst_window_seconds_for_strategy(self, strategy: str) -> int:
        return int(getattr(self.settings, f"{self._compact_settings_prefix(strategy)}_burst_window_seconds", 0) or 0)

    def _respects_consecutive_loss_limits(self, strategy: str) -> bool:
        return bool(getattr(self.settings, f"{self._settings_prefix(strategy)}_respect_consecutive_loss_limits", True))

    @staticmethod
    def _strategy_key(strategy: str | None) -> str:
        return strategy or "default"

    def record_trade(self, symbol: str, timestamp_ms: int, strategy: str = "") -> None:
        self.last_trade_ms[symbol] = timestamp_ms
        self.last_failed_entry_ms.pop(symbol, None)
        if strategy:
            self.last_trade_by_strategy_ms[(symbol, strategy)] = timestamp_ms
            session_key = (self._day_key(timestamp_ms), strategy, symbol)
            self.session_trade_counts[session_key] = self.session_trade_counts.get(session_key, 0) + 1
            day_strategy_key = (self._day_key(timestamp_ms), strategy)
            window_seconds = self._burst_window_seconds_for_strategy(strategy)
            cutoff_ms = timestamp_ms - max(0, window_seconds) * 1000
            timestamps = [
                trade_ms
                for trade_ms in self.strategy_trade_timestamps.get(day_strategy_key, [])
                if window_seconds <= 0 or trade_ms >= cutoff_ms
            ]
            timestamps.append(timestamp_ms)
            self.strategy_trade_timestamps[day_strategy_key] = timestamps

    def record_failed_entry(self, symbol: str, timestamp_ms: int) -> None:
        self.last_failed_entry_ms[symbol] = timestamp_ms

    def record_exit(self, pnl: float, timestamp_ms: int, symbol: str | None = None, strategy: str | None = None) -> None:
        day_key = self._day_key(timestamp_ms)
        strategy_key = self._strategy_key(strategy)
        self.daily_realized_pnl[day_key] = self.daily_realized_pnl.get(day_key, 0.0) + pnl
        if pnl < 0:
            self.consecutive_losses += 1
            strategy_losses = self.consecutive_losses_by_strategy.get(strategy_key, 0) + 1
            self.consecutive_losses_by_strategy[strategy_key] = strategy_losses
            if strategy_losses >= self._consecutive_loss_stop_count_for_strategy(strategy_key):
                self.stopped_strategy_day_keys.add((day_key, strategy_key))
            if strategy_losses >= self._consecutive_loss_pause_count_for_strategy(strategy_key):
                self.pause_until_by_strategy_ms[strategy_key] = (
                    timestamp_ms + self._consecutive_loss_pause_minutes_for_strategy(strategy_key) * 60_000
                )
            self._record_symbol_loss(day_key, symbol, strategy)
        elif pnl > 0:
            self.consecutive_losses = 0
            self.consecutive_losses_by_strategy[strategy_key] = 0
            self.pause_until_by_strategy_ms.pop(strategy_key, None)
            self._reset_symbol_loss(day_key, symbol, strategy)

    def _max_trades_per_symbol_for_strategy(self, strategy: str) -> int:
        return int(getattr(self.settings, f"{self._settings_prefix(strategy)}_max_trades_per_symbol_per_session", 0) or 0)

    def _record_symbol_loss(self, day_key: str, symbol: str | None, strategy: str | None) -> None:
        if not symbol or not strategy:
            return
        key = (day_key, strategy, symbol)
        streak = self.session_symbol_loss_streaks.get(key, 0) + 1
        self.session_symbol_loss_streaks[key] = streak
        lock_count = int(getattr(self.settings, f"{self._settings_prefix(strategy)}_symbol_loss_lock_count", 0) or 0)
        if lock_count > 0 and streak >= lock_count:
            self.session_symbol_locks.add(key)

    def _reset_symbol_loss(self, day_key: str, symbol: str | None, strategy: str | None) -> None:
        if not symbol or not strategy:
            return
        key = (day_key, strategy, symbol)
        self.session_symbol_loss_streaks.pop(key, None)

    def _consecutive_loss_pause_count_for_strategy(self, strategy: str) -> int:
        value = self._strategy_or_global_int(strategy, "consecutive_loss_pause_count")
        return value if value is not None else self._auto_consecutive_loss_pause_count()

    def _consecutive_loss_pause_minutes_for_strategy(self, strategy: str) -> int:
        return self._strategy_int_override(
            strategy,
            "consecutive_loss_pause_minutes",
            self.settings.consecutive_loss_pause_minutes,
        )

    def _consecutive_loss_stop_count_for_strategy(self, strategy: str) -> int:
        value = self._strategy_or_global_int(strategy, "consecutive_loss_stop_count")
        return value if value is not None else self._auto_consecutive_loss_stop_count()

    def _strategy_int_override(self, strategy: str, suffix: str, default: int) -> int:
        value = getattr(self.settings, f"{self._compact_settings_prefix(strategy)}_{suffix}", None)
        return default if value is None else int(value)

    def _strategy_or_global_int(self, strategy: str, suffix: str) -> int | None:
        value = getattr(self.settings, f"{self._compact_settings_prefix(strategy)}_{suffix}", None)
        if value is not None:
            return int(value)
        value = getattr(self.settings, suffix, None)
        return None if value is None else int(value)

    def consecutive_loss_limits_snapshot(self, strategy: str) -> dict[str, int | str]:
        pause_source = self._consecutive_loss_count_source(strategy, "consecutive_loss_pause_count")
        stop_source = self._consecutive_loss_count_source(strategy, "consecutive_loss_stop_count")
        return {
            "pause_count": self._consecutive_loss_pause_count_for_strategy(strategy),
            "pause_count_source": pause_source,
            "pause_minutes": self._consecutive_loss_pause_minutes_for_strategy(strategy),
            "stop_count": self._consecutive_loss_stop_count_for_strategy(strategy),
            "stop_count_source": stop_source,
            "symbol_count": self._symbol_count_for_auto_limits(),
        }

    def _consecutive_loss_count_source(self, strategy: str, suffix: str) -> str:
        if getattr(self.settings, f"{self._compact_settings_prefix(strategy)}_{suffix}", None) is not None:
            return "strategy"
        if getattr(self.settings, suffix, None) is not None:
            return "global"
        return "auto_symbols"

    def _auto_consecutive_loss_pause_count(self) -> int:
        return max(4, ceil(self._symbol_count_for_auto_limits() * 0.25))

    def _auto_consecutive_loss_stop_count(self) -> int:
        return max(8, ceil(self._symbol_count_for_auto_limits() * 0.50))

    def _symbol_count_for_auto_limits(self) -> int:
        return max(1, len(self.settings.symbols))

    def _daily_loss_limit(self) -> float:
        percent_limit = self.settings.starting_cash * self.settings.daily_max_loss_pct
        return min(self.settings.daily_max_loss, percent_limit)

    @staticmethod
    def _settings_prefix(strategy: str) -> str:
        if strategy == "stoch_macd_reversal":
            return "stoch_macd"
        return strategy

    @staticmethod
    def _compact_settings_prefix(strategy: str) -> str:
        if strategy == "stoch_macd_reversal":
            return "stoch_macd"
        if strategy == "macd_early_impulse":
            return "macd"
        return strategy

    @staticmethod
    def _day_key(timestamp_ms: int) -> str:
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).date().isoformat()
