import asyncio
import argparse
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ai_agent import SignalReviewer
from alpaca_stream import AlpacaStockStream, AlpacaStreamAuthError
from candle import SymbolState
from config import load_settings
from execution import build_executor
from models import Bar, Heartbeat, Quote
from opening_plan import DEFAULT_OPENING_PLAN_FILE, apply_opening_plan
from risk import RiskManager
from runtime_safety import flatten_on_shutdown, manage_all_exits
from strategies import build_strategies


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s | %(message)s"
LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "trader.log"
DIAGNOSTIC_LOGGERS = ("strategies.opening_impulse",)
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


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    console.addFilter(FriendlyAlpacaStreamErrorFilter())
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=10)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the paper-trading monitor.")
    parser.add_argument("--use-opening-plan", action="store_true", help="Use data/opening_plan.json for pre-market symbols/settings.")
    parser.add_argument("--opening-plan", type=Path, default=None, help=argparse.SUPPRESS)
    return parser.parse_args()


async def main(args: argparse.Namespace | None = None) -> None:
    setup_logging()

    args = args or parse_args()
    settings = load_settings()
    opening_plan_path = args.opening_plan or (DEFAULT_OPENING_PLAN_FILE if args.use_opening_plan else None)
    if opening_plan_path:
        settings = apply_opening_plan(settings, opening_plan_path)
        logging.info("Loaded opening plan from %s", opening_plan_path)

    states = {symbol: SymbolState(symbol) for symbol in settings.symbols}
    stream = AlpacaStockStream(settings)
    strategies = build_strategies(settings)
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
                    risk.record_trade(signal.symbol, signal.timestamp_ms)
                    break
    finally:
        flatten_on_shutdown(settings, executor, states, strategies_by_name)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AlpacaStreamAuthError as exc:
        logging.error("Alpaca stream authentication failed: %s", exc)
    except KeyboardInterrupt:
        logging.info("Stopped")
