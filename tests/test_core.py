import unittest
import sys
import types
import tempfile
import logging
import json
from collections import deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from candle import SymbolState
from config import Settings
import execution as execution_module
from execution import AlpacaPaperExecutor, LocalPaperExecutor, Position, PositionTracker
import main as trading_main
from models import Bar, Quote, Signal
from opening_plan import (
    DEFAULT_OPENING_PLAN_FILE,
    apply_opening_plan,
    default_plan_file_for_strategy,
    plan_overrides,
    selector_command_for_strategy,
)
from risk import RiskManager
from runtime_safety import flatten_on_shutdown
import scripts.select_market_universe as select_market_universe
import scripts.analyze_trade_journal as analyze_trade_journal
import scripts.select_gap_and_go as select_gap_and_go
from scripts.select_market_universe import daily_metrics, score_symbol
import scripts.select_opening_impulse as select_opening_impulse
from scripts.select_opening_impulse import DEFAULT_UNIVERSE, daily_gap_score, load_universe, opening_session_metrics, previous_session_dates, recent_compression_score, score_candidate, usable_quote, write_screen_output
from strategies import available_strategy_names, build_strategies
from strategies.gap_and_go import GapAndGoStrategy
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

        overrides = plan_overrides(settings, plan)

        self.assertEqual(overrides["symbols"], ["INTC", "PANW"])
        self.assertEqual(overrides["max_open_positions"], 2)
        self.assertEqual(overrides["max_position_value"], 2_500.0)
        self.assertEqual(overrides["stop_loss_pct"], 0.005)
        self.assertEqual(overrides["target_profit_pct"], 0.02)
        self.assertEqual(overrides["opening_impulse_volume_ratio"], 1.5)
        self.assertNotIn("regular_market_only", overrides)

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

            updated = apply_opening_plan(settings, path)

        self.assertEqual(updated.symbols, ["INTC"])
        self.assertEqual(updated.max_open_positions, 1)
        self.assertEqual(updated.opening_impulse_change_pct, 0.008)

    def test_default_opening_plan_path_is_strategy_specific(self):
        self.assertEqual(DEFAULT_OPENING_PLAN_FILE, Path("data/opening_impulse_plan.json"))
        self.assertEqual(default_plan_file_for_strategy("gap_and_go"), Path("data/gap_and_go_plan.json"))
        self.assertEqual(
            selector_command_for_strategy("gap_and_go"),
            "venv/bin/python scripts/select_gap_and_go.py --top 5",
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
            {"symbols": ["MSFT", "GONE"], "rejected": ["GONE"], "settings": {}, "risk_note": "test"},
            screen_result,
            limit=2,
        )

        self.assertEqual(validated["symbols"], ["MSFT", "AAPL"])
        self.assertEqual(validated["ranked"][0]["symbol"], "MSFT")

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
            self.assertAlmostEqual(summary["average_pnl_pct"], 0.0)
            self.assertAlmostEqual(summary["average_mfe_pct"], 0.01)
            self.assertAlmostEqual(summary["average_mae_pct"], -0.01)
            self.assertAlmostEqual(summary["average_missed_profit_pct"], 0.01)
            self.assertEqual(summary["by_exit_reason"]["target profit"]["trades"], 1)
            self.assertAlmostEqual(summary["by_exit_reason"]["target profit"]["average_pnl_pct"], 0.01)
            self.assertAlmostEqual(summary["by_exit_reason"]["target profit"]["average_mfe_pct"], 0.02)
            self.assertAlmostEqual(summary["by_exit_reason"]["target profit"]["average_hold_seconds"], 10.0)
            self.assertEqual(summary["by_exit_reason"]["momentum stall"]["trades"], 1)
            self.assertEqual(summary["by_symbol"]["MU"]["total_pnl"], 4.0)
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

    def test_opening_impulse_screener_writes_output_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "opening_screen.json"

            write_screen_output({"selected_symbols": ["AAPL"], "export": "export SYMBOLS=AAPL"}, output)

            self.assertEqual(json.loads(output.read_text())["selected_symbols"], ["AAPL"])

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

    def test_opening_impulse_exit_activation_delay_blocks_immediate_target_exit(self):
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
        self.assertIsNotNone(fill)
        self.assertEqual(fill.reason, "target profit")

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

    def test_opening_impulse_trailing_stop_protects_winner(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            opening_impulse_min_hold_seconds=15,
            opening_impulse_winner_min_pnl_pct=0.003,
            opening_impulse_retrace_from_high_pct=0.008,
            opening_impulse_exit_negative_steps=99,
        )
        strategy = OpeningImpulseStrategy(settings)
        state = SymbolState("AAPL")
        state.add_bar(Bar("AAPL", open=100.0, high=100.4, low=99.9, close=100.3, volume=100, vwap=100.2, start_ms=1_000, end_ms=61_000))
        state.add_bar(Bar("AAPL", open=100.3, high=100.8, low=100.2, close=100.7, volume=120, vwap=100.5, start_ms=61_000, end_ms=121_000))
        state.add_bar(Bar("AAPL", open=100.7, high=101.0, low=100.6, close=100.95, volume=130, vwap=100.8, start_ms=121_000, end_ms=181_000))
        state.add_bar(Bar("AAPL", open=100.95, high=101.2, low=100.8, close=101.05, volume=140, vwap=101.0, start_ms=181_000, end_ms=241_000))
        state.add_bar(Bar("AAPL", open=101.05, high=101.25, low=100.35, close=100.4, volume=150, vwap=100.7, start_ms=241_000, end_ms=301_000))
        state.update_quote(Quote("AAPL", bid=100.39, ask=100.41, bid_size=20, ask_size=20, timestamp_ms=301_000))

        position = Position(
            symbol="AAPL",
            strategy="opening_impulse",
            shares=10,
            entry_price=100.0,
            entry_ms=1_000,
            target_price=101.0,
            stop_price=99.5,
        )

        decision = strategy.should_exit(state, position)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "trailing stop")

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
                )

                tracker.record_entry(signal, shares=3, fill_price=100.1, reason="test impulse", order_id="buy-1")
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
                self.assertAlmostEqual(rows[1]["pnl"], 3.3)
                self.assertEqual(rows[1]["reason"], "target profit")
                self.assertEqual(rows[1]["trade_type"], "winner")
        finally:
            execution_module.TRADE_JOURNAL_FILE = old_trade_journal_file

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

    def test_opening_impulse_bar_confirmation_softens_wide_spread(self):
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

        signal = OpeningImpulseStrategy(settings).evaluate(state)

        self.assertIsNotNone(signal)
        self.assertIn("opening bar impulse", signal.reason)
        self.assertIn("wide spread", signal.reason)

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
            state.add_bar(Bar("AAPL", open=price, high=price + 0.2, low=price - 0.1, close=price + 0.05, volume=350, vwap=price, start_ms=start_ms, end_ms=start_ms + 60_000))
        state.add_bar(Bar("AAPL", open=102.5, high=102.8, low=102.4, close=102.7, volume=400, vwap=102.6, start_ms=today_open, end_ms=today_open + 60_000))
        state.update_quote(Quote("AAPL", bid=102.81, ask=102.83, bid_size=100, ask_size=100, timestamp_ms=today_open + 65_000))

        signal = strategy.evaluate(state)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.strategy, "gap_and_go")
        self.assertIn("breakout above premarket high", signal.reason)

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

    def test_gap_and_go_ai_selection_is_bounded_to_ranked_candidates(self):
        candidates = [
            select_gap_and_go.GapAndGoCandidate("AAPL", 7.2, 0.03, 3.4, 4.2, 103.5, 100.0, 102.0, 103.2, False),
            select_gap_and_go.GapAndGoCandidate("MSFT", 6.4, 0.025, 2.8, 5.1, 431.0, 420.0, 429.0, 430.5, False),
        ]

        validated = select_gap_and_go.validated_gap_and_go_selection(
            {"symbols": ["MSFT", "GONE"], "rejected": ["GONE"], "risk_note": "test"},
            candidates,
            limit=2,
        )

        self.assertEqual(validated["symbols"], ["MSFT", "AAPL"])
        self.assertEqual(validated["ranked"][0]["symbol"], "MSFT")

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
        self.assertIn("premarket volume 1.10x < 2.00x", candidate.quality_flags)

    def test_gap_and_go_skips_entries_outside_window(self):
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

        with self.assertLogs("strategies.gap_and_go", level="DEBUG") as captured:
            signal = strategy.evaluate(state)

        self.assertIsNone(signal)
        self.assertIn("outside gap-and-go entry window", "\n".join(captured.output))

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

    def test_opening_impulse_skips_entries_after_noon(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            opening_impulse_start_minute=0,
            opening_impulse_end_minute=180,
            opening_impulse_last_entry_hour_et=12,
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

        with self.assertLogs("strategies.opening_impulse", level="DEBUG") as captured:
            signal = OpeningImpulseStrategy(settings).evaluate(state)

        self.assertIsNone(signal)
        self.assertIn("outside opening impulse entry window", "\n".join(captured.output))

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
            opening_impulse_last_entry_hour_et=12,
            opening_impulse_min_hold_seconds=15,
            opening_impulse_exit_negative_steps=4,
            opening_impulse_winner_min_pnl_pct=0.003,
        )

        snapshot = trading_main.runtime_settings_snapshot(settings)

        self.assertEqual(snapshot["execution_mode"], "alpaca_paper")
        self.assertEqual(snapshot["symbols"], ["AAPL", "MSFT"])
        self.assertEqual(snapshot["risk"]["target_profit_pct"], 0.01)
        self.assertEqual(snapshot["gap_and_go"]["min_gap_pct"], 0.02)
        self.assertEqual(snapshot["opening_impulse"]["last_entry_hour_et"], 12)
        self.assertEqual(snapshot["opening_impulse"]["min_hold_seconds"], 15)
        self.assertEqual(snapshot["opening_impulse"]["exit_negative_steps"], 4)
        self.assertEqual(snapshot["opening_impulse"]["winner_min_pnl_pct"], 0.003)
        self.assertEqual(snapshot["spike"]["lookback_seconds"], settings.spike_lookback_seconds)

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
            strategy_names=["spike", "opening_impulse", "gap_and_go"],
        )

        strategies = build_strategies(settings)

        self.assertEqual([strategy.name for strategy in strategies], ["spike", "opening_impulse", "gap_and_go"])

    def test_available_strategy_names_lists_registry_order(self):
        self.assertEqual(available_strategy_names(), ["gap_and_go", "spike", "opening_impulse"])

    def test_parse_args_accepts_strategy_and_list_flag(self):
        args = trading_main.parse_args(["--strategy", "gap_and_go"])

        self.assertEqual(args.strategy, "gap_and_go")
        self.assertFalse(args.list_strategies)
        self.assertIsNone(args.opening_plan)

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


