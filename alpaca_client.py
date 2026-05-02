from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from alpaca.data.enums import DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.live.stock import StockDataStream
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient

from config import Settings
from models import Bar, Quote


class AlpacaConfigError(RuntimeError):
    pass


def _feed(name: str) -> DataFeed:
    mapping = {
        "iex": DataFeed.IEX,
        "sip": DataFeed.SIP,
        "delayed_sip": DataFeed.DELAYED_SIP,
    }
    try:
        return mapping[name.lower()]
    except KeyError as exc:
        raise AlpacaConfigError(f"Unsupported ALPACA_DATA_FEED: {name}") from exc


def _to_ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def to_bar(raw_bar) -> Bar:
    return Bar(
        symbol=raw_bar.symbol,
        open=float(raw_bar.open),
        high=float(raw_bar.high),
        low=float(raw_bar.low),
        close=float(raw_bar.close),
        volume=float(raw_bar.volume),
        vwap=float(raw_bar.vwap or raw_bar.close),
        start_ms=_to_ms(raw_bar.timestamp),
        end_ms=_to_ms(raw_bar.timestamp + timedelta(seconds=1)),
    )


def to_quote(raw_quote) -> Quote:
    return Quote(
        symbol=raw_quote.symbol,
        bid=float(raw_quote.bid_price),
        ask=float(raw_quote.ask_price),
        bid_size=int(raw_quote.bid_size),
        ask_size=int(raw_quote.ask_size),
        timestamp_ms=_to_ms(raw_quote.timestamp),
    )


@dataclass
class AlpacaClients:
    trading: TradingClient
    historical: StockHistoricalDataClient
    stream: StockDataStream
    feed: DataFeed


def make_clients(settings: Settings) -> AlpacaClients:
    feed = _feed(settings.alpaca_data_feed)
    trading = TradingClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        paper=settings.alpaca_paper,
        url_override=settings.alpaca_trading_base_url or None,
    )
    historical = StockHistoricalDataClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        url_override=settings.alpaca_data_base_url or None,
    )
    stream = StockDataStream(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        feed=feed,
        url_override=settings.alpaca_stream_url,
    )
    return AlpacaClients(trading=trading, historical=historical, stream=stream, feed=feed)


def get_latest_quotes(settings: Settings, symbols: Iterable[str]) -> dict[str, Quote]:
    clients = make_clients(settings)
    request = StockLatestQuoteRequest(symbol_or_symbols=list(symbols), feed=clients.feed)
    response = clients.historical.get_stock_latest_quote(request)
    return {symbol: to_quote(quote) for symbol, quote in response.items()}


def get_bars_between(
    clients: AlpacaClients,
    symbols: Iterable[str],
    timeframe,
    start: datetime,
    end: datetime,
) -> dict[str, list[Bar]]:
    symbol_list = list(symbols)
    if end <= start:
        return {symbol: [] for symbol in symbol_list}

    request = StockBarsRequest(
        symbol_or_symbols=symbol_list,
        timeframe=timeframe,
        start=start,
        end=end,
        feed=clients.feed,
    )
    response = clients.historical.get_stock_bars(request)
    return {symbol: [to_bar(item) for item in response.data.get(symbol, [])] for symbol in symbol_list}


def get_recent_bars(settings: Settings, symbols: Iterable[str], limit: int = 5) -> dict[str, list[Bar]]:
    clients = make_clients(settings)
    results: dict[str, list[Bar]] = {}
    for symbol in symbols:
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            limit=limit,
            feed=clients.feed,
        )
        bars = clients.historical.get_stock_bars(request)
        items = bars.data.get(symbol, [])
        results[symbol] = [to_bar(item) for item in items]
    return results
