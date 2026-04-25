import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from alpaca_client import make_clients
from config import load_settings


def rest_check(symbols: list[str]) -> None:
    settings = load_settings()
    clients = make_clients(settings)

    account = clients.trading.get_account()
    clock = clients.trading.get_clock()

    print("Account")
    print(
        json.dumps(
            {
                "account_number": account.account_number,
                "buying_power": str(account.buying_power),
                "cash": str(account.cash),
                "currency": account.currency,
                "pattern_day_trader": account.pattern_day_trader,
                "status": str(account.status),
                "trading_blocked": account.trading_blocked,
            },
            indent=2,
            sort_keys=True,
        )
    )

    print("Clock")
    print(
        json.dumps(
            {
                "is_open": clock.is_open,
                "next_open": clock.next_open.isoformat(),
                "next_close": clock.next_close.isoformat(),
                "timestamp": clock.timestamp.isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
    )

    request = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Minute, limit=5, feed=clients.feed)
    bars = clients.historical.get_stock_bars(request)
    print("Recent bars")
    for symbol, items in bars.data.items():
        compact = [
            {
                "symbol": item.symbol,
                "time": item.timestamp.isoformat(),
                "open": item.open,
                "high": item.high,
                "low": item.low,
                "close": item.close,
                "volume": item.volume,
            }
            for item in items[-3:]
        ]
        print(json.dumps({symbol: compact}, indent=2, sort_keys=True))


async def stream_check(symbols: list[str], seconds: int, max_messages: int) -> None:
    settings = load_settings()
    clients = make_clients(settings)
    queue: asyncio.Queue[dict] = asyncio.Queue()

    async def on_bar(bar) -> None:
        await queue.put(
            {
                "type": "bar",
                "symbol": bar.symbol,
                "time": bar.timestamp.isoformat(),
                "close": bar.close,
                "volume": bar.volume,
            }
        )

    async def on_quote(quote) -> None:
        await queue.put(
            {
                "type": "quote",
                "symbol": quote.symbol,
                "time": quote.timestamp.isoformat(),
                "bid": quote.bid_price,
                "ask": quote.ask_price,
            }
        )

    for symbol in symbols:
        clients.stream.subscribe_bars(on_bar, symbol)
        clients.stream.subscribe_quotes(on_quote, symbol)

    print(f"Streaming feed={clients.feed.value} symbols={','.join(symbols)}")
    task = asyncio.create_task(clients.stream._run_forever())
    seen = 0
    deadline = asyncio.get_running_loop().time() + seconds
    try:
        while seen < max_messages:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=remaining)
            except TimeoutError:
                break
            print(json.dumps(payload, sort_keys=True))
            seen += 1
    finally:
        await clients.stream.stop_ws()
        await asyncio.gather(task, return_exceptions=True)

    print(f"Messages seen: {seen}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test Alpaca paper trading and market data.")
    parser.add_argument("mode", choices=["rest", "stream", "both"], nargs="?", default="rest")
    parser.add_argument("--symbols", default="AAPL,MSFT")
    parser.add_argument("--seconds", type=int, default=15)
    parser.add_argument("--max-messages", type=int, default=10)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    symbols = [symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()]
    if args.mode in {"rest", "both"}:
        rest_check(symbols)
    if args.mode in {"stream", "both"}:
        asyncio.run(stream_check(symbols, args.seconds, args.max_messages))


if __name__ == "__main__":
    main()
