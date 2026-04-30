import asyncio
import argparse
import hashlib
import json
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ai_agent import SignalReviewer
from alpaca_stream import AlpacaStreamAuthError, AlpacaStreamConnectionLimitError, build_market_data_stream
from candle import SymbolState
from config import load_settings
from execution import build_executor
from models import Bar, Heartbeat, Quote
from opening_plan import (
    apply_opening_plan,
    default_plan_file_for_settings,
    load_opening_plan,
    parse_plan_symbols,
    selector_command_for_strategy,
)
from risk import RiskManager
from runtime_safety import flatten_on_shutdown, manage_all_exits
from strategies import available_strategy_names, build_strategies


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s | %(message)s"
LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "trader.log"
DIAGNOSTIC_LOGGERS = ("strategies.opening_impulse", "strategies.gap_and_go", "strategies.maha7_pullback_reclaim")
NOISY_LOGGERS = (
    "alpaca",
    "alpaca.data.live.websocket",
    "websockets",
    "websockets.client",
)
ALPACA_STREAM_LOGGER = "alpaca.data.live.websocket"


class FriendlyAlpacaStreamErrorFilter(logging.Filter):
    def __init__(self, min_interval_seconds: float = 60.0):
        super().__init__()
        self.min_interval_seconds = min_interval_seconds
        self._last_logged: dict[str, float] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != ALPACA_STREAM_LOGGER or "error during websocket communication" not in record.getMessage():
            return True

        detail = self._error_detail(record)
        key = "dns" if self._is_dns_error(detail) else "websocket"
        now = time.monotonic()
        last_logged = self._last_logged.get(key)
        if last_logged is not None and now - last_logged < self.min_interval_seconds:
            return False
        self._last_logged[key] = now

        if key == "dns":
            record.msg = (
                "Alpaca market-data stream connection problem: cannot resolve Alpaca's data host. "
                "Check internet/DNS/VPN; the bot will not receive live quotes until the stream reconnects. "
                "Detail: %s"
            )
        else:
            record.msg = (
                "Alpaca market-data stream interrupted. The bot may miss live quotes until the stream reconnects. "
                "Detail: %s"
            )
        record.args = (detail,)
        record.exc_info = None
        record.exc_text = None
        return True

    @staticmethod
    def _error_detail(record: logging.LogRecord) -> str:
        if record.exc_info and record.exc_info[1]:
            return str(record.exc_info[1]) or record.exc_info[1].__class__.__name__
        message = record.getMessage().replace("error during websocket communication", "").strip(": ")
        return message or "unknown stream error"

    @staticmethod
    def _is_dns_error(detail: str) -> bool:
        detail_lower = detail.lower()
        return (
            "nodename nor servname provided" in detail_lower
            or "name or service not known" in detail_lower
            or "temporary failure in name resolution" in detail_lower
        )


class FatalAlpacaStreamErrorFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != ALPACA_STREAM_LOGGER or "error during websocket communication" not in record.getMessage():
            return True
        detail = FriendlyAlpacaStreamErrorFilter._error_detail(record)
        if "connection limit exceeded" in detail.lower():
            raise AlpacaStreamConnectionLimitError(
                "Alpaca data stream connection limit exceeded for this API key/feed. "
                "Confirm this runner is using the intended .env key, stop other streams using that same key/feed, "
                "or set ALPACA_MARKET_DATA_MODE=rest for a polling runner."
            )
        return True


class RejectionLogThrottler:
    def __init__(self, min_interval_seconds: float = 30.0):
        self.min_interval_seconds = min_interval_seconds
        self._last_logged: dict[tuple[str, str, str, str], float] = {}

    def should_log(self, symbol: str, side: str, strategy: str, reason: str) -> bool:
        key = (symbol, side, strategy, reason)
        now = time.monotonic()
        last_logged = self._last_logged.get(key)
        if last_logged is not None and now - last_logged < self.min_interval_seconds:
            return False
        self._last_logged[key] = now
        return True


def mark_prices(states: dict[str, SymbolState]) -> dict[str, float]:
    return {symbol: state.last_price for symbol, state in states.items() if state.last_price is not None}


def credential_fingerprint(value: str | None) -> str | None:
    if not value:
        return None
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    suffix = value[-4:] if len(value) >= 4 else value
    return f"{digest}:{suffix}"


