import contextlib
import asyncio
import os
import unittest
import sys
import types
import tempfile
import logging
import json
from collections import deque
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from candle import SymbolState
from config import Settings, load_settings
import alpaca_client
from alpaca_stream import (
    AlpacaRestPollingStream,
    AlpacaStockStream,
    AlpacaStreamConnectionLimitError,
    AlpacaStreamEndedError,
    AlpacaStreamLock,
    build_market_data_stream,
)
import execution as execution_module
from execution import AlpacaPaperExecutor, LocalPaperExecutor, Position, PositionTracker
import main as trading_main
from modules.symbol_manager import SymbolManager
from models import Bar, NewsEvent, Quote, Signal
from opening_plan import (
    DEFAULT_OPENING_PLAN_FILE,
    PLAN_SETTING_MAP,
    apply_opening_plan,
    default_plan_file_for_strategy,
    plan_overrides,
    selector_command_for_strategy,
)


@contextlib.contextmanager
def _without_plan_setting_env():
    """Drop PLAN_SETTING_MAP env vars so plan_overrides is not masked by the shell."""
    saved = {k: os.environ.pop(k) for k in PLAN_SETTING_MAP if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)
from risk import RiskManager
from runtime_safety import flatten_on_shutdown
import strategy_selectors.select_market_universe as select_market_universe
import scripts.analyze_trade_journal as analyze_trade_journal
import strategy_selectors.select_gap_and_go as select_gap_and_go
import strategy_selectors.select_maha7 as select_maha7
import strategy_selectors.select_macd_early_impulse as select_macd_early_impulse
import strategy_selectors.select_stoch_macd_reversal as select_stoch_macd_reversal
from strategy_selectors.select_market_universe import daily_metrics, score_symbol
import strategy_selectors.select_opening_impulse as select_opening_impulse
import strategy_selectors.select_steady_intraday as select_steady_intraday
from strategy_selectors.select_opening_impulse import DEFAULT_UNIVERSE, daily_gap_score, load_universe, opening_session_metrics, previous_session_dates, recent_compression_score, score_candidate, usable_quote
from strategies import available_strategy_names, build_strategies
from strategies.gap_and_go import GapAndGoStrategy
from strategies.macd_early_impulse import MACDEarlyImpulseStrategy
from strategies.maha7 import Maha7Strategy
from strategies.opening_impulse import OpeningImpulseStrategy
from strategies.spike import SpikeStrategy
from strategies.steady_intraday import SteadyIntradayStrategy
from strategies.stoch_macd_reversal import StochMACDReversalStrategy


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


def opening_bar(
    symbol: str,
    open_price: float,
    high: float,
    close: float,
    volume: float,
    start_ms: int,
) -> Bar:
    return Bar(
        symbol=symbol,
        open=open_price,
        high=high,
        low=min(open_price, close),
        close=close,
        volume=volume,
        vwap=close,
        start_ms=start_ms,
        end_ms=start_ms + 60_000,
    )


def daily_bar_with_volume(symbol: str, close: float, low: float, high: float, volume: float, start_ms: int) -> Bar:
    return Bar(
        symbol=symbol,
        open=close,
        high=high,
        low=low,
        close=close,
        volume=volume,
        vwap=close,
        start_ms=start_ms,
        end_ms=start_ms + 86_400_000,
    )


def daily_bar(symbol: str, close: float, low: float, high: float, start_ms: int) -> Bar:
    return Bar(
        symbol=symbol,
        open=close,
        high=high,
        low=low,
        close=close,
        volume=100_000,
        vwap=close,
        start_ms=start_ms,
        end_ms=start_ms + 86_400_000,
    )


def opening_candidate(
    symbol: str = "AAPL",
    closes: tuple[float, float] = (102.0, 102.0),
    highs: tuple[float, float] = (103.0, 103.0),
    volumes: tuple[float, float] = (50_000, 60_000),
) -> list[Bar]:
    return [
        opening_bar(symbol, 100.0, highs[0], closes[0], volumes[0], market_ms(2026, 4, 23, 9, 30)),
        opening_bar(symbol, 100.0, highs[1], closes[1], volumes[1], market_ms(2026, 4, 24, 9, 45)),
    ]


def uptrend_daily_context(symbol: str = "AAPL") -> list[Bar]:
    return [
        daily_bar(symbol, 100.0, 98.0, 101.0, market_ms(2026, 4, 20, 9, 30)),
        daily_bar(symbol, 101.0, 99.0, 102.0, market_ms(2026, 4, 21, 9, 30)),
        daily_bar(symbol, 102.0, 100.0, 103.0, market_ms(2026, 4, 22, 9, 30)),
        daily_bar(symbol, 103.0, 101.0, 104.0, market_ms(2026, 4, 23, 9, 30)),
        daily_bar(symbol, 104.0, 102.0, 105.0, market_ms(2026, 4, 24, 9, 30)),
    ]


