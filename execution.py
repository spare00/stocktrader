import logging
from dataclasses import dataclass, field

from config import Settings
from models import Bar, Signal


LOG = logging.getLogger(__name__)


@dataclass
class Position:
    symbol: str
    shares: int
    entry_price: float
    entry_ms: int
    target_price: float
    stop_price: float


@dataclass
class Fill:
    symbol: str
    side: str
    shares: int
    price: float
    timestamp_ms: int
    pnl: float = 0.0
    reason: str = ""


@dataclass
class PaperBroker:
    settings: Settings
    cash: float = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    realized_pnl: float = 0.0

    def __post_init__(self) -> None:
        self.cash = self.settings.starting_cash

    def open_symbols(self) -> set[str]:
        return set(self.positions)

    def buy(self, signal: Signal) -> Fill | None:
        if signal.symbol in self.positions:
            return None

        budget = min(self.settings.max_position_value, self.cash)
        shares = int(budget // signal.price)
        if shares <= 0:
            LOG.info("Skipping %s: not enough cash for one share at %.2f", signal.symbol, signal.price)
            return None

        cost = shares * signal.price
        self.cash -= cost
        self.positions[signal.symbol] = Position(
            symbol=signal.symbol,
            shares=shares,
            entry_price=signal.price,
            entry_ms=signal.timestamp_ms,
            target_price=signal.price * (1 + self.settings.target_profit_pct),
            stop_price=signal.price * (1 - self.settings.stop_loss_pct),
        )
        fill = Fill(signal.symbol, "BUY", shares, signal.price, signal.timestamp_ms, reason=signal.reason)
        self.fills.append(fill)
        LOG.info("PAPER BUY %s %s @ %.2f | %s", shares, signal.symbol, signal.price, signal.reason)
        return fill

    def manage_exit(self, bar: Bar) -> Fill | None:
        position = self.positions.get(bar.symbol)
        if not position:
            return None

        age_seconds = (bar.end_ms - position.entry_ms) / 1000
        reason = ""
        exit_price = bar.close

        if bar.high >= position.target_price:
            reason = "target profit"
            exit_price = position.target_price
        elif bar.low <= position.stop_price:
            reason = "stop loss"
            exit_price = position.stop_price
        elif age_seconds >= self.settings.max_hold_seconds:
            reason = "max hold"

        if not reason:
            return None

        return self.sell(bar.symbol, position.shares, exit_price, bar.end_ms, reason)

    def sell(self, symbol: str, shares: int, price: float, timestamp_ms: int, reason: str) -> Fill | None:
        position = self.positions.pop(symbol, None)
        if not position:
            return None

        shares = min(shares, position.shares)
        proceeds = shares * price
        pnl = (price - position.entry_price) * shares
        self.cash += proceeds
        self.realized_pnl += pnl

        fill = Fill(symbol, "SELL", shares, price, timestamp_ms, pnl=pnl, reason=reason)
        self.fills.append(fill)
        LOG.info("PAPER SELL %s %s @ %.2f | pnl %.2f | %s", shares, symbol, price, pnl, reason)
        return fill
