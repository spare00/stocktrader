import logging
from dataclasses import dataclass, field

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from alpaca_client import make_clients
from config import Settings
from models import Bar, Signal


LOG = logging.getLogger(__name__)


@dataclass
class Position:
    symbol: str
    strategy: str
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
class PositionTracker:
    settings: Settings
    cash: float = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    realized_pnl: float = 0.0

    def __post_init__(self) -> None:
        self.cash = self.settings.starting_cash

    def open_symbols(self) -> set[str]:
        return set(self.positions)

    def planned_shares(self, price: float) -> int:
        budget = min(self.settings.max_position_value, self.cash)
        return int(budget // price)

    def record_entry(self, signal: Signal, shares: int, fill_price: float, reason: str) -> Fill:
        self.cash -= shares * fill_price
        self.positions[signal.symbol] = Position(
            symbol=signal.symbol,
            strategy=signal.strategy,
            shares=shares,
            entry_price=fill_price,
            entry_ms=signal.timestamp_ms,
            target_price=fill_price * (1 + self.settings.target_profit_pct),
            stop_price=fill_price * (1 - self.settings.stop_loss_pct),
        )
        fill = Fill(signal.symbol, "BUY", shares, fill_price, signal.timestamp_ms, reason=reason)
        self.fills.append(fill)
        return fill

    def record_exit(self, symbol: str, shares: int, price: float, timestamp_ms: int, reason: str) -> Fill | None:
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
        return fill


@dataclass
class LocalPaperExecutor:
    tracker: PositionTracker

    @property
    def realized_pnl(self) -> float:
        return self.tracker.realized_pnl

    def open_symbols(self) -> set[str]:
        return self.tracker.open_symbols()

    def buy(self, signal: Signal) -> Fill | None:
        if signal.symbol in self.tracker.positions:
            return None

        shares = self.tracker.planned_shares(signal.price)
        if shares <= 0:
            LOG.info("Skipping %s: not enough cash for one share at %.2f", signal.symbol, signal.price)
            return None

        fill = self.tracker.record_entry(signal, shares, signal.price, signal.reason)
        LOG.info("LOCAL PAPER BUY %s %s @ %.2f | %s", shares, signal.symbol, signal.price, signal.reason)
        return fill

    def manage_exit(self, state, strategies_by_name) -> Fill | None:
        position = self.tracker.positions.get(state.symbol)
        if not position:
            return None

        current_price = state.last_price
        if current_price is None or state.last_event_ms is None:
            return None

        age_seconds = (state.last_event_ms - position.entry_ms) / 1000
        reason = ""
        exit_price = current_price

        if current_price >= position.target_price:
            reason = "target profit"
            exit_price = position.target_price
        elif current_price <= position.stop_price:
            reason = "stop loss"
            exit_price = position.stop_price
        elif age_seconds >= self.tracker.settings.max_hold_seconds:
            reason = "max hold"

        if not reason:
            strategy = strategies_by_name.get(position.strategy)
            if strategy:
                decision = strategy.should_exit(state, position)
                if decision:
                    reason = decision.reason

        if not reason:
            return None

        fill = self.tracker.record_exit(state.symbol, position.shares, exit_price, state.last_event_ms, reason)
        if fill:
            LOG.info("LOCAL PAPER SELL %s %s @ %.2f | pnl %.2f | %s", fill.shares, fill.symbol, fill.price, fill.pnl, fill.reason)
        return fill


@dataclass
class AlpacaPaperExecutor:
    settings: Settings
    tracker: PositionTracker
    clients: object = field(init=False)

    def __post_init__(self) -> None:
        self.clients = make_clients(self.settings)

    @property
    def realized_pnl(self) -> float:
        return self.tracker.realized_pnl

    def open_symbols(self) -> set[str]:
        return self.tracker.open_symbols()

    def buy(self, signal: Signal) -> Fill | None:
        if signal.symbol in self.tracker.positions:
            return None

        shares = self.tracker.planned_shares(signal.price)
        if shares <= 0:
            LOG.info("Skipping %s: not enough cash for one share at %.2f", signal.symbol, signal.price)
            return None

        request = MarketOrderRequest(
            symbol=signal.symbol,
            qty=shares,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            client_order_id=f"codex-{signal.symbol.lower()}-{signal.timestamp_ms}-buy",
        )
        order = self.clients.trading.submit_order(order_data=request)
        fill_price = float(order.filled_avg_price or signal.price)
        fill = self.tracker.record_entry(signal, shares, fill_price, f"{signal.reason} | alpaca_order_id={order.id}")
        LOG.info("ALPACA PAPER BUY %s %s @ %.2f | order=%s", shares, signal.symbol, fill_price, order.id)
        return fill

    def manage_exit(self, state, strategies_by_name) -> Fill | None:
        position = self.tracker.positions.get(state.symbol)
        if not position:
            return None

        current_price = state.last_price
        if current_price is None or state.last_event_ms is None:
            return None

        age_seconds = (state.last_event_ms - position.entry_ms) / 1000
        reason = ""

        if current_price >= position.target_price:
            reason = "target profit"
        elif current_price <= position.stop_price:
            reason = "stop loss"
        elif age_seconds >= self.settings.max_hold_seconds:
            reason = "max hold"

        if not reason:
            strategy = strategies_by_name.get(position.strategy)
            if strategy:
                decision = strategy.should_exit(state, position)
                if decision:
                    reason = decision.reason

        if not reason:
            return None

        request = MarketOrderRequest(
            symbol=state.symbol,
            qty=position.shares,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=f"codex-{state.symbol.lower()}-{state.last_event_ms}-sell",
        )
        order = self.clients.trading.submit_order(order_data=request)
        fill_price = float(order.filled_avg_price or current_price)
        fill = self.tracker.record_exit(
            state.symbol,
            position.shares,
            fill_price,
            state.last_event_ms,
            f"{reason} | alpaca_order_id={order.id}",
        )
        if fill:
            LOG.info("ALPACA PAPER SELL %s %s @ %.2f | order=%s | %s", fill.shares, fill.symbol, fill.price, order.id, reason)
        return fill


def build_executor(settings: Settings):
    tracker = PositionTracker(settings)
    if settings.execution_mode == "alpaca_paper":
        return AlpacaPaperExecutor(settings, tracker)
    return LocalPaperExecutor(tracker)