class CoreTradingTests(unittest.TestCase):
    def setUp(self):
        self._old_trade_journal_file = execution_module.TRADE_JOURNAL_FILE
        self._trade_journal_tmpdir = tempfile.TemporaryDirectory()
        execution_module.TRADE_JOURNAL_FILE = Path(self._trade_journal_tmpdir.name) / "logs" / "trade_journal.jsonl"

    def tearDown(self):
        execution_module.TRADE_JOURNAL_FILE = self._old_trade_journal_file
        self._trade_journal_tmpdir.cleanup()

    def test_opening_plan_applies_symbols_and_bounded_settings(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL", "MSFT"],
            max_open_positions=2,
            max_position_value=2_500.0,
            stop_loss_pct=0.005,
        )
        plan = {
            "symbols": ["intc", "PANW", "intc"],
            "settings": {
                "MAX_OPEN_POSITIONS": 5,
                "MAX_POSITION_VALUE": 10_000.0,
                "STOP_LOSS_PCT": 0.02,
                "TARGET_PROFIT_PCT": 0.5,
                "OPENING_IMPULSE_VOLUME_RATIO": 0.5,
                "REGULAR_MARKET_ONLY": False,
            },
        }

        # plan_overrides defers to os.environ for PLAN_SETTING_MAP keys; clear so the test is
        # independent of the developer shell (e.g. MAX_OPEN_POSITIONS exported globally).
        with _without_plan_setting_env():
            overrides = plan_overrides(settings, plan)

        self.assertEqual(overrides["symbols"], ["INTC", "PANW"])
        self.assertEqual(overrides["max_open_positions"], 2)
        self.assertEqual(overrides["max_position_value"], 2_500.0)
        self.assertEqual(overrides["stop_loss_pct"], 0.005)
        self.assertEqual(overrides["target_profit_pct"], 0.02)
        self.assertEqual(overrides["opening_impulse_volume_ratio"], 1.5)
        self.assertNotIn("regular_market_only", overrides)

    def test_load_settings_reads_only_active_strategy_env(self):
        with patch.dict(
            "os.environ",
            {
                "ALPACA_API_KEY": "key",
                "ALPACA_SECRET_KEY": "secret",
                "STRATEGIES": "gap_and_go",
                "GAP_AND_GO_END_MINUTE": "45",
                "OPENING_IMPULSE_END_MINUTE": "360",
            },
            clear=True,
        ):
            settings = load_settings()

        self.assertEqual(settings.strategy_names, ["gap_and_go"])
        self.assertEqual(settings.gap_and_go_end_minute, 45)
        self.assertEqual(settings.opening_impulse_end_minute, 150)

    def test_load_settings_can_read_common_env_only(self):
        with patch.dict(
            "os.environ",
            {
                "ALPACA_API_KEY": "key",
                "ALPACA_SECRET_KEY": "secret",
                "SYMBOLS": "AAPL,MSFT",
                "ALPACA_MARKET_DATA_MODE": "rest",
                "ALPACA_MARKET_DATA_POLL_SECONDS": "7.5",
                "GAP_AND_GO_END_MINUTE": "45",
            },
            clear=True,
        ):
            settings = load_settings(strategy_names=[], validate=False)

        self.assertEqual(settings.strategy_names, [])
        self.assertEqual(settings.symbols, ["AAPL", "MSFT"])
        self.assertEqual(settings.alpaca_market_data_mode, "rest")
        self.assertEqual(settings.alpaca_market_data_poll_seconds, 7.5)
        self.assertEqual(settings.gap_and_go_end_minute, 30)

    def test_load_settings_reads_spike_window_only_when_spike_active(self):
        with patch.dict(
            "os.environ",
            {
                "ALPACA_API_KEY": "key",
                "ALPACA_SECRET_KEY": "secret",
                "STRATEGIES": "spike",
                "SPIKE_START_MINUTE": "15",
                "SPIKE_END_MINUTE": "120",
                "GAP_AND_GO_END_MINUTE": "45",
            },
            clear=True,
        ):
            settings = load_settings()

        self.assertEqual(settings.strategy_names, ["spike"])
        self.assertEqual(settings.spike_start_minute, 15)
        self.assertEqual(settings.spike_end_minute, 120)
        self.assertEqual(settings.gap_and_go_end_minute, 30)

    def test_position_entry_initializes_trailing_state(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            target_profit_pct=0.01,
        )
        tracker = PositionTracker(settings)
        signal = Signal(
            strategy="opening_impulse",
            symbol="AAPL",
            side="BUY",
            price=100.0,
            timestamp_ms=1_000,
            change_pct=0.01,
            volume_ratio=1.0,
            spread_bps=1.0,
            reason="test",
        )

        tracker.record_entry(signal, shares=10, fill_price=100.0, reason="test")

        self.assertEqual(tracker.positions["AAPL"].target_price, 101.0)
        self.assertEqual(tracker.positions["AAPL"].stop_price, 99.5)
        self.assertEqual(tracker.positions["AAPL"].max_price, 100.0)
        self.assertEqual(tracker.positions["AAPL"].last_high_ts, 1_000)

    def test_opening_plan_accepts_symbol_objects_from_ai(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"])
        plan = {
            "symbols": [
                {"symbol": "intc", "reason": "best candidate"},
                {"symbol": "HAL", "side": "long"},
            ]
        }

        overrides = plan_overrides(settings, plan)

        self.assertEqual(overrides["symbols"], ["INTC", "HAL"])

    def test_opening_plan_file_updates_settings(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"], max_open_positions=2)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "opening_plan.json"
            path.write_text(
                '{"symbols":["INTC"],"settings":{"MAX_OPEN_POSITIONS":1,"OPENING_IMPULSE_CHANGE_PCT":0.008}}'
            )

            with _without_plan_setting_env():
                updated = apply_opening_plan(settings, path)

        self.assertEqual(updated.symbols, ["INTC"])
        self.assertEqual(updated.max_open_positions, 1)
        self.assertEqual(updated.opening_impulse_change_pct, 0.008)

    def test_opening_plan_does_not_override_explicit_env_settings(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            max_open_positions=8,
            opening_impulse_change_pct=0.012,
        )
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            "os.environ",
            {
                "MAX_OPEN_POSITIONS": "8",
                "OPENING_IMPULSE_CHANGE_PCT": "0.012",
            },
            clear=False,
        ):
            path = Path(tmpdir) / "opening_plan.json"
            path.write_text(
                '{"symbols":["INTC"],"settings":{"MAX_OPEN_POSITIONS":2,"OPENING_IMPULSE_CHANGE_PCT":0.008}}'
            )

            updated = apply_opening_plan(settings, path)

        self.assertEqual(updated.symbols, ["INTC"])
        self.assertEqual(updated.max_open_positions, 8)
        self.assertEqual(updated.opening_impulse_change_pct, 0.012)

    def test_opening_plan_does_not_override_symbols_when_symbols_env_set(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["RIG"])
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            "os.environ",
            {"SYMBOLS": "RIG"},
            clear=False,
        ):
            path = Path(tmpdir) / "opening_plan.json"
            path.write_text('{"symbols":["INTC","PANW"],"settings":{}}')

            updated = apply_opening_plan(settings, path)

        self.assertEqual(updated.symbols, ["RIG"])

    def test_opening_plan_applies_when_symbols_env_empty_allows_plan(self):
        """When SYMBOLS is unset/empty, plan tickers apply."""
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=[],
        )
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            "os.environ",
            {"SYMBOLS": ""},
            clear=False,
        ):
            path = Path(tmpdir) / "opening_plan.json"
            path.write_text('{"symbols":["INTC","PANW"],"settings":{}}')

            updated = apply_opening_plan(settings, path)

        self.assertEqual(updated.symbols, ["INTC", "PANW"])

    def test_empty_global_symbols_can_run_with_strategy_local_plan_symbols(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=[],
            strategy_names=["steady_intraday"],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / "steady_intraday_plan.json"
            plan_path.write_text('{"symbols":["AAA","BBB"]}')

            with patch.object(trading_main, "default_plan_file_for_strategy", return_value=plan_path):
                strategy_symbols = trading_main.load_strategy_local_symbols(settings)

        initial_symbols = sorted(set(settings.symbols).union(*(set(symbols) for symbols in strategy_symbols.values())))
        self.assertEqual(settings.symbols, [])
        self.assertEqual(strategy_symbols, {"steady_intraday": ["AAA", "BBB"]})
        self.assertEqual(initial_symbols, ["AAA", "BBB"])

    def test_explicit_plan_path_loads_single_strategy_local_symbols(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=[],
            strategy_names=["macd_early_impulse"],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / "macd_plan.json"
            plan_path.write_text('{"symbols":["MSFT","NVDA"]}')

            strategy_symbols = trading_main.load_strategy_local_symbols(
                settings,
                {"macd_early_impulse": plan_path},
            )

        self.assertEqual(settings.symbols, [])
        self.assertEqual(strategy_symbols, {"macd_early_impulse": ["MSFT", "NVDA"]})

    def test_default_opening_plan_path_is_strategy_specific(self):
        self.assertEqual(DEFAULT_OPENING_PLAN_FILE, Path("data/opening_impulse_plan.json"))
        self.assertEqual(default_plan_file_for_strategy("gap_and_go"), Path("data/gap_and_go_plan.json"))
        self.assertEqual(default_plan_file_for_strategy("maha7"), Path("data/maha7_plan.json"))
        self.assertEqual(default_plan_file_for_strategy("steady_intraday"), Path("data/steady_intraday_plan.json"))
        self.assertEqual(
            selector_command_for_strategy("gap_and_go"),
            ".venv/bin/python strategy_selectors/select_gap_and_go.py --top 5",
        )
        self.assertEqual(
            selector_command_for_strategy("maha7"),
            ".venv/bin/python strategy_selectors/select_maha7.py --top 12",
        )
        self.assertEqual(
            selector_command_for_strategy("steady_intraday"),
            ".venv/bin/python strategy_selectors/select_steady_intraday.py --top 12",
        )
        self.assertEqual(
            selector_command_for_strategy("stoch_macd_reversal"),
            ".venv/bin/python strategy_selectors/select_stoch_macd_reversal.py --top 12",
        )

    def test_opening_selector_ai_plan_is_bounded_to_screen_candidates(self):
        screen_result = {
            "as_of": "2026-04-28T08:00:00-04:00",
            "candidates": [
                {"symbol": "AAPL", "score": 6.0, "quality_flags": []},
                {"symbol": "MSFT", "score": 5.0, "quality_flags": ["weak spread"]},
            ],
        }

        validated = select_opening_impulse.validated_opening_plan(
            {
                "adjustments": {
                    "MSFT": {"ai_score_delta": 1.5, "ai_reason": "Cleaner opening structure"},
                    "GONE": {"ai_score_delta": 2.0, "ai_reason": "should be ignored"},
                },
                "rejected": ["GONE"],
                "settings": {},
                "risk_note": "test",
            },
            screen_result,
            limit=2,
        )

        self.assertEqual(validated["symbols"], ["MSFT", "AAPL"])
        self.assertEqual(validated["ranked"][0]["symbol"], "MSFT")
        self.assertEqual(validated["ranked"][0]["base_score"], 5.0)
        self.assertEqual(validated["ranked"][0]["ai_score_delta"], 1.5)
        self.assertEqual(validated["ranked"][0]["score"], 6.5)
        self.assertEqual(validated["ranked"][0]["ai_reason"], "Cleaner opening structure")

    def test_trade_journal_analyzer_summarizes_round_trips(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            journal = Path(tmpdir) / "trade_journal.jsonl"
            rows = [
                {
                    "event": "buy",
                    "symbol": "MU",
                    "strategy": "opening_impulse",
                    "shares": 4,
                    "price": 100.0,
                    "pnl": 0,
                    "reason": "entry",
                    "timestamp_ms": 1_000,
                },
                {
                    "event": "mark",
                    "symbol": "MU",
                    "strategy": "opening_impulse",
                    "shares": 0,
                    "price": 102.0,
                    "pnl": 0,
                    "reason": "high water mark",
                    "timestamp_ms": 6_000,
                },
                {
                    "event": "sell",
                    "symbol": "MU",
                    "strategy": "opening_impulse",
                    "shares": 4,
                    "price": 101.0,
                    "pnl": 4.0,
                    "reason": "target profit | alpaca_order_id=sell-1",
                    "r_multiple": 2.0,
                    "cumulative_daily_pnl": 4.0,
                    "timestamp_ms": 11_000,
                },
                {
                    "event": "buy",
                    "symbol": "CRWD",
                    "strategy": "opening_impulse",
                    "shares": 2,
                    "price": 50.0,
                    "pnl": 0,
                    "reason": "entry",
                    "timestamp_ms": 20_000,
                },
                {
                    "event": "mark",
                    "symbol": "CRWD",
                    "strategy": "opening_impulse",
                    "shares": 0,
                    "price": 49.0,
                    "pnl": 0,
                    "reason": "low water mark",
                    "timestamp_ms": 25_000,
                },
                {
                    "event": "sell",
                    "symbol": "CRWD",
                    "strategy": "opening_impulse",
                    "shares": 2,
                    "price": 49.5,
                    "pnl": -1.0,
                    "reason": "momentum stall | alpaca_order_id=sell-2",
                    "r_multiple": -1.0,
                    "cumulative_daily_pnl": 3.0,
                    "timestamp_ms": 30_000,
                },
            ]
            journal.write_text("\n".join(json.dumps(row) for row in rows))

            summary = analyze_trade_journal.analyze(journal)

            self.assertEqual(summary["trades"], 2)
            self.assertEqual(summary["wins"], 1)
            self.assertEqual(summary["losses"], 1)
            self.assertAlmostEqual(summary["total_pnl"], 3.0)
            self.assertAlmostEqual(summary["win_rate"], 0.5)
            self.assertAlmostEqual(summary["expectancy_r"], 0.5)
            self.assertAlmostEqual(summary["average_pnl_pct"], 0.0)
            self.assertAlmostEqual(summary["average_mfe_pct"], 0.01)
            self.assertAlmostEqual(summary["average_mae_pct"], -0.01)
            self.assertAlmostEqual(summary["average_missed_profit_pct"], 0.01)
            self.assertEqual(summary["by_exit_reason"]["target profit"]["trades"], 1)
            self.assertAlmostEqual(summary["by_exit_reason"]["target profit"]["expectancy_r"], 2.0)
            self.assertAlmostEqual(summary["by_exit_reason"]["target profit"]["average_pnl_pct"], 0.01)
            self.assertAlmostEqual(summary["by_exit_reason"]["target profit"]["average_mfe_pct"], 0.02)
            self.assertAlmostEqual(summary["by_exit_reason"]["target profit"]["average_hold_seconds"], 10.0)
            self.assertEqual(summary["by_exit_reason"]["momentum stall"]["trades"], 1)
            self.assertEqual(summary["by_symbol"]["MU"]["total_pnl"], 4.0)
            self.assertAlmostEqual(summary["by_day"]["1969-12-31"]["expectancy_r"], 0.5)
            self.assertAlmostEqual(summary["by_day"]["1969-12-31"]["max_drawdown"], 1.0)
            self.assertAlmostEqual(summary["best_trade"]["mfe_pct"], 0.02)
            self.assertAlmostEqual(summary["worst_trade"]["mae_pct"], -0.02)

    def test_trade_journal_analyzer_allocates_partial_exit_pnl(self):
        events = [
            analyze_trade_journal.TradeEvent("buy", "AAPL", 1_000, 10, 100.0, 0.0, "opening_impulse", "entry", "buy-1"),
            analyze_trade_journal.TradeEvent("mark", "AAPL", 3_000, 0, 102.0, 0.0, "opening_impulse", "mark", ""),
            analyze_trade_journal.TradeEvent("sell", "AAPL", 6_000, 4, 101.0, 4.0, "opening_impulse", "partial", "sell-1"),
            analyze_trade_journal.TradeEvent("mark", "AAPL", 8_000, 0, 98.0, 0.0, "opening_impulse", "mark", ""),
            analyze_trade_journal.TradeEvent("sell", "AAPL", 11_000, 6, 99.0, -6.0, "opening_impulse", "stop loss", "sell-2"),
        ]

        round_trips, unmatched = analyze_trade_journal.build_round_trips(events)
        summary = analyze_trade_journal.summarize(round_trips, unmatched)

        self.assertEqual(summary["trades"], 2)
        self.assertEqual(summary["unmatched_events"], [])
        self.assertAlmostEqual(summary["total_pnl"], -2.0)
        self.assertEqual([trade.shares for trade in round_trips], [4, 6])
        self.assertAlmostEqual(round_trips[0].hold_seconds, 5.0)
        self.assertAlmostEqual(round_trips[0].pnl_pct, 0.01)
        self.assertAlmostEqual(round_trips[0].mfe_pct, 0.02)
        self.assertAlmostEqual(round_trips[1].mae_pct, -0.02)

    def test_trade_journal_analyzer_groups_by_strategy_and_day(self):
        events = [
            analyze_trade_journal.TradeEvent(
                "buy", "AAPL", market_ms(2026, 4, 28, 9, 31), 10, 100.0, 0.0, "opening_impulse", "entry", "buy-1"
            ),
            analyze_trade_journal.TradeEvent(
                "sell", "AAPL", market_ms(2026, 4, 28, 9, 40), 10, 101.0, 10.0, "opening_impulse", "target profit", "sell-1"
            ),
            analyze_trade_journal.TradeEvent(
                "buy", "MSFT", market_ms(2026, 4, 29, 9, 35), 5, 200.0, 0.0, "gap_and_go", "entry", "buy-2"
            ),
            analyze_trade_journal.TradeEvent(
                "sell", "MSFT", market_ms(2026, 4, 29, 9, 45), 5, 198.0, -10.0, "gap_and_go", "stop loss", "sell-2"
            ),
        ]

        round_trips, unmatched = analyze_trade_journal.build_round_trips(events)
        summary = analyze_trade_journal.summarize(round_trips, unmatched)

        self.assertEqual(summary["by_strategy"]["opening_impulse"]["trades"], 1)
        self.assertEqual(summary["by_strategy"]["gap_and_go"]["trades"], 1)
        self.assertEqual(summary["by_day"]["2026-04-28"]["total_pnl"], 10.0)
        self.assertEqual(summary["by_day"]["2026-04-29"]["total_pnl"], -10.0)
        self.assertEqual(summary["by_day_strategy"]["2026-04-28"]["opening_impulse"]["trades"], 1)
        self.assertEqual(summary["by_day_strategy"]["2026-04-29"]["gap_and_go"]["trades"], 1)
        self.assertEqual(summary["best_trade"]["trade_day"], "2026-04-28")

    def test_trade_journal_analyzer_win_rate_by_entry_noon_et(self):
        day = 2026, 4, 28
        y, m, d = day
        events = [
            # Before noon: win
            analyze_trade_journal.TradeEvent(
                "buy", "AAPL", market_ms(y, m, d, 9, 45), 10, 100.0, 0.0, "opening_impulse", "entry", "buy-1"
            ),
            analyze_trade_journal.TradeEvent(
                "sell", "AAPL", market_ms(y, m, d, 10, 0), 10, 101.0, 10.0, "opening_impulse", "target", "sell-1"
            ),
            # After noon: loss
            analyze_trade_journal.TradeEvent(
                "buy", "MSFT", market_ms(y, m, d, 12, 15), 5, 200.0, 0.0, "opening_impulse", "entry", "buy-2"
            ),
            analyze_trade_journal.TradeEvent(
                "sell", "MSFT", market_ms(y, m, d, 12, 20), 5, 199.0, -5.0, "opening_impulse", "stop", "sell-2"
            ),
            # Exactly 12:00 ET: counts as "from 12:00"
            analyze_trade_journal.TradeEvent(
                "buy", "NVDA", market_ms(y, m, d, 12, 0), 1, 50.0, 0.0, "opening_impulse", "entry", "buy-3"
            ),
            analyze_trade_journal.TradeEvent(
                "sell", "NVDA", market_ms(y, m, d, 12, 5), 1, 51.0, 1.0, "opening_impulse", "target", "sell-3"
            ),
        ]
        round_trips, unmatched = analyze_trade_journal.build_round_trips(events)
        self.assertEqual(unmatched, [])
        summary = analyze_trade_journal.summarize(round_trips, unmatched)
        b = summary["by_entry_time_et"]["before_12_00_et"]
        a = summary["by_entry_time_et"]["from_12_00_et"]
        self.assertEqual(b["trades"], 1)
        self.assertEqual(b["wins"], 1)
        self.assertEqual(b["win_rate"], 1.0)
        self.assertEqual(a["trades"], 2)
        self.assertEqual(a["wins"], 1)
        self.assertEqual(a["losses"], 1)
        self.assertEqual(a["win_rate"], 0.5)
        self.assertEqual(list(summary["by_day_entry_time_et"].keys()), ["2026-04-28"])
        d0 = summary["by_day_entry_time_et"]["2026-04-28"]
        self.assertEqual(d0["before_12_00_et"]["trades"], 1)
        self.assertEqual(d0["from_12_00_et"]["trades"], 2)

    def test_trade_journal_analyzer_win_rate_by_entry_noon_et_splits_multiple_days(self):
        """Per-day noon buckets use entry date in ET."""
        events = [
            analyze_trade_journal.TradeEvent(
                "buy", "AAPL", market_ms(2026, 4, 28, 9, 31), 10, 100.0, 0.0, "opening_impulse", "entry", "b1"
            ),
            analyze_trade_journal.TradeEvent(
                "sell", "AAPL", market_ms(2026, 4, 28, 9, 40), 10, 101.0, 5.0, "opening_impulse", "tp", "s1"
            ),
            analyze_trade_journal.TradeEvent(
                "buy", "MSFT", market_ms(2026, 4, 29, 14, 0), 5, 200.0, 0.0, "opening_impulse", "entry", "b2"
            ),
            analyze_trade_journal.TradeEvent(
                "sell", "MSFT", market_ms(2026, 4, 29, 14, 10), 5, 199.0, -5.0, "opening_impulse", "sl", "s2"
            ),
        ]
        round_trips, unmatched = analyze_trade_journal.build_round_trips(events)
        self.assertEqual(unmatched, [])
        summary = analyze_trade_journal.summarize(round_trips, unmatched)
        byd = summary["by_day_entry_time_et"]
        self.assertEqual(list(byd.keys()), ["2026-04-28", "2026-04-29"])
        self.assertEqual(byd["2026-04-28"]["before_12_00_et"]["trades"], 1)
        self.assertEqual(byd["2026-04-28"]["from_12_00_et"]["trades"], 0)
        self.assertEqual(byd["2026-04-29"]["before_12_00_et"]["trades"], 0)
        self.assertEqual(byd["2026-04-29"]["from_12_00_et"]["trades"], 1)

    def test_trade_journal_analyze_filters_by_strategy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            journal = Path(tmpdir) / "trade_journal.jsonl"
            y, m, d = 2026, 4, 28
            rows = [
                {
                    "event": "buy",
                    "symbol": "AAPL",
                    "strategy": "opening_impulse",
                    "shares": 10,
                    "price": 100.0,
                    "pnl": 0,
                    "reason": "entry",
                    "timestamp_ms": market_ms(y, m, d, 9, 31),
                    "order_id": "b1",
                },
                {
                    "event": "sell",
                    "symbol": "AAPL",
                    "strategy": "opening_impulse",
                    "shares": 10,
                    "price": 101.0,
                    "pnl": 10.0,
                    "reason": "tp",
                    "timestamp_ms": market_ms(y, m, d, 9, 40),
                    "order_id": "s1",
                },
                {
                    "event": "buy",
                    "symbol": "MSFT",
                    "strategy": "gap_and_go",
                    "shares": 5,
                    "price": 200.0,
                    "pnl": 0,
                    "reason": "entry",
                    "timestamp_ms": market_ms(y, m, d, 10, 0),
                    "order_id": "b2",
                },
                {
                    "event": "sell",
                    "symbol": "MSFT",
                    "strategy": "gap_and_go",
                    "shares": 5,
                    "price": 198.0,
                    "pnl": -10.0,
                    "reason": "sl",
                    "timestamp_ms": market_ms(y, m, d, 10, 10),
                    "order_id": "s2",
                },
            ]
            journal.write_text("\n".join(json.dumps(row) for row in rows))

            all_summary = analyze_trade_journal.analyze(journal)
            self.assertEqual(all_summary["trades"], 2)

            oi = analyze_trade_journal.analyze(journal, strategy="opening_impulse")
            self.assertEqual(oi["trades"], 1)
            self.assertAlmostEqual(oi["total_pnl"], 10.0)
            self.assertEqual(list(oi["by_strategy"].keys()), ["opening_impulse"])

    def test_opening_impulse_screener_uses_prior_regular_opening_sessions(self):
        as_of = datetime(2026, 4, 27, 8, 0, tzinfo=MARKET_TZ)

        self.assertEqual(
            [value.isoformat() for value in previous_session_dates(as_of, 2)],
            ["2026-04-23", "2026-04-24"],
        )

        bars = [
            opening_bar("AAPL", 100.0, 101.0, 100.8, 50_000, market_ms(2026, 4, 23, 9, 30)),
            opening_bar("AAPL", 101.0, 103.0, 102.0, 60_000, market_ms(2026, 4, 24, 9, 45)),
            opening_bar("AAPL", 102.0, 120.0, 119.0, 1_000, market_ms(2026, 4, 24, 8, 0)),
            opening_bar("AAPL", 102.0, 130.0, 129.0, 1_000, market_ms(2026, 4, 24, 10, 5)),
        ]

        sessions = opening_session_metrics(bars, opening_minutes=30)
        result = score_candidate(
            symbol="AAPL",
            bars=bars,
            daily_bars=uptrend_daily_context(),
            quote=Quote("AAPL", bid=102.0, ask=102.04, bid_size=200, ask_size=200, timestamp_ms=0),
            opening_minutes=30,
            min_price=10.0,
            max_price=900.0,
            min_opening_days=2,
            min_opening_dollar_volume=1_000_000.0,
            min_impulse_bps=60.0,
            min_opening_range_bps=100.0,
            max_spread_bps=8.0,
            trend_lookback_days=5,
            min_trend_bps=50.0,
            min_reversal_bps=100.0,
            require_daily_context=True,
        )

        self.assertEqual(len(sessions), 2)
        self.assertEqual(result["opening_days"], 2)
        self.assertGreaterEqual(result["median_opening_high_move_bps"], 100.0)
        self.assertGreaterEqual(result["median_opening_range_bps"], 100.0)
        self.assertEqual(result["daily_context"], "uptrend")

    def test_opening_impulse_screener_uses_default_universe_file_when_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            universe_file = Path(tmpdir) / "opening_universe.txt"
            universe_file.write_text("msft,aapl,msft\n")

            self.assertEqual(load_universe(universe_file, ""), ["AAPL", "MSFT"])

    def test_opening_impulse_screener_falls_back_when_default_universe_file_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "missing.txt"

            self.assertEqual(load_universe(missing, ""), DEFAULT_UNIVERSE)

    def test_opening_impulse_screener_ignores_invalid_quote_snapshot(self):
        self.assertIsNone(usable_quote(Quote("AAPL", bid=102.0, ask=0.0, bid_size=0, ask_size=0, timestamp_ms=0)))
        self.assertIsNone(usable_quote(Quote("AAPL", bid=102.0, ask=101.0, bid_size=10, ask_size=10, timestamp_ms=0)))

    def test_opening_impulse_screener_penalizes_weak_daily_context(self):
        bars = opening_candidate()

        result = score_candidate(
            symbol="AAPL",
            bars=bars,
            daily_bars=[
                daily_bar("AAPL", 110.0, 109.0, 112.0, market_ms(2026, 4, 20, 9, 30)),
                daily_bar("AAPL", 108.0, 107.0, 110.0, market_ms(2026, 4, 21, 9, 30)),
                daily_bar("AAPL", 105.0, 104.0, 106.0, market_ms(2026, 4, 22, 9, 30)),
                daily_bar("AAPL", 102.0, 101.0, 104.0, market_ms(2026, 4, 23, 9, 30)),
                daily_bar("AAPL", 100.0, 99.0, 103.0, market_ms(2026, 4, 24, 9, 30)),
            ],
            quote=Quote("AAPL", bid=100.0, ask=100.05, bid_size=200, ask_size=200, timestamp_ms=0),
            opening_minutes=30,
            min_price=10.0,
            max_price=900.0,
            min_opening_days=2,
            min_opening_dollar_volume=1_000_000.0,
            min_impulse_bps=60.0,
            min_opening_range_bps=100.0,
            max_spread_bps=8.0,
            trend_lookback_days=5,
            min_trend_bps=50.0,
            min_reversal_bps=100.0,
            require_daily_context=True,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["daily_context"], "weak")
        self.assertTrue(any("weak daily context" in flag for flag in result["quality_flags"]))

    def test_opening_impulse_screener_rewards_follow_through_over_fade(self):
        common = {
            "symbol": "AAPL",
            "daily_bars": uptrend_daily_context(),
            "quote": Quote("AAPL", bid=104.0, ask=104.04, bid_size=200, ask_size=200, timestamp_ms=0),
            "opening_minutes": 30,
            "min_price": 10.0,
            "max_price": 900.0,
            "min_opening_days": 2,
            "min_opening_dollar_volume": 1_000_000.0,
            "min_impulse_bps": 60.0,
            "min_opening_range_bps": 100.0,
            "max_spread_bps": 8.0,
            "trend_lookback_days": 5,
            "min_trend_bps": 50.0,
            "min_reversal_bps": 100.0,
            "require_daily_context": True,
            "min_close_capture_ratio": -10.0,
            "min_positive_close_day_ratio": 0.0,
            "min_median_opening_close_bps": -200.0,
        }

        follow_through = score_candidate(
            bars=opening_candidate(closes=(102.8, 102.7), highs=(103.0, 103.0)),
            **common,
        )
        fade = score_candidate(
            bars=opening_candidate(closes=(99.7, 99.8), highs=(103.0, 103.0)),
            **common,
        )

        self.assertIsNotNone(follow_through)
        self.assertIsNotNone(fade)
        self.assertGreater(follow_through["score"], fade["score"])
        self.assertGreater(follow_through["close_capture_ratio"], fade["close_capture_ratio"])

    def test_opening_impulse_screener_penalizes_spike_and_fade_history(self):
        result = score_candidate(
            symbol="AAPL",
            bars=opening_candidate(closes=(99.7, 99.8), highs=(103.0, 103.0)),
            daily_bars=uptrend_daily_context(),
            quote=Quote("AAPL", bid=104.0, ask=104.04, bid_size=200, ask_size=200, timestamp_ms=0),
            opening_minutes=30,
            min_price=10.0,
            max_price=900.0,
            min_opening_days=2,
            min_opening_dollar_volume=1_000_000.0,
            min_impulse_bps=60.0,
            min_opening_range_bps=100.0,
            max_spread_bps=8.0,
            trend_lookback_days=5,
            min_trend_bps=50.0,
            min_reversal_bps=100.0,
            require_daily_context=True,
        )

        self.assertIsNotNone(result)
        self.assertLess(result["close_capture_ratio"], 0.1)
        self.assertTrue(any("close_capture_ratio" in flag for flag in result["quality_flags"]))

    def test_opening_impulse_screener_accepts_bottom_reversal_context(self):
        result = score_candidate(
            symbol="AAPL",
            bars=opening_candidate(),
            daily_bars=[
                daily_bar("AAPL", 100.0, 99.0, 101.0, market_ms(2026, 4, 20, 9, 30)),
                daily_bar("AAPL", 98.0, 96.0, 99.0, market_ms(2026, 4, 21, 9, 30)),
                daily_bar("AAPL", 96.0, 94.0, 97.0, market_ms(2026, 4, 22, 9, 30)),
                daily_bar("AAPL", 97.0, 95.0, 98.0, market_ms(2026, 4, 23, 9, 30)),
                daily_bar("AAPL", 99.0, 96.0, 100.0, market_ms(2026, 4, 24, 9, 30)),
            ],
            quote=Quote("AAPL", bid=99.5, ask=99.55, bid_size=200, ask_size=200, timestamp_ms=0),
            opening_minutes=30,
            min_price=10.0,
            max_price=900.0,
            min_opening_days=2,
            min_opening_dollar_volume=1_000_000.0,
            min_impulse_bps=60.0,
            min_opening_range_bps=100.0,
            max_spread_bps=8.0,
            trend_lookback_days=5,
            min_trend_bps=50.0,
            min_reversal_bps=100.0,
            require_daily_context=True,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["daily_context"], "bottom_reversal")
        self.assertEqual(result["pattern_score"], 7.0)

    def test_opening_impulse_screener_boosts_daily_state_patterns(self):
        common = {
            "symbol": "AAPL",
            "bars": opening_candidate(),
            "quote": Quote("AAPL", bid=104.0, ask=104.04, bid_size=200, ask_size=200, timestamp_ms=0),
            "opening_minutes": 30,
            "min_price": 10.0,
            "max_price": 900.0,
            "min_opening_days": 2,
            "min_opening_dollar_volume": 1_000_000.0,
            "min_impulse_bps": 60.0,
            "min_opening_range_bps": 100.0,
            "max_spread_bps": 8.0,
            "trend_lookback_days": 5,
            "min_trend_bps": 50.0,
            "min_reversal_bps": 100.0,
            "require_daily_context": True,
        }
        trend = score_candidate(daily_bars=uptrend_daily_context(), **common)
        selloff = score_candidate(
            daily_bars=[
                daily_bar("AAPL", 110.0, 109.0, 112.0, market_ms(2026, 4, 20, 9, 30)),
                daily_bar("AAPL", 108.0, 107.0, 110.0, market_ms(2026, 4, 21, 9, 30)),
                daily_bar("AAPL", 105.0, 104.0, 106.0, market_ms(2026, 4, 22, 9, 30)),
                daily_bar("AAPL", 102.0, 101.0, 104.0, market_ms(2026, 4, 23, 9, 30)),
                daily_bar("AAPL", 100.0, 99.0, 103.0, market_ms(2026, 4, 24, 9, 30)),
            ],
            **common,
        )

        self.assertEqual(trend["pattern_score"], 9.0)
        self.assertEqual(selloff["pattern_score"], 7.0)

    def test_opening_impulse_screener_boosts_gap_compression_and_opening_impulse_patterns(self):
        compression_bars = [
            daily_bar_with_volume("AAPL", 100.0, 94.0, 104.0, 1_000_000, market_ms(2026, 4, 22, 9, 30)),
            daily_bar_with_volume("AAPL", 100.0, 95.0, 103.0, 1_000_000, market_ms(2026, 4, 23, 9, 30)),
            daily_bar_with_volume("AAPL", 100.0, 96.0, 102.0, 1_000_000, market_ms(2026, 4, 24, 9, 30)),
            daily_bar_with_volume("AAPL", 100.0, 99.0, 101.0, 1_000_000, market_ms(2026, 4, 27, 9, 30)),
        ]
        gap_up_bars = [
            daily_bar_with_volume("AAPL", 100.0, 99.0, 101.0, 1_000_000, market_ms(2026, 4, 24, 9, 30)),
            Bar("AAPL", open=103.0, high=104.0, low=102.0, close=103.5, volume=1_000_000, vwap=103.0, start_ms=market_ms(2026, 4, 27, 9, 30), end_ms=market_ms(2026, 4, 27, 9, 31)),
        ]
        gap_down_bars = [
            daily_bar_with_volume("AAPL", 100.0, 99.0, 101.0, 1_000_000, market_ms(2026, 4, 24, 9, 30)),
            Bar("AAPL", open=97.0, high=98.0, low=96.0, close=97.5, volume=1_000_000, vwap=97.0, start_ms=market_ms(2026, 4, 27, 9, 30), end_ms=market_ms(2026, 4, 27, 9, 31)),
        ]

        result = score_candidate(
            symbol="AAPL",
            bars=opening_candidate(highs=(101.0, 101.0), closes=(100.8, 100.8)),
            daily_bars=compression_bars,
            quote=Quote("AAPL", bid=100.0, ask=100.04, bid_size=200, ask_size=200, timestamp_ms=0),
            opening_minutes=30,
            min_price=10.0,
            max_price=900.0,
            min_opening_days=2,
            min_opening_dollar_volume=1_000_000.0,
            min_impulse_bps=60.0,
            min_opening_range_bps=100.0,
            max_spread_bps=8.0,
            trend_lookback_days=5,
            min_trend_bps=50.0,
            min_reversal_bps=100.0,
            require_daily_context=True,
        )

        self.assertEqual(daily_gap_score(gap_up_bars), 3.0)
        self.assertEqual(daily_gap_score(gap_down_bars), 2.0)
        self.assertEqual(recent_compression_score(compression_bars), 2.0)
        self.assertGreaterEqual(result["pattern_score"], 4.0)

    def test_opening_impulse_screener_scores_weak_boundaries_instead_of_filtering(self):
        common = {
            "symbol": "AAPL",
            "bars": opening_candidate(),
            "daily_bars": uptrend_daily_context(),
            "quote": Quote("AAPL", bid=104.0, ask=104.04, bid_size=200, ask_size=200, timestamp_ms=0),
            "opening_minutes": 30,
            "min_price": 10.0,
            "max_price": 900.0,
            "min_opening_days": 2,
            "min_opening_dollar_volume": 1_000_000.0,
            "min_impulse_bps": 60.0,
            "min_opening_range_bps": 100.0,
            "max_spread_bps": 8.0,
            "trend_lookback_days": 5,
            "min_trend_bps": 50.0,
            "min_reversal_bps": 100.0,
            "require_daily_context": True,
        }

        cases = [
            ({"bars": opening_candidate(volumes=(100, 100))}, "opening dollar volume"),
            ({"bars": [opening_candidate()[0]]}, "opening_days"),
            ({"bars": []}, "opening_days"),
            ({"bars": opening_candidate(highs=(100.5, 100.5), closes=(100.4, 100.4))}, "opening range"),
            ({"quote": Quote("AAPL", bid=104.0, ask=105.0, bid_size=200, ask_size=200, timestamp_ms=0)}, "spread"),
        ]
        for override, expected_flag in cases:
            params = {**common, **override}
            with self.subTest(override=override):
                result = score_candidate(**params)
                self.assertIsNotNone(result)
                self.assertTrue(any(expected_flag in flag for flag in result["quality_flags"]))

    def test_opening_impulse_screener_falls_back_when_quote_is_invalid(self):
        result = score_candidate(
            symbol="AAPL",
            bars=opening_candidate(),
            daily_bars=uptrend_daily_context(),
            quote=Quote("AAPL", bid=104.0, ask=0.0, bid_size=0, ask_size=0, timestamp_ms=0),
            opening_minutes=30,
            min_price=10.0,
            max_price=900.0,
            min_opening_days=2,
            min_opening_dollar_volume=1_000_000.0,
            min_impulse_bps=60.0,
            min_opening_range_bps=100.0,
            max_spread_bps=8.0,
            trend_lookback_days=5,
            min_trend_bps=50.0,
            min_reversal_bps=100.0,
            require_daily_context=True,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["spread_bps"], 8.0)
        self.assertEqual(result["quote_size"], 0)

    def test_opening_universe_builder_scores_liquid_symbols_without_pattern_filtering(self):
        bars = [
            daily_bar_with_volume("AAPL", 100.0, 99.8, 100.2, 2_000_000, market_ms(2026, 4, 20, 9, 30)),
            daily_bar_with_volume("AAPL", 100.1, 99.9, 100.3, 2_200_000, market_ms(2026, 4, 21, 9, 30)),
            daily_bar_with_volume("AAPL", 100.2, 100.0, 100.4, 2_400_000, market_ms(2026, 4, 22, 9, 30)),
        ]
        result = score_symbol(
            symbol="AAPL",
            bars=bars,
            quote=Quote("AAPL", bid=100.0, ask=101.5, bid_size=100, ask_size=100, timestamp_ms=0),
            min_price=5.0,
            max_price=500.0,
            min_average_volume=1_000_000.0,
            max_spread_bps=12.0,
        )

        self.assertIsNotNone(result)
        self.assertGreater(daily_metrics(bars)["average_volume"], 1_000_000.0)
        self.assertEqual(result["symbol"], "AAPL")
        self.assertGreater(result["spread_bps"], 12.0)

    def test_opening_universe_builder_sorts_limits_and_writes_output(self):
        original_get_symbols = select_market_universe.get_active_tradable_symbols
        original_get_bars = select_market_universe.get_daily_bars
        original_get_quotes = select_market_universe.get_latest_quotes
        captured = {}

        def fake_get_symbols(settings, exchanges=None):
            captured["exchanges"] = exchanges
            return ["LOW", "HIGH", "FAIL"]

        def fake_get_bars(settings, symbols, lookback_days, batch_size):
            captured["lookback_days"] = lookback_days
            captured["batch_size"] = batch_size
            return {
                "LOW": [
                    daily_bar_with_volume("LOW", 50.0, 49.0, 52.0, 100_000, market_ms(2026, 4, 20, 9, 30)),
                    daily_bar_with_volume("LOW", 51.0, 50.0, 53.0, 100_000, market_ms(2026, 4, 21, 9, 30)),
                ],
                "HIGH": [
                    daily_bar_with_volume("HIGH", 100.0, 99.0, 101.0, 2_000_000, market_ms(2026, 4, 20, 9, 30)),
                    daily_bar_with_volume("HIGH", 100.5, 99.5, 101.5, 2_000_000, market_ms(2026, 4, 21, 9, 30)),
                ],
                "FAIL": [
                    daily_bar_with_volume("FAIL", 5.0, 4.9, 5.1, 500_000, market_ms(2026, 4, 20, 9, 30)),
                    daily_bar_with_volume("FAIL", 5.1, 5.0, 5.2, 500_000, market_ms(2026, 4, 21, 9, 30)),
                ],
            }

        def fake_get_quotes(settings, symbols, batch_size):
            raise AssertionError("quotes should be skipped")

        try:
            select_market_universe.get_active_tradable_symbols = fake_get_symbols
            select_market_universe.get_daily_bars = fake_get_bars
            select_market_universe.get_latest_quotes = fake_get_quotes
            with tempfile.TemporaryDirectory() as tmpdir:
                output = Path(tmpdir) / "opening_universe.txt"
                result = select_market_universe.build_universe(
                    types.SimpleNamespace(
                        top=1,
                        output=output,
                        lookback_days=2,
                        batch_size=2,
                        exchanges="NASDAQ,NYSE",
                        min_price=5.0,
                        max_price=500.0,
                        min_average_volume=1_000_000.0,
                        max_spread_bps=12.0,
                        skip_quotes=True,
                        alpaca_api_key="test",
                        alpaca_secret_key="test",
                    )
                )

                self.assertEqual(result["selected_symbols"], ["HIGH"])
                self.assertEqual(output.read_text(), "HIGH\n")
                self.assertEqual(captured["exchanges"], {"NASDAQ", "NYSE"})
                self.assertEqual(captured["lookback_days"], 2)
                self.assertEqual(captured["batch_size"], 2)
        finally:
            select_market_universe.get_active_tradable_symbols = original_get_symbols
            select_market_universe.get_daily_bars = original_get_bars
            select_market_universe.get_latest_quotes = original_get_quotes

    def test_opening_universe_builder_rejects_invalid_arguments(self):
        args = types.SimpleNamespace(top=0, lookback_days=2, batch_size=1)

        with self.assertRaises(ValueError):
            select_market_universe.build_universe(args)

    def test_maha7_selector_writes_plan_from_universe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            universe = Path(tmpdir) / "universe.txt"
            output = Path(tmpdir) / "maha7_plan.json"
            universe.write_text("AAPL,MSFT,AAPL\nNVDA # comment\nTSLA\n")
            settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"])
            start_ms = market_ms(2026, 4, 1, 9, 30)
            bars_by_symbol = {
                "AAPL": self._maha7_selector_bars("AAPL", 100.0, start_ms, final_pullback=True, volume=200_000),
                "MSFT": self._maha7_selector_bars("MSFT", 100.0, start_ms, final_pullback=False, volume=200_000),
                "NVDA": self._maha7_selector_bars("NVDA", 100.0, start_ms, final_pullback=True, volume=100_000),
            }
            quotes = {
                "AAPL": Quote("AAPL", bid=107.9, ask=108.0, bid_size=100, ask_size=100, timestamp_ms=start_ms),
                "MSFT": Quote("MSFT", bid=120.0, ask=120.2, bid_size=100, ask_size=100, timestamp_ms=start_ms),
                "NVDA": Quote("NVDA", bid=107.8, ask=108.0, bid_size=100, ask_size=100, timestamp_ms=start_ms),
            }

            plan = select_maha7.build_plan(
                select_maha7.load_universe(universe),
                2,
                bars_by_symbol=bars_by_symbol,
                quotes=quotes,
                settings=settings,
                min_dollar_volume=1_000_000,
            )
            select_maha7.write_plan(plan, output)

            self.assertEqual(plan["strategy"], "maha7")
            self.assertEqual(plan["symbols"][0], "AAPL")
            self.assertEqual(len(plan["ranked"]), 2)
            self.assertEqual(json.loads(output.read_text())["symbols"][0], "AAPL")

    def test_maha7_selector_scores_rsi_reclaim_and_vwap_distance(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"])
        start_ms = market_ms(2026, 4, 1, 9, 30)
        bars = self._maha7_reclaim_selector_bars("AAPL", start_ms)

        candidate = select_maha7.score_maha7_candidate(
            "AAPL",
            bars,
            Quote("AAPL", bid=103.45, ask=103.55, bid_size=100, ask_size=100, timestamp_ms=start_ms),
            settings,
            min_price=5.0,
            max_price=500.0,
            max_spread_bps=12.0,
            min_dollar_volume=1_000_000,
            pullback_max_distance_pct=0.03,
            max_extension_pct=0.08,
            stage="intraday",
        )

        self.assertIsNotNone(candidate)
        self.assertGreater(candidate.prev_rsi, 50)
        self.assertLess(candidate.prev_rsi, 55)
        self.assertGreater(candidate.rsi, 55)
        self.assertEqual(candidate.reclaim_score, 2.0)
        self.assertGreaterEqual(candidate.vwap_distance_pct, 0.002)
        self.assertTrue(candidate.pullback_reaction)

    def test_maha7_selector_keeps_symbols_with_short_history_as_penalty_rows(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"])
        ranked = select_maha7.rank_candidates(
            ["AAPL"],
            {"AAPL": []},
            {"AAPL": Quote("AAPL", bid=100.0, ask=100.1, bid_size=100, ask_size=100, timestamp_ms=0)},
            settings,
            top=5,
            min_price=5.0,
            max_price=500.0,
            max_spread_bps=12.0,
            min_dollar_volume=1_000_000,
            pullback_max_distance_pct=0.03,
            max_extension_pct=0.08,
            stage="daily",
        )

        self.assertEqual([item.symbol for item in ranked], ["AAPL"])
        self.assertIn("bar_history 0 < 21", ranked[0].quality_flags)

    def test_alpaca_bar_helper_skips_invalid_time_window(self):
        class FakeHistorical:
            def get_stock_bars(self, request):
                raise AssertionError("invalid time windows should not call Alpaca")

        clients = types.SimpleNamespace(historical=FakeHistorical(), feed="iex")
        start = datetime(2026, 4, 24, 9, 30, tzinfo=MARKET_TZ)
        end = datetime(2026, 4, 24, 8, 0, tzinfo=MARKET_TZ)

        bars = alpaca_client.get_bars_between(clients, ["AAPL"], object(), start, end)

        self.assertEqual(bars, {"AAPL": []})

    def test_maha7_selector_skips_intraday_before_market_open(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"])
        premarket = datetime(2026, 4, 24, 8, 0, tzinfo=MARKET_TZ)

        bars = select_maha7.get_today_minute_bars(settings, ["AAPL"], now=premarket)

        self.assertEqual(bars, {"AAPL": []})

    def test_steady_intraday_selector_writes_plan_from_universe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            universe = Path(tmpdir) / "universe.txt"
            output = Path(tmpdir) / "steady_intraday_plan.json"
            universe.write_text("NVDA,AAPL,NVDA\nMSFT\n")
            settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["NVDA"])
            start_ms = market_ms(2026, 4, 24, 9, 30)
            bars_by_symbol = {
                "NVDA": self._steady_intraday_selector_bars("NVDA", start_ms, trigger=True),
                "AAPL": self._steady_intraday_selector_bars("AAPL", start_ms, trigger=False),
                "MSFT": [],
            }
            quotes = {
                "NVDA": Quote("NVDA", bid=102.13, ask=102.17, bid_size=100, ask_size=100, timestamp_ms=start_ms),
                "AAPL": Quote("AAPL", bid=100.00, ask=100.06, bid_size=100, ask_size=100, timestamp_ms=start_ms),
            }

            plan = select_steady_intraday.build_plan(
                select_steady_intraday.load_universe(universe),
                2,
                bars_by_symbol=bars_by_symbol,
                quotes=quotes,
                settings=settings,
                stage="intraday",
                min_dollar_volume=1_000_000,
            )
            select_steady_intraday.write_plan(plan, output)

            self.assertEqual(plan["strategy"], "steady_intraday")
            self.assertEqual(plan["symbols"][0], "NVDA")
            self.assertEqual(plan["ranked"][0]["pullback_reclaim_ready"], True)
            self.assertEqual(json.loads(output.read_text())["symbols"][0], "NVDA")

    def test_steady_intraday_selector_skips_intraday_before_market_open(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"])
        premarket = datetime(2026, 4, 24, 8, 0, tzinfo=MARKET_TZ)

        bars = select_steady_intraday.get_today_minute_bars(settings, ["AAPL"], now=premarket)

        self.assertEqual(bars, {"AAPL": []})

    def test_steady_intraday_selector_history_threshold_matches_runtime(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            steady_intraday_min_bars=55,
            steady_intraday_ema_slow=50,
        )

        self.assertEqual(select_steady_intraday.required_intraday_bar_count(settings), 55)

    def test_steady_intraday_ai_selection_is_bounded_to_ranked_candidates(self):
        ranked = [
            {"symbol": "NVDA", "score": 5.0, "selection_stage": "intraday"},
            {"symbol": "AAPL", "score": 4.5, "selection_stage": "intraday"},
        ]
        ai_plan = {
            "selection_stage": "intraday",
            "adjustments": {
                "NVDA": {"ai_score_delta": -5.0, "ai_reason": "too extended"},
                "AAPL": {"ai_score_delta": 5.0, "ai_reason": "cleaner pullback"},
                "FAKE": {"ai_score_delta": 2.0, "ai_reason": "not allowed"},
            },
            "rejected": ["FAKE", "NVDA"],
            "risk_note": "bounded test",
        }

        validated = select_steady_intraday.validated_steady_intraday_selection(ai_plan, ranked, 2)

        self.assertEqual(validated["symbols"], ["AAPL", "NVDA"])
        self.assertEqual(validated["ranked"][0]["ai_score_delta"], 2.0)
        self.assertEqual(validated["ranked"][1]["ai_score_delta"], -2.0)
        self.assertEqual(validated["rejected"], ["NVDA"])

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

    def test_spike_strategy_respects_entry_window(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            spike_start_minute=15,
            spike_end_minute=60,
        )

        before_window = SpikeStrategy(settings).evaluate(self._spike_state(market_ms(2026, 4, 24, 9, 40)))
        inside_window = SpikeStrategy(settings).evaluate(self._spike_state(market_ms(2026, 4, 24, 10, 0)))

        self.assertIsNone(before_window)
        self.assertIsNotNone(inside_window)

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

    def test_opening_impulse_ignores_fixed_target_exit(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            target_profit_pct=0.01,
            regular_market_only=False,
            opening_impulse_min_hold_seconds=15,
        )
        broker = LocalPaperExecutor(PositionTracker(settings))
        broker.tracker.positions["AAPL"] = Position(
            symbol="AAPL",
            strategy="opening_impulse",
            shares=10,
            entry_price=100.0,
            entry_ms=1_000,
            target_price=101.0,
            stop_price=99.5,
        )
        state = SymbolState("AAPL")
        state.update_quote(Quote("AAPL", bid=101.18, ask=101.22, bid_size=20, ask_size=20, timestamp_ms=10_000))

        fill = broker.manage_exit(state, {"opening_impulse": OpeningImpulseStrategy(settings)})
        self.assertIsNone(fill)

        fill = broker.manage_exit(
            state,
            {"opening_impulse": OpeningImpulseStrategy(settings)},
            now_ms=16_500,
        )
        self.assertIsNone(fill)

    def test_manage_exit_updates_position_high_watermark(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            max_hold_seconds=3600,
            opening_impulse_min_hold_seconds=15,
        )
        broker = LocalPaperExecutor(PositionTracker(settings))
        broker.tracker.positions["AAPL"] = Position(
            symbol="AAPL",
            strategy="opening_impulse",
            shares=10,
            entry_price=100.0,
            entry_ms=1_000,
            target_price=101.0,
            stop_price=99.5,
            max_price=100.0,
            last_high_ts=1_000,
        )
        state = SymbolState("AAPL")
        state.update_quote(Quote("AAPL", bid=100.39, ask=100.41, bid_size=20, ask_size=20, timestamp_ms=20_000))

        fill = broker.manage_exit(state, {"opening_impulse": OpeningImpulseStrategy(settings)})

        self.assertIsNone(fill)
        self.assertEqual(broker.tracker.positions["AAPL"].max_price, 100.4)
        self.assertEqual(broker.tracker.positions["AAPL"].last_high_ts, 20_000)

    def test_opening_impulse_max_hold_still_applies_with_strategy_exit_logic(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            max_hold_seconds=60,
            opening_impulse_min_hold_seconds=15,
        )
        broker = LocalPaperExecutor(PositionTracker(settings))
        broker.tracker.positions["AAPL"] = Position(
            symbol="AAPL",
            strategy="opening_impulse",
            shares=10,
            entry_price=100.0,
            entry_ms=1_000,
            target_price=110.0,
            stop_price=99.5,
        )
        state = SymbolState("AAPL")
        state.update_quote(Quote("AAPL", bid=100.18, ask=100.22, bid_size=20, ask_size=20, timestamp_ms=70_000))

        fill = broker.manage_exit(state, {"opening_impulse": OpeningImpulseStrategy(settings)})

        self.assertIsNotNone(fill)
        self.assertEqual(fill.reason, "max hold")

    def test_paper_broker_forces_exit_at_max_trade_loss_r(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            max_trade_loss_r=1.2,
        )
        broker = LocalPaperExecutor(PositionTracker(settings))
        broker.tracker.positions["AAPL"] = Position(
            symbol="AAPL",
            strategy="opening_impulse",
            shares=10,
            entry_price=100.0,
            entry_ms=1_000,
            target_price=110.0,
            stop_price=99.5,
        )
        state = SymbolState("AAPL")
        state.update_quote(Quote("AAPL", bid=99.39, ask=99.41, bid_size=20, ask_size=20, timestamp_ms=20_000))

        fill = broker.manage_exit(state, {"opening_impulse": OpeningImpulseStrategy(settings)})

        self.assertIsNotNone(fill)
        self.assertEqual(fill.reason, "max trade loss")

    def test_opening_impulse_cuts_loser_early_after_activation_delay(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            opening_impulse_min_hold_seconds=15,
            opening_impulse_early_loss_cut_pct=0.0,
        )
        broker = LocalPaperExecutor(PositionTracker(settings))
        broker.tracker.positions["AAPL"] = Position(
            symbol="AAPL",
            strategy="opening_impulse",
            shares=10,
            entry_price=100.0,
            entry_ms=1_000,
            target_price=101.0,
            stop_price=99.5,
        )
        state = SymbolState("AAPL")
        state.update_quote(Quote("AAPL", bid=99.88, ask=99.90, bid_size=20, ask_size=20, timestamp_ms=16_500))

        fill = broker.manage_exit(state, {"opening_impulse": OpeningImpulseStrategy(settings)})

        self.assertIsNotNone(fill)
        self.assertEqual(fill.reason, "cut loss early")
        self.assertEqual(fill.trade_type, "loser")

    def test_opening_impulse_dynamic_trailing_stop_protects_winner(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            opening_impulse_min_hold_seconds=15,
            opening_impulse_exit_negative_steps=99,
        )
        strategy = OpeningImpulseStrategy(settings)
        state = SymbolState("AAPL")
        state.update_quote(Quote("AAPL", bid=101.17, ask=101.19, bid_size=20, ask_size=20, timestamp_ms=301_000))

        position = Position(
            symbol="AAPL",
            strategy="opening_impulse",
            shares=10,
            entry_price=100.0,
            entry_ms=1_000,
            target_price=110.0,
            stop_price=99.5,
            max_price=102.0,
            last_high_ts=280_000,
        )

        decision = strategy.should_exit(state, position)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "partial take profit")
        self.assertEqual(decision.shares, 5)
        self.assertTrue(decision.mark_partial)

    def test_opening_impulse_runner_exits_on_wider_pullback(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            opening_impulse_min_hold_seconds=15,
            opening_impulse_exit_negative_steps=99,
            opening_impulse_runner_pullback_pct=0.012,
        )
        strategy = OpeningImpulseStrategy(settings)
        state = SymbolState("AAPL")
        state.update_quote(Quote("AAPL", bid=100.75, ask=100.77, bid_size=20, ask_size=20, timestamp_ms=301_000))

        position = Position(
            symbol="AAPL",
            strategy="opening_impulse",
            shares=5,
            entry_price=100.0,
            entry_ms=1_000,
            target_price=110.0,
            stop_price=99.5,
            max_price=102.0,
            last_high_ts=280_000,
            partial_exit_taken=True,
        )

        decision = strategy.should_exit(state, position)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "runner pullback")

    def test_opening_impulse_strong_volume_allows_wider_pullback(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            opening_impulse_min_hold_seconds=15,
            opening_impulse_exit_negative_steps=99,
            opening_impulse_pullback_pct=0.005,
            opening_impulse_strong_volume_ratio=2.5,
            opening_impulse_strong_pullback_pct=0.01,
        )
        strategy = OpeningImpulseStrategy(settings)
        state = SymbolState("AAPL")
        state.add_bar(Bar("AAPL", open=100.0, high=100.8, low=99.9, close=100.6, volume=100, vwap=100.4, start_ms=1_000, end_ms=61_000))
        state.add_bar(Bar("AAPL", open=100.6, high=101.1, low=100.5, close=101.0, volume=100, vwap=100.8, start_ms=61_000, end_ms=121_000))
        state.add_bar(Bar("AAPL", open=101.0, high=101.8, low=100.9, close=101.6, volume=300, vwap=101.4, start_ms=121_000, end_ms=181_000))
        state.update_quote(Quote("AAPL", bid=101.29, ask=101.31, bid_size=20, ask_size=20, timestamp_ms=200_000))
        position = Position(
            symbol="AAPL",
            strategy="opening_impulse",
            shares=10,
            entry_price=100.0,
            entry_ms=1_000,
            target_price=110.0,
            stop_price=99.5,
            max_price=102.0,
            last_high_ts=181_000,
            partial_exit_taken=True,
        )

        decision = strategy.should_exit(state, position)

        self.assertIsNone(decision)

    def test_opening_impulse_runner_ignores_normal_pullback_before_runner_limit(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            opening_impulse_min_hold_seconds=15,
            opening_impulse_exit_negative_steps=99,
            opening_impulse_pullback_pct=0.005,
            opening_impulse_strong_volume_ratio=2.5,
            opening_impulse_strong_pullback_pct=0.01,
        )
        strategy = OpeningImpulseStrategy(settings)
        state = SymbolState("AAPL")
        state.add_bar(Bar("AAPL", open=100.0, high=100.8, low=99.9, close=100.6, volume=100, vwap=100.4, start_ms=1_000, end_ms=61_000))
        state.add_bar(Bar("AAPL", open=100.6, high=101.1, low=100.5, close=101.0, volume=100, vwap=100.8, start_ms=61_000, end_ms=121_000))
        state.add_bar(Bar("AAPL", open=101.0, high=101.8, low=100.9, close=101.6, volume=120, vwap=101.4, start_ms=121_000, end_ms=181_000))
        state.update_quote(Quote("AAPL", bid=101.29, ask=101.31, bid_size=20, ask_size=20, timestamp_ms=200_000))
        position = Position(
            symbol="AAPL",
            strategy="opening_impulse",
            shares=10,
            entry_price=100.0,
            entry_ms=1_000,
            target_price=110.0,
            stop_price=99.5,
            max_price=102.0,
            last_high_ts=181_000,
            partial_exit_taken=True,
        )

        decision = strategy.should_exit(state, position)

        self.assertIsNone(decision)

    def test_opening_impulse_volume_collapse_stall_exits_profitable_trade(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            opening_impulse_min_hold_seconds=15,
            opening_impulse_exit_negative_steps=99,
        )
        strategy = OpeningImpulseStrategy(settings)
        state = SymbolState("AAPL")
        state.add_bar(Bar("AAPL", open=100.0, high=100.1, low=99.9, close=100.0, volume=100, vwap=100.0, start_ms=1_000, end_ms=61_000))
        state.add_bar(Bar("AAPL", open=100.0, high=100.2, low=100.0, close=100.1, volume=100, vwap=100.1, start_ms=61_000, end_ms=121_000))
        state.add_bar(Bar("AAPL", open=100.1, high=100.3, low=100.1, close=100.2, volume=20, vwap=100.2, start_ms=121_000, end_ms=181_000))
        state.update_quote(Quote("AAPL", bid=100.19, ask=100.21, bid_size=20, ask_size=20, timestamp_ms=200_000))
        position = Position(
            symbol="AAPL",
            strategy="opening_impulse",
            shares=10,
            entry_price=100.0,
            entry_ms=1_000,
            target_price=110.0,
            stop_price=99.5,
            max_price=100.3,
            last_high_ts=100_000,
        )

        decision = strategy.should_exit(state, position)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "volume collapse stall")

    def test_opening_impulse_higher_high_break_exits_profitable_trade(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            opening_impulse_min_hold_seconds=15,
            opening_impulse_exit_negative_steps=99,
        )
        strategy = OpeningImpulseStrategy(settings)
        state = SymbolState("AAPL")
        state.add_bar(Bar("AAPL", open=100.0, high=100.8, low=99.9, close=100.6, volume=100, vwap=100.4, start_ms=1_000, end_ms=61_000))
        state.add_bar(Bar("AAPL", open=100.6, high=100.7, low=100.4, close=100.5, volume=100, vwap=100.5, start_ms=61_000, end_ms=121_000))
        state.add_bar(Bar("AAPL", open=100.5, high=100.65, low=100.3, close=100.4, volume=120, vwap=100.5, start_ms=121_000, end_ms=181_000))
        state.update_quote(Quote("AAPL", bid=100.49, ask=100.51, bid_size=20, ask_size=20, timestamp_ms=200_000))
        position = Position(
            symbol="AAPL",
            strategy="opening_impulse",
            shares=10,
            entry_price=100.0,
            entry_ms=1_000,
            target_price=110.0,
            stop_price=99.5,
            max_price=100.8,
            last_high_ts=61_000,
        )

        decision = strategy.should_exit(state, position)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "higher-high break")

    def test_opening_impulse_first_lower_high_does_not_exit_without_confirmation(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            opening_impulse_min_hold_seconds=15,
            opening_impulse_exit_negative_steps=99,
        )
        strategy = OpeningImpulseStrategy(settings)
        state = SymbolState("AAPL")
        state.add_bar(Bar("AAPL", open=100.0, high=100.8, low=99.9, close=100.6, volume=100, vwap=100.4, start_ms=1_000, end_ms=61_000))
        state.add_bar(Bar("AAPL", open=100.6, high=100.7, low=100.4, close=100.6, volume=100, vwap=100.5, start_ms=61_000, end_ms=121_000))
        state.update_quote(Quote("AAPL", bid=100.59, ask=100.61, bid_size=20, ask_size=20, timestamp_ms=140_000))
        position = Position(
            symbol="AAPL",
            strategy="opening_impulse",
            shares=10,
            entry_price=100.0,
            entry_ms=1_000,
            target_price=110.0,
            stop_price=99.5,
            max_price=100.8,
            last_high_ts=61_000,
        )

        decision = strategy.should_exit(state, position)

        self.assertIsNone(decision)

    def test_opening_impulse_momentum_stall_does_not_exit_loser(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            opening_impulse_min_hold_seconds=15,
            opening_impulse_exit_min_quotes=99,
        )
        strategy = OpeningImpulseStrategy(settings)
        state = SymbolState("AAPL")
        state.update_quote(Quote("AAPL", bid=99.89, ask=99.91, bid_size=20, ask_size=20, timestamp_ms=200_000))
        position = Position(
            symbol="AAPL",
            strategy="opening_impulse",
            shares=10,
            entry_price=100.0,
            entry_ms=1_000,
            target_price=110.0,
            stop_price=99.5,
            max_price=100.0,
            last_high_ts=1_000,
        )

        decision = strategy.should_exit(state, position)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "cut loss early")

    def test_opening_impulse_failed_continuation_exits_if_no_new_highs(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            opening_impulse_min_hold_seconds=0,
            opening_impulse_exit_negative_steps=99,
            opening_impulse_failed_continuation_no_high_seconds=30,
            opening_impulse_failed_continuation_max_mfe_pct=0.004,
        )
        strategy = OpeningImpulseStrategy(settings)
        state = SymbolState("AAPL")
        state.add_bar(Bar("AAPL", open=100.0, high=100.25, low=99.9, close=100.15, volume=100, vwap=100.1, start_ms=1_000, end_ms=61_000))
        state.add_bar(Bar("AAPL", open=100.15, high=100.30, low=100.0, close=100.18, volume=90, vwap=100.15, start_ms=61_000, end_ms=121_000))
        state.add_bar(Bar("AAPL", open=100.18, high=100.32, low=100.05, close=100.20, volume=85, vwap=100.18, start_ms=121_000, end_ms=181_000))
        state.update_quote(Quote("AAPL", bid=100.19, ask=100.21, bid_size=20, ask_size=20, timestamp_ms=220_000))
        position = Position(
            symbol="AAPL",
            strategy="opening_impulse",
            shares=10,
            entry_price=100.0,
            entry_ms=1_000,
            target_price=110.0,
            stop_price=99.5,
            max_price=100.30,
            last_high_ts=150_000,
        )

        decision = strategy.should_exit(state, position)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "failed continuation no new highs")

    def test_opening_impulse_failed_continuation_exits_on_lower_high_chain(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            opening_impulse_min_hold_seconds=0,
            opening_impulse_exit_negative_steps=99,
            opening_impulse_failed_continuation_max_mfe_pct=0.005,
        )
        strategy = OpeningImpulseStrategy(settings)
        state = SymbolState("AAPL")
        state.add_bar(Bar("AAPL", open=100.0, high=100.60, low=99.9, close=100.30, volume=100, vwap=100.2, start_ms=1_000, end_ms=61_000))
        state.add_bar(Bar("AAPL", open=100.3, high=100.50, low=100.1, close=100.35, volume=100, vwap=100.3, start_ms=61_000, end_ms=121_000))
        state.add_bar(Bar("AAPL", open=100.35, high=100.40, low=100.2, close=100.36, volume=95, vwap=100.33, start_ms=121_000, end_ms=181_000))
        state.update_quote(Quote("AAPL", bid=100.07, ask=100.09, bid_size=20, ask_size=20, timestamp_ms=200_000))
        position = Position(
            symbol="AAPL",
            strategy="opening_impulse",
            shares=10,
            entry_price=100.0,
            entry_ms=1_000,
            target_price=110.0,
            stop_price=99.5,
            max_price=100.30,
            last_high_ts=100_000,
        )

        decision = strategy.should_exit(state, position)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "failed continuation lower highs")

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

    def test_shutdown_flatten_skips_wall_clock_in_replay_mode(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            replay_market_data=True,
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

        flatten_on_shutdown(
            settings,
            broker,
            {"AAPL": state},
            {"spike": SpikeStrategy(settings)},
            now_ms=market_ms(2026, 4, 24, 15, 55),
        )

        self.assertIn("AAPL", broker.tracker.positions)

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

    def test_position_tracker_writes_trade_journal_entries(self):
        old_trade_journal_file = execution_module.TRADE_JOURNAL_FILE
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                execution_module.TRADE_JOURNAL_FILE = Path(tmpdir) / "logs" / "trade_journal.jsonl"
                settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"])
                tracker = PositionTracker(settings)
                signal = Signal(
                    strategy="opening_impulse",
                    symbol="AAPL",
                    side="BUY",
                    price=100.0,
                    timestamp_ms=market_ms(2026, 4, 24, 9, 35),
                    change_pct=0.004,
                    volume_ratio=3.0,
                    spread_bps=4.0,
                    reason="test impulse",
                    session_open_price=99.0,
                    entry_open_pct=0.011111111111111112,
                )

                tracker.record_entry(
                    signal,
                    shares=3,
                    fill_price=100.1,
                    reason="test impulse",
                    order_id="buy-1",
                    fill_latency_seconds=0.42,
                )
                tracker.record_exit(
                    "AAPL",
                    shares=3,
                    price=101.2,
                    timestamp_ms=market_ms(2026, 4, 24, 9, 40),
                    reason="target profit",
                    order_id="sell-1",
                )

                rows = [json.loads(line) for line in execution_module.TRADE_JOURNAL_FILE.read_text().splitlines()]

                self.assertEqual([row["event"] for row in rows], ["buy", "sell"])
                self.assertEqual(rows[0]["symbol"], "AAPL")
                self.assertEqual(rows[0]["strategy"], "opening_impulse")
                self.assertEqual(rows[0]["order_id"], "buy-1")
                self.assertAlmostEqual(rows[0]["entry_open_pct"], 0.011111111111111112)
                self.assertAlmostEqual(rows[0]["signal_price"], 100.0)
                self.assertAlmostEqual(rows[0]["slippage_pct"], 0.001)
                self.assertAlmostEqual(rows[0]["fill_latency_seconds"], 0.42)
                self.assertAlmostEqual(rows[1]["pnl"], 3.3)
                self.assertEqual(rows[1]["reason"], "target profit")
                self.assertEqual(rows[1]["trade_type"], "winner")
                self.assertAlmostEqual(rows[1]["entry_open_pct"], 0.011111111111111112)
                self.assertAlmostEqual(rows[1]["hold_seconds"], 300.0)
                self.assertAlmostEqual(rows[1]["mfe_pct"], (101.2 - 100.1) / 100.1)
                self.assertAlmostEqual(rows[1]["r_multiple"], (101.2 - 100.1) / (100.1 - 100.1 * 0.995))
                self.assertEqual(rows[1]["exit_stage"], "full")
                self.assertAlmostEqual(rows[1]["full_trade_r_multiple"], rows[1]["r_multiple"])
                self.assertAlmostEqual(rows[1]["cumulative_daily_pnl"], 3.3)
        finally:
            execution_module.TRADE_JOURNAL_FILE = old_trade_journal_file

    def test_position_tracker_logs_runner_effectiveness_metrics(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"])
        tracker = PositionTracker(settings)
        signal = Signal(
            strategy="opening_impulse",
            symbol="AAPL",
            side="BUY",
            price=100.0,
            timestamp_ms=market_ms(2026, 4, 24, 9, 35),
            change_pct=0.004,
            volume_ratio=3.0,
            spread_bps=4.0,
            reason="test impulse",
        )

        tracker.record_entry(signal, shares=10, fill_price=100.0, reason="test impulse")
        partial = tracker.record_exit("AAPL", shares=5, price=101.0, timestamp_ms=market_ms(2026, 4, 24, 9, 36), reason="partial take profit", mark_partial=True)
        runner = tracker.record_exit("AAPL", shares=5, price=102.0, timestamp_ms=market_ms(2026, 4, 24, 9, 40), reason="runner pullback")

        self.assertEqual(partial.exit_stage, "partial")
        self.assertIsNone(partial.full_trade_r_multiple)
        self.assertEqual(runner.exit_stage, "runner")
        self.assertAlmostEqual(runner.runner_r_multiple, (102.0 - 100.0) / (100.0 - 99.5))
        self.assertAlmostEqual(runner.full_trade_r_multiple, 15.0 / ((100.0 - 99.5) * 10))

    def test_position_tracker_uses_risk_sizing_when_enabled(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            starting_cash=25_000.0,
            max_position_value=20_000.0,
            position_sizing_mode="risk",
            risk_per_trade_pct=0.005,
        )
        tracker = PositionTracker(settings)

        shares = tracker.planned_shares(100.0, 99.0)

        self.assertEqual(shares, 125)

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

    def test_alpaca_partial_fill_is_kept_without_canceling_remainder(self):
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
        self.assertFalse(executor.clients.trading.cancel_called)
        self.assertEqual(settled[0], 3)

    def test_alpaca_buy_skips_chased_price_before_submit(self):
        install_fake_alpaca_modules()
        try:
            settings = Settings(
                alpaca_api_key="test",
                alpaca_secret_key="test",
                symbols=["AAPL"],
                regular_market_only=False,
                max_entry_chase_pct=0.003,
            )
            executor = AlpacaPaperExecutor.__new__(AlpacaPaperExecutor)
            executor.settings = settings
            executor.tracker = PositionTracker(settings)
            executor.clients = FakeClients(
                [FakeOrder("buy-1", status="filled", filled_qty="5", filled_avg_price="100.00")],
                latest_quotes={"AAPL": types.SimpleNamespace(ask_price="100.40")},
            )
            signal = Signal(
                symbol="AAPL",
                side="BUY",
                price=100.0,
                change_pct=0.01,
                volume_ratio=2.5,
                spread_bps=4.0,
                reason="test",
                timestamp_ms=market_ms(2026, 4, 24, 10, 0),
                strategy="opening_impulse",
            )
            fill = executor.buy(signal)

            self.assertIsNone(fill)
            self.assertEqual(executor.clients.trading.submitted_orders, [])
            self.assertEqual(len(executor.clients.historical.latest_quote_requests), 1)
            self.assertTrue(executor.consume_failed_entry("AAPL"))
        finally:
            remove_fake_alpaca_modules()

    def test_alpaca_cancel_unfilled_order_ignores_already_filled_race(self):
        install_fake_alpaca_modules()
        try:
            settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"])
            executor = AlpacaPaperExecutor.__new__(AlpacaPaperExecutor)
            executor.settings = settings
            executor.tracker = PositionTracker(settings)
            executor.clients = FakeClients(
                [],
                cancel_error=FakeAPIError('{"code":42210000,"message":"order is already in \\"filled\\" state"}'),
            )
            order = FakeOrder("order-1", status="new")

            with self.assertLogs("execution", level="INFO") as captured:
                executor._cancel_unfilled_order(order)

            self.assertIn("already filled before cancel", captured.output[0])
        finally:
            remove_fake_alpaca_modules()

    def test_alpaca_reconciles_only_target_positions(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"])
        executor = AlpacaPaperExecutor.__new__(AlpacaPaperExecutor)
        executor.settings = settings
        executor.tracker = PositionTracker(settings)
        executor.clients = FakeClients(
            [],
            positions=[
                FakePosition("AAPL", qty="7", avg_entry_price="101.25"),
                FakePosition("MSFT", qty="3", avg_entry_price="250.00"),
            ],
        )

        executor._reconcile_target_positions()

        self.assertEqual(set(executor.tracker.positions), {"AAPL"})
        self.assertEqual(executor.tracker.positions["AAPL"].shares, 7)
        self.assertEqual(executor.tracker.positions["AAPL"].entry_price, 101.25)

    def test_alpaca_syncs_account_cash_for_sizing(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"], starting_cash=25_000.0)
        executor = AlpacaPaperExecutor.__new__(AlpacaPaperExecutor)
        executor.settings = settings
        executor.tracker = PositionTracker(settings)
        executor.clients = FakeClients([], cash="4321.50")

        executor._sync_account_cash()

        self.assertEqual(executor.tracker.cash, 4321.50)

    def test_alpaca_startup_cancels_target_open_orders(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"])
        executor = AlpacaPaperExecutor.__new__(AlpacaPaperExecutor)
        executor.settings = settings
        executor.tracker = PositionTracker(settings)
        executor.clients = FakeClients(
            [],
            open_orders=[
                FakeOrder("order-aapl", status="new", symbol="AAPL"),
                FakeOrder("order-msft", status="new", symbol="MSFT"),
            ],
        )

        executor._cancel_target_open_orders()

        self.assertEqual(executor.clients.trading.canceled_order_ids, ["order-aapl"])

    def test_alpaca_flatten_does_not_require_cached_price(self):
        install_fake_alpaca_modules()
        try:
            settings = Settings(
                alpaca_api_key="test",
                alpaca_secret_key="test",
                symbols=["AAPL"],
                flatten_before_close_minutes=5,
                alpaca_fill_timeout_seconds=0.0,
            )
            executor = AlpacaPaperExecutor.__new__(AlpacaPaperExecutor)
            executor.settings = settings
            executor.tracker = PositionTracker(settings)
            executor.tracker.positions["AAPL"] = Position(
                symbol="AAPL",
                strategy="reconciled",
                shares=5,
                entry_price=100.0,
                entry_ms=market_ms(2026, 4, 24, 10, 0),
                target_price=101.0,
                stop_price=99.5,
            )
            executor.clients = FakeClients(
                [
                    FakeOrder("sell-1", status="filled", filled_qty="5", filled_avg_price="100.10"),
                ]
            )
            state = SymbolState("AAPL")

            fill = executor.manage_exit(state, {}, now_ms=market_ms(2026, 4, 24, 15, 55))

            self.assertIsNotNone(fill)
            self.assertEqual(fill.reason.split(" | ")[0], "end-of-day flatten")
            self.assertEqual(executor.clients.trading.submitted_orders[0].symbol, "AAPL")
        finally:
            remove_fake_alpaca_modules()

    def test_alpaca_buy_generates_unique_client_order_id(self):
        install_fake_alpaca_modules()
        try:
            settings = Settings(
                alpaca_api_key="test",
                alpaca_secret_key="test",
                symbols=["AAPL"],
                regular_market_only=False,
                alpaca_fill_timeout_seconds=0.0,
            )
            executor = AlpacaPaperExecutor.__new__(AlpacaPaperExecutor)
            executor.settings = settings
            executor.tracker = PositionTracker(settings)
            executor.clients = FakeClients(
                [FakeOrder("buy-1", status="filled", filled_qty="5", filled_avg_price="100.00")]
            )
            signal = Signal(
                symbol="AAPL",
                side="BUY",
                price=100.0,
                change_pct=0.01,
                volume_ratio=2.5,
                spread_bps=4.0,
                reason="test",
                timestamp_ms=market_ms(2026, 4, 24, 10, 0),
                strategy="opening_impulse",
            )

            executor.buy(signal)
            first = executor.clients.trading.submitted_orders[0].client_order_id
            executor.clients.trading.orders.append(FakeOrder("buy-2", status="filled", filled_qty="5", filled_avg_price="100.00"))
            executor.tracker.positions.pop("AAPL", None)
            executor.buy(signal)
            second = executor.clients.trading.submitted_orders[1].client_order_id

            self.assertNotEqual(first, second)
        finally:
            remove_fake_alpaca_modules()

    def test_alpaca_buy_api_error_is_logged_and_skipped(self):
        install_fake_alpaca_modules()
        try:
            settings = Settings(
                alpaca_api_key="test",
                alpaca_secret_key="test",
                symbols=["AAPL"],
                regular_market_only=False,
            )
            executor = AlpacaPaperExecutor.__new__(AlpacaPaperExecutor)
            executor.settings = settings
            executor.tracker = PositionTracker(settings)
            executor.clients = FakeClients([], submit_error=FakeAPIError('{"code":40010001,"message":"client_order_id must be unique"}'))
            signal = Signal(
                symbol="AAPL",
                side="BUY",
                price=100.0,
                change_pct=0.01,
                volume_ratio=2.5,
                spread_bps=4.0,
                reason="test",
                timestamp_ms=market_ms(2026, 4, 24, 10, 0),
                strategy="opening_impulse",
            )

            with self.assertLogs("execution", level="WARNING") as captured:
                fill = executor.buy(signal)

            self.assertIsNone(fill)
            self.assertIn("Alpaca buy order rejected", captured.output[0])
        finally:
            remove_fake_alpaca_modules()

    def test_alpaca_buy_timeout_records_failed_entry_cooldown_marker(self):
        install_fake_alpaca_modules()
        try:
            settings = Settings(
                alpaca_api_key="test",
                alpaca_secret_key="test",
                symbols=["AAPL"],
                regular_market_only=False,
                alpaca_fill_timeout_seconds=0.0,
            )
            executor = AlpacaPaperExecutor.__new__(AlpacaPaperExecutor)
            executor.settings = settings
            executor.tracker = PositionTracker(settings)
            executor.clients = FakeClients(
                [
                    FakeOrder("buy-1", status="new"),
                    FakeOrder("buy-1", status="canceled"),
                ]
            )
            signal = Signal(
                symbol="AAPL",
                side="BUY",
                price=100.0,
                change_pct=0.01,
                volume_ratio=2.5,
                spread_bps=4.0,
                reason="test",
                timestamp_ms=market_ms(2026, 4, 24, 10, 0),
                strategy="opening_impulse",
            )

            fill = executor.buy(signal)

            self.assertIsNone(fill)
            self.assertTrue(executor.consume_failed_entry("AAPL"))
            self.assertFalse(executor.consume_failed_entry("AAPL"))
        finally:
            remove_fake_alpaca_modules()

    def test_shutdown_flatten_only_runs_inside_close_window(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"], flatten_before_close_minutes=5)
        executor = FakeExecutor()
        states = {"AAPL": SymbolState("AAPL")}

        flatten_on_shutdown(settings, executor, states, {}, now_ms=market_ms(2026, 4, 24, 15, 54))
        flatten_on_shutdown(settings, executor, states, {}, now_ms=market_ms(2026, 4, 24, 15, 55))

        self.assertEqual(executor.exit_calls, [("AAPL", market_ms(2026, 4, 24, 15, 55))])

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

    def test_opening_impulse_rejects_short_quote_spike(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            opening_impulse_start_minute=0,
            opening_impulse_end_minute=90,
            opening_impulse_window_seconds=30,
            opening_impulse_min_quotes=10,
            opening_impulse_min_quote_move_seconds=20,
            opening_impulse_change_pct=0.009,
            opening_impulse_volume_ratio=1.5,
            opening_impulse_max_spread_bps=15.0,
            opening_impulse_min_quote_size=25,
        )
        state = SymbolState("AAPL")
        base_ms = 1777037400000
        for index in range(4):
            state.add_bar(bar("AAPL", close=100.0 + (index * 0.1), volume=100, end_ms=base_ms + ((index + 1) * 60_000)))
        state.add_bar(bar("AAPL", close=100.4, volume=320, end_ms=base_ms + (5 * 60_000)))
        for index in range(10):
            bid = 100.00 + (index * 0.11)
            ask = bid + 0.015
            state.update_quote(Quote("AAPL", bid=bid, ask=ask, bid_size=30, ask_size=30, timestamp_ms=base_ms + (index * 1_000)))

        with self.assertLogs("strategies.opening_impulse", level="DEBUG") as captured:
            signal = OpeningImpulseStrategy(settings).evaluate(state)

        self.assertIsNone(signal)
        self.assertIn("quote impulse duration", "\n".join(captured.output))

    def test_opening_impulse_rejects_zero_volume_signal(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            opening_impulse_start_minute=0,
            opening_impulse_end_minute=90,
            opening_impulse_window_seconds=30,
            opening_impulse_min_quotes=10,
            opening_impulse_change_pct=0.009,
            opening_impulse_volume_ratio=1.5,
            opening_impulse_max_spread_bps=15.0,
            opening_impulse_min_quote_size=25,
        )
        state = SymbolState("AAPL")
        base_ms = 1777037400000
        for index in range(4):
            state.add_bar(bar("AAPL", close=100.0 + (index * 0.1), volume=100, end_ms=base_ms + ((index + 1) * 60_000)))
        state.add_bar(bar("AAPL", close=100.4, volume=0, end_ms=base_ms + (5 * 60_000)))
        for index in range(10):
            bid = 100.00 + (index * 0.11)
            ask = bid + 0.015
            state.update_quote(Quote("AAPL", bid=bid, ask=ask, bid_size=30, ask_size=30, timestamp_ms=base_ms + (index * 3_000)))

        with self.assertLogs("strategies.opening_impulse", level="DEBUG") as captured:
            signal = OpeningImpulseStrategy(settings).evaluate(state)

        self.assertIsNone(signal)
        self.assertIn("volume ratio is zero", "\n".join(captured.output))

    def test_opening_impulse_rejects_missing_higher_high_structure(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            opening_impulse_start_minute=0,
            opening_impulse_end_minute=90,
            opening_impulse_window_seconds=30,
            opening_impulse_min_quotes=10,
            opening_impulse_change_pct=0.009,
            opening_impulse_volume_ratio=1.5,
            opening_impulse_max_spread_bps=15.0,
            opening_impulse_min_quote_size=25,
        )
        state = SymbolState("AAPL")
        base_ms = 1777037400000
        state.add_bar(Bar("AAPL", open=100.0, high=100.6, low=99.9, close=100.1, volume=100, vwap=100.1, start_ms=base_ms, end_ms=base_ms + 60_000))
        state.add_bar(Bar("AAPL", open=100.1, high=100.5, low=100.0, close=100.2, volume=100, vwap=100.2, start_ms=base_ms + 60_000, end_ms=base_ms + 120_000))
        state.add_bar(Bar("AAPL", open=100.2, high=100.4, low=100.1, close=100.4, volume=320, vwap=100.3, start_ms=base_ms + 120_000, end_ms=base_ms + 180_000))
        for index in range(10):
            bid = 100.00 + (index * 0.11)
            ask = bid + 0.015
            state.update_quote(Quote("AAPL", bid=bid, ask=ask, bid_size=30, ask_size=30, timestamp_ms=base_ms + 180_000 + (index * 3_000)))

        with self.assertLogs("strategies.opening_impulse", level="DEBUG") as captured:
            signal = OpeningImpulseStrategy(settings).evaluate(state)

        self.assertIsNone(signal)
        self.assertIn("higher-high", "\n".join(captured.output))

    def test_opening_impulse_hot_news_allows_early_entry_without_strict_structure(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            opening_impulse_start_minute=0,
            opening_impulse_end_minute=90,
            opening_impulse_window_seconds=30,
            opening_impulse_min_quotes=10,
            opening_impulse_change_pct=0.02,
            opening_impulse_volume_ratio=2.5,
            opening_impulse_news_hot_minutes=10,
            opening_impulse_news_change_pct=0.003,
            opening_impulse_news_min_volume_ratio=1.3,
            opening_impulse_max_spread_bps=15.0,
            opening_impulse_min_quote_size=25,
        )
        state = SymbolState("AAPL")
        base_ms = 1777037400000
        # Non-higher-high bars on purpose; news path should allow early entry.
        state.add_bar(Bar("AAPL", open=100.0, high=100.3, low=99.9, close=100.1, volume=100, vwap=100.1, start_ms=base_ms, end_ms=base_ms + 60_000))
        state.add_bar(Bar("AAPL", open=100.1, high=100.25, low=100.0, close=100.2, volume=100, vwap=100.2, start_ms=base_ms + 60_000, end_ms=base_ms + 120_000))
        state.add_bar(Bar("AAPL", open=100.2, high=100.2, low=100.1, close=100.3, volume=140, vwap=100.2, start_ms=base_ms + 120_000, end_ms=base_ms + 180_000))
        for index in range(10):
            bid = 100.00 + (index * 0.04)
            ask = bid + 0.015
            state.update_quote(Quote("AAPL", bid=bid, ask=ask, bid_size=30, ask_size=30, timestamp_ms=base_ms + 180_000 + (index * 3_000)))
        state.mark_news(base_ms + 180_000, price=100.35)

        signal = OpeningImpulseStrategy(settings).evaluate(state)

        self.assertIsNotNone(signal)
        self.assertIn("news_early_impulse", signal.reason)

    def test_opening_impulse_hot_news_reentry_requires_reclaim_after_failed_continuation(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            opening_impulse_start_minute=0,
            opening_impulse_end_minute=90,
            opening_impulse_window_seconds=30,
            opening_impulse_min_quotes=4,
            opening_impulse_change_pct=0.02,
            opening_impulse_news_hot_minutes=10,
            opening_impulse_news_change_pct=0.003,
            opening_impulse_news_min_volume_ratio=1.2,
            opening_impulse_reentry_reclaim_lookback_bars=5,
            opening_impulse_reentry_min_volume_ratio=1.2,
            opening_impulse_max_spread_bps=15.0,
            opening_impulse_min_quote_size=25,
        )
        state = SymbolState("AAPL")
        # Prior bars show failed continuation (lower highs and lower closes).
        state.add_bar(Bar("AAPL", open=100.0, high=100.6, low=99.8, close=100.5, volume=100, vwap=100.3, start_ms=market_ms(2026, 4, 24, 9, 31), end_ms=market_ms(2026, 4, 24, 9, 32)))
        state.add_bar(Bar("AAPL", open=100.5, high=100.5, low=100.1, close=100.4, volume=95, vwap=100.4, start_ms=market_ms(2026, 4, 24, 9, 32), end_ms=market_ms(2026, 4, 24, 9, 33)))
        state.add_bar(Bar("AAPL", open=100.4, high=100.4, low=100.0, close=100.3, volume=90, vwap=100.3, start_ms=market_ms(2026, 4, 24, 9, 33), end_ms=market_ms(2026, 4, 24, 9, 34)))
        state.add_bar(Bar("AAPL", open=100.3, high=100.3, low=99.9, close=100.2, volume=92, vwap=100.2, start_ms=market_ms(2026, 4, 24, 9, 34), end_ms=market_ms(2026, 4, 24, 9, 35)))
        # Latest bar does not reclaim prior high yet.
        state.add_bar(Bar("AAPL", open=100.2, high=100.45, low=100.1, close=100.45, volume=220, vwap=100.35, start_ms=market_ms(2026, 4, 24, 9, 35), end_ms=market_ms(2026, 4, 24, 9, 36)))
        quote_base_ms = market_ms(2026, 4, 24, 9, 35)
        state.mark_news(quote_base_ms + 20_000, price=100.20, sentiment=1, impact=0.9)
        for ms, price in (
            (quote_base_ms + 30_000, 100.30),
            (quote_base_ms + 40_000, 100.45),
            (quote_base_ms + 50_000, 100.56),
            (market_ms(2026, 4, 24, 9, 36), 100.66),
        ):
            state.update_quote(Quote("AAPL", bid=price - 0.01, ask=price + 0.01, bid_size=50, ask_size=50, timestamp_ms=ms))

        strategy = OpeningImpulseStrategy(settings)
        rejected = strategy.evaluate(state)
        self.assertIsNone(rejected)

        # Reclaim the prior swing high with a strong follow-through bar.
        state.add_bar(Bar("AAPL", open=100.45, high=100.85, low=100.4, close=100.70, volume=280, vwap=100.65, start_ms=market_ms(2026, 4, 24, 9, 36), end_ms=market_ms(2026, 4, 24, 9, 37)))
        state.update_quote(Quote("AAPL", bid=100.71, ask=100.73, bid_size=55, ask_size=55, timestamp_ms=market_ms(2026, 4, 24, 9, 37)))
        accepted = strategy.evaluate(state)

        self.assertIsNotNone(accepted)
        self.assertIn("hot_news", accepted.reason)

    def test_opening_impulse_hot_news_uses_tighter_trailing_and_max_hold(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            opening_impulse_min_hold_seconds=0,
            opening_impulse_news_hot_minutes=10,
            opening_impulse_news_tight_pullback_pct=0.002,
            opening_impulse_news_max_hold_seconds=90,
            opening_impulse_runner_pullback_pct=0.01,
            opening_impulse_exit_negative_steps=99,
        )
        strategy = OpeningImpulseStrategy(settings)
        state = SymbolState("AAPL")
        state.add_bar(Bar("AAPL", open=100.0, high=100.8, low=99.9, close=100.6, volume=100, vwap=100.4, start_ms=1_000, end_ms=61_000))
        state.add_bar(Bar("AAPL", open=100.6, high=101.1, low=100.5, close=101.0, volume=120, vwap=100.8, start_ms=61_000, end_ms=121_000))
        state.add_bar(Bar("AAPL", open=101.0, high=101.1, low=100.6, close=100.7, volume=300, vwap=100.8, start_ms=121_000, end_ms=181_000))
        state.update_quote(Quote("AAPL", bid=100.69, ask=100.71, bid_size=20, ask_size=20, timestamp_ms=181_000))
        state.mark_news(160_000, price=100.7)
        position = Position(
            symbol="AAPL",
            strategy="opening_impulse",
            shares=10,
            entry_price=100.0,
            entry_ms=80_000,
            target_price=101.0,
            stop_price=99.5,
            max_price=101.1,
            last_high_ts=120_000,
        )

        decision = strategy.should_exit(state, position)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "news max hold")

    def test_opening_impulse_skips_hot_news_when_move_is_already_extended(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            opening_impulse_start_minute=0,
            opening_impulse_end_minute=90,
            opening_impulse_window_seconds=30,
            opening_impulse_min_quotes=10,
            opening_impulse_change_pct=0.02,
            opening_impulse_volume_ratio=2.5,
            opening_impulse_news_hot_minutes=10,
            opening_impulse_news_change_pct=0.003,
            opening_impulse_news_min_volume_ratio=1.3,
            opening_impulse_news_max_move_since_event_pct=0.02,
            opening_impulse_max_spread_bps=15.0,
            opening_impulse_min_quote_size=25,
        )
        state = SymbolState("AAPL")
        base_ms = 1777037400000
        state.add_bar(Bar("AAPL", open=100.0, high=100.3, low=99.9, close=100.1, volume=100, vwap=100.1, start_ms=base_ms, end_ms=base_ms + 60_000))
        state.add_bar(Bar("AAPL", open=100.1, high=100.25, low=100.0, close=100.2, volume=100, vwap=100.2, start_ms=base_ms + 60_000, end_ms=base_ms + 120_000))
        state.add_bar(Bar("AAPL", open=100.2, high=102.7, low=100.1, close=102.6, volume=350, vwap=101.8, start_ms=base_ms + 120_000, end_ms=base_ms + 180_000))
        for index in range(10):
            bid = 102.50 + (index * 0.03)
            ask = bid + 0.015
            state.update_quote(Quote("AAPL", bid=bid, ask=ask, bid_size=30, ask_size=30, timestamp_ms=base_ms + 180_000 + (index * 3_000)))
        state.mark_news(base_ms + 180_000, price=100.30)

        signal = OpeningImpulseStrategy(settings).evaluate(state)

        self.assertIsNone(signal)

    def test_opening_impulse_rejects_late_entry_extension(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            opening_impulse_start_minute=0,
            opening_impulse_end_minute=150,
            opening_impulse_window_seconds=30,
            opening_impulse_min_quotes=10,
            opening_impulse_change_pct=0.009,
            opening_impulse_volume_ratio=1.5,
            opening_impulse_max_spread_bps=15.0,
            opening_impulse_min_quote_size=25,
            opening_impulse_max_entry_extension_pct=0.02,
        )
        state = SymbolState("AAPL")
        base_ms = 1777037400000
        state.add_bar(Bar("AAPL", open=100.0, high=100.5, low=99.9, close=100.4, volume=100, vwap=100.2, start_ms=base_ms, end_ms=base_ms + 60_000))
        state.add_bar(Bar("AAPL", open=100.4, high=101.5, low=100.3, close=101.4, volume=120, vwap=101.0, start_ms=base_ms + 60_000, end_ms=base_ms + 120_000))
        state.add_bar(Bar("AAPL", open=101.4, high=102.5, low=101.3, close=102.4, volume=320, vwap=102.0, start_ms=base_ms + 120_000, end_ms=base_ms + 180_000))
        for index in range(10):
            bid = 101.50 + (index * 0.11)
            ask = bid + 0.015
            state.update_quote(Quote("AAPL", bid=bid, ask=ask, bid_size=30, ask_size=30, timestamp_ms=base_ms + 180_000 + (index * 3_000)))

        with self.assertLogs("strategies.opening_impulse", level="DEBUG") as captured:
            signal = OpeningImpulseStrategy(settings).evaluate(state)

        self.assertIsNone(signal)
        self.assertIn("entry extension", "\n".join(captured.output))

    def test_opening_impulse_uses_bar_confirmation_when_quotes_are_flat(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            opening_impulse_start_minute=0,
            opening_impulse_end_minute=90,
            opening_impulse_window_seconds=30,
            opening_impulse_min_quotes=6,
            opening_impulse_change_pct=0.009,
            opening_impulse_bar_confirmation=True,
            opening_impulse_bar_window=3,
            opening_impulse_bar_change_pct=0.003,
            opening_impulse_bar_volume_ratio=1.5,
            opening_impulse_max_spread_bps=8.0,
            opening_impulse_min_quote_size=25,
        )
        state = SymbolState("AAPL")
        base_ms = 1777037400000
        state.add_bar(Bar("AAPL", open=100.00, high=100.25, low=99.90, close=100.20, volume=100, vwap=100.1, start_ms=base_ms, end_ms=base_ms + 60_000))
        state.add_bar(Bar("AAPL", open=100.20, high=100.50, low=100.10, close=100.45, volume=120, vwap=100.3, start_ms=base_ms + 60_000, end_ms=base_ms + 120_000))
        state.add_bar(Bar("AAPL", open=100.45, high=100.80, low=100.40, close=100.70, volume=260, vwap=100.6, start_ms=base_ms + 120_000, end_ms=base_ms + 180_000))

        for index in range(6):
            bid = 100.69 + (index * 0.001)
            ask = bid + 0.01
            state.update_quote(Quote("AAPL", bid=bid, ask=ask, bid_size=30, ask_size=30, timestamp_ms=base_ms + 180_000 + (index * 3_000)))

        signal = OpeningImpulseStrategy(settings).evaluate(state)

        self.assertIsNotNone(signal)
        self.assertIn("opening bar impulse", signal.reason)
        self.assertGreaterEqual(signal.change_pct, 0.003)

    def test_opening_impulse_bar_signal_does_not_require_quote_window(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            opening_impulse_start_minute=0,
            opening_impulse_end_minute=90,
            opening_impulse_min_quotes=10,
            opening_impulse_bar_confirmation=True,
            opening_impulse_bar_window=3,
            opening_impulse_bar_change_pct=0.003,
            opening_impulse_bar_volume_ratio=1.5,
            opening_impulse_max_spread_bps=8.0,
            opening_impulse_min_quote_size=25,
        )
        state = SymbolState("AAPL")
        base_ms = 1777037400000
        state.update_quote(Quote("AAPL", bid=100.69, ask=100.70, bid_size=30, ask_size=30, timestamp_ms=base_ms))
        state.add_bar(Bar("AAPL", open=100.00, high=100.25, low=99.90, close=100.20, volume=100, vwap=100.1, start_ms=base_ms, end_ms=base_ms + 60_000))
        state.add_bar(Bar("AAPL", open=100.20, high=100.50, low=100.10, close=100.45, volume=120, vwap=100.3, start_ms=base_ms + 60_000, end_ms=base_ms + 120_000))
        state.add_bar(Bar("AAPL", open=100.45, high=100.80, low=100.40, close=100.70, volume=260, vwap=100.6, start_ms=base_ms + 120_000, end_ms=base_ms + 180_000))

        signal = OpeningImpulseStrategy(settings).evaluate(state)

        self.assertIsNotNone(signal)
        self.assertEqual(state.last_event_kind, "bar")
        self.assertIn("opening bar impulse", signal.reason)

    def test_opening_impulse_rejects_wide_spread(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            opening_impulse_start_minute=0,
            opening_impulse_end_minute=90,
            opening_impulse_min_quotes=6,
            opening_impulse_change_pct=0.009,
            opening_impulse_bar_confirmation=True,
            opening_impulse_bar_window=3,
            opening_impulse_bar_change_pct=0.003,
            opening_impulse_bar_volume_ratio=1.5,
            opening_impulse_max_spread_bps=8.0,
            opening_impulse_min_quote_size=25,
        )
        state = SymbolState("AAPL")
        base_ms = 1777037400000
        state.add_bar(Bar("AAPL", open=100.00, high=100.25, low=99.90, close=100.20, volume=100, vwap=100.1, start_ms=base_ms, end_ms=base_ms + 60_000))
        state.add_bar(Bar("AAPL", open=100.20, high=100.50, low=100.10, close=100.45, volume=120, vwap=100.3, start_ms=base_ms + 60_000, end_ms=base_ms + 120_000))
        state.add_bar(Bar("AAPL", open=100.45, high=100.80, low=100.40, close=100.70, volume=260, vwap=100.6, start_ms=base_ms + 120_000, end_ms=base_ms + 180_000))

        for index in range(6):
            state.update_quote(
                Quote(
                    "AAPL",
                    bid=100.60,
                    ask=101.00,
                    bid_size=30,
                    ask_size=30,
                    timestamp_ms=base_ms + 180_000 + (index * 3_000),
                )
            )

        with self.assertLogs("strategies.opening_impulse", level="DEBUG") as captured:
            signal = OpeningImpulseStrategy(settings).evaluate(state)

        self.assertIsNone(signal)
        self.assertIn("spread", "\n".join(captured.output))

    def test_opening_impulse_still_rejects_invalid_quote(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            opening_impulse_start_minute=0,
            opening_impulse_end_minute=90,
            opening_impulse_min_quotes=1,
            opening_impulse_bar_confirmation=True,
            opening_impulse_bar_window=3,
            opening_impulse_bar_change_pct=0.003,
            opening_impulse_bar_volume_ratio=1.5,
        )
        state = SymbolState("AAPL")
        base_ms = 1777037400000
        state.add_bar(Bar("AAPL", open=100.00, high=100.25, low=99.90, close=100.20, volume=100, vwap=100.1, start_ms=base_ms, end_ms=base_ms + 60_000))
        state.add_bar(Bar("AAPL", open=100.20, high=100.50, low=100.10, close=100.45, volume=120, vwap=100.3, start_ms=base_ms + 60_000, end_ms=base_ms + 120_000))
        state.add_bar(Bar("AAPL", open=100.45, high=100.80, low=100.40, close=100.70, volume=260, vwap=100.6, start_ms=base_ms + 120_000, end_ms=base_ms + 180_000))
        state.update_quote(Quote("AAPL", bid=100.60, ask=0.0, bid_size=30, ask_size=30, timestamp_ms=base_ms + 180_000))

        signal = OpeningImpulseStrategy(settings).evaluate(state)

        self.assertIsNone(signal)

    def test_gap_and_go_emits_buy_on_premarket_high_breakout(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            gap_and_go_start_minute=0,
            gap_and_go_end_minute=30,
            gap_and_go_min_gap_pct=0.02,
            gap_and_go_premarket_volume_ratio=2.0,
            gap_and_go_max_spread_bps=10.0,
            gap_and_go_min_price=5.0,
        )
        strategy = GapAndGoStrategy(settings)
        state = SymbolState("AAPL")
        prev_day = market_ms(2026, 4, 23, 15, 0)
        today_pre = market_ms(2026, 4, 24, 8, 0)
        today_open = market_ms(2026, 4, 24, 9, 30)

        for index in range(30):
            start_ms = prev_day + (index * 60_000)
            state.add_bar(Bar("AAPL", open=99.8, high=100.1, low=99.7, close=100.0, volume=100, vwap=100.0, start_ms=start_ms, end_ms=start_ms + 60_000))
        for index in range(6):
            start_ms = today_pre + (index * 60_000)
            price = 102.0 + (index * 0.1)
            state.add_bar(Bar("AAPL", open=price, high=price + 0.2, low=price - 0.1, close=price + 0.05, volume=4_000, vwap=price, start_ms=start_ms, end_ms=start_ms + 60_000))
        state.add_bar(Bar("AAPL", open=102.5, high=102.8, low=102.4, close=102.7, volume=400, vwap=102.6, start_ms=today_open, end_ms=today_open + 60_000))
        state.update_quote(Quote("AAPL", bid=102.81, ask=102.83, bid_size=100, ask_size=100, timestamp_ms=today_open + 65_000))

        signal = strategy.evaluate(state)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.strategy, "gap_and_go")
        self.assertIn("gap_and_go gap", signal.reason)
        self.assertIn("premarket_high", signal.reason)
        self.assertIn("entry_type=", signal.reason)

    def test_gap_and_go_selector_ranks_best_candidates(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            gap_and_go_min_gap_pct=0.02,
            gap_and_go_premarket_volume_ratio=2.0,
            gap_and_go_max_spread_bps=10.0,
            gap_and_go_min_price=5.0,
        )
        leader = SymbolState("AAPL")
        follower = SymbolState("MSFT")
        prev_day = market_ms(2026, 4, 23, 15, 0)
        today_pre = market_ms(2026, 4, 24, 8, 0)
        today_open = market_ms(2026, 4, 24, 9, 30)

        for symbol, state, close_price, pre_open, pre_volume in (
            ("AAPL", leader, 100.0, 103.0, 400),
            ("MSFT", follower, 100.0, 102.2, 240),
        ):
            for index in range(30):
                start_ms = prev_day + (index * 60_000)
                state.add_bar(Bar(symbol, open=99.8, high=100.1, low=99.7, close=close_price, volume=100, vwap=close_price, start_ms=start_ms, end_ms=start_ms + 60_000))
            for index in range(6):
                start_ms = today_pre + (index * 60_000)
                price = pre_open + (index * 0.1)
                state.add_bar(Bar(symbol, open=price, high=price + 0.2, low=price - 0.1, close=price + 0.05, volume=pre_volume, vwap=price, start_ms=start_ms, end_ms=start_ms + 60_000))
            state.add_bar(Bar(symbol, open=pre_open, high=pre_open + 0.4, low=pre_open - 0.1, close=pre_open + 0.2, volume=500, vwap=pre_open + 0.1, start_ms=today_open, end_ms=today_open + 60_000))

        leader.update_quote(Quote("AAPL", bid=103.55, ask=103.57, bid_size=100, ask_size=100, timestamp_ms=today_open + 65_000))
        follower.update_quote(Quote("MSFT", bid=102.70, ask=102.72, bid_size=100, ask_size=100, timestamp_ms=today_open + 65_000))

        ranked = select_gap_and_go.rank_gap_and_go_candidates(
            {"AAPL": leader, "MSFT": follower},
            settings,
            previous_closes={"AAPL": 100.0, "MSFT": 100.0},
            top_n=5,
        )

        self.assertEqual([item.symbol for item in ranked], ["AAPL", "MSFT"])
        self.assertGreater(ranked[0].score, ranked[1].score)

    def test_gap_and_go_selector_uses_premarket_data_before_open(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            gap_and_go_min_gap_pct=0.02,
            gap_and_go_premarket_volume_ratio=2.0,
            gap_and_go_max_spread_bps=10.0,
            gap_and_go_min_price=5.0,
        )
        state = SymbolState("NVDA")
        prev_day = market_ms(2026, 4, 23, 15, 0)
        today_pre = market_ms(2026, 4, 24, 8, 0)

        for index in range(30):
            start_ms = prev_day + (index * 60_000)
            state.add_bar(
                Bar(
                    "NVDA",
                    open=99.8,
                    high=100.1,
                    low=99.7,
                    close=100.0,
                    volume=100,
                    vwap=100.0,
                    start_ms=start_ms,
                    end_ms=start_ms + 60_000,
                )
            )
        for index in range(6):
            start_ms = today_pre + (index * 60_000)
            price = 103.0 + (index * 0.1)
            state.add_bar(
                Bar(
                    "NVDA",
                    open=price,
                    high=price + 0.2,
                    low=price - 0.1,
                    close=price + 0.05,
                    volume=320,
                    vwap=price,
                    start_ms=start_ms,
                    end_ms=start_ms + 60_000,
                )
            )
        state.update_quote(Quote("NVDA", bid=103.55, ask=103.57, bid_size=100, ask_size=100, timestamp_ms=today_pre + 6 * 60_000))

        candidate = select_gap_and_go.score_gap_and_go_candidate(state, settings, previous_close=100.0)

        self.assertIsNotNone(candidate)
        self.assertAlmostEqual(candidate.gap_pct, 0.0357, places=4)
        self.assertAlmostEqual(candidate.open_price, 103.57, places=2)

    def test_gap_and_go_penalty_candidate_keeps_unrankable_symbols(self):
        candidate = select_gap_and_go.penalty_gap_and_go_candidate("AAPL", "universe symbol could not be ranked")

        self.assertEqual(candidate.symbol, "AAPL")
        self.assertEqual(candidate.score, -999.0)
        self.assertIn("universe symbol could not be ranked", candidate.quality_flags)

    def test_gap_and_go_ai_selection_is_bounded_to_ranked_candidates(self):
        candidates = [
            select_gap_and_go.GapAndGoCandidate("AAPL", 7.2, 0.03, 3.4, 4.2, 103.5, 100.0, 102.0, 103.2, False),
            select_gap_and_go.GapAndGoCandidate("MSFT", 6.4, 0.025, 2.8, 5.1, 431.0, 420.0, 429.0, 430.5, False),
        ]

        validated = select_gap_and_go.validated_gap_and_go_selection(
            {
                "adjustments": {
                    "MSFT": {"ai_score_delta": 1.0, "ai_reason": "Tighter premarket setup"},
                    "GONE": {"ai_score_delta": 2.0, "ai_reason": "ignored"},
                },
                "rejected": ["GONE"],
                "risk_note": "test",
            },
            candidates,
            limit=2,
        )

        self.assertEqual(validated["symbols"], ["MSFT", "AAPL"])
        self.assertEqual(validated["ranked"][0]["symbol"], "MSFT")
        self.assertEqual(validated["ranked"][0]["base_score"], 6.4)
        self.assertEqual(validated["ranked"][0]["ai_score_delta"], 1.0)
        self.assertEqual(validated["ranked"][0]["score"], 7.4)
        self.assertEqual(validated["ranked"][0]["ai_reason"], "Tighter premarket setup")

    def test_maha7_ai_selection_is_bounded_to_ranked_candidates(self):
        ranked = [
            {"symbol": "AAPL", "score": 7.2, "selection_stage": "intraday"},
            {"symbol": "MSFT", "score": 6.4, "selection_stage": "intraday"},
        ]

        validated = select_maha7.validated_maha7_selection(
            {
                "adjustments": {
                    "MSFT": {"ai_score_delta": 1.0, "ai_reason": "Cleaner reclaim context"},
                    "GONE": {"ai_score_delta": 2.0, "ai_reason": "ignored"},
                },
                "rejected": ["GONE"],
                "risk_note": "test",
            },
            ranked,
            limit=2,
        )

        self.assertEqual(validated["symbols"], ["MSFT", "AAPL"])
        self.assertEqual(validated["ranked"][0]["symbol"], "MSFT")
        self.assertEqual(validated["ranked"][0]["base_score"], 6.4)
        self.assertEqual(validated["ranked"][0]["ai_score_delta"], 1.0)
        self.assertEqual(validated["ranked"][0]["score"], 7.4)
        self.assertEqual(validated["ranked"][0]["ai_reason"], "Cleaner reclaim context")

    def test_maha7_ai_selection_parses_json_response(self):
        ranked = [{"symbol": "AAPL", "score": 7.2, "selection_stage": "intraday"}]
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"])
        with patch("strategy_selectors.select_maha7.request_json_response", return_value='{"strategy":"maha7","adjustments":{}}'):
            result = select_maha7.ai_maha7_selection(settings, ranked, limit=1)
        self.assertEqual(result["strategy"], "maha7")

    def test_gap_and_go_selector_scores_weak_candidates_instead_of_dropping_them(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            gap_and_go_min_gap_pct=0.02,
            gap_and_go_premarket_volume_ratio=2.0,
            gap_and_go_max_spread_bps=10.0,
            gap_and_go_min_price=5.0,
        )
        state = SymbolState("MSFT")
        prev_day = market_ms(2026, 4, 23, 15, 0)
        today_pre = market_ms(2026, 4, 24, 8, 0)
        today_open = market_ms(2026, 4, 24, 9, 30)

        for index in range(30):
            start_ms = prev_day + (index * 60_000)
            state.add_bar(Bar("MSFT", open=99.8, high=100.1, low=99.7, close=100.0, volume=100, vwap=100.0, start_ms=start_ms, end_ms=start_ms + 60_000))
        for index in range(6):
            start_ms = today_pre + (index * 60_000)
            price = 100.4 + (index * 0.05)
            state.add_bar(Bar("MSFT", open=price, high=price + 0.1, low=price - 0.05, close=price + 0.02, volume=110, vwap=price, start_ms=start_ms, end_ms=start_ms + 60_000))
        state.add_bar(Bar("MSFT", open=100.8, high=101.0, low=100.7, close=100.9, volume=120, vwap=100.85, start_ms=today_open, end_ms=today_open + 60_000))
        state.update_quote(Quote("MSFT", bid=100.92, ask=100.94, bid_size=100, ask_size=100, timestamp_ms=today_open + 65_000))

        candidate = select_gap_and_go.score_gap_and_go_candidate(state, settings, previous_close=100.0)

        self.assertIsNotNone(candidate)
        self.assertIn("gap 0.940% < 2.000%", candidate.quality_flags)
        self.assertIn("premarket volume 0.11x < 2.00x", candidate.quality_flags)

    def test_gap_and_go_selector_scores_candidates_without_premarket_bars(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            gap_and_go_min_gap_pct=0.02,
            gap_and_go_premarket_volume_ratio=2.0,
            gap_and_go_max_spread_bps=10.0,
            gap_and_go_min_price=5.0,
        )
        state = SymbolState("AAPL")
        state.update_quote(Quote("AAPL", bid=101.0, ask=101.02, bid_size=100, ask_size=100, timestamp_ms=market_ms(2026, 4, 24, 3, 30)))

        candidate = select_gap_and_go.score_gap_and_go_candidate(state, settings, previous_close=100.0)

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.symbol, "AAPL")
        self.assertIn("missing premarket high", candidate.quality_flags)
        self.assertIn("premarket volume 0.00x < 2.00x", candidate.quality_flags)

    def test_gap_and_go_selector_flags_vwap_exhaustion_and_prior_range(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            gap_and_go_min_gap_pct=0.02,
            gap_and_go_premarket_volume_ratio=2.0,
            gap_and_go_max_spread_bps=10.0,
            gap_and_go_min_price=5.0,
        )
        state = SymbolState("AAPL")
        prev_day = market_ms(2026, 4, 23, 15, 0)
        today_pre = market_ms(2026, 4, 24, 8, 0)

        for index in range(30):
            start_ms = prev_day + (index * 60_000)
            state.add_bar(
                Bar(
                    "AAPL",
                    open=100.0,
                    high=106.0,
                    low=96.0,
                    close=100.0,
                    volume=100,
                    vwap=100.0,
                    start_ms=start_ms,
                    end_ms=start_ms + 60_000,
                )
            )
        for index in range(6):
            start_ms = today_pre + (index * 60_000)
            state.add_bar(
                Bar(
                    "AAPL",
                    open=109.0,
                    high=111.0,
                    low=108.0,
                    close=109.5,
                    volume=4_000,
                    vwap=110.0,
                    start_ms=start_ms,
                    end_ms=start_ms + 60_000,
                )
            )
        state.update_quote(Quote("AAPL", bid=108.98, ask=109.0, bid_size=100, ask_size=100, timestamp_ms=today_pre + 6 * 60_000))

        candidate = select_gap_and_go.score_gap_and_go_candidate(state, settings, previous_close=100.0)

        self.assertIsNotNone(candidate)
        self.assertTrue(candidate.exhaustion_flag)
        self.assertTrue(candidate.hard_reject)
        self.assertLess(candidate.vwap_distance_pct, 0)
        self.assertGreater(candidate.prev_range_pct, 0.08)
        self.assertIn("overextended gap", candidate.quality_flags)
        self.assertIn("price below VWAP", candidate.quality_flags)
        self.assertIn("weak gap vs prior range", candidate.quality_flags)

    def test_gap_and_go_selector_keeps_symbols_without_quotes_as_penalty_rows(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test")
        ranked = select_gap_and_go.rank_gap_and_go_candidates(
            {"AAPL": SymbolState("AAPL")},
            settings,
            previous_closes={},
            top_n=5,
        )

        self.assertEqual([item.symbol for item in ranked], ["AAPL"])
        self.assertEqual(ranked[0].score, -999.0)
        self.assertIn("insufficient quote or market data", ranked[0].quality_flags)

    def test_gap_and_go_skips_entries_outside_window_without_log_noise(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            gap_and_go_start_minute=0,
            gap_and_go_end_minute=30,
        )
        strategy = GapAndGoStrategy(settings)
        state = SymbolState("AAPL")
        state.update_quote(
            Quote(
                "AAPL",
                bid=105.0,
                ask=105.02,
                bid_size=100,
                ask_size=100,
                timestamp_ms=market_ms(2026, 4, 24, 10, 15),
            )
        )

        signal = strategy.evaluate(state)
        self.assertIsNone(signal)
        self.assertEqual(strategy._last_reject_log_ms, {})

    def test_gap_and_go_throttles_repeated_rejection_logs(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            gap_and_go_start_minute=0,
            gap_and_go_end_minute=30,
        )
        strategy = GapAndGoStrategy(settings)
        state = SymbolState("AAPL")
        start_ms = market_ms(2026, 4, 24, 9, 35)

        with self.assertLogs("strategies.gap_and_go", level="DEBUG") as captured:
            for index in range(20):
                state.update_quote(
                    Quote(
                        "AAPL",
                        bid=105.0,
                        ask=105.02,
                        bid_size=100,
                        ask_size=100,
                        timestamp_ms=start_ms + (index * 1_000),
                    )
                )
                self.assertIsNone(strategy.evaluate(state))

        outside_window_logs = [
            line for line in captured.output if "missing previous close" in line
        ]
        self.assertEqual(len(outside_window_logs), 2)

    def test_opening_impulse_enters_opening_range_breakout(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            opening_impulse_start_minute=0,
            opening_impulse_end_minute=90,
            opening_impulse_min_quotes=6,
            opening_impulse_change_pct=0.009,
            opening_impulse_bar_confirmation=False,
            opening_impulse_range_minutes=5,
            opening_impulse_enable_range_breakout=True,
            opening_impulse_enable_range_reversal=False,
            opening_impulse_range_volume_ratio=1.2,
            opening_impulse_max_spread_bps=8.0,
            opening_impulse_min_quote_size=25,
        )
        state = SymbolState("AAPL")
        base_ms = market_ms(2026, 4, 24, 9, 30)
        range_bars = [
            Bar("AAPL", open=100.00, high=100.20, low=99.80, close=100.05, volume=100, vwap=100.0, start_ms=base_ms, end_ms=base_ms + 60_000),
            Bar("AAPL", open=100.05, high=100.30, low=99.95, close=100.10, volume=100, vwap=100.1, start_ms=base_ms + 60_000, end_ms=base_ms + 120_000),
            Bar("AAPL", open=100.10, high=100.35, low=100.00, close=100.20, volume=100, vwap=100.2, start_ms=base_ms + 120_000, end_ms=base_ms + 180_000),
            Bar("AAPL", open=100.20, high=100.40, low=100.10, close=100.35, volume=100, vwap=100.3, start_ms=base_ms + 180_000, end_ms=base_ms + 240_000),
            Bar("AAPL", open=100.35, high=100.50, low=100.20, close=100.45, volume=100, vwap=100.4, start_ms=base_ms + 240_000, end_ms=base_ms + 300_000),
            Bar("AAPL", open=100.45, high=100.90, low=100.40, close=100.82, volume=220, vwap=100.7, start_ms=base_ms + 300_000, end_ms=base_ms + 360_000),
        ]
        for item in range_bars:
            state.add_bar(item)
        for index in range(6):
            bid = 100.84 + (index * 0.001)
            state.update_quote(Quote("AAPL", bid=bid, ask=bid + 0.01, bid_size=30, ask_size=30, timestamp_ms=base_ms + 360_000 + (index * 3_000)))

        signal = OpeningImpulseStrategy(settings).evaluate(state)

        self.assertIsNotNone(signal)
        self.assertIn("opening_range_breakout", signal.reason)

    def test_opening_impulse_enters_opening_range_reversal(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            opening_impulse_start_minute=0,
            opening_impulse_end_minute=90,
            opening_impulse_min_quotes=6,
            opening_impulse_change_pct=0.009,
            opening_impulse_bar_confirmation=False,
            opening_impulse_range_minutes=5,
            opening_impulse_enable_range_breakout=False,
            opening_impulse_enable_range_reversal=True,
            opening_impulse_range_reversal_min_drop_pct=0.005,
            opening_impulse_range_volume_ratio=1.2,
            opening_impulse_max_spread_bps=8.0,
            opening_impulse_min_quote_size=25,
        )
        state = SymbolState("AAPL")
        base_ms = market_ms(2026, 4, 24, 9, 30)
        bars = [
            Bar("AAPL", open=100.00, high=100.10, low=99.20, close=99.40, volume=120, vwap=99.6, start_ms=base_ms, end_ms=base_ms + 60_000),
            Bar("AAPL", open=99.40, high=99.60, low=98.80, close=99.10, volume=120, vwap=99.2, start_ms=base_ms + 60_000, end_ms=base_ms + 120_000),
            Bar("AAPL", open=99.10, high=99.50, low=98.90, close=99.30, volume=120, vwap=99.2, start_ms=base_ms + 120_000, end_ms=base_ms + 180_000),
            Bar("AAPL", open=99.30, high=99.70, low=99.20, close=99.55, volume=120, vwap=99.4, start_ms=base_ms + 180_000, end_ms=base_ms + 240_000),
            Bar("AAPL", open=99.55, high=99.80, low=99.40, close=99.65, volume=120, vwap=99.6, start_ms=base_ms + 240_000, end_ms=base_ms + 300_000),
            Bar("AAPL", open=99.65, high=100.05, low=99.60, close=99.75, volume=220, vwap=99.8, start_ms=base_ms + 300_000, end_ms=base_ms + 360_000),
        ]
        for item in bars:
            state.add_bar(item)
        for index in range(6):
            bid = 99.76 + (index * 0.001)
            state.update_quote(Quote("AAPL", bid=bid, ask=bid + 0.01, bid_size=30, ask_size=30, timestamp_ms=base_ms + 360_000 + (index * 3_000)))

        signal = OpeningImpulseStrategy(settings).evaluate(state)

        self.assertIsNotNone(signal)
        self.assertIn("opening_range_reversal", signal.reason)

    def test_opening_impulse_logs_rejection_reason(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            opening_impulse_min_quotes=10,
        )
        state = SymbolState("AAPL")
        state.update_quote(
            Quote(
                "AAPL",
                bid=100.0,
                ask=100.02,
                bid_size=100,
                ask_size=100,
                timestamp_ms=market_ms(2026, 4, 24, 9, 45),
            )
        )

        with self.assertLogs("strategies.opening_impulse", level="DEBUG") as captured:
            signal = OpeningImpulseStrategy(settings).evaluate(state)

        self.assertIsNone(signal)
        self.assertIn("quotes 1 < 10", "\n".join(captured.output))

    def test_opening_impulse_skips_entries_after_end_minute(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            opening_impulse_start_minute=0,
            opening_impulse_end_minute=150,
            opening_impulse_min_quotes=1,
        )
        state = SymbolState("AAPL")
        state.update_quote(
            Quote(
                "AAPL",
                bid=100.0,
                ask=100.02,
                bid_size=100,
                ask_size=100,
                timestamp_ms=market_ms(2026, 4, 24, 12, 5),
            )
        )

        signal = OpeningImpulseStrategy(settings).evaluate(state)

        self.assertIsNone(signal)

    def test_opening_impulse_exit_on_momentum_fade(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            opening_impulse_min_hold_seconds=0,
            opening_impulse_exit_negative_steps=3,
        )
        strategy = OpeningImpulseStrategy(settings)
        state = SymbolState("AAPL")
        state.quotes = deque(
            [
                Quote("AAPL", bid=101.05, ask=101.07, bid_size=20, ask_size=20, timestamp_ms=10_000),
                Quote("AAPL", bid=101.00, ask=101.02, bid_size=20, ask_size=20, timestamp_ms=12_000),
                Quote("AAPL", bid=100.97, ask=100.99, bid_size=20, ask_size=20, timestamp_ms=14_000),
                Quote("AAPL", bid=100.96, ask=100.98, bid_size=20, ask_size=20, timestamp_ms=16_000),
                Quote("AAPL", bid=100.95, ask=100.97, bid_size=20, ask_size=20, timestamp_ms=18_000),
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

    def test_maha7_emits_buy_after_rsi_reclaim(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            maha7_trend_min_bars=1,
        )
        strategy = Maha7Strategy(settings)
        state = self._maha7_reclaim_state()

        signal = strategy.evaluate(state)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.strategy, "maha7")
        self.assertEqual(signal.side, "BUY")
        self.assertAlmostEqual(signal.stop_price, 113.13675, places=3)
        self.assertIn("optimized entry", signal.reason)

    def test_maha7_skips_rsi_chop(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"])
        strategy = Maha7Strategy(settings)
        state = self._maha7_reclaim_state()

        with patch.object(strategy, "_last_n_green_bars", return_value=False):
            signal = strategy.evaluate(state)

        self.assertIsNone(signal)

    def test_maha7_skips_current_neutral_rsi(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            maha7_min_r_pct=0.02,
        )
        strategy = Maha7Strategy(settings)
        state = self._maha7_reclaim_state()

        signal = strategy.evaluate(state)

        self.assertIsNone(signal)

    def test_maha7_requires_stabilized_ma_cross(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            maha7_trend_min_bars=99,
        )
        strategy = Maha7Strategy(settings)

        signal = strategy.evaluate(self._maha7_reclaim_state())

        self.assertIsNone(signal)

    def test_maha7_requires_rsi_duration(self):
        """Deprecated name: entry now uses min R% band — too-small R rejects."""
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            maha7_min_r_pct=0.02,
        )
        strategy = Maha7Strategy(settings)

        signal = strategy.evaluate(self._maha7_reclaim_state())

        self.assertIsNone(signal)

    def test_maha7_requires_vwap_distance(self):
        """Late chase: entry must not be within max_chase_pct of recent high."""
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            maha7_max_chase_pct=0.05,
        )
        strategy = Maha7Strategy(settings)

        signal = strategy.evaluate(self._maha7_reclaim_state())

        self.assertIsNone(signal)

    def test_maha7_requires_volume_confirmation(self):
        """No entry when neither shallow pullback nor continuation (volume) qualifies."""
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            maha7_pullback_ma7_distance_pct=0.0005,
            maha7_continuation_volume_ratio=5.0,
        )
        strategy = Maha7Strategy(settings)

        signal = strategy.evaluate(self._maha7_reclaim_state())

        self.assertIsNone(signal)

    def test_maha7_partial_and_final_exit(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"])
        strategy = Maha7Strategy(settings)
        position = Position(
            symbol="AAPL",
            strategy="maha7",
            shares=10,
            entry_price=112.0,
            entry_ms=market_ms(2026, 4, 24, 10, 9),
            target_price=120.0,
            stop_price=104.0,
            initial_stop_price=104.0,
        )
        state = SymbolState("AAPL")
        state.update_quote(Quote("AAPL", bid=116.05, ask=116.15, bid_size=100, ask_size=100, timestamp_ms=market_ms(2026, 4, 24, 10, 15)))

        partial = strategy.should_exit(state, position)

        self.assertEqual(partial.reason, "partial 0.5R")
        self.assertEqual(partial.shares, 5)
        self.assertTrue(partial.mark_partial)

        position.partial_exit_taken = True
        state.update_quote(Quote("AAPL", bid=128.05, ask=128.15, bid_size=100, ask_size=100, timestamp_ms=market_ms(2026, 4, 24, 10, 20)))
        final = strategy.should_exit(state, position)

        self.assertEqual(final.reason, "target 2.0R")

    def test_maha7_no_hard_target_without_flag(self):
        """With hard 2R target off, a quote at 2R+ does not force a full exit (runner path)."""
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            maha7_hard_target_r_exit=False,
        )
        strategy = Maha7Strategy(settings)
        position = Position(
            symbol="AAPL",
            strategy="maha7",
            shares=10,
            entry_price=112.0,
            entry_ms=market_ms(2026, 4, 24, 10, 9),
            target_price=120.0,
            stop_price=104.0,
            initial_stop_price=104.0,
            partial_exit_taken=True,
        )
        state = SymbolState("AAPL")
        state.update_quote(
            Quote(
                "AAPL",
                bid=128.05,
                ask=128.15,
                bid_size=100,
                ask_size=100,
                timestamp_ms=market_ms(2026, 4, 24, 10, 20),
            )
        )
        self.assertIsNone(strategy.should_exit(state, position))

    def test_maha7_disable_ma7_exit_skips_confirmed_breakdown(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            maha7_disable_ma7_exit=True,
            maha7_min_hold_seconds=0,
            maha7_runner_confirm_break_bars=2,
        )
        strategy = Maha7Strategy(settings)
        state = self._maha7_reclaim_state()
        last_bar = state.bars[-1]
        t0 = last_bar.end_ms
        state.add_bar(
            Bar(
                "AAPL",
                open=114.0,
                high=114.1,
                low=110.0,
                close=110.5,
                volume=1_500,
                vwap=110.5,
                start_ms=t0,
                end_ms=t0 + 60_000,
            )
        )
        t1 = state.bars[-1].end_ms
        state.add_bar(
            Bar(
                "AAPL",
                open=110.0,
                high=110.3,
                low=104.5,
                close=105.0,
                volume=1_500,
                vwap=105.0,
                start_ms=t1,
                end_ms=t1 + 60_000,
            )
        )
        position = Position(
            symbol="AAPL",
            strategy="maha7",
            shares=10,
            entry_price=112.0,
            entry_ms=state.last_event_ms - 300_000,
            target_price=120.0,
            stop_price=104.0,
            initial_stop_price=104.0,
        )
        self.assertIsNone(strategy.should_exit(state, position))

    def test_maha7_runner_ignores_quote_dip_below_ma7(self):
        """v2.1: after partial, a quote below MA7 alone does not exit if closes are still holding MA7."""
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            maha7_min_hold_seconds=0,
            maha7_hard_target_r_exit=False,
        )
        strategy = Maha7Strategy(settings)
        sym = "AAPL"
        base = market_ms(2026, 4, 24, 9, 30)
        state = SymbolState(sym)
        entry_ms = base + 6 * 60_000 + 1
        for i in range(50):
            end = base + (i + 1) * 60_000
            c = 95.0 + i * 0.95
            state.add_bar(Bar(sym, c - 0.1, c + 0.2, c - 0.2, c, 1_200, c, end - 60_000, end))
        state.update_quote(
            Quote(sym, bid=129.95, ask=130.05, bid_size=100, ask_size=100, timestamp_ms=state.bars[-1].end_ms)
        )
        position = Position(
            symbol=sym,
            strategy="maha7",
            shares=10,
            entry_price=100.0,
            entry_ms=entry_ms,
            target_price=130.0,
            stop_price=90.0,
            initial_stop_price=90.0,
            partial_exit_taken=True,
            # Mid ~130: above runner peak-pullback floor vs max 131; still below last bars' MA7; 2R off via settings.
            max_price=131.0,
        )
        self.assertIsNone(strategy.should_exit(state, position))

    def test_maha7_min_hold_blocks_soft_exit(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            maha7_min_hold_seconds=120,
            maha7_runner_confirm_break_bars=2,
            maha7_early_loss_cut_pct=0.1,
        )
        strategy = Maha7Strategy(settings)
        state = self._maha7_reclaim_state()
        last_bar = state.bars[-1]
        t0 = last_bar.end_ms
        # Two consecutive closes below each bar's MA7; second bar also enforces non-rising MA7 for breakdown.
        state.add_bar(
            Bar(
                "AAPL",
                open=114.0,
                high=114.1,
                low=110.0,
                close=110.5,
                volume=1_500,
                vwap=110.5,
                start_ms=t0,
                end_ms=t0 + 60_000,
            )
        )
        t1 = state.bars[-1].end_ms
        state.add_bar(
            Bar(
                "AAPL",
                open=110.0,
                high=110.3,
                low=104.5,
                close=105.0,
                volume=1_500,
                vwap=105.0,
                start_ms=t1,
                end_ms=t1 + 60_000,
            )
        )
        position = Position(
            symbol="AAPL",
            strategy="maha7",
            shares=10,
            entry_price=112.0,
            entry_ms=state.last_event_ms - 60_000,
            target_price=120.0,
            stop_price=104.0,
            initial_stop_price=104.0,
        )

        self.assertIsNone(strategy.should_exit(state, position))

        position.entry_ms = state.last_event_ms - 180_000
        decision = strategy.should_exit(state, position)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "MA7 confirmed breakdown")

    def test_opening_impulse_min_hold_delays_structure_exit(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            opening_impulse_min_hold_seconds=30,
            opening_impulse_exit_negative_steps=99,
            opening_impulse_retrace_from_high_pct=0.1,
        )
        strategy = OpeningImpulseStrategy(settings)
        state = SymbolState("AAPL")
        state.quotes = deque(
            [
                Quote("AAPL", bid=100.98, ask=101.00, bid_size=20, ask_size=20, timestamp_ms=10_000),
                Quote("AAPL", bid=100.97, ask=100.99, bid_size=20, ask_size=20, timestamp_ms=12_000),
                Quote("AAPL", bid=100.96, ask=100.98, bid_size=20, ask_size=20, timestamp_ms=14_000),
                Quote("AAPL", bid=100.95, ask=100.97, bid_size=20, ask_size=20, timestamp_ms=16_000),
            ],
            maxlen=2400,
        )
        state.quote = state.quotes[-1]
        state.last_event_kind = "quote"
        state.last_event_ms = state.quote.timestamp_ms
        position = Position(
            symbol="AAPL",
            strategy="opening_impulse",
            shares=10,
            entry_price=101.0,
            entry_ms=5_000,
            target_price=103.02,
            stop_price=100.4,
        )

        state.add_bar(Bar("AAPL", open=101.00, high=101.10, low=100.97, close=101.00, volume=100, vwap=101.0, start_ms=8_000, end_ms=9_000))
        state.add_bar(Bar("AAPL", open=101.00, high=101.05, low=100.95, close=100.96, volume=100, vwap=101.0, start_ms=9_000, end_ms=10_000))

        self.assertIsNone(strategy.should_exit(state, position))

        position.entry_ms = -20_000
        decision = strategy.should_exit(state, position)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "break structure")

    def test_macd_min_hold_delays_soft_exits(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["SMR"],
            macd_min_hold_seconds=60,
            macd_trailing_activation_pct=0.003,
            macd_trailing_stop_pct=0.0045,
        )
        strategy = MACDEarlyImpulseStrategy(settings)
        state = SymbolState("SMR")
        state.update_quote(
            Quote(
                "SMR",
                bid=100.45,
                ask=100.47,
                bid_size=100,
                ask_size=100,
                timestamp_ms=market_ms(2026, 5, 8, 13, 1),
            )
        )
        position = Position(
            symbol="SMR",
            strategy="macd_early_impulse",
            shares=10,
            entry_price=100.0,
            entry_ms=state.last_event_ms - 15_000,
            target_price=101.2,
            stop_price=99.65,
            max_price=101.0,
        )

        self.assertIsNone(strategy.should_exit(state, position))

        position.entry_ms = state.last_event_ms - 75_000
        decision = strategy.should_exit(state, position)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "trailing stop")

    def test_macd_target_profit_not_blocked_by_min_hold(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["SMR"],
            macd_min_hold_seconds=60,
            macd_target_profit_pct=0.012,
        )
        strategy = MACDEarlyImpulseStrategy(settings)
        state = SymbolState("SMR")
        state.update_quote(
            Quote(
                "SMR",
                bid=101.25,
                ask=101.27,
                bid_size=100,
                ask_size=100,
                timestamp_ms=market_ms(2026, 5, 8, 13, 1),
            )
        )
        position = Position(
            symbol="SMR",
            strategy="macd_early_impulse",
            shares=10,
            entry_price=100.0,
            entry_ms=state.last_event_ms - 15_000,
            target_price=101.2,
            stop_price=99.65,
        )

        decision = strategy.should_exit(state, position)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "target profit")

    def test_macd_rejects_negative_histogram_even_if_rising(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["SMR"],
            macd_hist_threshold=0.00001,
            macd_volume_ratio=1.0,
            macd_chop_range_pct=0.0001,
        )
        strategy = MACDEarlyImpulseStrategy(settings)
        state = SymbolState("SMR")
        base_ms = market_ms(2026, 5, 8, 13, 0)
        for index in range(25):
            close = 100.0 + index * 0.05
            state.add_bar(
                Bar(
                    "SMR",
                    open=close - 0.04,
                    high=close + 0.05,
                    low=close - 0.05,
                    close=close,
                    volume=1_000,
                    vwap=close - 0.1,
                    start_ms=base_ms + index * 60_000,
                    end_ms=base_ms + (index + 1) * 60_000,
                )
            )
        state.update_quote(
            Quote(
                "SMR",
                bid=101.24,
                ask=101.25,
                bid_size=100,
                ask_size=100,
                timestamp_ms=state.bars[-1].end_ms,
            )
        )

        with patch.object(strategy, "_compute_macd", return_value=([0.0], [0.0], [-0.003, -0.002, -0.001])):
            signal = strategy.evaluate(state)

        self.assertIsNone(signal)

    def test_macd_enters_on_minute_reacceleration_reclaim(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["SMR"],
            macd_hist_threshold=0.00001,
            macd_volume_ratio=1.2,
            macd_chop_range_pct=0.0001,
        )
        strategy = MACDEarlyImpulseStrategy(settings)
        state = SymbolState("SMR")
        base_ms = market_ms(2026, 5, 8, 13, 0)
        for index in range(24):
            close = 100.0 + index * 0.08
            state.add_bar(
                Bar(
                    "SMR",
                    open=close - 0.04,
                    high=close + 0.08,
                    low=close - 0.08,
                    close=close,
                    volume=1_000,
                    vwap=close - 0.15,
                    start_ms=base_ms + index * 60_000,
                    end_ms=base_ms + (index + 1) * 60_000,
                )
            )
        state.add_bar(
            Bar(
                "SMR",
                open=101.84,
                high=102.08,
                low=101.80,
                close=102.02,
                volume=2_000,
                vwap=101.88,
                start_ms=base_ms + 24 * 60_000,
                end_ms=base_ms + 25 * 60_000,
            )
        )
        state.update_quote(
            Quote(
                "SMR",
                bid=102.01,
                ask=102.03,
                bid_size=100,
                ask_size=100,
                timestamp_ms=state.bars[-1].end_ms,
            )
        )

        with patch.object(
            strategy,
            "_compute_macd",
            return_value=(
                [0.010, 0.014, 0.018, 0.024],
                [0.009, 0.012, 0.015, 0.019],
                [0.0010, 0.0015, 0.0014, 0.0022],
            ),
        ):
            signal = strategy.evaluate(state)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.reason, "macd early impulse entry")

    def test_macd_rejects_minute_histogram_fade_entry(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["SMR"],
            macd_hist_threshold=0.00001,
            macd_volume_ratio=1.2,
            macd_chop_range_pct=0.0001,
        )
        strategy = MACDEarlyImpulseStrategy(settings)
        state = SymbolState("SMR")
        base_ms = market_ms(2026, 5, 8, 13, 0)
        for index in range(25):
            close = 100.0 + index * 0.03
            state.add_bar(
                Bar(
                    "SMR",
                    open=close - 0.04,
                    high=close + 0.08,
                    low=close - 0.08,
                    close=close,
                    volume=2_000 if index == 24 else 1_000,
                    vwap=close - 0.15,
                    start_ms=base_ms + index * 60_000,
                    end_ms=base_ms + (index + 1) * 60_000,
                )
            )
        state.update_quote(
            Quote(
                "SMR",
                bid=101.92,
                ask=101.94,
                bid_size=100,
                ask_size=100,
                timestamp_ms=state.bars[-1].end_ms,
            )
        )

        with patch.object(
            strategy,
            "_compute_macd",
            return_value=(
                [0.010, 0.014, 0.018, 0.024],
                [0.009, 0.012, 0.015, 0.019],
                [0.0010, 0.0022, 0.0020, 0.0018],
            ),
        ):
            signal = strategy.evaluate(state)

        self.assertIsNone(signal)

    def test_macd_runner_mode_allows_strong_reclaim_without_perfect_histogram(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["SMR"],
            macd_chop_range_pct=0.0001,
        )
        strategy = MACDEarlyImpulseStrategy(settings)
        strategy._runner_plan_ranks["SMR"] = 1
        state = SymbolState("SMR")
        base_ms = market_ms(2026, 5, 8, 13, 0)
        for index in range(24):
            close = 100.0 + index * 0.10
            volume = 1_000
            state.add_bar(
                Bar(
                    "SMR",
                    open=close - 0.06,
                    high=close + 0.10,
                    low=close - 0.10,
                    close=close,
                    volume=volume,
                    vwap=close - 0.18,
                    start_ms=base_ms + index * 60_000,
                    end_ms=base_ms + (index + 1) * 60_000,
                )
            )
        state.add_bar(
            Bar(
                "SMR",
                open=102.22,
                high=102.54,
                low=102.18,
                close=102.46,
                volume=1_000,
                vwap=102.18,
                start_ms=base_ms + 24 * 60_000,
                end_ms=base_ms + 25 * 60_000,
            )
        )
        state.update_quote(
            Quote(
                "SMR",
                bid=102.45,
                ask=102.47,
                bid_size=100,
                ask_size=100,
                timestamp_ms=state.bars[-1].end_ms,
            )
        )

        with patch.object(
            strategy,
            "_compute_macd",
            return_value=(
                [0.18, 0.24, 0.29, 0.33],
                [0.14, 0.19, 0.24, 0.29],
                [0.030, 0.050, 0.045, 0.040],
            ),
        ):
            signal = strategy.evaluate(state)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.reason, "macd early impulse entry")
        self.assertLess(signal.stop_price, signal.price * (1.0 - settings.macd_stop_loss_pct))

    def test_macd_runner_mode_holds_through_small_early_pullback(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["SMR"],
        )
        strategy = MACDEarlyImpulseStrategy(settings)
        strategy._runner_plan_ranks["SMR"] = 1
        state = SymbolState("SMR")
        base_ms = market_ms(2026, 5, 8, 13, 0)
        for index in range(5):
            close = 100.0 + index * 0.20
            state.add_bar(
                Bar(
                    "SMR",
                    open=close - 0.05,
                    high=close + 0.10,
                    low=close - 0.10,
                    close=close,
                    volume=1_200,
                    vwap=close - 0.12,
                    start_ms=base_ms + index * 60_000,
                    end_ms=base_ms + (index + 1) * 60_000,
                )
            )
        state.update_quote(
            Quote(
                "SMR",
                bid=99.69,
                ask=99.71,
                bid_size=100,
                ask_size=100,
                timestamp_ms=base_ms + 60_000,
            )
        )
        position = Position(
            symbol="SMR",
            strategy="macd_early_impulse",
            shares=10,
            entry_price=100.0,
            entry_ms=base_ms,
            target_price=101.0,
            stop_price=99.45,
            initial_stop_price=99.45,
            max_price=100.2,
            last_high_ts=base_ms,
            original_shares=10,
        )

        decision = strategy.should_exit(state, position)

        self.assertIsNone(decision)

    def test_macd_selector_ranks_daily_macd_reclaim_reversal(self):
        def daily_bars_from_changes(symbol: str, changes: list[float]) -> list[Bar]:
            price = 100.0
            bars: list[Bar] = []
            base_ms = market_ms(2026, 1, 1, 16, 0)
            bars.append(daily_bar_with_volume(symbol, price, price * 0.99, price * 1.01, 1_000_000, base_ms))
            for index, change in enumerate(changes, start=1):
                price *= 1.0 + change
                volume = 2_000_000 if index == len(changes) else 1_000_000
                bars.append(
                    daily_bar_with_volume(
                        symbol,
                        price,
                        price * 0.99,
                        price * 1.01,
                        volume,
                        base_ms + index * 86_400_000,
                    )
                )
            return bars

        reversal_changes = [0.001] * 20 + [-0.04] * 8 + [0.015] * 8 + [0.02] * 5
        zero_reclaim_changes = [0.002] * 20 + [-0.04] * 8 + [0.015] * 8 + [0.03] * 8
        stale_changes = [0.001] * 20 + [-0.002] * 20

        candidates, rejected, stage_counts = select_macd_early_impulse.rank_candidates(
            ["TURN", "ZERO", "STALE"],
            {
                "TURN": daily_bars_from_changes("TURN", reversal_changes),
                "ZERO": daily_bars_from_changes("ZERO", zero_reclaim_changes),
                "STALE": daily_bars_from_changes("STALE", stale_changes),
            },
        )

        selected = {candidate.symbol: candidate for candidate in candidates}
        self.assertEqual(set(selected), {"TURN", "ZERO", "STALE"})
        self.assertLess(selected["TURN"].daily_macd, 0)
        self.assertEqual(selected["TURN"].macd_zone, "negative_reclaim")
        self.assertGreater(selected["TURN"].daily_hist, 0)
        self.assertEqual(selected["TURN"].quality_flags, ())
        self.assertGreaterEqual(selected["TURN"].daily_volume_ratio, 1.0)
        self.assertTrue(selected["TURN"].above_key_ma_structure)
        self.assertGreater(selected["ZERO"].daily_macd, 0)
        self.assertEqual(selected["ZERO"].macd_zone, "zero_reclaim")
        self.assertLessEqual(selected["ZERO"].ema_extension_pct, select_macd_early_impulse.DAILY_MAX_EMA_EXTENSION_PCT)
        self.assertEqual(rejected, [])
        self.assertLess(selected["STALE"].score, selected["TURN"].score)
        self.assertTrue(selected["STALE"].quality_flags)
        self.assertEqual(stage_counts["passed_golden_cross"], 2)
        self.assertEqual(stage_counts["passed_not_overextended"], 3)

        plan = select_macd_early_impulse.deterministic_plan(candidates, rejected, "macd_early_impulse", 2)
        self.assertEqual(plan["selection_stage"], "ranked")
        self.assertEqual(plan["symbols"], ["ZERO", "TURN"])

    def test_macd_ai_selection_can_reorder_meaningful_score_gap(self):
        ranked = [
            {"symbol": "AAPL", "score": 112.0, "macd_zone": "zero_reclaim"},
            {"symbol": "MSFT", "score": 101.0, "macd_zone": "negative_reclaim"},
            {"symbol": "NVDA", "score": 70.0, "macd_zone": "positive_impulse"},
        ]
        ai_plan = {
            "adjustments": {
                "AAPL": {"ai_score_delta": -30.0, "ai_reason": "too extended"},
                "MSFT": {"ai_score_delta": 30.0, "ai_reason": "cleaner daily MACD"},
                "FAKE": {"ai_score_delta": 15.0, "ai_reason": "not allowed"},
            },
            "rejected": ["FAKE"],
            "risk_note": "bounded test",
        }

        validated = select_macd_early_impulse.validated_macd_selection(ai_plan, ranked, 2)

        self.assertEqual(validated["symbols"], ["MSFT", "AAPL"])
        self.assertEqual(validated["ranked"][0]["ai_score_delta"], select_macd_early_impulse.AI_SCORE_DELTA_LIMIT)
        self.assertEqual(validated["ranked"][1]["ai_score_delta"], -select_macd_early_impulse.AI_SCORE_DELTA_LIMIT)

    def test_stoch_macd_selector_prefers_daily_confirmed_stack(self):
        def daily_bars_from_closes(symbol: str, closes: list[float]) -> list[Bar]:
            base_ms = market_ms(2026, 1, 1, 16, 0)
            bars: list[Bar] = []
            for index, close in enumerate(closes):
                previous = closes[index - 1] if index else close
                high = max(close, previous) * 1.012
                low = min(close, previous) * 0.988
                volume = 2_000_000 if index == len(closes) - 1 else 1_000_000
                bars.append(daily_bar_with_volume(symbol, close, low, high, volume, base_ms + index * 86_400_000))
            return bars

        base = [100 + index * 0.08 for index in range(35)]
        confirmed = base + [102, 100, 98, 96, 94, 92, 90, 88, 89, 91, 94, 98, 103, 108, 113]
        weak_stoch = base + [102, 100, 98, 96, 94, 92, 90, 88, 89, 91, 94, 98, 103, 95, 90]
        weak_macd = base + [102, 101.8, 101.6, 101.5, 101.4, 101.3, 101.2, 101.1, 101.0, 100.9, 100.8, 100.7]

        candidates, rejected, stage_counts = select_stoch_macd_reversal.rank_candidates(
            ["CONFIRMED", "WEAK_STOCH", "WEAK_MACD"],
            {
                "CONFIRMED": daily_bars_from_closes("CONFIRMED", confirmed),
                "WEAK_STOCH": daily_bars_from_closes("WEAK_STOCH", weak_stoch),
                "WEAK_MACD": daily_bars_from_closes("WEAK_MACD", weak_macd),
            },
        )

        selected = {candidate.symbol: candidate for candidate in candidates}
        self.assertEqual(rejected, [])
        self.assertEqual(selected["CONFIRMED"].setup_stage, "confirmed_stack")
        self.assertGreater(selected["CONFIRMED"].ema_confirm, selected["CONFIRMED"].supertrend)
        self.assertGreater(selected["CONFIRMED"].daily_macd, selected["CONFIRMED"].daily_signal)
        self.assertGreaterEqual(selected["CONFIRMED"].daily_macd, 0)
        self.assertGreater(selected["CONFIRMED"].stoch_k, selected["CONFIRMED"].stoch_d)
        self.assertEqual(selected["WEAK_STOCH"].setup_stage, "not_confirmed")
        self.assertGreater(selected["CONFIRMED"].score, selected["WEAK_STOCH"].score)
        self.assertGreater(selected["CONFIRMED"].score, selected["WEAK_MACD"].score)
        self.assertGreaterEqual(stage_counts["passed_trend_confirmed"], 1)
        self.assertGreaterEqual(stage_counts["passed_macd_confirmed"], 1)
        self.assertGreaterEqual(stage_counts["passed_stoch_bullish"], 1)

        plan = select_stoch_macd_reversal.deterministic_plan(
            candidates,
            rejected,
            "stoch_macd_reversal",
            2,
            filter_stage_counts=stage_counts,
        )
        self.assertEqual(plan["strategy"], "stoch_macd_reversal")
        self.assertEqual(plan["symbols"][0], "CONFIRMED")
        self.assertEqual(plan["settings"]["filter_thresholds"]["indicator_input"], "daily OHLCV bars")

    def test_stoch_macd_ai_selection_can_reorder_meaningful_score_gap(self):
        ranked = [
            {"symbol": "AAPL", "score": 112.0, "setup_stage": "confirmed_stack"},
            {"symbol": "MSFT", "score": 101.0, "setup_stage": "confirmed_stack"},
            {"symbol": "NVDA", "score": 70.0, "setup_stage": "not_confirmed"},
        ]
        ai_plan = {
            "adjustments": {
                "AAPL": {"ai_score_delta": -30.0, "ai_reason": "too extended"},
                "MSFT": {"ai_score_delta": 30.0, "ai_reason": "cleaner daily stack"},
                "FAKE": {"ai_score_delta": 15.0, "ai_reason": "not allowed"},
            },
            "rejected": ["FAKE"],
            "risk_note": "bounded test",
        }

        validated = select_stoch_macd_reversal.validated_stoch_macd_selection(ai_plan, ranked, 2)

        self.assertEqual(validated["symbols"], ["MSFT", "AAPL"])
        self.assertEqual(validated["ranked"][0]["ai_score_delta"], select_stoch_macd_reversal.AI_SCORE_DELTA_LIMIT)
        self.assertEqual(validated["ranked"][1]["ai_score_delta"], -select_stoch_macd_reversal.AI_SCORE_DELTA_LIMIT)

    def test_setup_logging_creates_rotating_log_file(self):
        old_log_dir = trading_main.LOG_DIR
        old_log_file = trading_main.LOG_FILE
        old_handlers = logging.getLogger().handlers[:]
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                trading_main.LOG_DIR = Path(tmpdir) / "logs"
                trading_main.LOG_FILE = trading_main.LOG_DIR / "trader.log"

                target_log_file = trading_main.LOG_DIR / "trader_opening_impulse.log"
                trading_main.setup_logging(target_log_file)
                logging.getLogger("strategies.opening_impulse").debug("diagnostic test")

                for handler in logging.getLogger().handlers:
                    handler.flush()

                self.assertTrue(target_log_file.exists())
                self.assertIn("diagnostic test", target_log_file.read_text())
                self.assertEqual(logging.getLogger("strategies.opening_impulse").level, logging.DEBUG)
                self.assertEqual(logging.getLogger("websockets.client").level, logging.INFO)
        finally:
            for handler in logging.getLogger().handlers[:]:
                handler.close()
                logging.getLogger().removeHandler(handler)
            for handler in old_handlers:
                logging.getLogger().addHandler(handler)
            trading_main.LOG_DIR = old_log_dir
            trading_main.LOG_FILE = old_log_file

    def test_strategy_log_file_includes_strategy_names(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            strategy_names=["opening_impulse", "gap_and_go"],
        )

        log_file = trading_main.strategy_log_file(settings)

        self.assertEqual(log_file, trading_main.LOG_DIR / "trader_opening_impulse__gap_and_go.log")

    def test_heartbeat_reporter_emits_strategy_summary(self):
        reporter = trading_main.HeartbeatReporter(min_interval_seconds=0.0)
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            strategy_names=["gap_and_go", "maha7"],
            symbols=["AAPL", "MSFT"],
        )
        states = {
            "AAPL": SymbolState("AAPL"),
            "MSFT": SymbolState("MSFT"),
        }
        states["AAPL"].last_event_ms = market_ms(2026, 4, 24, 10, 0)
        states["MSFT"].last_event_ms = market_ms(2026, 4, 24, 10, 0)
        executor = LocalPaperExecutor(PositionTracker(settings))
        executor.tracker.positions["AAPL"] = Position(
            symbol="AAPL",
            strategy="gap_and_go",
            shares=5,
            entry_price=100.0,
            entry_ms=market_ms(2026, 4, 24, 9, 45),
            target_price=101.0,
            stop_price=99.5,
        )
        reporter.record_quote()
        reporter.record_bar()
        reporter.record_heartbeat()
        reporter.record_signal("gap_and_go")
        reporter.record_entry("gap_and_go")
        reporter.record_rejection("maha7", "outside 10:00-14:30 ET entry window")

        with self.assertLogs(level="INFO") as captured:
            reporter.emit(settings, states, executor)

        self.assertIn("Heartbeat", captured.output[0])
        self.assertIn('"gap_and_go"', captured.output[0])
        self.assertIn('"maha7"', captured.output[0])
        self.assertIn('"open_positions": ["AAPL"]', captured.output[0])

    def test_news_sentiment_prioritizes_negative_terms(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            news_hot_positive_only=True,
            news_hot_min_sentiment_score=1.0,
        )
        positive = NewsEvent(symbols=("AAPL",), timestamp_ms=1_000, headline="AAPL beats earnings and raises guidance")
        negative = NewsEvent(symbols=("AAPL",), timestamp_ms=1_000, headline="AAPL misses earnings and cuts guidance")
        mixed = NewsEvent(
            symbols=("AAPL",),
            timestamp_ms=1_000,
            headline="AAPL announces contract win but says prior contract terminated",
        )

        self.assertTrue(trading_main.is_high_impact_news(positive.headline, positive.summary))
        self.assertFalse(trading_main.is_high_impact_news(negative.headline, negative.summary))
        self.assertFalse(trading_main.is_high_impact_news(mixed.headline, mixed.summary))
        self.assertTrue(trading_main.should_mark_hot_from_news(settings, positive))
        self.assertFalse(trading_main.should_mark_hot_from_news(settings, negative))
        self.assertFalse(trading_main.should_mark_hot_from_news(settings, mixed))

    def test_news_listener_accepts_analyst_price_target_headline(self):
        from modules.news_listener import NewsListener

        listener = NewsListener(symbol_cooldown_seconds=120, min_impact=0.5, positive_only=True)
        event = NewsEvent(
            symbols=("MCHP",),
            timestamp_ms=1_000,
            headline="Evercore ISI Group Maintains Outperform on Microchip Technology, Raises Price Target to $117",
        )

        classified = listener.process(event)

        self.assertEqual([item.symbol for item in classified], ["MCHP"])
        self.assertEqual(classified[0].sentiment, 1)
        self.assertGreaterEqual(classified[0].impact, 0.5)

    def test_warm_dynamic_news_symbol_backfills_bars_and_quote(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test")
        state = SymbolState("MCHP")
        warmed_bar = bar("MCHP", close=100.0, volume=2_000, end_ms=market_ms(2026, 4, 24, 9, 34))
        warmed_quote = Quote("MCHP", bid=100.10, ask=100.12, bid_size=100, ask_size=100, timestamp_ms=market_ms(2026, 4, 24, 9, 35))

        with (
            patch("main.get_recent_bars", return_value={"MCHP": [warmed_bar]}),
            patch("main.get_latest_quotes", return_value={"MCHP": warmed_quote}),
        ):
            warmed = trading_main.warm_dynamic_news_symbol(settings, state, "MCHP")

        self.assertTrue(warmed)
        self.assertEqual(list(state.bars), [warmed_bar])
        self.assertEqual(state.quote, warmed_quote)

    def test_warm_dynamic_news_symbol_skips_replay(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", replay_market_data=True)
        state = SymbolState("MCHP")

        with (
            patch("main.get_recent_bars") as get_bars,
            patch("main.get_latest_quotes") as get_quotes,
        ):
            warmed = trading_main.warm_dynamic_news_symbol(settings, state, "MCHP")

        self.assertFalse(warmed)
        get_bars.assert_not_called()
        get_quotes.assert_not_called()

    def test_news_dynamic_symbols_only_expand_during_regular_market(self):
        open_event = NewsEvent(
            symbols=("MCHP",),
            timestamp_ms=market_ms(2026, 4, 24, 9, 45),
            headline="MCHP beats estimates",
        )
        premarket_event = NewsEvent(
            symbols=("MCHP",),
            timestamp_ms=market_ms(2026, 4, 24, 8, 45),
            headline="MCHP beats estimates",
        )

        self.assertTrue(trading_main.should_expand_symbols_from_news(open_event))
        self.assertFalse(trading_main.should_expand_symbols_from_news(premarket_event))

    def test_symbol_manager_adds_news_symbol_to_global_universe(self):
        class Stream:
            def __init__(self):
                self.symbols = []
                self.removed = []

            def add_symbol(self, symbol):
                self.symbols.append(symbol)

            def remove_symbol(self, symbol):
                self.removed.append(symbol)

        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            strategy_names=["steady_intraday"],
        )
        strategy = SteadyIntradayStrategy(settings)
        states = {"AAPL": SymbolState("AAPL")}
        stream = Stream()
        manager = SymbolManager(states, stream, [strategy])

        added = manager.add_symbol("mchp")

        self.assertTrue(added)
        self.assertIn("MCHP", states)
        self.assertEqual(stream.symbols, ["MCHP"])
        self.assertEqual(strategy.settings.symbols, ["AAPL"])
        self.assertIn("MCHP", strategy.allowed_symbols)
        self.assertEqual(manager.symbol_refcount_for("MCHP"), 1)

    def test_symbol_manager_keeps_shared_stream_until_last_owner_removes_symbol(self):
        class Stream:
            def __init__(self):
                self.added = []
                self.removed = []

            def add_symbol(self, symbol):
                self.added.append(symbol)

            def remove_symbol(self, symbol):
                self.removed.append(symbol)

        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=[], strategy_names=["maha7"])
        strategy = Maha7Strategy(settings)
        states = {}
        stream = Stream()
        manager = SymbolManager(states, stream, [strategy])

        manager.add_global_symbols(["TSLA"])
        manager.register_strategy_symbols("maha7", ["TSLA"])
        self.assertEqual(manager.symbol_refcount_for("TSLA"), 2)
        self.assertEqual(stream.added, ["TSLA"])

        manager.register_strategy_symbols("maha7", [])
        self.assertEqual(manager.symbol_refcount_for("TSLA"), 1)
        self.assertEqual(stream.removed, [])

        manager.remove_global_symbols(["TSLA"])
        self.assertEqual(manager.symbol_refcount_for("TSLA"), 0)
        self.assertEqual(stream.removed, ["TSLA"])

    def test_runtime_settings_snapshot_includes_tuning_parameters(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL", "MSFT"],
            execution_mode="alpaca_paper",
            strategy_names=["opening_impulse", "spike", "gap_and_go"],
            target_profit_pct=0.01,
            stop_loss_pct=0.005,
            max_hold_seconds=120,
            gap_and_go_min_gap_pct=0.02,
            opening_impulse_min_hold_seconds=15,
            opening_impulse_exit_negative_steps=4,
            opening_impulse_winner_min_pnl_pct=0.003,
        )

        snapshot = trading_main.runtime_settings_snapshot(settings)

        self.assertEqual(snapshot["execution_mode"], "alpaca_paper")
        self.assertEqual(snapshot["symbols"], ["AAPL", "MSFT"])
        self.assertEqual(snapshot["alpaca_api_key_fingerprint"], trading_main.credential_fingerprint("test"))
        self.assertEqual(snapshot["alpaca_market_data_mode"], "stream")
        self.assertFalse(snapshot["replay_market_data"])
        self.assertEqual(snapshot["stream"]["alpaca_market_data_poll_seconds"], 5.0)
        self.assertEqual(snapshot["stream"]["alpaca_fill_timeout_seconds"], 5.0)
        self.assertEqual(snapshot["stream"]["max_entry_chase_pct"], 0.003)
        self.assertNotIn("alpaca_secret_key", snapshot)
        self.assertEqual(snapshot["risk"]["target_profit_pct"], 0.01)
        self.assertEqual(snapshot["risk"]["max_trade_loss_r"], 1.2)
        self.assertEqual(snapshot["risk"]["consecutive_loss_stop_count"], 5)
        self.assertEqual(snapshot["gap_and_go"]["min_gap_pct"], 0.02)
        self.assertEqual(snapshot["opening_impulse"]["min_hold_seconds"], 15)
        self.assertEqual(snapshot["opening_impulse"]["exit_negative_steps"], 4)
        self.assertEqual(snapshot["opening_impulse"]["winner_min_pnl_pct"], 0.003)
        self.assertEqual(snapshot["opening_impulse"]["pullback_pct"], 0.005)
        self.assertEqual(snapshot["opening_impulse"]["strong_volume_ratio"], 2.5)
        self.assertEqual(snapshot["opening_impulse"]["strong_pullback_pct"], 0.01)
        self.assertEqual(snapshot["spike"]["start_minute"], settings.spike_start_minute)
        self.assertEqual(snapshot["spike"]["end_minute"], settings.spike_end_minute)
        self.assertEqual(snapshot["spike"]["lookback_seconds"], settings.spike_lookback_seconds)

    def test_replay_market_data_skips_heartbeat_exit_clock(self):
        replay_settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", replay_market_data=True)
        live_settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", replay_market_data=False)

        self.assertFalse(trading_main.should_manage_exits_on_heartbeat(replay_settings))
        self.assertTrue(trading_main.should_manage_exits_on_heartbeat(live_settings))

    def test_alpaca_stream_error_filter_rewrites_dns_traceback(self):
        log_filter = trading_main.FriendlyAlpacaStreamErrorFilter(min_interval_seconds=60)
        exc = OSError("[Errno 8] nodename nor servname provided, or not known")
        record = logging.LogRecord(
            "alpaca.data.live.websocket",
            logging.ERROR,
            "stream.py",
            10,
            "error during websocket communication: %s",
            (exc,),
            (type(exc), exc, None),
        )

        self.assertTrue(log_filter.filter(record))

        message = record.getMessage()
        self.assertIn("Alpaca market-data stream connection problem", message)
        self.assertIn("Check internet/DNS/VPN", message)
        self.assertIsNone(record.exc_info)

    def test_alpaca_stream_error_filter_throttles_repeated_dns_errors(self):
        log_filter = trading_main.FriendlyAlpacaStreamErrorFilter(min_interval_seconds=60)
        exc = OSError("[Errno 8] nodename nor servname provided, or not known")

        def make_record() -> logging.LogRecord:
            return logging.LogRecord(
                "alpaca.data.live.websocket",
                logging.ERROR,
                "stream.py",
                10,
                "error during websocket communication: %s",
                (exc,),
                (type(exc), exc, None),
            )

        self.assertTrue(log_filter.filter(make_record()))
        self.assertFalse(log_filter.filter(make_record()))

    def test_alpaca_stream_connection_limit_is_fatal(self):
        log_filter = trading_main.FatalAlpacaStreamErrorFilter()
        exc = ValueError('{"message":"connection limit exceeded"}')
        record = logging.LogRecord(
            "alpaca.data.live.websocket",
            logging.ERROR,
            "stream.py",
            10,
            "error during websocket communication: %s",
            (exc,),
            (type(exc), exc, None),
        )

        with self.assertRaises(AlpacaStreamConnectionLimitError):
            log_filter.filter(record)

    def test_credential_fingerprint_is_stable_without_exposing_full_key(self):
        fingerprint = trading_main.credential_fingerprint("paper-key-abc123")

        self.assertTrue(fingerprint.endswith(":c123"))
        self.assertNotIn("paper-key-abc123", fingerprint)
        self.assertEqual(fingerprint, trading_main.credential_fingerprint("paper-key-abc123"))
        self.assertNotEqual(fingerprint, trading_main.credential_fingerprint("paper-key-def456"))
        self.assertIsNone(trading_main.credential_fingerprint(None))

    def test_market_data_stream_factory_uses_configured_mode(self):
        stream_settings = Settings(
            alpaca_api_key="test-key",
            alpaca_secret_key="test",
            alpaca_market_data_mode="stream",
        )
        rest_settings = Settings(
            alpaca_api_key="test-key",
            alpaca_secret_key="test",
            alpaca_market_data_mode="rest",
        )

        self.assertIsInstance(build_market_data_stream(stream_settings), AlpacaStockStream)
        self.assertIsInstance(build_market_data_stream(rest_settings), AlpacaRestPollingStream)

    def test_alpaca_stream_raises_when_websocket_task_ends_silently(self):
        class FakeStream:
            def __init__(self):
                self.stopped = False

            def subscribe_bars(self, callback, symbol):
                return None

            def subscribe_quotes(self, callback, symbol):
                return None

            def subscribe_news(self, callback, symbol):
                return None

            async def _run_forever(self):
                return None

            async def stop_ws(self):
                self.stopped = True

        class FakeClients:
            def __init__(self):
                self.stream = FakeStream()
                self.news_stream = FakeStream()

        settings = Settings(
            alpaca_api_key="test-key",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            heartbeat_seconds=5,
        )
        clients = FakeClients()

        async def consume_once():
            stream = AlpacaStockStream(settings)
            with patch("alpaca_stream.make_clients", return_value=clients):
                async for _event in stream.events():
                    self.fail("silent stream completion should not yield an event")

        with self.assertRaises(AlpacaStreamEndedError):
            asyncio.run(consume_once())

        self.assertTrue(clients.stream.stopped)
        self.assertTrue(clients.news_stream.stopped)

    def test_alpaca_stream_lock_rejects_duplicate_local_stream(self):
        settings = Settings(
            alpaca_api_key="test-key",
            alpaca_secret_key="test",
            alpaca_data_feed="iex",
            alpaca_paper=True,
        )
        first = AlpacaStreamLock(settings)
        second = AlpacaStreamLock(settings)
        try:
            first.acquire()
            with self.assertRaises(AlpacaStreamConnectionLimitError):
                second.acquire()
        finally:
            first.release()
            second.release()

    def test_alpaca_stream_lock_allows_different_api_keys(self):
        first_settings = Settings(
            alpaca_api_key="test-key-one",
            alpaca_secret_key="test",
            alpaca_data_feed="iex",
            alpaca_paper=True,
        )
        second_settings = Settings(
            alpaca_api_key="test-key-two",
            alpaca_secret_key="test",
            alpaca_data_feed="iex",
            alpaca_paper=True,
        )
        first = AlpacaStreamLock(first_settings)
        second = AlpacaStreamLock(second_settings)
        try:
            first.acquire()
            second.acquire()
        finally:
            first.release()
            second.release()

    def test_rejection_log_throttler_suppresses_repeated_rejections(self):
        throttler = trading_main.RejectionLogThrottler(min_interval_seconds=60)

        self.assertTrue(throttler.should_log("AAPL", "BUY", "opening_impulse", "symbol cooldown active"))
        self.assertFalse(throttler.should_log("AAPL", "BUY", "opening_impulse", "symbol cooldown active"))
        self.assertTrue(throttler.should_log("AAPL", "BUY", "opening_impulse", "position already open"))

    def test_build_strategies_returns_enabled_strategies(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            strategy_names=["spike", "opening_impulse", "gap_and_go", "maha7", "steady_intraday"],
        )

        strategies = build_strategies(settings)

        self.assertEqual(
            [strategy.name for strategy in strategies],
            ["spike", "opening_impulse", "gap_and_go", "maha7", "steady_intraday"],
        )

    def test_available_strategy_names_lists_registry_order(self):
        self.assertEqual(
            available_strategy_names(),
            [
                "gap_and_go",
                "macd_early_impulse",
                "stoch_macd_reversal",
                "maha7",
                "steady_intraday",
                "spike",
                "opening_impulse",
            ],
        )

    def test_steady_intraday_emits_pullback_reclaim_signal(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["NVDA"],
            strategy_names=["steady_intraday"],
        )
        strategy = SteadyIntradayStrategy(settings)
        state = SymbolState("NVDA")
        start_ms = market_ms(2026, 4, 24, 9, 30)
        closes = []
        for index in range(60):
            if index < 15:
                close = 100 + index * 0.03
            elif index < 56:
                close = 100.5 + (index - 15) * 0.04
            elif index == 56:
                close = 102.0
            elif index == 57:
                close = 101.9
            elif index == 58:
                close = 101.85
            else:
                close = 102.15
            closes.append(close)
            open_price = close - 0.10
            state.add_bar(
                Bar(
                    "NVDA",
                    open=open_price,
                    high=close + 0.08,
                    low=open_price - 0.08,
                    close=close,
                    volume=100_000 if index < 59 else 150_000,
                    vwap=close,
                    start_ms=start_ms + index * 60_000,
                    end_ms=start_ms + (index + 1) * 60_000,
                )
            )
        state.update_quote(Quote("NVDA", 102.13, 102.17, 100, 100, start_ms + 60 * 60_000))
        state.last_event_kind = "bar"
        state.last_event_ms = state.bars[-1].end_ms

        signal = strategy.evaluate(state)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.strategy, "steady_intraday")
        self.assertIn("pullback_reclaim", signal.reason)
        self.assertLess(signal.stop_price, signal.price)
        self.assertEqual(signal.position_size_multiplier, 0.8)

    def test_steady_intraday_ignores_symbols_outside_selected_universe(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            strategy_names=["steady_intraday"],
        )
        state = SymbolState("NVDA")
        for bar_item in self._steady_intraday_selector_bars("NVDA", market_ms(2026, 4, 24, 9, 30), trigger=True):
            state.add_bar(bar_item)

        signal = SteadyIntradayStrategy(settings).evaluate(state)

        self.assertIsNone(signal)

    def test_build_strategies_rejects_removed_news_impulse_strategy(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            strategy_names=["news_impulse"],
        )

        with self.assertRaisesRegex(ValueError, "Unknown strategy: news_impulse"):
            build_strategies(settings)

    def test_position_sizing_multiplier_reduces_signal_shares(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            regular_market_only=False,
            max_position_value=1_000.0,
        )
        executor = LocalPaperExecutor(PositionTracker(settings))
        signal = Signal(
            strategy="opening_impulse",
            symbol="AAPL",
            side="BUY",
            price=100.0,
            timestamp_ms=market_ms(2026, 4, 24, 10, 0),
            change_pct=0.005,
            volume_ratio=2.0,
            spread_bps=4.0,
            reason="reduced-size entry",
            position_size_multiplier=0.5,
        )

        fill = executor.buy(signal)

        self.assertIsNotNone(fill)
        self.assertEqual(fill.shares, 5)

    def test_parse_args_accepts_strategy_and_list_flag(self):
        args = trading_main.parse_args(["--strategy", "gap_and_go"])

        self.assertEqual(args.strategy, ["gap_and_go"])
        self.assertFalse(args.list_strategies)
        self.assertIsNone(args.opening_plan)

    def test_parse_args_accepts_multiple_strategies(self):
        args = trading_main.parse_args(["-s", "macd_early_impulse,stoch_macd_reversal", "steady_intraday"])

        self.assertEqual(args.strategy, ["macd_early_impulse", "stoch_macd_reversal", "steady_intraday"])

    def test_configured_strategy_names_reads_strategies_without_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(trading_main.configured_strategy_names(), [])
        with patch.dict(os.environ, {"STRATEGIES": "gap_and_go,maha7"}, clear=True):
            self.assertEqual(trading_main.configured_strategy_names(), ["gap_and_go", "maha7"])

    def test_prompt_for_strategy_names_requires_tty(self):
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "No strategy was provided"):
                trading_main.prompt_for_strategy_names()

    def test_validate_strategy_plan_requires_existing_file(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            strategy_names=["gap_and_go"],
        )

        with self.assertRaisesRegex(FileNotFoundError, "Run the selector first"):
            trading_main.validate_strategy_plan(Path("/tmp/missing-gap-plan.json"), settings)

    def test_validate_strategy_plan_requires_selected_symbols(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            strategy_names=["opening_impulse"],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / "opening_impulse_plan.json"
            plan_path.write_text(json.dumps({"strategy": "opening_impulse", "symbols": []}))

            with self.assertRaisesRegex(ValueError, "Regenerate it first"):
                trading_main.validate_strategy_plan(plan_path, settings)

    def test_strategy_plan_guide_includes_selector_and_rerun_command(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            strategy_names=["maha7"],
        )

        guide = trading_main.strategy_plan_guide(
            Path("data/maha7_plan.json"),
            settings,
            FileNotFoundError("missing plan"),
        )

        self.assertIn("Strategy plan is not ready", guide)
        self.assertIn(".venv/bin/python strategy_selectors/select_maha7.py --top 12", guide)
        self.assertIn("scripts/run_paper.sh -s maha7", guide)

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

    def test_risk_pauses_after_consecutive_losses(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            consecutive_loss_pause_count=3,
            consecutive_loss_pause_minutes=30,
        )
        risk = RiskManager(settings)
        base_ms = market_ms(2026, 4, 24, 10, 0)
        risk.record_exit(-1.0, base_ms)
        risk.record_exit(-2.0, base_ms + 60_000)
        risk.record_exit(-3.0, base_ms + 120_000)
        signal = Signal(
            strategy="opening_impulse",
            symbol="AAPL",
            side="BUY",
            price=100.0,
            timestamp_ms=base_ms + 10 * 60_000,
            change_pct=0.0,
            volume_ratio=1.0,
            spread_bps=4.0,
            reason="test",
        )

        decision = risk.check_entry(signal, set(), 0)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "consecutive loss pause active")

    def test_risk_stops_day_after_five_consecutive_losses(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            daily_max_loss=10_000.0,
            consecutive_loss_stop_count=5,
        )
        risk = RiskManager(settings)
        base_ms = market_ms(2026, 4, 24, 10, 0)
        for index in range(5):
            risk.record_exit(-1.0, base_ms + index * 60_000)
        signal = Signal(
            strategy="opening_impulse",
            symbol="AAPL",
            side="BUY",
            price=100.0,
            timestamp_ms=base_ms + 6 * 60_000,
            change_pct=0.0,
            volume_ratio=1.0,
            spread_bps=4.0,
            reason="test",
        )

        decision = risk.check_entry(signal, set(), 0)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "consecutive loss day stop active")

    def test_risk_rejects_daily_loss_percent_limit(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            starting_cash=25_000.0,
            daily_max_loss=1_000.0,
            daily_max_loss_pct=0.02,
            consecutive_loss_pause_count=99,
        )
        risk = RiskManager(settings)
        base_ms = market_ms(2026, 4, 24, 10, 0)
        risk.record_exit(-500.0, base_ms)
        signal = Signal(
            strategy="opening_impulse",
            symbol="AAPL",
            side="BUY",
            price=100.0,
            timestamp_ms=base_ms + 60_000,
            change_pct=0.0,
            volume_ratio=1.0,
            spread_bps=4.0,
            reason="test",
        )

        decision = risk.check_entry(signal, set(), 0)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "daily loss limit reached")

    def test_risk_rejects_maha7_within_opening_impulse_cooldown(self):
        settings = Settings(alpaca_api_key="test", alpaca_secret_key="test", symbols=["AAPL"])
        risk = RiskManager(settings)
        risk.record_trade("AAPL", market_ms(2026, 4, 24, 10, 0), "opening_impulse")
        signal = Signal(
            strategy="maha7",
            symbol="AAPL",
            side="BUY",
            price=100.0,
            timestamp_ms=market_ms(2026, 4, 24, 10, 4),
            change_pct=0.0,
            volume_ratio=1.0,
            spread_bps=None,
            reason="test",
        )

        decision = risk.check_entry(signal, set(), 0)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "opening impulse cooldown active")

    def test_risk_rejects_maha7_after_symbol_session_trade_cap(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            trade_cooldown_seconds=0,
            maha7_reentry_cooldown_seconds=0,
            maha7_max_trades_per_symbol_per_session=3,
        )
        risk = RiskManager(settings)
        base_ms = market_ms(2026, 4, 24, 10, 0)
        for index in range(3):
            risk.record_trade("AAPL", base_ms + index * 60_000, "maha7")
        signal = Signal(
            strategy="maha7",
            symbol="AAPL",
            side="BUY",
            price=100.0,
            timestamp_ms=base_ms + 10 * 60_000,
            change_pct=0.0,
            volume_ratio=1.0,
            spread_bps=None,
            reason="test",
        )

        decision = risk.check_entry(signal, set(), 0)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "max trades per symbol per session reached")

    def test_risk_rejects_opening_impulse_after_symbol_session_trade_cap(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            trade_cooldown_seconds=0,
            opening_impulse_max_trades_per_symbol_per_session=2,
        )
        risk = RiskManager(settings)
        base_ms = market_ms(2026, 4, 24, 10, 0)
        for index in range(2):
            risk.record_trade("AAPL", base_ms + index * 60_000, "opening_impulse")
        signal = Signal(
            strategy="opening_impulse",
            symbol="AAPL",
            side="BUY",
            price=100.0,
            timestamp_ms=base_ms + 10 * 60_000,
            change_pct=0.0,
            volume_ratio=1.0,
            spread_bps=4.0,
            reason="test",
        )

        decision = risk.check_entry(signal, set(), 0)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "max trades per symbol per session reached")

    def test_risk_rejects_opening_impulse_after_symbol_loss_lock(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            opening_impulse_symbol_loss_lock_count=2,
        )
        risk = RiskManager(settings)
        base_ms = market_ms(2026, 4, 24, 10, 0)
        risk.record_exit(-5.0, base_ms, "AAPL", "opening_impulse")
        risk.record_exit(-3.0, base_ms + 60_000, "AAPL", "opening_impulse")
        signal = Signal(
            strategy="opening_impulse",
            symbol="AAPL",
            side="BUY",
            price=100.0,
            timestamp_ms=base_ms + 10 * 60_000,
            change_pct=0.0,
            volume_ratio=1.0,
            spread_bps=4.0,
            reason="test",
        )

        decision = risk.check_entry(signal, set(), 0)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "symbol session loss lock active")

    def test_risk_rejects_steady_intraday_after_symbol_session_trade_cap(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            trade_cooldown_seconds=0,
            steady_intraday_max_trades_per_symbol_per_session=2,
        )
        risk = RiskManager(settings)
        base_ms = market_ms(2026, 4, 24, 10, 0)
        for index in range(2):
            risk.record_trade("AAPL", base_ms + index * 60_000, "steady_intraday")
        signal = Signal(
            strategy="steady_intraday",
            symbol="AAPL",
            side="BUY",
            price=100.0,
            timestamp_ms=base_ms + 10 * 60_000,
            change_pct=0.0,
            volume_ratio=1.0,
            spread_bps=4.0,
            reason="test",
        )

        decision = risk.check_entry(signal, set(), 0)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "max trades per symbol per session reached")

    def test_risk_rejects_steady_intraday_after_symbol_loss_lock(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            steady_intraday_symbol_loss_lock_count=1,
        )
        risk = RiskManager(settings)
        base_ms = market_ms(2026, 4, 24, 10, 0)
        risk.record_exit(-5.0, base_ms, "AAPL", "steady_intraday")
        signal = Signal(
            strategy="steady_intraday",
            symbol="AAPL",
            side="BUY",
            price=100.0,
            timestamp_ms=base_ms + 10 * 60_000,
            change_pct=0.0,
            volume_ratio=1.0,
            spread_bps=4.0,
            reason="test",
        )

        decision = risk.check_entry(signal, set(), 0)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "symbol session loss lock active")

    def test_risk_rejects_symbol_during_failed_entry_cooldown(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            failed_entry_cooldown_seconds=120,
        )
        risk = RiskManager(settings)
        risk.record_failed_entry("AAPL", market_ms(2026, 4, 24, 10, 0))
        signal = Signal(
            strategy="opening_impulse",
            symbol="AAPL",
            side="BUY",
            price=100.0,
            timestamp_ms=market_ms(2026, 4, 24, 10, 1),
            change_pct=0.0,
            volume_ratio=1.0,
            spread_bps=4.0,
            reason="test",
        )

        decision = risk.check_entry(signal, set(), 0)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "failed entry cooldown active")

    @staticmethod
    def _maha7_reclaim_state() -> SymbolState:
        """Synthetic session satisfying optimized MAHA7 entry (R band, reclaim, chase, momentum)."""
        state = SymbolState("AAPL")
        start_ms = market_ms(2026, 4, 24, 9, 30)
        bar_index = 0

        def add_bar(open_price: float, high: float, low: float, close: float, volume: int = 1_400) -> None:
            nonlocal bar_index
            state.add_bar(
                Bar(
                    "AAPL",
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    vwap=close,
                    start_ms=start_ms + bar_index * 60_000,
                    end_ms=start_ms + (bar_index + 1) * 60_000,
                )
            )
            bar_index += 1

        price = 100.0
        for _ in range(24):
            close = price + 0.38
            add_bar(price, close + 0.15, price - 0.12, close)
            price = close
        add_bar(price, 119.0, price - 0.5, 111.0, 2_500)
        ramp = [111.5, 112.0, 112.4, 112.8, 113.1, 113.4, 113.6, 113.85, 113.95]
        for index, close in enumerate(ramp):
            open_price = ramp[index - 1] if index else 111.0
            low = min(open_price, close) - 0.08
            high = max(open_price, close) + 0.12
            add_bar(open_price, high, low, close, 1_500 + index * 20)
        for row in (
            (113.9, 114.05, 113.25, 113.55),
            (113.55, 113.95, 113.35, 113.75),
            (113.75, 114.05, 113.45, 113.92),
            (113.92, 114.12, 113.65, 114.02),
            (114.02, 114.18, 113.80, 114.10),
            (114.08, 114.35, 113.95, 114.26, 2_200),
        ):
            add_bar(*row)
        return state

    def _stoch_macd_state(self, closes: list[float] | None = None, *, symbol: str = "AAPL") -> SymbolState:
        closes = closes or [
            100.0,
            99.8,
            99.6,
            99.4,
            99.2,
            99.0,
            98.8,
            98.6,
            98.4,
            98.2,
            98.0,
            97.8,
            97.6,
            97.4,
            97.2,
            97.0,
            96.8,
            96.6,
            96.4,
            96.2,
            96.0,
            95.8,
            95.6,
            95.4,
            95.2,
            95.0,
            94.8,
            94.6,
            94.4,
            94.2,
            94.0,
            93.9,
            93.8,
            93.7,
            93.6,
            93.8,
            94.1,
            94.5,
            95.0,
            95.6,
        ]
        state = SymbolState(symbol)
        start_ms = market_ms(2026, 4, 24, 9, 30)
        for index, close in enumerate(closes):
            ts = start_ms + index * 60_000
            state.add_bar(
                Bar(
                    symbol=symbol,
                    open=closes[index - 1] if index else close,
                    high=close + 0.25,
                    low=close - 0.25,
                    close=close,
                    volume=120_000 if index == len(closes) - 1 else 100_000,
                    vwap=close,
                    start_ms=ts,
                    end_ms=ts + 60_000,
                )
            )
        last = closes[-1]
        state.update_quote(Quote(symbol, last - 0.01, last + 0.01, 100, 100, state.last_event_ms or 0))
        return state

    def _stoch_macd_premarket_warmup_state(self, *, symbol: str = "AAPL") -> SymbolState:
        closes = [100.0 - index * 0.08 for index in range(34)] + [
            97.2,
            97.0,
            96.8,
            96.7,
            96.8,
            97.0,
            97.3,
            97.7,
            98.2,
            98.8,
            99.4,
            100.1,
            100.9,
            101.6,
            102.2,
            102.8,
        ]
        state = SymbolState(symbol)
        start_ms = market_ms(2026, 4, 24, 8, 50)
        for index, close in enumerate(closes):
            ts = start_ms + index * 60_000
            state.add_bar(
                Bar(
                    symbol=symbol,
                    open=closes[index - 1] if index else close,
                    high=close + 0.25,
                    low=close - 0.25,
                    close=close,
                    volume=160_000 if index == len(closes) - 1 else 100_000,
                    vwap=close,
                    start_ms=ts,
                    end_ms=ts + 60_000,
                )
            )
        last = closes[-1]
        state.update_quote(Quote(symbol, last - 0.01, last + 0.01, 100, 100, state.last_event_ms or 0))
        return state

    def test_stoch_macd_reversal_emits_buy_on_confirmed_indicator_stack(self):
        settings = Settings(symbols=["AAPL"])
        strategy = StochMACDReversalStrategy(settings)

        with patch.object(
            strategy,
            "_compute_stoch",
            return_value=([70.0, 96.3], [68.0, 80.97]),
        ), patch.object(
            strategy,
            "_compute_macd",
            return_value=(
                [-0.03, -0.02],
                [-0.04, -0.03],
                [0.01, 0.02],
            ),
        ), patch.object(
            strategy,
            "_compute_supertrend",
            return_value=(60.80, True),
        ), patch.object(
            strategy,
            "_fast_ema",
            return_value=60.92,
        ):
            signal = strategy.evaluate(self._stoch_macd_state())

        self.assertIsNotNone(signal)
        self.assertEqual(signal.strategy, "stoch_macd_reversal")
        self.assertEqual(signal.side, "BUY")
        self.assertIn("confirmed trend", signal.reason)
        self.assertLess(signal.stop_price, signal.price)
        self.assertEqual(signal.position_size_multiplier, 0.8)

    def test_stoch_macd_reversal_can_enter_near_0940_with_premarket_warmup(self):
        settings = Settings(symbols=["AAPL"])
        strategy = StochMACDReversalStrategy(settings)
        state = self._stoch_macd_premarket_warmup_state()

        indicator_bars = strategy._indicator_bars(state)
        regular_bars = [
            bar
            for bar in indicator_bars
            if datetime.fromtimestamp(bar.start_ms / 1000, tz=MARKET_TZ).time() >= datetime.strptime("09:30", "%H:%M").time()
        ]

        self.assertGreaterEqual(len(indicator_bars), 45)
        self.assertLess(len(regular_bars), 35)
        self.assertIsNotNone(strategy._compute_macd(state))

        with patch.object(
            strategy,
            "_compute_stoch",
            return_value=([68.0, 82.0], [64.0, 76.0]),
        ), patch.object(
            strategy,
            "_compute_macd",
            return_value=(
                [-0.06, -0.04],
                [-0.07, -0.05],
                [0.01, 0.01],
            ),
        ), patch.object(
            strategy,
            "_compute_supertrend",
            return_value=(101.0, True),
        ), patch.object(
            strategy,
            "_fast_ema",
            return_value=102.0,
        ):
            signal = strategy.evaluate(state)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "BUY")

    def test_stoch_macd_reversal_rejects_without_bullish_stoch(self):
        settings = Settings(symbols=["AAPL"])
        strategy = StochMACDReversalStrategy(settings)

        with patch.object(
            strategy,
            "_compute_stoch",
            return_value=([70.0, 75.0], [72.0, 80.0]),
        ), patch.object(
            strategy,
            "_compute_macd",
            return_value=(
                [0.01, 0.02],
                [0.00, 0.01],
                [0.01, 0.01],
            ),
        ), patch.object(
            strategy,
            "_compute_supertrend",
            return_value=(94.0, True),
        ), patch.object(
            strategy,
            "_fast_ema",
            return_value=95.0,
        ):
            signal = strategy.evaluate(self._stoch_macd_state())

        self.assertIsNone(signal)

    def test_stoch_macd_reversal_rejects_bearish_supertrend_bounce(self):
        settings = Settings(symbols=["AAPL"])
        strategy = StochMACDReversalStrategy(settings)
        closes = [100.0 - index * 0.35 for index in range(36)] + [87.6, 87.9, 88.1, 88.3]
        state = self._stoch_macd_state(closes)

        with patch.object(
            strategy,
            "_compute_stoch",
            return_value=(
                [45.0, 32.0, 18.0, 12.0, 15.0, 18.0, 24.0, 30.0, 36.0, 42.0],
                [46.0, 38.0, 28.0, 18.0, 15.0, 17.0, 21.0, 27.0, 33.0, 39.0],
            ),
        ), patch.object(
            strategy,
            "_compute_macd",
            return_value=(
                [0.01, 0.02],
                [0.00, 0.01],
                [0.01, 0.01],
            ),
        ):
            signal = strategy.evaluate(state)

        self.assertIsNone(signal)

    def test_stoch_macd_reversal_allows_supertrend_filter_to_be_disabled(self):
        settings = Settings(symbols=["AAPL"], stoch_macd_supertrend_enabled=False)
        strategy = StochMACDReversalStrategy(settings)
        closes = [100.0 - index * 0.35 for index in range(36)] + [87.6, 87.9, 88.1, 88.3]
        state = self._stoch_macd_state(closes)

        with patch.object(
            strategy,
            "_compute_stoch",
            return_value=(
                [45.0, 32.0, 18.0, 12.0, 15.0, 18.0, 24.0, 30.0, 36.0, 42.0],
                [46.0, 38.0, 28.0, 18.0, 15.0, 17.0, 21.0, 27.0, 33.0, 39.0],
            ),
        ), patch.object(
            strategy,
            "_compute_macd",
            return_value=(
                [0.01, 0.02],
                [0.00, 0.01],
                [0.01, 0.01],
            ),
        ):
            signal = strategy.evaluate(state)

        self.assertIsNotNone(signal)

    def test_stoch_macd_reversal_exits_on_bearish_indicator_stack(self):
        settings = Settings(symbols=["AAPL"], stoch_macd_min_hold_seconds=0)
        strategy = StochMACDReversalStrategy(settings)
        closes = [100.0 + index * 0.2 for index in range(35)] + [107.2, 107.4, 107.5, 107.45, 107.35]
        state = self._stoch_macd_state(closes)
        position = Position(
            symbol="AAPL",
            strategy="stoch_macd_reversal",
            shares=10,
            entry_price=106.5,
            entry_ms=market_ms(2026, 4, 24, 10, 0),
            target_price=107.5,
            stop_price=105.5,
            max_price=107.6,
        )

        with patch.object(
            strategy,
            "_compute_stoch",
            return_value=([90.0, 70.0], [85.0, 75.0]),
        ), patch.object(
            strategy,
            "_compute_macd",
            return_value=(
                [0.02, 0.00],
                [0.01, 0.01],
                [0.01, -0.01],
            ),
        ), patch.object(
            strategy,
            "_compute_supertrend",
            return_value=(107.0, False),
        ), patch.object(
            strategy,
            "_fast_ema",
            return_value=106.8,
        ):
            decision = strategy.should_exit(state, position)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "stoch_macd indicator sell")

    @staticmethod
    def _maha7_selector_bars(symbol: str, base: float, start_ms: int, final_pullback: bool, volume: float) -> list[Bar]:
        closes = [base + index * 0.35 for index in range(24)]
        closes.extend([108.0, 107.7, 107.4, 107.2, 107.6, 108.0] if final_pullback else [110.0, 112.0, 114.0, 116.0, 118.0, 120.0])
        bars = []
        for index, close in enumerate(closes):
            open_price = closes[index - 1] if index else close - 0.1
            bars.append(
                Bar(
                    symbol,
                    open=open_price,
                    high=max(open_price, close) + 0.2,
                    low=min(open_price, close) - 0.2,
                    close=close,
                    volume=volume,
                    vwap=close,
                    start_ms=start_ms + index * 86_400_000,
                    end_ms=start_ms + (index + 1) * 86_400_000,
                )
            )
        return bars

    @staticmethod
    def _steady_intraday_selector_bars(symbol: str, start_ms: int, trigger: bool) -> list[Bar]:
        bars = []
        for index in range(60):
            if index < 15:
                close = 100 + index * 0.03
            elif index < 56:
                close = 100.5 + (index - 15) * (0.04 if trigger else 0.01)
            elif trigger and index == 56:
                close = 102.0
            elif trigger and index == 57:
                close = 101.9
            elif trigger and index == 58:
                close = 101.85
            elif trigger:
                close = 102.15
            else:
                close = 100.9
            open_price = close - 0.10
            bars.append(
                Bar(
                    symbol,
                    open=open_price,
                    high=close + 0.08,
                    low=open_price - 0.08,
                    close=close,
                    volume=100_000 if index < 59 else 150_000,
                    vwap=close,
                    start_ms=start_ms + index * 60_000,
                    end_ms=start_ms + (index + 1) * 60_000,
                )
            )
        return bars

    @staticmethod
    def _maha7_reclaim_selector_bars(symbol: str, start_ms: int) -> list[Bar]:
        closes = [100 + index * 0.1 for index in range(24)] + [104.0, 103.0, 102.0, 101.0, 101.5, 102.0, 103.5]
        bars = []
        for index, close in enumerate(closes):
            open_price = closes[index - 1] if index else close - 0.1
            bars.append(
                Bar(
                    symbol,
                    open=open_price,
                    high=max(open_price, close) + 0.2,
                    low=min(open_price, close) - 0.2,
                    close=close,
                    volume=200_000,
                    vwap=close,
                    start_ms=start_ms + index * 60_000,
                    end_ms=start_ms + (index + 1) * 60_000,
                )
            )
        return bars

    @staticmethod
    def _spike_state(base_ms: int) -> SymbolState:
        state = SymbolState("AAPL")
        state.update_quote(Quote("AAPL", bid=100.00, ask=100.05, bid_size=10, ask_size=10, timestamp_ms=base_ms))
        for index in range(6):
            state.add_bar(bar("AAPL", close=100.0, volume=100, end_ms=base_ms + (index * 1000)))
        state.add_bar(bar("AAPL", close=100.40, volume=350, end_ms=base_ms + 7000))
        return state


