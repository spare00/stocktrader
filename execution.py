import json
import logging
import time
from uuid import uuid4
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from requests.exceptions import RequestException

from config import Settings
from market_hours import is_regular_market_time, should_flatten_before_close, trading_day_key
from models import Bar, Signal
from order_prefixes import CLIENT_ORDER_ID_ROOT, strategy_order_prefix


LOG = logging.getLogger(__name__)
TRADE_JOURNAL_FILE = Path("logs") / "trade_journal.jsonl"
FILLED_ORDER_STATUSES = {"filled"}
FINAL_ORDER_STATUSES = {"canceled", "done_for_day", "expired", "rejected", "suspended"}


@dataclass
class Position:
    symbol: str
    strategy: str
    shares: int
    entry_price: float
    entry_ms: int
    target_price: float
    stop_price: float
    initial_stop_price: float | None = None
    max_price: float = 0.0
    last_high_ts: int = 0
    partial_exit_taken: bool = False
    original_shares: int = 0
    realized_pnl: float = 0.0
    session_open_price: float | None = None
    entry_open_pct: float | None = None
    runner_mode: bool = False
    stop_confirmation_count: int = 0
    quote_stop_confirmed: bool = True


@dataclass
class Fill:
    symbol: str
    side: str
    shares: int
    price: float
    timestamp_ms: int
    strategy: str = ""
    pnl: float = 0.0
    reason: str = ""
    order_id: str = ""
    trade_type: str = ""
    entry_open_pct: float | None = None
    hold_seconds: float | None = None
    mfe_pct: float | None = None
    signal_price: float | None = None
    slippage_pct: float | None = None
    fill_latency_seconds: float | None = None
    r_multiple: float | None = None
    runner_r_multiple: float | None = None
    full_trade_r_multiple: float | None = None
    cumulative_daily_pnl: float | None = None
    exit_stage: str = ""


