from dataclasses import dataclass, field
from datetime import datetime, timezone

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
    pause_until_ms: int = 0
    stopped_day_keys: set[str] = field(default_factory=set)
    daily_realized_pnl: dict[str, float] = field(default_factory=dict)
    session_trade_counts: dict[tuple[str, str, str], int] = field(default_factory=dict)
    session_symbol_loss_streaks: dict[tuple[str, str, str], int] = field(default_factory=dict)
    session_symbol_locks: set[tuple[str, str, str]] = field(default_factory=set)

    def check_entry(self, signal: Signal, open_symbols: set[str], total_pnl: float) -> RiskDecision:
        if self.settings.regular_market_only and not is_regular_market_time(signal.timestamp_ms):
            return RiskDecision(False, "outside regular market hours")

        if should_flatten_before_close(signal.timestamp_ms, self.settings.flatten_before_close_minutes):
            return RiskDecision(False, "close flatten window active")

        day_key = self._day_key(signal.timestamp_ms)
        if day_key in self.stopped_day_keys:
            return RiskDecision(False, "consecutive loss day stop active")

        if signal.timestamp_ms < self.pause_until_ms:
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
                self.settings.trade_cooldown_seconds,
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
        return getattr(self.settings, f"{strategy}_reentry_cooldown_seconds", 0)

    def record_trade(self, symbol: str, timestamp_ms: int, strategy: str = "") -> None:
        self.last_trade_ms[symbol] = timestamp_ms
        self.last_failed_entry_ms.pop(symbol, None)
        if strategy:
            self.last_trade_by_strategy_ms[(symbol, strategy)] = timestamp_ms
            session_key = (self._day_key(timestamp_ms), strategy, symbol)
            self.session_trade_counts[session_key] = self.session_trade_counts.get(session_key, 0) + 1

    def record_failed_entry(self, symbol: str, timestamp_ms: int) -> None:
        self.last_failed_entry_ms[symbol] = timestamp_ms

    def record_exit(self, pnl: float, timestamp_ms: int, symbol: str | None = None, strategy: str | None = None) -> None:
        day_key = self._day_key(timestamp_ms)
        self.daily_realized_pnl[day_key] = self.daily_realized_pnl.get(day_key, 0.0) + pnl
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.settings.consecutive_loss_stop_count:
                self.stopped_day_keys.add(day_key)
            if self.consecutive_losses >= self.settings.consecutive_loss_pause_count:
                self.pause_until_ms = timestamp_ms + self.settings.consecutive_loss_pause_minutes * 60_000
            self._record_symbol_loss(day_key, symbol, strategy)
        elif pnl > 0:
            self.consecutive_losses = 0
            self._reset_symbol_loss(day_key, symbol, strategy)

    def _max_trades_per_symbol_for_strategy(self, strategy: str) -> int:
        return int(getattr(self.settings, f"{strategy}_max_trades_per_symbol_per_session", 0) or 0)

    def _record_symbol_loss(self, day_key: str, symbol: str | None, strategy: str | None) -> None:
        if not symbol or not strategy:
            return
        key = (day_key, strategy, symbol)
        streak = self.session_symbol_loss_streaks.get(key, 0) + 1
        self.session_symbol_loss_streaks[key] = streak
        lock_count = int(getattr(self.settings, f"{strategy}_symbol_loss_lock_count", 0) or 0)
        if lock_count > 0 and streak >= lock_count:
            self.session_symbol_locks.add(key)

    def _reset_symbol_loss(self, day_key: str, symbol: str | None, strategy: str | None) -> None:
        if not symbol or not strategy:
            return
        key = (day_key, strategy, symbol)
        self.session_symbol_loss_streaks.pop(key, None)

    def _daily_loss_limit(self) -> float:
        percent_limit = self.settings.starting_cash * self.settings.daily_max_loss_pct
        return min(self.settings.daily_max_loss, percent_limit)

    @staticmethod
    def _day_key(timestamp_ms: int) -> str:
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).date().isoformat()
