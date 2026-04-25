import asyncio
import logging

from ai_agent import SignalReviewer
from alpaca_stream import AlpacaStockStream, AlpacaStreamAuthError
from candle import SymbolState
from config import load_settings
from execution import build_executor
from models import Bar, Heartbeat, Quote
from risk import RiskManager
from strategies import build_strategies


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s | %(message)s"


def mark_prices(states: dict[str, SymbolState]) -> dict[str, float]:
    return {symbol: state.last_price for symbol, state in states.items() if state.last_price is not None}


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

    settings = load_settings()
    states = {symbol: SymbolState(symbol) for symbol in settings.symbols}
    stream = AlpacaStockStream(settings)
    strategies = build_strategies(settings)
    strategies_by_name = {strategy.name: strategy for strategy in strategies}
    executor = build_executor(settings)
    risk = RiskManager(settings)
    reviewer = SignalReviewer(settings)

    logging.info(
        "Monitoring %s with execution mode %s and strategies %s",
        ", ".join(settings.symbols),
        settings.execution_mode,
        ", ".join(settings.strategy_names),
    )

    async for event in stream.events():
        if isinstance(event, Heartbeat):
            for exit_state in states.values():
                executor.manage_exit(exit_state, strategies_by_name, event.timestamp_ms)
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
        for exit_state in states.values():
            executor.manage_exit(exit_state, strategies_by_name, event_ms)

        for strategy in strategies:
            signal = strategy.evaluate(state)
            if not signal:
                continue

            decision = risk.check_entry(signal, executor.open_symbols(), executor.total_pnl(mark_prices(states)))
            if not decision.allowed:
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


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AlpacaStreamAuthError as exc:
        logging.error("Alpaca stream authentication failed: %s", exc)
    except KeyboardInterrupt:
        logging.info("Stopped")
