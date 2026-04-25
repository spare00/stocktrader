import unittest
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from candle import SymbolState
from config import Settings
from execution import AlpacaPaperExecutor, LocalPaperExecutor, Position, PositionTracker
from models import Bar, Quote
from risk import RiskManager
from strategies import build_strategies
from strategies.opening_impulse import OpeningImpulseStrategy
from strategies.spike import SpikeStrategy


MARKET_TZ = ZoneInfo("America/New_York")


def market_ms(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=MARKET_TZ).timestamp() * 1000)


def bar(symbol: str, close: float, volume: float, end_ms: int) -> Bar:
    return Bar(
        symbol=symbol,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=volume,
        vwap=close,
        start_ms=end_ms - 1000,
        end_ms=end_ms,
    )


class CoreTradingTests(unittest.TestCase):
    def test_spike_strategy_emits_buy_on_price_and_volume_spike(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"], regular_market_only=False)
        state = SymbolState("AAPL")
        state.update_quote(Quote("AAPL", bid=100.00, ask=100.05, bid_size=10, ask_size=10, timestamp_ms=1))

        for index in range(6):
            state.add_bar(bar("AAPL", close=100.0, volume=100, end_ms=index * 1000))
        state.add_bar(bar("AAPL", close=100.40, volume=350, end_ms=7000))

        signal = SpikeStrategy(settings).evaluate(state)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "BUY")

    def test_spike_strategy_uses_timestamp_lookback_not_bar_count(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"], regular_market_only=False)
        state = SymbolState("AAPL")
        base_ms = market_ms(2026, 4, 24, 10, 0)
        state.update_quote(Quote("AAPL", bid=100.00, ask=100.05, bid_size=10, ask_size=10, timestamp_ms=base_ms))

        for index in range(6):
            state.add_bar(bar("AAPL", close=100.0, volume=100, end_ms=base_ms + (index * 60_000)))
        state.add_bar(bar("AAPL", close=100.40, volume=350, end_ms=base_ms + (6 * 60_000)))

        signal = SpikeStrategy(settings).evaluate(state)

        self.assertIsNone(signal)

    def test_risk_rejects_short_entries(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"], regular_market_only=False)
        state = SymbolState("AAPL")
        for index in range(6):
            state.add_bar(bar("AAPL", close=100.0, volume=100, end_ms=index * 1000))
        state.add_bar(bar("AAPL", close=99.60, volume=350, end_ms=7000))
        signal = SpikeStrategy(settings).evaluate(state)

        decision = RiskManager(settings).check_entry(signal, set(), 0)

        self.assertFalse(decision.allowed)
        self.assertIn("short", decision.reason)

    def test_paper_broker_exits_at_target(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            target_profit_pct=0.01,
            regular_market_only=False,
        )
        broker = LocalPaperExecutor(PositionTracker(settings))
        state = SymbolState("AAPL")
        for index in range(6):
            state.add_bar(bar("AAPL", close=100.0, volume=100, end_ms=index * 1000))
        state.add_bar(bar("AAPL", close=100.40, volume=350, end_ms=7000))
        signal = SpikeStrategy(settings).evaluate(state)

        broker.buy(signal)
        state.add_bar(Bar("AAPL", open=100.50, high=101.60, low=100.20, close=101.50, volume=200, vwap=101.2, start_ms=8000, end_ms=9000))
        fill = broker.manage_exit(state, {"spike": SpikeStrategy(settings)})

        self.assertIsNotNone(fill)
        self.assertEqual(fill.side, "SELL")
        self.assertGreater(fill.pnl, 0)

    def test_paper_broker_flattens_before_close(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            flatten_before_close_minutes=5,
            max_hold_seconds=3600,
        )
        broker = LocalPaperExecutor(PositionTracker(settings))
        broker.tracker.positions["AAPL"] = Position(
            symbol="AAPL",
            strategy="spike",
            shares=10,
            entry_price=100.0,
            entry_ms=market_ms(2026, 4, 24, 15, 30),
            target_price=101.0,
            stop_price=99.5,
        )
        state = SymbolState("AAPL")
        state.update_quote(
            Quote(
                "AAPL",
                bid=100.19,
                ask=100.21,
                bid_size=20,
                ask_size=20,
                timestamp_ms=market_ms(2026, 4, 24, 15, 55),
            )
        )

        fill = broker.manage_exit(state, {"spike": SpikeStrategy(settings)})

        self.assertIsNotNone(fill)
        self.assertEqual(fill.reason, "end-of-day flatten")

    def test_paper_broker_flattens_stale_symbol_using_latest_event_time(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            flatten_before_close_minutes=5,
            max_hold_seconds=3600,
        )
        broker = LocalPaperExecutor(PositionTracker(settings))
        broker.tracker.positions["AAPL"] = Position(
            symbol="AAPL",
            strategy="spike",
            shares=10,
            entry_price=100.0,
            entry_ms=market_ms(2026, 4, 24, 15, 30),
            target_price=101.0,
            stop_price=99.5,
        )
        state = SymbolState("AAPL")
        state.update_quote(
            Quote(
                "AAPL",
                bid=100.19,
                ask=100.21,
                bid_size=20,
                ask_size=20,
                timestamp_ms=market_ms(2026, 4, 24, 15, 40),
            )
        )

        fill = broker.manage_exit(
            state,
            {"spike": SpikeStrategy(settings)},
            now_ms=market_ms(2026, 4, 24, 15, 55),
        )

        self.assertIsNotNone(fill)
        self.assertEqual(fill.reason, "end-of-day flatten")
        self.assertEqual(fill.timestamp_ms, market_ms(2026, 4, 24, 15, 55))

    def test_paper_broker_does_not_flatten_too_early(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            flatten_before_close_minutes=5,
            max_hold_seconds=3600,
        )
        broker = LocalPaperExecutor(PositionTracker(settings))
        broker.tracker.positions["AAPL"] = Position(
            symbol="AAPL",
            strategy="spike",
            shares=10,
            entry_price=100.0,
            entry_ms=market_ms(2026, 4, 24, 15, 30),
            target_price=101.0,
            stop_price=99.5,
        )
        state = SymbolState("AAPL")
        state.update_quote(
            Quote(
                "AAPL",
                bid=100.19,
                ask=100.21,
                bid_size=20,
                ask_size=20,
                timestamp_ms=market_ms(2026, 4, 24, 15, 54),
            )
        )

        fill = broker.manage_exit(state, {"spike": SpikeStrategy(settings)})

        self.assertIsNone(fill)

    def test_position_tracker_keeps_remaining_shares_after_partial_exit(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"])
        tracker = PositionTracker(settings)
        tracker.positions["AAPL"] = Position(
            symbol="AAPL",
            strategy="spike",
            shares=10,
            entry_price=100.0,
            entry_ms=market_ms(2026, 4, 24, 10, 0),
            target_price=101.0,
            stop_price=99.5,
        )

        fill = tracker.record_exit("AAPL", shares=4, price=100.5, timestamp_ms=market_ms(2026, 4, 24, 10, 1), reason="partial")

        self.assertIsNotNone(fill)
        self.assertEqual(fill.shares, 4)
        self.assertEqual(tracker.positions["AAPL"].shares, 6)

    def test_position_tracker_total_pnl_includes_unrealized_loss(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"], daily_max_loss=250.0)
        tracker = PositionTracker(settings)
        tracker.positions["AAPL"] = Position(
            symbol="AAPL",
            strategy="spike",
            shares=10,
            entry_price=100.0,
            entry_ms=market_ms(2026, 4, 24, 10, 0),
            target_price=101.0,
            stop_price=99.5,
        )
        signal = SpikeStrategy(settings).evaluate(self._spike_state(market_ms(2026, 4, 24, 10, 1)))

        total_pnl = tracker.total_pnl({"AAPL": 70.0})
        decision = RiskManager(settings).check_entry(signal, set(), total_pnl)

        self.assertEqual(total_pnl, -300.0)
        self.assertFalse(decision.allowed)
        self.assertIn("daily loss", decision.reason)

    def test_alpaca_partial_fill_is_canceled_before_recording(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            alpaca_fill_timeout_seconds=0.0,
        )
        executor = AlpacaPaperExecutor.__new__(AlpacaPaperExecutor)
        executor.settings = settings
        executor.tracker = PositionTracker(settings)
        executor.clients = FakeClients(
            [
                FakeOrder("order-1", status="canceled", filled_qty="3", filled_avg_price="100.25"),
            ]
        )
        order = FakeOrder("order-1", status="partially_filled", filled_qty="3", filled_avg_price="100.25")

        settled = executor._settled_fill(order)

        self.assertIsNotNone(settled)
        self.assertTrue(executor.clients.trading.cancel_called)
        self.assertEqual(settled[0], 3)

    def test_opening_impulse_emits_buy_after_fast_rise(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            opening_impulse_start_minute=0,
            opening_impulse_end_minute=90,
            opening_impulse_window_seconds=30,
            opening_impulse_min_quotes=10,
            opening_impulse_change_pct=0.009,
            opening_impulse_volume_ratio=2.5,
            opening_impulse_max_spread_bps=8.0,
            opening_impulse_min_quote_size=25,
        )
        state = SymbolState("AAPL")
        base_ms = 1777037400000  # 2026-04-24 13:30:00 UTC
        for index in range(4):
            state.add_bar(bar("AAPL", close=100.0 + (index * 0.1), volume=100, end_ms=base_ms + ((index + 1) * 60_000)))
        state.add_bar(bar("AAPL", close=100.4, volume=320, end_ms=base_ms + (5 * 60_000)))

        for index in range(10):
            bid = 100.00 + (index * 0.11)
            ask = bid + 0.015
            state.update_quote(Quote("AAPL", bid=bid, ask=ask, bid_size=30, ask_size=30, timestamp_ms=base_ms + (index * 3_000)))

        signal = OpeningImpulseStrategy(settings).evaluate(state)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.strategy, "opening_impulse")
        self.assertEqual(signal.side, "BUY")

    def test_opening_impulse_exit_on_momentum_fade(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"], regular_market_only=False)
        strategy = OpeningImpulseStrategy(settings)
        state = SymbolState("AAPL")
        state.quotes = deque(
            [
                Quote("AAPL", bid=101.05, ask=101.07, bid_size=20, ask_size=20, timestamp_ms=10_000),
                Quote("AAPL", bid=101.00, ask=101.02, bid_size=20, ask_size=20, timestamp_ms=12_000),
                Quote("AAPL", bid=100.97, ask=100.99, bid_size=20, ask_size=20, timestamp_ms=14_000),
                Quote("AAPL", bid=100.96, ask=100.98, bid_size=20, ask_size=20, timestamp_ms=16_000),
            ],
            maxlen=2400,
        )
        state.quote = state.quotes[-1]
        state.last_event_kind = "quote"
        state.last_event_ms = state.quote.timestamp_ms

        broker = LocalPaperExecutor(PositionTracker(settings))
        broker.tracker.positions["AAPL"] = Position(
            symbol="AAPL",
            strategy="opening_impulse",
            shares=10,
            entry_price=101.0,
            entry_ms=5_000,
            target_price=103.02,
            stop_price=100.4,
        )

        decision = strategy.should_exit(state, broker.tracker.positions["AAPL"])

        self.assertIsNotNone(decision)
        self.assertIn("momentum", decision.reason)

    def test_build_strategies_returns_enabled_strategies(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            strategy_names=["spike", "opening_impulse"],
        )

        strategies = build_strategies(settings)

        self.assertEqual([strategy.name for strategy in strategies], ["spike", "opening_impulse"])

    def test_risk_rejects_entries_outside_regular_market_hours(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"])
        signal = SpikeStrategy(settings).evaluate(self._spike_state(market_ms(2026, 4, 24, 16, 1)))

        decision = RiskManager(settings).check_entry(signal, set(), 0)

        self.assertFalse(decision.allowed)
        self.assertIn("regular market", decision.reason)

    def test_risk_allows_entries_during_regular_market_hours(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"])
        signal = SpikeStrategy(settings).evaluate(self._spike_state(market_ms(2026, 4, 24, 10, 0)))

        decision = RiskManager(settings).check_entry(signal, set(), 0)

        self.assertTrue(decision.allowed)

    def test_risk_rejects_entries_during_close_flatten_window(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"], flatten_before_close_minutes=5)
        signal = SpikeStrategy(settings).evaluate(self._spike_state(market_ms(2026, 4, 24, 15, 55)))

        decision = RiskManager(settings).check_entry(signal, set(), 0)

        self.assertFalse(decision.allowed)
        self.assertIn("flatten", decision.reason)

    @staticmethod
    def _spike_state(base_ms: int) -> SymbolState:
        state = SymbolState("AAPL")
        state.update_quote(Quote("AAPL", bid=100.00, ask=100.05, bid_size=10, ask_size=10, timestamp_ms=base_ms))
        for index in range(6):
            state.add_bar(bar("AAPL", close=100.0, volume=100, end_ms=base_ms + (index * 1000)))
        state.add_bar(bar("AAPL", close=100.40, volume=350, end_ms=base_ms + 7000))
        return state


class FakeOrder:
    def __init__(self, order_id: str, status: str, filled_qty: str = "0", filled_avg_price: str | None = None):
        self.id = order_id
        self.status = status
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price


class FakeTrading:
    def __init__(self, orders: list[FakeOrder]):
        self.orders = orders
        self.cancel_called = False

    def cancel_order_by_id(self, order_id: str) -> None:
        self.cancel_called = True

    def get_order_by_id(self, order_id: str) -> FakeOrder:
        return self.orders.pop(0)


class FakeClients:
    def __init__(self, orders: list[FakeOrder]):
        self.trading = FakeTrading(orders)


if __name__ == "__main__":
    unittest.main()