@dataclass
class PositionTracker:
    settings: Settings
    cash: float = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    realized_pnl: float = 0.0
    daily_realized_pnl: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.cash = self.settings.starting_cash

    def open_symbols(self) -> set[str]:
        return set(self.positions)

    def open_strategy_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for position in self.positions.values():
            strategy = position.strategy or "unknown"
            counts[strategy] = counts.get(strategy, 0) + 1
        return counts

    def planned_shares(
        self,
        price: float,
        stop_price: float | None = None,
        size_multiplier: float = 1.0,
        strategy: str = "",
    ) -> int:
        max_position_value = self._max_position_value_for_strategy(strategy)
        budget = min(max_position_value, self.cash)
        budget *= max(0.0, size_multiplier)
        if self.settings.position_sizing_mode == "risk":
            stop_distance = price - stop_price if stop_price is not None else price * self.settings.stop_loss_pct
            if stop_distance > 0:
                risk_budget = max(0.0, self.cash * self.settings.risk_per_trade_pct)
                budget = min(budget, risk_budget / stop_distance * price)
        return int(budget // price)

    def _max_position_value_for_strategy(self, strategy: str) -> float:
        value = float(getattr(self.settings, f"{self._compact_settings_prefix(strategy)}_max_position_value", 0.0) or 0.0)
        return value if value > 0 else self.settings.max_position_value

    def max_hold_seconds_for_strategy(self, strategy: str) -> int:
        value = int(getattr(self.settings, f"{self._compact_settings_prefix(strategy)}_max_hold_seconds", 0) or 0)
        return value if value > 0 else self.settings.max_hold_seconds

    @staticmethod
    def _compact_settings_prefix(strategy: str) -> str:
        if strategy == "stoch_macd_reversal":
            return "stoch_macd"
        if strategy == "macd_early_impulse":
            return "macd"
        if strategy == "breakout_power":
            return "bp"
        return strategy

    def total_pnl(self, mark_prices: dict[str, float]) -> float:
        unrealized = 0.0
        for symbol, position in self.positions.items():
            mark_price = mark_prices.get(symbol)
            if mark_price is not None:
                unrealized += (mark_price - position.entry_price) * position.shares
        return self.realized_pnl + unrealized

    def record_entry(
        self,
        signal: Signal,
        shares: int,
        fill_price: float,
        reason: str,
        order_id: str = "",
        fill_latency_seconds: float | None = None,
    ) -> Fill:
        self.cash -= shares * fill_price
        slippage_pct = (fill_price - signal.price) / signal.price if signal.price > 0 else None
        stop_px = signal.stop_price or fill_price * (1 - self.settings.stop_loss_pct)
        self.positions[signal.symbol] = Position(
            symbol=signal.symbol,
            strategy=signal.strategy,
            shares=shares,
            entry_price=fill_price,
            entry_ms=signal.timestamp_ms,
            target_price=fill_price * (1 + self.settings.target_profit_pct),
            stop_price=stop_px,
            initial_stop_price=stop_px,
            max_price=fill_price,
            last_high_ts=signal.timestamp_ms,
            original_shares=shares,
            session_open_price=signal.session_open_price,
            entry_open_pct=signal.entry_open_pct,
            runner_mode=signal.runner_mode,
        )
        fill = Fill(
            signal.symbol,
            "BUY",
            shares,
            fill_price,
            signal.timestamp_ms,
            strategy=signal.strategy,
            reason=reason,
            order_id=order_id,
            entry_open_pct=signal.entry_open_pct,
            signal_price=signal.price,
            slippage_pct=slippage_pct,
            fill_latency_seconds=fill_latency_seconds,
        )
        self.fills.append(fill)
        self._write_trade_journal(fill)
        return fill

    def record_reconciled_position(self, symbol: str, shares: int, entry_price: float, timestamp_ms: int) -> None:
        recon_stop = entry_price * (1 - self.settings.stop_loss_pct)
        self.positions[symbol] = Position(
            symbol=symbol,
            strategy="reconciled",
            shares=shares,
            entry_price=entry_price,
            entry_ms=timestamp_ms,
            target_price=entry_price * (1 + self.settings.target_profit_pct),
            stop_price=recon_stop,
            initial_stop_price=recon_stop,
            max_price=entry_price,
            last_high_ts=timestamp_ms,
            original_shares=shares,
        )

    def record_exit(
        self,
        symbol: str,
        shares: int,
        price: float,
        timestamp_ms: int,
        reason: str,
        order_id: str = "",
        mark_partial: bool = False,
    ) -> Fill | None:
        position = self.positions.get(symbol)
        if not position:
            return None

        shares = min(shares, position.shares)
        proceeds = shares * price
        pnl = (price - position.entry_price) * shares
        self.cash += proceeds
        self.realized_pnl += pnl
        prior_position_pnl = position.realized_pnl
        position.realized_pnl += pnl
        day_key = trading_day_key(timestamp_ms)
        self.daily_realized_pnl[day_key] = self.daily_realized_pnl.get(day_key, 0.0) + pnl
        closing_position = shares >= position.shares
        was_partial = position.partial_exit_taken
        if shares >= position.shares:
            self.positions.pop(symbol, None)
        else:
            position.shares -= shares
            if mark_partial:
                position.partial_exit_taken = True
                if (
                    position.strategy == "maha7"
                    and self.settings.maha7_move_stop_to_entry_after_partial
                ):
                    position.stop_price = position.entry_price

        trade_type = "winner" if pnl > 0 else "loser"
        hold_seconds = (timestamp_ms - position.entry_ms) / 1000
        mfe_price = max(position.max_price, price)
        mfe_pct = (mfe_price - position.entry_price) / position.entry_price if position.entry_price > 0 else 0.0
        risk_per_share = position.entry_price - position.stop_price
        r_multiple = (price - position.entry_price) / risk_per_share if risk_per_share > 0 else None
        runner_r_multiple = r_multiple if was_partial and closing_position else None
        full_trade_r_multiple = None
        if closing_position and risk_per_share > 0 and position.original_shares > 0:
            full_trade_r_multiple = (prior_position_pnl + pnl) / (risk_per_share * position.original_shares)
        cumulative_daily_pnl = self.daily_realized_pnl[day_key]
        if mark_partial:
            exit_stage = "partial"
        elif was_partial:
            exit_stage = "runner"
        else:
            exit_stage = "full"
        fill = Fill(
            symbol,
            "SELL",
            shares,
            price,
            timestamp_ms,
            strategy=position.strategy,
            pnl=pnl,
            reason=reason,
            order_id=order_id,
            trade_type=trade_type,
            entry_open_pct=position.entry_open_pct,
            hold_seconds=hold_seconds,
            mfe_pct=mfe_pct,
            r_multiple=r_multiple,
            runner_r_multiple=runner_r_multiple,
            full_trade_r_multiple=full_trade_r_multiple,
            cumulative_daily_pnl=cumulative_daily_pnl,
            exit_stage=exit_stage,
        )
        self.fills.append(fill)
        self._write_trade_journal(fill)
        return fill

    def _write_trade_journal(self, fill: Fill) -> None:
        entry = {
            "timestamp": datetime.fromtimestamp(fill.timestamp_ms / 1000, tz=timezone.utc).isoformat(),
            "timestamp_ms": fill.timestamp_ms,
            "event": fill.side.lower(),
            "symbol": fill.symbol,
            "strategy": fill.strategy,
            "shares": fill.shares,
            "price": fill.price,
            "pnl": fill.pnl,
            "reason": fill.reason,
        }
        if fill.trade_type:
            entry["trade_type"] = fill.trade_type
        if fill.entry_open_pct is not None:
            entry["entry_open_pct"] = fill.entry_open_pct
        if fill.hold_seconds is not None:
            entry["hold_seconds"] = fill.hold_seconds
        if fill.mfe_pct is not None:
            entry["mfe_pct"] = fill.mfe_pct
        if fill.signal_price is not None:
            entry["signal_price"] = fill.signal_price
        if fill.slippage_pct is not None:
            entry["slippage_pct"] = fill.slippage_pct
        if fill.fill_latency_seconds is not None:
            entry["fill_latency_seconds"] = fill.fill_latency_seconds
        if fill.r_multiple is not None:
            entry["r_multiple"] = fill.r_multiple
        if fill.runner_r_multiple is not None:
            entry["runner_r_multiple"] = fill.runner_r_multiple
        if fill.full_trade_r_multiple is not None:
            entry["full_trade_r_multiple"] = fill.full_trade_r_multiple
        if fill.cumulative_daily_pnl is not None:
            entry["cumulative_daily_pnl"] = fill.cumulative_daily_pnl
        if fill.exit_stage:
            entry["exit_stage"] = fill.exit_stage
        if fill.order_id:
            entry["order_id"] = fill.order_id

        try:
            TRADE_JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
            with TRADE_JOURNAL_FILE.open("a", encoding="utf-8") as journal:
                journal.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
        except OSError:
            LOG.exception("Failed to write trade journal entry for %s %s", fill.side, fill.symbol)

    @staticmethod
    def update_position_price(position: Position, price: float, timestamp_ms: int) -> None:
        if position.max_price <= 0:
            position.max_price = position.entry_price
        if position.last_high_ts <= 0:
            position.last_high_ts = position.entry_ms
        if price > position.max_price:
            position.max_price = price
            position.last_high_ts = timestamp_ms


@dataclass
class LocalPaperExecutor:
    tracker: PositionTracker

    @property
    def realized_pnl(self) -> float:
        return self.tracker.realized_pnl

    def open_symbols(self) -> set[str]:
        return self.tracker.open_symbols()

    def open_strategy_counts(self) -> dict[str, int]:
        return self.tracker.open_strategy_counts()

    def total_pnl(self, mark_prices: dict[str, float]) -> float:
        return self.tracker.total_pnl(mark_prices)

    def consume_failed_entry(self, symbol: str) -> bool:
        return False

    def buy(self, signal: Signal, state=None) -> Fill | None:
        if self.tracker.settings.regular_market_only and not is_regular_market_time(signal.timestamp_ms):
            LOG.info("Skipping %s: outside regular market hours", signal.symbol)
            return None

        if signal.symbol in self.tracker.positions:
            return None

        shares = self.tracker.planned_shares(
            signal.price,
            signal.stop_price,
            signal.position_size_multiplier,
            signal.strategy,
        )
        if shares <= 0:
            LOG.info("Skipping %s: not enough cash for one share at %.2f", signal.symbol, signal.price)
            return None

        fresh_price = self._fresh_entry_price(signal, state)
        if fresh_price is not None and fresh_price > signal.price * (1 + self.tracker.settings.max_entry_chase_pct):
            LOG.info(
                "Skipping %s: entry chase protection %.2f > %.2f",
                signal.symbol,
                fresh_price,
                signal.price * (1 + self.tracker.settings.max_entry_chase_pct),
            )
            return None

        fill = self.tracker.record_entry(signal, shares, signal.price, signal.reason, fill_latency_seconds=0.0)
        LOG.info("LOCAL PAPER BUY %s %s @ %.2f | %s", shares, signal.symbol, signal.price, signal.reason)
        return fill

    def manage_exit(self, state, strategies_by_name, now_ms: int | None = None) -> Fill | None:
        position = self.tracker.positions.get(state.symbol)
        if not position:
            return None

        current_price = state.last_price
        if current_price is None or state.last_event_ms is None:
            return None
        event_ms = now_ms or state.last_event_ms
        if self.tracker.settings.regular_market_only and not is_regular_market_time(event_ms):
            return None

        age_seconds = (event_ms - position.entry_ms) / 1000
        strategy = strategies_by_name.get(position.strategy)
        exit_activation_delay = strategy.exit_activation_delay_seconds(position) if strategy else 0
        use_fixed_target = strategy.use_fixed_target_exit(position) if strategy else True
        reason = ""
        exit_price = current_price
        self.tracker.update_position_price(position, current_price, event_ms)

        stop_check_price = self._stop_check_price(state, position, current_price)
        max_loss_price = self._max_loss_price(position)
        if stop_check_price > position.stop_price:
            position.stop_confirmation_count = 0
        quote_stop_confirmed = True
        if stop_check_price <= position.stop_price:
            quote_stop_confirmed = self._confirm_quote_stop(state, position, age_seconds, stop_check_price)
        position.quote_stop_confirmed = quote_stop_confirmed
        if max_loss_price is not None and stop_check_price <= max_loss_price and quote_stop_confirmed:
            reason = "max trade loss"
            exit_price = stop_check_price
        elif should_flatten_before_close(event_ms, self.tracker.settings.flatten_before_close_minutes):
            reason = "end-of-day flatten"
        elif (
            stop_check_price <= position.stop_price
            and quote_stop_confirmed
            and (
                age_seconds >= exit_activation_delay
                or not (strategy and strategy.delay_stop_loss_until_exit_activation(position))
            )
        ):
            reason = "stop loss"
            exit_price = min(position.stop_price, stop_check_price)
        max_hold_seconds = self.tracker.max_hold_seconds_for_strategy(position.strategy)
        if not reason and age_seconds >= max_hold_seconds and current_price < position.entry_price:
            pnl_pct = (current_price - position.entry_price) / position.entry_price if position.entry_price > 0 else 0.0
            if strategy and not strategy.allow_max_hold_exit(state, position, age_seconds, pnl_pct):
                LOG.debug("Max hold deferred %s by %s strategy exit policy", state.symbol, position.strategy)
                return None
            reason = "max hold"
        elif not reason and age_seconds < exit_activation_delay:
            return None
        elif not reason and use_fixed_target and current_price >= position.target_price:
            reason = "target profit"
            exit_price = position.target_price

        if not reason:
            if strategy:
                decision = strategy.should_exit(state, position)
                if decision:
                    reason = decision.reason
                    if decision.shares is not None:
                        shares_to_sell = min(position.shares, max(1, decision.shares))
                    else:
                        shares_to_sell = position.shares
                    mark_partial = decision.mark_partial
                else:
                    shares_to_sell = position.shares
                    mark_partial = False
            else:
                shares_to_sell = position.shares
                mark_partial = False
        else:
            shares_to_sell = position.shares
            mark_partial = False

        if not reason:
            return None

        fill = self.tracker.record_exit(state.symbol, shares_to_sell, exit_price, event_ms, reason, mark_partial=mark_partial)
        if fill:
            LOG.info("LOCAL PAPER SELL %s %s @ %.2f | pnl %.2f | %s", fill.shares, fill.symbol, fill.price, fill.pnl, fill.reason)
        return fill

    @staticmethod
    def _fresh_entry_price(signal: Signal, state) -> float | None:
        if state is None or getattr(state, "symbol", signal.symbol) != signal.symbol:
            return None
        quote = getattr(state, "quote", None)
        if quote is not None and quote.ask > 0:
            return quote.ask
        return getattr(state, "last_price", None)

    def _max_loss_price(self, position: Position) -> float | None:
        risk_per_share = position.entry_price - position.stop_price
        if risk_per_share <= 0:
            return None
        return position.entry_price - self.tracker.settings.max_trade_loss_r * risk_per_share

    @staticmethod
    def _stop_check_price(state, position: Position, fallback_price: float) -> float:
        quote = getattr(state, "quote", None)
        if position.strategy == "stoch_macd_reversal" and quote is not None and quote.bid > 0:
            return quote.bid
        return fallback_price

    def _confirm_quote_stop(self, state, position: Position, age_seconds: float, stop_price: float) -> bool:
        if position.strategy != "stoch_macd_reversal":
            return True
        catastrophic_price = position.entry_price * (1.0 - max(0.0, self.tracker.settings.stoch_macd_catastrophic_stop_loss_pct))
        if stop_price <= catastrophic_price:
            position.stop_confirmation_count = 0
            return True
        if age_seconds < max(0, self.tracker.settings.stoch_macd_stop_grace_seconds):
            position.stop_confirmation_count = 0
            return False
        quote = getattr(state, "quote", None)
        if quote is not None and quote.spread_bps > self.tracker.settings.stoch_macd_stop_max_spread_bps:
            position.stop_confirmation_count = 0
            return False
        position.stop_confirmation_count += 1
        return position.stop_confirmation_count >= max(1, self.tracker.settings.stoch_macd_stop_confirmations)


@dataclass
class AlpacaPaperExecutor:
    settings: Settings
    tracker: PositionTracker
    clients: object = field(init=False)
    _failed_entry_symbol: str | None = field(init=False, default=None)
    _failed_entry_reason: str = field(init=False, default="")
    _market_clock_cache_is_open: bool | None = field(init=False, default=None)
    _market_clock_cache_monotonic: float = field(init=False, default=0.0)
    _market_clock_cache_ttl_seconds: float = field(init=False, default=15.0)
    _market_clock_failure_grace_seconds: float = field(init=False, default=300.0)

    def __post_init__(self) -> None:
        from alpaca_client import make_clients

        self.clients = make_clients(self.settings)
        try:
            clk = self.clients.trading.get_clock()
            self._record_market_clock(clk)
            LOG.info(
                "Alpaca trading clock: is_open=%s timestamp=%s next_open=%s next_close=%s",
                clk.is_open,
                getattr(clk, "timestamp", None),
                clk.next_open,
                clk.next_close,
            )
        except Exception as exc:
            LOG.warning("Could not read Alpaca trading clock (buys may be skipped if regular_market_only): %s", exc)
        self._sync_account_cash()
        self._reconcile_target_positions()
        self._cancel_target_open_orders()

    @property
    def realized_pnl(self) -> float:
        return self.tracker.realized_pnl

    def open_symbols(self) -> set[str]:
        return self.tracker.open_symbols()

    def open_strategy_counts(self) -> dict[str, int]:
        return self.tracker.open_strategy_counts()

    def total_pnl(self, mark_prices: dict[str, float]) -> float:
        return self.tracker.total_pnl(mark_prices)

    def consume_failed_entry(self, symbol: str) -> bool:
        failed_symbol = getattr(self, "_failed_entry_symbol", None)
        if failed_symbol != symbol:
            return False
        self._failed_entry_symbol = None
        self._failed_entry_reason = ""
        return True

    def buy(self, signal: Signal, state=None) -> Fill | None:
        from alpaca.common.exceptions import APIError
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        self._failed_entry_symbol = None
        self._failed_entry_reason = ""

        if self.settings.regular_market_only and not self._market_is_open():
            LOG.info("Skipping %s: Alpaca market clock is closed", signal.symbol)
            return None

        if signal.symbol in self.tracker.positions:
            return None

        shares = self.tracker.planned_shares(
            signal.price,
            signal.stop_price,
            signal.position_size_multiplier,
            signal.strategy,
        )
        if shares <= 0:
            LOG.info("Skipping %s: not enough cash for one share at %.2f", signal.symbol, signal.price)
            return None

        fresh_price = self._fresh_entry_price(signal, state)
        if fresh_price is not None and fresh_price > signal.price * (1 + self.settings.max_entry_chase_pct):
            self._failed_entry_symbol = signal.symbol
            self._failed_entry_reason = "entry chase protection"
            LOG.info(
                "Skipping %s: entry chase protection %.2f > %.2f",
                signal.symbol,
                fresh_price,
                signal.price * (1 + self.settings.max_entry_chase_pct),
            )
            return None

        request = MarketOrderRequest(
            symbol=signal.symbol,
            qty=shares,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            client_order_id=self._new_client_order_id(signal.symbol, signal.strategy, "buy", signal.timestamp_ms),
        )
        started = time.monotonic()
        try:
            order = self.clients.trading.submit_order(order_data=request)
        except APIError as exc:
            LOG.warning("Skipping %s: Alpaca buy order rejected: %s", signal.symbol, exc)
            return None
        settled = self._settled_fill(order)
        fill_latency_seconds = time.monotonic() - started
        if settled is None:
            self._failed_entry_symbol = signal.symbol
            self._failed_entry_reason = "unfilled buy timeout"
            LOG.info("Skipping %s: Alpaca buy order was not confirmed filled | order=%s", signal.symbol, order.id)
            return None

        filled_shares, fill_price, order = settled
        fill = self.tracker.record_entry(
            signal,
            filled_shares,
            fill_price,
            f"{signal.reason} | alpaca_order_id={order.id}",
            order_id=str(order.id),
            fill_latency_seconds=fill_latency_seconds,
        )
        LOG.info(
            "ALPACA PAPER BUY %s %s @ %.2f | order=%s | slippage %.3f%% | fill_latency %.2fs",
            filled_shares,
            signal.symbol,
            fill_price,
            order.id,
            ((fill_price - signal.price) / signal.price) * 100 if signal.price > 0 else 0.0,
            fill_latency_seconds,
        )
        return fill

    def manage_exit(self, state, strategies_by_name, now_ms: int | None = None) -> Fill | None:
        from alpaca.common.exceptions import APIError
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        position = self.tracker.positions.get(state.symbol)
        if not position:
            return None

        event_ms = now_ms or state.last_event_ms
        if event_ms is None:
            return None
        if self.settings.regular_market_only and not self._market_is_open():
            return None

        current_price = state.last_price
        flatten = should_flatten_before_close(event_ms, self.settings.flatten_before_close_minutes)
        if current_price is None and not flatten:
            return None

        age_seconds = (event_ms - position.entry_ms) / 1000
        strategy = strategies_by_name.get(position.strategy)
        exit_activation_delay = strategy.exit_activation_delay_seconds(position) if strategy else 0
        use_fixed_target = strategy.use_fixed_target_exit(position) if strategy else True
        reason = ""
        if current_price is not None:
            self.tracker.update_position_price(position, current_price, event_ms)

        stop_check_price = self._stop_check_price(state, position, current_price) if current_price is not None else None
        max_loss_price = self._max_loss_price(position)
        if stop_check_price is not None and stop_check_price > position.stop_price:
            position.stop_confirmation_count = 0
        quote_stop_confirmed = True
        if stop_check_price is not None and stop_check_price <= position.stop_price:
            quote_stop_confirmed = self._confirm_quote_stop(state, position, age_seconds, stop_check_price)
        position.quote_stop_confirmed = quote_stop_confirmed
        if (
            stop_check_price is not None
            and max_loss_price is not None
            and stop_check_price <= max_loss_price
            and quote_stop_confirmed
        ):
            reason = "max trade loss"
        elif flatten:
            reason = "end-of-day flatten"
        elif (
            stop_check_price is not None
            and stop_check_price <= position.stop_price
            and quote_stop_confirmed
            and (
                age_seconds >= exit_activation_delay
                or not (strategy and strategy.delay_stop_loss_until_exit_activation(position))
            )
        ):
            reason = "stop loss"
        max_hold_seconds = self.tracker.max_hold_seconds_for_strategy(position.strategy)
        if not reason and age_seconds >= max_hold_seconds and current_price < position.entry_price:
            pnl_pct = (current_price - position.entry_price) / position.entry_price if position.entry_price > 0 else 0.0
            if strategy and not strategy.allow_max_hold_exit(state, position, age_seconds, pnl_pct):
                LOG.debug("Max hold deferred %s by %s strategy exit policy", state.symbol, position.strategy)
                return None
            reason = "max hold"
        elif not reason and age_seconds < exit_activation_delay:
            return None
        elif not reason and use_fixed_target and current_price >= position.target_price:
            reason = "target profit"

        if not reason:
            if strategy:
                decision = strategy.should_exit(state, position)
                if decision:
                    reason = decision.reason
                    if decision.shares is not None:
                        shares_to_sell = min(position.shares, max(1, decision.shares))
                    else:
                        shares_to_sell = position.shares
                    mark_partial = decision.mark_partial
                else:
                    shares_to_sell = position.shares
                    mark_partial = False
            else:
                shares_to_sell = position.shares
                mark_partial = False
        else:
            shares_to_sell = position.shares
            mark_partial = False

        if not reason:
            return None

        request = MarketOrderRequest(
            symbol=state.symbol,
            qty=shares_to_sell,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=self._new_client_order_id(state.symbol, position.strategy, "sell", event_ms),
        )
        try:
            order = self.clients.trading.submit_order(order_data=request)
        except APIError as exc:
            LOG.warning("Keeping %s open: Alpaca sell order rejected: %s", state.symbol, exc)
            return None
        settled = self._settled_fill(order)
        if settled is None:
            LOG.info("Keeping %s open: Alpaca sell order was not confirmed filled | order=%s", state.symbol, order.id)
            return None

        filled_shares, fill_price, order = settled
        fill = self.tracker.record_exit(
            state.symbol,
            filled_shares,
            fill_price,
            event_ms,
            f"{reason} | alpaca_order_id={order.id}",
            order_id=str(order.id),
            mark_partial=mark_partial,
        )
        if fill:
            LOG.info("ALPACA PAPER SELL %s %s @ %.2f | order=%s | %s", fill.shares, fill.symbol, fill.price, order.id, reason)
        return fill

    def _market_is_open(self) -> bool:
        now = time.monotonic()
        cached_is_open = getattr(self, "_market_clock_cache_is_open", None)
        cached_at = getattr(self, "_market_clock_cache_monotonic", 0.0)
        cache_age = now - cached_at
        cache_ttl = getattr(self, "_market_clock_cache_ttl_seconds", 15.0)
        failure_grace = getattr(self, "_market_clock_failure_grace_seconds", 300.0)

        if cached_is_open is not None and cache_age <= cache_ttl:
            return cached_is_open

        try:
            clk = self.clients.trading.get_clock()
        except (OSError, RequestException) as exc:
            if cached_is_open is not None and cache_age <= failure_grace:
                LOG.warning(
                    "Could not refresh Alpaca trading clock (%s: %s); using cached is_open=%s age=%.1fs",
                    type(exc).__name__,
                    exc,
                    cached_is_open,
                    cache_age,
                )
                return cached_is_open
            LOG.warning(
                "Could not read Alpaca trading clock (%s: %s); treating market as closed",
                type(exc).__name__,
                exc,
            )
            return False

        return self._record_market_clock(clk)

    def _record_market_clock(self, clock) -> bool:
        is_open = bool(clock.is_open)
        self._market_clock_cache_is_open = is_open
        self._market_clock_cache_monotonic = time.monotonic()
        return is_open

    @staticmethod
    def _new_client_order_id(symbol: str, strategy: str, side: str, timestamp_ms: int) -> str:
        nonce = uuid4().hex[:8]
        prefix = strategy_order_prefix(strategy)
        side_code = "s" if side.lower().startswith("s") else "b"
        return f"{CLIENT_ORDER_ID_ROOT}-{prefix}-{symbol.lower()}-{timestamp_ms}-{side_code}-{nonce}"

    def _sync_account_cash(self) -> None:
        try:
            account = self.clients.trading.get_account()
        except Exception:
            LOG.exception("Failed to sync Alpaca account cash on startup")
            return

        try:
            self.tracker.cash = float(account.cash)
        except (TypeError, ValueError):
            LOG.warning("Ignoring Alpaca account cash value: %s", getattr(account, "cash", None))

    def _reconcile_target_positions(self) -> None:
        target_symbols = set(self.settings.symbols)
        now_ms = int(time.time() * 1000)
        try:
            positions = self.clients.trading.get_all_positions()
        except Exception:
            LOG.exception("Failed to reconcile Alpaca positions on startup")
            return

        for position in positions:
            symbol = str(getattr(position, "symbol", "")).upper()
            if symbol not in target_symbols:
                continue

            shares = self._position_shares(position)
            if shares <= 0:
                LOG.warning("Ignoring non-long reconciled Alpaca position %s qty=%s", symbol, getattr(position, "qty", None))
                continue

            entry_price = self._position_entry_price(position)
            if entry_price is None:
                LOG.warning("Ignoring reconciled Alpaca position %s without entry price", symbol)
                continue

            self.tracker.record_reconciled_position(symbol, shares, entry_price, now_ms)
            LOG.info("Reconciled Alpaca position %s %s @ %.2f", shares, symbol, entry_price)

    def _cancel_target_open_orders(self) -> None:
        target_symbols = set(self.settings.symbols)
        try:
            orders = self.clients.trading.get_orders()
        except Exception:
            LOG.exception("Failed to inspect Alpaca open orders on startup")
            return

        for order in orders:
            symbol = str(getattr(order, "symbol", "")).upper()
            if symbol not in target_symbols:
                continue
            status = self._order_status(order)
            if status in FILLED_ORDER_STATUSES or status in FINAL_ORDER_STATUSES:
                continue
            self._cancel_unfilled_order(order)
            LOG.info("Canceled startup Alpaca open order for %s | order=%s", symbol, getattr(order, "id", "unknown"))

    def _settled_fill(self, order) -> tuple[int, float, object] | None:
        deadline = time.monotonic() + self.settings.alpaca_fill_timeout_seconds
        current_order = order

        while True:
            status = self._order_status(current_order)
            filled_shares = self._filled_shares(current_order)
            fill_price = self._fill_price(current_order)
            if status in FILLED_ORDER_STATUSES and filled_shares > 0 and fill_price is not None:
                return filled_shares, fill_price, current_order

            if status in FINAL_ORDER_STATUSES:
                return self._existing_fill(current_order)

            if time.monotonic() >= deadline:
                if filled_shares > 0 and fill_price is not None:
                    LOG.info(
                        "Recording partial Alpaca fill after %.1fs timeout and leaving remainder open | order=%s filled_qty=%s",
                        self.settings.alpaca_fill_timeout_seconds,
                        getattr(current_order, "id", "unknown"),
                        filled_shares,
                    )
                    return filled_shares, fill_price, current_order

                LOG.info(
                    "Canceling unfilled Alpaca order after %.1fs timeout | order=%s status=%s filled_qty=%s",
                    self.settings.alpaca_fill_timeout_seconds,
                    getattr(current_order, "id", "unknown"),
                    status,
                    filled_shares,
                )
                self._cancel_unfilled_order(current_order)
                current_order = self.clients.trading.get_order_by_id(current_order.id)
                return self._existing_fill(current_order)

            time.sleep(self.settings.alpaca_fill_poll_seconds)
            current_order = self.clients.trading.get_order_by_id(current_order.id)

    def _existing_fill(self, order) -> tuple[int, float, object] | None:
        filled_shares = self._filled_shares(order)
        fill_price = self._fill_price(order)
        if filled_shares > 0 and fill_price is not None:
            return filled_shares, fill_price, order
        return None

    def _fresh_entry_price(self, signal: Signal, state) -> float | None:
        from alpaca.data.requests import StockLatestQuoteRequest

        try:
            request = StockLatestQuoteRequest(symbol_or_symbols=[signal.symbol], feed=self.clients.feed)
            response = self.clients.historical.get_stock_latest_quote(request)
            latest_quote = response.get(signal.symbol)
            ask_price = float(getattr(latest_quote, "ask_price", 0) or 0)
            if ask_price > 0:
                return ask_price
        except Exception:
            LOG.debug("Could not fetch fresh quote for %s before submit; using cached state", signal.symbol, exc_info=True)

        if state is None or getattr(state, "symbol", signal.symbol) != signal.symbol:
            return None
        quote = getattr(state, "quote", None)
        if quote is not None and quote.ask > 0:
            return quote.ask
        return getattr(state, "last_price", None)

    def _max_loss_price(self, position: Position) -> float | None:
        risk_per_share = position.entry_price - position.stop_price
        if risk_per_share <= 0:
            return None
        return position.entry_price - self.settings.max_trade_loss_r * risk_per_share

    @staticmethod
    def _stop_check_price(state, position: Position, fallback_price: float) -> float:
        quote = getattr(state, "quote", None)
        if position.strategy == "stoch_macd_reversal" and quote is not None and quote.bid > 0:
            return quote.bid
        return fallback_price

    def _confirm_quote_stop(self, state, position: Position, age_seconds: float, stop_price: float) -> bool:
        if position.strategy != "stoch_macd_reversal":
            return True
        catastrophic_price = position.entry_price * (1.0 - max(0.0, self.settings.stoch_macd_catastrophic_stop_loss_pct))
        if stop_price <= catastrophic_price:
            position.stop_confirmation_count = 0
            return True
        if age_seconds < max(0, self.settings.stoch_macd_stop_grace_seconds):
            position.stop_confirmation_count = 0
            return False
        quote = getattr(state, "quote", None)
        if quote is not None and quote.spread_bps > self.settings.stoch_macd_stop_max_spread_bps:
            position.stop_confirmation_count = 0
            return False
        position.stop_confirmation_count += 1
        return position.stop_confirmation_count >= max(1, self.settings.stoch_macd_stop_confirmations)

    def _cancel_unfilled_order(self, order) -> None:
        from alpaca.common.exceptions import APIError

        if self._order_status(order) in FINAL_ORDER_STATUSES:
            return
        try:
            self.clients.trading.cancel_order_by_id(order.id)
        except APIError as exc:
            message = str(exc).lower()
            if "already in" in message and "filled" in message and "state" in message:
                LOG.info("Cancel skipped for Alpaca order %s: already filled before cancel", order.id)
                return
            LOG.exception("Failed to cancel unfilled Alpaca order %s", order.id)
        except Exception:
            LOG.exception("Failed to cancel unfilled Alpaca order %s", order.id)

    @staticmethod
    def _filled_shares(order) -> int:
        return int(float(getattr(order, "filled_qty", 0) or 0))

    @staticmethod
    def _fill_price(order) -> float | None:
        value = getattr(order, "filled_avg_price", None)
        return None if value is None else float(value)

    @staticmethod
    def _order_status(order) -> str:
        return str(getattr(order, "status", "")).lower().split(".")[-1]

    @staticmethod
    def _position_shares(position) -> int:
        return int(float(getattr(position, "qty", 0) or 0))

    @staticmethod
    def _position_entry_price(position) -> float | None:
        value = getattr(position, "avg_entry_price", None)
        return None if value is None else float(value)


def build_executor(settings: Settings):
    tracker = PositionTracker(settings)
    if settings.execution_mode == "alpaca_paper":
        return AlpacaPaperExecutor(settings, tracker)
    return LocalPaperExecutor(tracker)
