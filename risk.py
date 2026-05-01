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
    daily_realized_pnl: dict[str, float] = field(default_factory=dict)

    def check_entry(self, signal: Signal, open_symbols: set[str], total_pnl: float) -> RiskDecision:
        if self.settings.regular_market_only and not is_regular_market_time(signal.timestamp_ms):
            return RiskDecision(False, "outside regular market hours")

        if should_flatten_before_close(signal.timestamp_ms, self.settings.flatten_before_close_minutes):
            return RiskDecision(False, "close flatten window active")

        if signal.timestamp_ms < self.pause_until_ms:
            return RiskDecision(False, "consecutive loss pause active")

        daily_limit = self._daily_loss_limit()
        daily_pnl = self.daily_realized_pnl.get(self._day_key(signal.timestamp_ms), 0.0)
        if total_pnl <= -daily_limit or daily_pnl <= -daily_limit:
            return RiskDecision(False, "daily loss limit reached")

        if signal.side != "BUY":
            return RiskDecision(False, "short entries are disabled in paper mode")

        if signal.symbol in open_symbols:
            return RiskDecision(False, "position already open")

        if len(open_symbols) >= self.settings.max_open_positions:
            return RiskDecision(False, "max open positions reached")

        opening_impulse_ms = self.last_trade_by_strategy_ms.get((signal.symbol, "opening_impulse"))
        if signal.strategy == "maha7_pullback_reclaim" and opening_impulse_ms is not None:
            elapsed = (signal.timestamp_ms - opening_impulse_ms) / 1000
            cooldown_seconds = self.settings.maha7_pullback_reclaim_min_minutes_after_opening_impulse * 60
            if elapsed < cooldown_seconds:
                return RiskDecision(False, "opening impulse cooldown active")

        last_ms = self.last_trade_ms.get(signal.symbol)
        if last_ms is not None:
            elapsed = (signal.timestamp_ms - last_ms) / 1000
            cooldown_seconds = max(
                self.settings.trade_cooldown_seconds,
                getattr(self.settings, f"{signal.strategy}_reentry_cooldown_seconds", 0),
            )
            if elapsed < cooldown_seconds:
                return RiskDecision(False, "symbol cooldown active")

        failed_entry_ms = self.last_failed_entry_ms.get(signal.symbol)
        if failed_entry_ms is not None and self.settings.failed_entry_cooldown_seconds > 0:
            elapsed = (signal.timestamp_ms - failed_entry_ms) / 1000
            if elapsed < self.settings.failed_entry_cooldown_seconds:
                return RiskDecision(False, "failed entry cooldown active")

        return RiskDecision(True, "accepted")

    def record_trade(self, symbol: str, timestamp_ms: int, strategy: str = "") -> None:
        self.last_trade_ms[symbol] = timestamp_ms
        self.last_failed_entry_ms.pop(symbol, None)
        if strategy:
            self.last_trade_by_strategy_ms[(symbol, strategy)] = timestamp_ms

    def record_failed_entry(self, symbol: str, timestamp_ms: int) -> None:
        self.last_failed_entry_ms[symbol] = timestamp_ms

    def record_exit(self, pnl: float, timestamp_ms: int) -> None:
        day_key = self._day_key(timestamp_ms)
        self.daily_realized_pnl[day_key] = self.daily_realized_pnl.get(day_key, 0.0) + pnl
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.settings.consecutive_loss_pause_count:
                self.pause_until_ms = timestamp_ms + self.settings.consecutive_loss_pause_minutes * 60_000
        elif pnl > 0:
            self.consecutive_losses = 0

    def _daily_loss_limit(self) -> float:
        percent_limit = self.settings.starting_cash * self.settings.daily_max_loss_pct
        return min(self.settings.daily_max_loss, percent_limit)

    @staticmethod
    def _day_key(timestamp_ms: int) -> str:
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).date().isoformat()
