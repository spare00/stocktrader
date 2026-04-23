import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import load_settings
from massive_rest import MassiveApiError, MassiveRestClient


def compact_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    ticker = payload.get("ticker") or payload
    last_trade = ticker.get("lastTrade") or {}
    last_quote = ticker.get("lastQuote") or {}
    day = ticker.get("day") or {}
    minute = ticker.get("min") or {}
    return {
        "ticker": ticker.get("ticker"),
        "last_trade_price": last_trade.get("p"),
        "last_quote_bid": last_quote.get("p"),
        "last_quote_ask": last_quote.get("P"),
        "day_close": day.get("c"),
        "day_volume": day.get("v") or day.get("dv"),
        "minute_close": minute.get("c"),
        "today_change_pct": ticker.get("todaysChangePerc"),
        "updated": ticker.get("updated"),
    }


def compact_previous_day(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload.get("results") or []
    row = rows[0] if rows else {}
    return {
        "ticker": payload.get("ticker") or row.get("T"),
        "open": row.get("o"),
        "high": row.get("h"),
        "low": row.get("l"),
        "close": row.get("c"),
        "volume": row.get("v"),
        "vwap": row.get("vw"),
        "timestamp_ms": row.get("t"),
        "status": payload.get("status"),
    }


def compact_last_trade(payload: dict[str, Any]) -> dict[str, Any]:
    row = payload.get("results") or {}
    return {
        "ticker": row.get("T"),
        "price": row.get("p"),
        "size": row.get("s") or row.get("ds"),
        "exchange": row.get("x"),
        "conditions": row.get("c"),
        "timestamp_ns": row.get("t"),
        "status": payload.get("status"),
    }


def print_rest(symbols: list[str], include_last_trade: bool, include_movers: bool, include_snapshot: bool) -> None:
    settings = load_settings()
    client = MassiveRestClient(settings)

    print("Market status")
    print(json.dumps(client.get_market_status(), indent=2, sort_keys=True))

    print("Previous day bars")
    for symbol in symbols:
        payload = client.get_previous_day_bar(symbol)
        print(json.dumps(compact_previous_day(payload), indent=2, sort_keys=True))

    if include_last_trade:
        print("Last trades")
        for symbol in symbols:
            try:
                payload = client.get_last_trade(symbol)
                print(json.dumps(compact_last_trade(payload), indent=2, sort_keys=True))
            except MassiveApiError as exc:
                print(f"{symbol}: {exc}")

    if include_snapshot:
        print("Snapshots")
        for symbol in symbols:
            try:
                payload = client.get_single_ticker_snapshot(symbol)
                print(json.dumps(compact_snapshot(payload), indent=2, sort_keys=True))
            except MassiveApiError as exc:
                print(f"{symbol}: {exc}")

    if include_movers:
        try:
            movers = client.get_top_movers("gainers")
        except MassiveApiError as exc:
            print(f"Top gainers: {exc}")
            return
        rows = []
        for item in movers.get("tickers", [])[:5]:
            rows.append(
                {
                    "ticker": item.get("ticker"),
                    "price": (item.get("lastTrade") or {}).get("p"),
                    "change_pct": item.get("todaysChangePerc"),
                    "volume": (item.get("day") or {}).get("v"),
                }
            )
        print("Top gainers")
        print(json.dumps(rows, indent=2, sort_keys=True))


async def print_websocket(symbols: list[str], seconds: int, max_messages: int) -> None:
    import websockets
    from websockets.exceptions import ConnectionClosed

    settings = load_settings()
    aggregate_channels = [f"A.{symbol}" for symbol in symbols]
    quote_channels = [f"Q.{symbol}" for symbol in symbols]
    params = ",".join(aggregate_channels + quote_channels)

    print(f"WebSocket subscribe: {params}")
    messages_seen = 0
    async with websockets.connect(settings.websocket_url, ping_interval=20) as ws:
        await ws.send(json.dumps({"action": "auth", "params": settings.massive_api_key}))
        await ws.send(json.dumps({"action": "subscribe", "params": params}))

        deadline = asyncio.get_running_loop().time() + seconds
        while messages_seen < max_messages:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except TimeoutError:
                break
            except ConnectionClosed as exc:
                print(f"WebSocket closed: {exc}")
                break

            payload = json.loads(raw)
            messages = payload if isinstance(payload, list) else [payload]
            for msg in messages:
                print(json.dumps(msg, sort_keys=True))
                messages_seen += 1
                if msg.get("ev") == "status" and msg.get("status") == "auth_failed":
                    print("WebSocket auth failed; this Massive plan may not include streaming access.")
                    return
                if messages_seen >= max_messages:
                    break

    print(f"WebSocket messages seen: {messages_seen}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test Massive REST and WebSocket market data.")
    parser.add_argument("mode", choices=["rest", "ws", "both"], nargs="?", default="rest")
    parser.add_argument("--symbols", default="AAPL,MSFT", help="Comma-separated symbols to request.")
    parser.add_argument("--seconds", type=int, default=15, help="WebSocket listen duration.")
    parser.add_argument("--max-messages", type=int, default=10, help="Maximum WebSocket messages to print.")
    parser.add_argument("--last-trade", action="store_true", help="Also fetch last trade; this may require a higher Massive plan.")
    parser.add_argument("--top-movers", action="store_true", help="Also fetch top gainers over REST.")
    parser.add_argument("--snapshot", action="store_true", help="Also fetch snapshot data; this may require a higher Massive plan.")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    args = parse_args()
    symbols = [symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()]

    if args.mode in {"rest", "both"}:
        print_rest(symbols, args.last_trade, args.top_movers, args.snapshot)

    if args.mode in {"ws", "both"}:
        asyncio.run(print_websocket(symbols, args.seconds, args.max_messages))


if __name__ == "__main__":
    main()
