import asyncio
import logging

from ai_agent import SignalReviewer
from alpaca_stream import AlpacaStockStream, AlpacaStreamAuthError
from candle import SymbolState
from config import load_settings
from execution import PaperBroker
from models import Bar, Quote
from risk import RiskManager
from strategy import SpikeStrategy


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s | %(message)s"


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

    settings = load_settings()
    states = {symbol: SymbolState(symbol) for symbol in settings.symbols}
    stream = AlpacaStockStream(settings)
    strategy = SpikeStrategy(settings)
    broker = PaperBroker(settings)
    risk = RiskManager(settings)
    reviewer = SignalReviewer(settings)

    logging.info("Monitoring %s in Alpaca paper mode", ", ".join(settings.symbols))

    async for event in stream.events():
        state = states.get(event.symbol)
        if state is None:
            continue

        if isinstance(event, Quote):
            state.update_quote(event)
            continue

        if isinstance(event, Bar):
            state.add_bar(event)
            broker.manage_exit(event)

            signal = strategy.evaluate(state)
            if not signal:
                continue

            decision = risk.check_entry(signal, broker.open_symbols(), broker.realized_pnl)
            if not decision.allowed:
                logging.info("Signal rejected %s %s: %s", signal.symbol, signal.side, decision.reason)
                continue

            note = await asyncio.to_thread(reviewer.review, signal)
            if note:
                logging.info("AI review %s: %s", signal.symbol, note)

            fill = broker.buy(signal)
            if fill:
                risk.record_trade(signal.symbol, signal.timestamp_ms)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AlpacaStreamAuthError as exc:
        logging.error("Alpaca stream authentication failed: %s", exc)
    except KeyboardInterrupt:
        logging.info("Stopped")