class FakeClients:
    def __init__(
        self,
        orders: list[FakeOrder],
        positions: list[FakePosition] | None = None,
        open_orders: list[FakeOrder] | None = None,
        cash: str = "10000.00",
        submit_error: Exception | None = None,
        cancel_error: Exception | None = None,
    ):
        self.trading = FakeTrading(
            orders,
            positions=positions,
            open_orders=open_orders,
            cash=cash,
            submit_error=submit_error,
            cancel_error=cancel_error,
        )


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

    enums.OrderSide = types.SimpleNamespace(SELL="sell", BUY="buy")
    enums.TimeInForce = types.SimpleNamespace(DAY="day")

    class MarketOrderRequest:
        def __init__(self, symbol, qty, side, time_in_force, client_order_id):
            self.symbol = symbol
            self.qty = qty
            self.side = side
            self.time_in_force = time_in_force
            self.client_order_id = client_order_id

    class APIError(Exception):
        pass

    FakeAPIError = APIError
    exceptions.APIError = APIError
    requests.MarketOrderRequest = MarketOrderRequest
    sys.modules["alpaca"] = alpaca
    sys.modules["alpaca.common"] = common
    sys.modules["alpaca.common.exceptions"] = exceptions
    sys.modules["alpaca.trading"] = trading
    sys.modules["alpaca.trading.enums"] = enums
    sys.modules["alpaca.trading.requests"] = requests


def remove_fake_alpaca_modules() -> None:
    for name in ["alpaca.common.exceptions", "alpaca.common", "alpaca.trading.requests", "alpaca.trading.enums", "alpaca.trading", "alpaca"]:
        sys.modules.pop(name, None)

FakeAPIError = Exception


if __name__ == "__main__":
    unittest.main()