class FakeOrder:
    def __init__(
        self,
        order_id: str,
        status: str,
        filled_qty: str = "0",
        filled_avg_price: str | None = None,
        symbol: str = "AAPL",
    ):
        self.id = order_id
        self.status = status
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price
        self.symbol = symbol


class FakePosition:
    def __init__(self, symbol: str, qty: str, avg_entry_price: str):
        self.symbol = symbol
        self.qty = qty
        self.avg_entry_price = avg_entry_price


class FakeTrading:
    def __init__(
        self,
        orders: list[FakeOrder],
        positions: list[FakePosition] | None = None,
        open_orders: list[FakeOrder] | None = None,
        cash: str = "10000.00",
        submit_error: Exception | None = None,
        cancel_error: Exception | None = None,
    ):
        self.orders = orders
        self.positions = positions or []
        self.open_orders = open_orders or []
        self.cash = cash
        self.submit_error = submit_error
        self.cancel_error = cancel_error
        self.cancel_called = False
        self.canceled_order_ids = []
        self.submitted_orders = []

    def get_clock(self):
        return types.SimpleNamespace(is_open=True)

    def get_account(self):
        return types.SimpleNamespace(cash=self.cash)

    def get_all_positions(self) -> list[FakePosition]:
        return self.positions

    def get_orders(self) -> list[FakeOrder]:
        return self.open_orders

    def submit_order(self, order_data):
        self.submitted_orders.append(order_data)
        if self.submit_error is not None:
            raise self.submit_error
        return self.orders.pop(0)

    def cancel_order_by_id(self, order_id: str) -> None:
        self.cancel_called = True
        self.canceled_order_ids.append(order_id)
        if self.cancel_error is not None:
            raise self.cancel_error

    def get_order_by_id(self, order_id: str) -> FakeOrder:
        return self.orders.pop(0)