def runtime_settings_snapshot(settings) -> dict:
    snapshot = {
        "execution_mode": settings.execution_mode,
        "strategies": settings.strategy_names,
        "symbols": settings.symbols,
        "alpaca_paper": settings.alpaca_paper,
        "alpaca_data_feed": settings.alpaca_data_feed,
        "alpaca_api_key_fingerprint": credential_fingerprint(settings.alpaca_api_key),
        "alpaca_market_data_mode": settings.alpaca_market_data_mode,
        "regular_market_only": settings.regular_market_only,
        "ai_review": settings.ai_review,
        "openai_model": settings.openai_model if settings.ai_review else None,
        "risk": {
            "target_profit_pct": settings.target_profit_pct,
            "stop_loss_pct": settings.stop_loss_pct,
            "max_hold_seconds": settings.max_hold_seconds,
            "max_position_value": settings.max_position_value,
            "max_open_positions": settings.max_open_positions,
            "trade_cooldown_seconds": settings.trade_cooldown_seconds,
            "daily_max_loss": settings.daily_max_loss,
            "flatten_before_close_minutes": settings.flatten_before_close_minutes,
        },
        "stream": {
            "heartbeat_seconds": settings.heartbeat_seconds,
            "alpaca_market_data_poll_seconds": settings.alpaca_market_data_poll_seconds,
            "alpaca_fill_timeout_seconds": settings.alpaca_fill_timeout_seconds,
            "alpaca_fill_poll_seconds": settings.alpaca_fill_poll_seconds,
        },
    }

    if "spike" in settings.strategy_names:
        snapshot["spike"] = {
            "start_minute": settings.spike_start_minute,
            "end_minute": settings.spike_end_minute,
            "lookback_seconds": settings.spike_lookback_seconds,
            "change_pct": settings.spike_change_pct,
            "volume_ratio": settings.volume_ratio,
            "max_spread_bps": settings.max_spread_bps,
        }

    if "gap_and_go" in settings.strategy_names:
        snapshot["gap_and_go"] = {
            "start_minute": settings.gap_and_go_start_minute,
            "end_minute": settings.gap_and_go_end_minute,
            "min_gap_pct": settings.gap_and_go_min_gap_pct,
            "premarket_volume_ratio": settings.gap_and_go_premarket_volume_ratio,
            "max_spread_bps": settings.gap_and_go_max_spread_bps,
            "min_price": settings.gap_and_go_min_price,
            "breakout_buffer_pct": settings.gap_and_go_breakout_buffer_pct,
            "exit_activation_delay_seconds": settings.gap_and_go_exit_activation_delay_seconds,
            "trailing_retrace_pct": settings.gap_and_go_trailing_retrace_pct,
            "bar_window": settings.gap_and_go_bar_window,
        }

    if "opening_impulse" in settings.strategy_names:
        snapshot["opening_impulse"] = {
            "start_minute": settings.opening_impulse_start_minute,
            "end_minute": settings.opening_impulse_end_minute,
            "last_entry_hour_et": settings.opening_impulse_last_entry_hour_et,
            "window_seconds": settings.opening_impulse_window_seconds,
            "min_quotes": settings.opening_impulse_min_quotes,
            "change_pct": settings.opening_impulse_change_pct,
            "skip_extended_pct": settings.opening_impulse_skip_extended_pct,
            "volume_ratio": settings.opening_impulse_volume_ratio,
            "bar_confirmation": settings.opening_impulse_bar_confirmation,
            "bar_window": settings.opening_impulse_bar_window,
            "bar_min_rising": settings.opening_impulse_bar_min_rising,
            "bar_change_pct": settings.opening_impulse_bar_change_pct,
            "bar_volume_ratio": settings.opening_impulse_bar_volume_ratio,
            "range_minutes": settings.opening_impulse_range_minutes,
            "range_breakout_enabled": settings.opening_impulse_enable_range_breakout,
            "range_reversal_enabled": settings.opening_impulse_enable_range_reversal,
            "range_breakout_buffer_pct": settings.opening_impulse_range_breakout_buffer_pct,
            "range_reversal_min_drop_pct": settings.opening_impulse_range_reversal_min_drop_pct,
            "range_reclaim_buffer_pct": settings.opening_impulse_range_reclaim_buffer_pct,
            "range_volume_ratio": settings.opening_impulse_range_volume_ratio,
            "max_spread_bps": settings.opening_impulse_max_spread_bps,
            "min_quote_size": settings.opening_impulse_min_quote_size,
            "max_negative_steps": settings.opening_impulse_max_negative_steps,
            "exit_window_seconds": settings.opening_impulse_exit_window_seconds,
            "exit_min_quotes": settings.opening_impulse_exit_min_quotes,
            "exit_negative_steps": settings.opening_impulse_exit_negative_steps,
            "min_hold_seconds": settings.opening_impulse_min_hold_seconds,
            "winner_min_pnl_pct": settings.opening_impulse_winner_min_pnl_pct,
            "early_loss_cut_pct": settings.opening_impulse_early_loss_cut_pct,
            "stall_buffer_pct": settings.opening_impulse_stall_buffer_pct,
            "retrace_from_high_pct": settings.opening_impulse_retrace_from_high_pct,
        }

    if "maha7_pullback_reclaim" in settings.strategy_names:
        snapshot["maha7_pullback_reclaim"] = {
            "start_minute": settings.maha7_pullback_reclaim_start_minute,
            "end_minute": settings.maha7_pullback_reclaim_end_minute,
            "rsi_period": settings.maha7_pullback_reclaim_rsi_period,
            "rsi_above_min_bars": settings.maha7_pullback_reclaim_rsi_above_min_bars,
            "flat_slope_pct": settings.maha7_pullback_reclaim_flat_slope_pct,
            "consolidation_candles": settings.maha7_pullback_reclaim_consolidation_candles,
            "vwap_min_distance_pct": settings.maha7_pullback_reclaim_vwap_min_distance_pct,
            "pullback_ma7_distance_pct": settings.maha7_pullback_reclaim_pullback_ma7_distance_pct,
            "volume_min_ratio": settings.maha7_pullback_reclaim_volume_min_ratio,
            "min_minutes_after_opening_impulse": settings.maha7_pullback_reclaim_min_minutes_after_opening_impulse,
            "reentry_cooldown_seconds": settings.maha7_pullback_reclaim_reentry_cooldown_seconds,
            "partial_r": settings.maha7_pullback_reclaim_partial_r,
            "target_r": settings.maha7_pullback_reclaim_target_r,
        }

    return snapshot

