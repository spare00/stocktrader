from dataclasses import dataclass, field

from config import Settings
from models import Signal


@dataclass
class RiskDecision:
    allowed: bool
    reason: str


@dataclass
class RiskManager:
    settings: Settings
    last_trade_ms: dict[str, int] = field(default_factory=dict)

    def check_entry(self, signal: Signal, open_symbols: set[str], realized_pnl: float) -> RiskDecision:
        if realized_pnl <= -self.settings.daily_max_loss:
            return RiskDecision(False, "daily loss limit reached")

        if signal.side != "BUY":
            return RiskDecision(False, "short entries are disabled in paper mode")

        if signal.symbol in open_symbols:
            return RiskDecision(False, "position already open")

        if len(open_symbols) >= self.settings.max_open_positions:
            return RiskDecision(False, "max open positions reached")

        last_ms = self.last_trade_ms.get(signal.symbol)
        if last_ms is not None:
            elapsed = (signal.timestamp_ms - last_ms) / 1000
            if elapsed < self.settings.trade_cooldown_seconds:
                return RiskDecision(False, "symbol cooldown active")

        return RiskDecision(True, "accepted")

    def record_trade(self, symbol: str, timestamp_ms: int) -> None:
        self.last_trade_ms[symbol] = timestamp_ms
