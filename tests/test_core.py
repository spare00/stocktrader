import unittest

from candle import SymbolState
from config import Settings
from execution import PaperBroker
from models import Bar, Quote
from risk import RiskManager
from strategy import SpikeStrategy


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
        settings = Settings(massive_api_key="test", symbols=["AAPL"])
        state = SymbolState("AAPL")
        state.update_quote(Quote("AAPL", bid=100.00, ask=100.05, bid_size=10, ask_size=10, timestamp_ms=1))

        for index in range(6):
            state.add_bar(bar("AAPL", close=100.0, volume=100, end_ms=index * 1000))
        state.add_bar(bar("AAPL", close=100.40, volume=350, end_ms=7000))

        signal = SpikeStrategy(settings).evaluate(state)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "BUY")

    def test_risk_rejects_short_entries(self):
        settings = Settings(massive_api_key="test", symbols=["AAPL"])
        state = SymbolState("AAPL")
        for index in range(6):
            state.add_bar(bar("AAPL", close=100.0, volume=100, end_ms=index * 1000))
        state.add_bar(bar("AAPL", close=99.60, volume=350, end_ms=7000))
        signal = SpikeStrategy(settings).evaluate(state)

        decision = RiskManager(settings).check_entry(signal, set(), 0)

        self.assertFalse(decision.allowed)
        self.assertIn("short", decision.reason)

    def test_paper_broker_exits_at_target(self):
        settings = Settings(massive_api_key="test", symbols=["AAPL"], target_profit_pct=0.01)
        broker = PaperBroker(settings)
        state = SymbolState("AAPL")
        for index in range(6):
            state.add_bar(bar("AAPL", close=100.0, volume=100, end_ms=index * 1000))
        state.add_bar(bar("AAPL", close=100.40, volume=350, end_ms=7000))
        signal = SpikeStrategy(settings).evaluate(state)

        broker.buy(signal)
        exit_bar = Bar("AAPL", open=100.50, high=101.50, low=100.20, close=101.20, volume=200, vwap=101.0, start_ms=8000, end_ms=9000)
        fill = broker.manage_exit(exit_bar)

        self.assertIsNotNone(fill)
        self.assertEqual(fill.side, "SELL")
        self.assertGreater(fill.pnl, 0)


if __name__ == "__main__":
    unittest.main()