class FakeHistorical:
    def __init__(self, latest_quotes: dict[str, object] | None = None):
        self.latest_quotes = latest_quotes or {}
        self.latest_quote_requests = []

    def get_stock_latest_quote(self, request):
        self.latest_quote_requests.append(request)
        return self.latest_quotes


class FakeClients:
    def __init__(
        self,
        orders: list[FakeOrder],
        positions: list[FakePosition] | None = None,
        open_orders: list[FakeOrder] | None = None,
        cash: str = "10000.00",
        submit_error: Exception | None = None,
        cancel_error: Exception | None = None,
        latest_quotes: dict[str, object] | None = None,
    ):
        self.trading = FakeTrading(
            orders,
            positions=positions,
            open_orders=open_orders,
            cash=cash,
            submit_error=submit_error,
            cancel_error=cancel_error,
        )
        self.historical = FakeHistorical(latest_quotes)
        self.feed = "iex"


class FakeExecutor:
    def __init__(self):
        self.exit_calls = []

    def manage_exit(self, state, strategies_by_name, now_ms=None):
        self.exit_calls.append((state.symbol, now_ms))


def install_fake_alpaca_modules() -> None:
    global FakeAPIError
    alpaca = types.ModuleType("alpaca")
    common = types.ModuleType("alpaca.common")
    exceptions = types.ModuleType("alpaca.common.exceptions")
    trading = types.ModuleType("alpaca.trading")
    enums = types.ModuleType("alpaca.trading.enums")
    requests = types.ModuleType("alpaca.trading.requests")
    data = types.ModuleType("alpaca.data")
    data_requests = types.ModuleType("alpaca.data.requests")

    enums.OrderSide = types.SimpleNamespace(SELL="sell", BUY="buy")
    enums.TimeInForce = types.SimpleNamespace(DAY="day")

    class MarketOrderRequest:
        def __init__(self, symbol, qty, side, time_in_force, client_order_id):
            self.symbol = symbol
            self.qty = qty
            self.side = side
            self.time_in_force = time_in_force
            self.client_order_id = client_order_id

    class StockLatestQuoteRequest:
        def __init__(self, symbol_or_symbols, feed):
            self.symbol_or_symbols = symbol_or_symbols
            self.feed = feed

    class APIError(Exception):
        pass

    FakeAPIError = APIError
    exceptions.APIError = APIError
    requests.MarketOrderRequest = MarketOrderRequest
    data_requests.StockLatestQuoteRequest = StockLatestQuoteRequest
    sys.modules["alpaca"] = alpaca
    sys.modules["alpaca.common"] = common
    sys.modules["alpaca.common.exceptions"] = exceptions
    sys.modules["alpaca.trading"] = trading
    sys.modules["alpaca.trading.enums"] = enums
    sys.modules["alpaca.trading.requests"] = requests
    sys.modules["alpaca.data"] = data
    sys.modules["alpaca.data.requests"] = data_requests


def remove_fake_alpaca_modules() -> None:
    for name in [
        "alpaca.data.requests",
        "alpaca.data",
        "alpaca.common.exceptions",
        "alpaca.common",
        "alpaca.trading.requests",
        "alpaca.trading.enums",
        "alpaca.trading",
        "alpaca",
    ]:
        sys.modules.pop(name, None)

FakeAPIError = Exception


if __name__ == "__main__":
    unittest.main()
