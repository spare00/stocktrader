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
from opening_plan import DEFAULT_OPENING_PLAN_FILE, apply_opening_plan, plan_overrides
from risk import RiskManager
from runtime_safety import flatten_on_shutdown
import scripts.build_opening_universe as build_opening_universe
from scripts.ai_opening_plan import build_plan, extract_json_object, plan_from_screen
from scripts.build_opening_universe import daily_metrics, score_symbol
import scripts.screen_opening_impulse as screen_opening_impulse
from scripts.screen_opening_impulse import DEFAULT_UNIVERSE, load_universe, opening_session_metrics, previous_session_dates, score_candidate, usable_quote, write_screen_output
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

    def test_default_opening_plan_path_is_conventional(self):
        self.assertEqual(DEFAULT_OPENING_PLAN_FILE, Path("data/opening_plan.json"))

    def test_ai_opening_plan_extracts_json_before_export_line(self):
        text = '{"symbols":["AAPL"],"settings":{"MAX_OPEN_POSITIONS":1}}\nexport SYMBOLS=AAPL\n'

        result = extract_json_object(text)

        self.assertEqual(result["symbols"], ["AAPL"])

    def test_ai_opening_plan_fallback_filters_weak_candidates(self):
        screen = {
            "candidates": [
                {"symbol": "KEEP", "close_capture_ratio": 0.25, "positive_close_day_ratio": 0.7, "fade_bps": 50},
                {"symbol": "DROP", "close_capture_ratio": -0.1, "positive_close_day_ratio": 0.4, "fade_bps": 180},
            ]
        }

        plan = plan_from_screen(screen, limit=12)

        self.assertEqual(plan["symbols"], ["KEEP"])
        self.assertEqual(plan["settings"]["MAX_OPEN_POSITIONS"], 1)
        self.assertEqual(plan["rejected"][0]["symbol"], "DROP")

    def test_ai_opening_plan_writes_default_shape_without_openai(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            screen_file = root / "opening_screen.json"
            universe_file = root / "opening_universe.txt"
            output = root / "opening_plan.json"
            screen_file.write_text(
                '{"candidates":[{"symbol":"AAPL","close_capture_ratio":0.2,"positive_close_day_ratio":0.6,"fade_bps":40}]}\n'
            )
            universe_file.write_text("AAPL,MSFT\n")

            plan = build_plan(
                types.SimpleNamespace(
                    universe_file=universe_file,
                    screen_file=screen_file,
                    output=output,
                    limit=12,
                    openai_api_key="",
                    alpaca_api_key="test",
                    alpaca_secret_key="test",
                )
            )

            saved = extract_json_object(output.read_text())

        self.assertEqual(plan["symbols"], ["AAPL"])
        self.assertEqual(saved["symbols"], ["AAPL"])
        self.assertIn("settings", saved)

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

            self.assertEqual(extract_json_object(output.read_text())["selected_symbols"], ["AAPL"])

    def test_opening_impulse_screener_ignores_invalid_quote_snapshot(self):
        self.assertIsNone(usable_quote(Quote("AAPL", bid=102.0, ask=0.0, bid_size=0, ask_size=0, timestamp_ms=0)))
        self.assertIsNone(usable_quote(Quote("AAPL", bid=102.0, ask=101.0, bid_size=10, ask_size=10, timestamp_ms=0)))

    def test_opening_impulse_screener_rejects_weak_daily_context(self):
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

        self.assertIsNone(result)

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

    def test_opening_impulse_screener_rejects_filter_boundaries(self):
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
            {"bars": opening_candidate(volumes=(100, 100))},
            {"bars": opening_candidate(highs=(100.5, 100.5), closes=(100.4, 100.4))},
            {"bars": [opening_candidate()[0]]},
            {"quote": Quote("AAPL", bid=104.0, ask=105.0, bid_size=200, ask_size=200, timestamp_ms=0)},
        ]
        for override in cases:
            params = {**common, **override}
            with self.subTest(override=override):
                self.assertIsNone(score_candidate(**params))

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

    def test_opening_universe_builder_scores_liquid_movers(self):
        bars = [
            daily_bar("AAPL", 100.0, 98.0, 103.0, market_ms(2026, 4, 20, 9, 30)),
            daily_bar("AAPL", 102.0, 99.0, 105.0, market_ms(2026, 4, 21, 9, 30)),
            daily_bar("AAPL", 104.0, 101.0, 107.0, market_ms(2026, 4, 22, 9, 30)),
        ]
        result = score_symbol(
            symbol="AAPL",
            bars=bars,
            quote=Quote("AAPL", bid=104.0, ask=104.04, bid_size=100, ask_size=100, timestamp_ms=0),
            min_price=10.0,
            max_price=900.0,
            min_dollar_volume=1_000_000.0,
            min_daily_range_bps=100.0,
            max_spread_bps=12.0,
        )

        self.assertIsNotNone(result)
        self.assertGreater(daily_metrics(bars)["median_daily_range_bps"], 100.0)
        self.assertEqual(result["symbol"], "AAPL")

    def test_opening_universe_builder_sorts_limits_and_writes_output(self):
        original_get_symbols = build_opening_universe.get_active_tradable_symbols
        original_get_bars = build_opening_universe.get_daily_bars
        original_get_quotes = build_opening_universe.get_latest_quotes
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
                    daily_bar_with_volume("HIGH", 100.0, 96.0, 106.0, 500_000, market_ms(2026, 4, 20, 9, 30)),
                    daily_bar_with_volume("HIGH", 108.0, 102.0, 114.0, 500_000, market_ms(2026, 4, 21, 9, 30)),
                ],
                "FAIL": [
                    daily_bar_with_volume("FAIL", 5.0, 4.9, 5.1, 500_000, market_ms(2026, 4, 20, 9, 30)),
                    daily_bar_with_volume("FAIL", 5.1, 5.0, 5.2, 500_000, market_ms(2026, 4, 21, 9, 30)),
                ],
            }

        def fake_get_quotes(settings, symbols, batch_size):
            raise AssertionError("quotes should be skipped")

        try:
            build_opening_universe.get_active_tradable_symbols = fake_get_symbols
            build_opening_universe.get_daily_bars = fake_get_bars
            build_opening_universe.get_latest_quotes = fake_get_quotes
            with tempfile.TemporaryDirectory() as tmpdir:
                output = Path(tmpdir) / "opening_universe.txt"
                result = build_opening_universe.build_universe(
                    types.SimpleNamespace(
                        limit=1,
                        output=output,
                        lookback_days=2,
                        batch_size=2,
                        exchanges="NASDAQ,NYSE",
                        min_price=10.0,
                        max_price=900.0,
                        min_dollar_volume=1_000_000.0,
                        min_daily_range_pct=0.01,
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
            build_opening_universe.get_active_tradable_symbols = original_get_symbols
            build_opening_universe.get_daily_bars = original_get_bars
            build_opening_universe.get_latest_quotes = original_get_quotes

    def test_opening_universe_builder_rejects_invalid_arguments(self):
        args = types.SimpleNamespace(limit=0, lookback_days=2, batch_size=1)

        with self.assertRaises(ValueError):
            build_opening_universe.build_universe(args)

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

    def test_opening_impulse_bar_confirmation_still_requires_tight_quote(self):
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

        self.assertIsNone(signal)

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

    def test_opening_impulse_exit_on_momentum_fade(self):
        settings = Settings(
            alpaca_api_key="test",
            alpaca_secret_key="test",
            symbols=["AAPL"],
            regular_market_only=False,
            opening_impulse_min_hold_seconds=0,
        )
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

    def test_opening_impulse_min_hold_delays_momentum_stall(self):
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

        self.assertIsNone(strategy.should_exit(state, position))

        position.entry_ms = -20_000
        decision = strategy.should_exit(state, position)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.reason, "momentum stall")

    def test_setup_logging_creates_rotating_log_file(self):
        old_log_dir = trading_main.LOG_DIR
        old_log_file = trading_main.LOG_FILE
        old_handlers = logging.getLogger().handlers[:]
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                trading_main.LOG_DIR = Path(tmpdir) / "logs"
                trading_main.LOG_FILE = trading_main.LOG_DIR / "trader.log"

                trading_main.setup_logging()
                logging.getLogger("strategies.opening_impulse").debug("diagnostic test")

                for handler in logging.getLogger().handlers:
                    handler.flush()

                self.assertTrue(trading_main.LOG_FILE.exists())
                self.assertIn("diagnostic test", trading_main.LOG_FILE.read_text())
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
    ):
        self.orders = orders
        self.positions = positions or []
        self.open_orders = open_orders or []
        self.cash = cash
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
        return self.orders.pop(0)

    def cancel_order_by_id(self, order_id: str) -> None:
        self.cancel_called = True
        self.canceled_order_ids.append(order_id)

    def get_order_by_id(self, order_id: str) -> FakeOrder:
        return self.orders.pop(0)


class FakeClients:
    def __init__(
        self,
        orders: list[FakeOrder],
        positions: list[FakePosition] | None = None,
        open_orders: list[FakeOrder] | None = None,
        cash: str = "10000.00",
    ):
        self.trading = FakeTrading(orders, positions=positions, open_orders=open_orders, cash=cash)


class FakeExecutor:
    def __init__(self):
        self.exit_calls = []

    def manage_exit(self, state, strategies_by_name, now_ms=None):
        self.exit_calls.append((state.symbol, now_ms))


def install_fake_alpaca_modules() -> None:
    alpaca = types.ModuleType("alpaca")
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

    requests.MarketOrderRequest = MarketOrderRequest
    sys.modules["alpaca"] = alpaca
    sys.modules["alpaca.trading"] = trading
    sys.modules["alpaca.trading.enums"] = enums
    sys.modules["alpaca.trading.requests"] = requests


def remove_fake_alpaca_modules() -> None:
    for name in ["alpaca.trading.requests", "alpaca.trading.enums", "alpaca.trading", "alpaca"]:
        sys.modules.pop(name, None)


if __name__ == "__main__":
    unittest.main()