def strategy_log_file(settings) -> Path:
    strategies = [name.strip().lower().replace(" ", "_") for name in settings.strategy_names if name.strip()]
    if not strategies:
        return LOG_FILE
    suffix = "__".join(strategies)
    return LOG_DIR / f"trader_{suffix}.log"


def setup_logging(log_file: Path | None = None) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    console.addFilter(FatalAlpacaStreamErrorFilter())
    console.addFilter(FriendlyAlpacaStreamErrorFilter())
    target_log_file = log_file or LOG_FILE
    file_handler = RotatingFileHandler(target_log_file, maxBytes=5_000_000, backupCount=10)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(FatalAlpacaStreamErrorFilter())
    file_handler.addFilter(FriendlyAlpacaStreamErrorFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(file_handler)

    for logger_name in DIAGNOSTIC_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.DEBUG)
    for logger_name in NOISY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.INFO)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the paper-trading monitor.")
    parser.add_argument(
        "-s",
        "--strategy",
        choices=available_strategy_names(),
        help="Run exactly one strategy for this session. Overrides STRATEGIES for main.py.",
    )
    parser.add_argument(
        "-l",
        "--list-strategies",
        action="store_true",
        help="List available strategies and exit.",
    )
    parser.add_argument("--opening-plan", type=Path, default=None, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def resolve_strategy_plan_path(settings, explicit_path: Path | None) -> Path:
    return explicit_path or default_plan_file_for_settings(settings)


def validate_strategy_plan(path: Path, settings) -> list[str]:
    if not path.exists():
        strategy_name = settings.strategy_names[0] if settings.strategy_names else "opening_impulse"
        command = selector_command_for_strategy(strategy_name)
        raise FileNotFoundError(
            f"Missing strategy plan file: {path}. Run the selector first, for example: {command}"
        )

    plan = load_opening_plan(path)
    symbols = parse_plan_symbols(plan)
    if not symbols:
        strategy_name = settings.strategy_names[0] if settings.strategy_names else "opening_impulse"
        command = selector_command_for_strategy(strategy_name)
        raise ValueError(
            f"Strategy plan file has no selected symbols: {path}. Regenerate it first, for example: {command}"
        )
    return symbols


def strategy_plan_guide(path: Path, settings, error: Exception) -> str:
    strategy_name = settings.strategy_names[0] if settings.strategy_names else "opening_impulse"
    selector_command = selector_command_for_strategy(strategy_name)
    run_command = f"scripts/run_paper.sh -s {strategy_name}"
    return "\n".join(
        [
            f"Strategy plan is not ready for `{strategy_name}`.",
            f"Problem: {error}",
            "",
            "Create or refresh the strategy plan first:",
            f"  {selector_command}",
            "",
            "Then start paper trading again:",
            f"  {run_command}",
        ]
    )


async def main(args: argparse.Namespace | None = None) -> None:
    args = args or parse_args()
    if args.list_strategies:
        print("\n".join(available_strategy_names()))
        return
    requested_strategies = [args.strategy] if args.strategy else None
    settings = load_settings(strategy_names=requested_strategies)
    opening_plan_path = resolve_strategy_plan_path(settings, args.opening_plan)
    try:
        validate_strategy_plan(opening_plan_path, settings)
    except (FileNotFoundError, ValueError) as exc:
        print(strategy_plan_guide(opening_plan_path, settings, exc), file=sys.stderr)
        raise SystemExit(2) from None
    settings = apply_opening_plan(settings, opening_plan_path)
    log_file = strategy_log_file(settings)
    setup_logging(log_file)
    logging.info("Loaded opening plan from %s", opening_plan_path)

    settings_snapshot = runtime_settings_snapshot(settings)
    states = {symbol: SymbolState(symbol) for symbol in settings.symbols}
    stream = build_market_data_stream(settings)
    strategies = build_strategies(settings)
    for strategy in strategies:
        strategy.bootstrap_states(states)
    strategies_by_name = {strategy.name: strategy for strategy in strategies}
    executor = build_executor(settings)
    risk = RiskManager(settings)
    reviewer = SignalReviewer(settings)
    rejection_logs = RejectionLogThrottler()

    logging.info(
        "Monitoring %s with execution mode %s and strategies %s",
        ", ".join(settings.symbols),
        settings.execution_mode,
        ", ".join(settings.strategy_names),
    )
    logging.info("Runtime settings %s", json.dumps(settings_snapshot, sort_keys=True))

    try:
        async for event in stream.events():
            if isinstance(event, Heartbeat):
                manage_all_exits(executor, states, strategies_by_name, event.timestamp_ms)
                continue

            state = states.get(event.symbol)
            if state is None:
                continue

            if isinstance(event, Quote):
                state.update_quote(event)
            elif isinstance(event, Bar):
                state.add_bar(event)
            else:
                continue

            event_ms = state.last_event_ms
            manage_all_exits(executor, states, strategies_by_name, event_ms)

            for strategy in strategies:
                signal = strategy.evaluate(state)
                if not signal:
                    continue

                decision = risk.check_entry(signal, executor.open_symbols(), executor.total_pnl(mark_prices(states)))
                if not decision.allowed:
                    if rejection_logs.should_log(signal.symbol, signal.side, signal.strategy, decision.reason):
                        logging.info(
                            "Signal rejected %s %s from %s: %s",
                            signal.symbol,
                            signal.side,
                            signal.strategy,
                            decision.reason,
                        )
                    continue

                note = await asyncio.to_thread(reviewer.review, signal)
                if note:
                    logging.info("AI review %s %s: %s", signal.strategy, signal.symbol, note)

                fill = executor.buy(signal)
                if fill:
                    risk.record_trade(signal.symbol, signal.timestamp_ms, signal.strategy)
                    break
    finally:
        flatten_on_shutdown(settings, executor, states, strategies_by_name)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AlpacaStreamAuthError as exc:
        logging.error("Alpaca stream authentication failed: %s", exc)
    except AlpacaStreamConnectionLimitError as exc:
        logging.error("%s", exc)
    except KeyboardInterrupt:
        logging.info("Stopped")
