from __future__ import annotations

import asyncio
import fcntl
import hashlib
import logging
import os
import tempfile
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from requests.exceptions import RequestException

from alpaca_client import AlpacaConfigError, get_bars_between, make_clients, to_bar, to_quote
from config import Settings
from models import Bar, Heartbeat, NewsEvent, Quote

logger = logging.getLogger(__name__)


class AlpacaStreamAuthError(RuntimeError):
    pass


class AlpacaStreamConnectionLimitError(RuntimeError):
    pass


class AlpacaStreamEndedError(RuntimeError):
    pass


class AlpacaStreamLock:
    def __init__(self, settings: Settings):
        key = f"{settings.alpaca_api_key or 'missing'}:{settings.alpaca_data_feed}:{settings.alpaca_paper}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        self.path = Path(tempfile.gettempdir()) / f"stocktrader-alpaca-stream-{digest}.lock"
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise AlpacaStreamConnectionLimitError(
                "another local stocktrader process is already using this Alpaca API key/feed stream"
            ) from exc
        self._handle.write(f"pid={os.getpid()}\n")
        self._handle.flush()

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


class AlpacaRestPollingStream:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._last_bar_start_ms: dict[str, int] = {}
        self._symbols: list[str] = list(settings.symbols)

    def add_symbol(self, symbol: str) -> None:
        normalized = symbol.strip().upper()
        if not normalized:
            return
        if normalized in self._symbols:
            return
        self._symbols.append(normalized)

    def remove_symbol(self, symbol: str) -> None:
        normalized = symbol.strip().upper()
        self._symbols = [existing for existing in self._symbols if existing != normalized]

    async def events(self) -> AsyncIterator[Bar | Heartbeat | Quote]:
        clients = make_clients(self.settings)
        consecutive_poll_errors = 0
        while True:
            now = datetime.now(tz=timezone.utc)
            symbols = list(self._symbols)
            if not symbols:
                yield Heartbeat(timestamp_ms=int(now.timestamp() * 1000))
                await asyncio.sleep(self.settings.alpaca_market_data_poll_seconds)
                continue
            quote_request = StockLatestQuoteRequest(symbol_or_symbols=symbols, feed=clients.feed)
            try:
                quote_response = await asyncio.to_thread(clients.historical.get_stock_latest_quote, quote_request)
            except (OSError, RequestException) as exc:
                consecutive_poll_errors += 1
                delay_seconds = self._retry_delay_seconds(consecutive_poll_errors)
                logger.warning(
                    "Alpaca REST quote poll failed (%s: %s); retrying in %.1fs",
                    type(exc).__name__,
                    exc,
                    delay_seconds,
                )
                clients = make_clients(self.settings)
                yield Heartbeat(timestamp_ms=int(now.timestamp() * 1000))
                await asyncio.sleep(delay_seconds)
                continue
            for quote in quote_response.values():
                yield to_quote(quote)

            start = now - timedelta(minutes=3)
            try:
                bars_by_symbol = await asyncio.to_thread(get_bars_between, clients, symbols, TimeFrame.Minute, start, now)
            except (OSError, RequestException) as exc:
                consecutive_poll_errors += 1
                delay_seconds = self._retry_delay_seconds(consecutive_poll_errors)
                logger.warning(
                    "Alpaca REST bar poll failed (%s: %s); retrying in %.1fs",
                    type(exc).__name__,
                    exc,
                    delay_seconds,
                )
                clients = make_clients(self.settings)
                yield Heartbeat(timestamp_ms=int(now.timestamp() * 1000))
                await asyncio.sleep(delay_seconds)
                continue
            consecutive_poll_errors = 0
            for symbol in symbols:
                last_seen_start_ms = self._last_bar_start_ms.get(symbol, 0)
                new_bars = [bar for bar in bars_by_symbol.get(symbol, []) if bar.start_ms > last_seen_start_ms]
                for bar in new_bars:
                    self._last_bar_start_ms[symbol] = max(self._last_bar_start_ms.get(symbol, 0), bar.start_ms)
                    yield bar

            yield Heartbeat(timestamp_ms=int(now.timestamp() * 1000))
            await asyncio.sleep(self.settings.alpaca_market_data_poll_seconds)

    def _retry_delay_seconds(self, consecutive_errors: int) -> float:
        base_delay = max(1.0, self.settings.alpaca_market_data_poll_seconds)
        return min(60.0, base_delay * min(2 ** max(0, consecutive_errors - 1), 12))


class AlpacaStockStream:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._symbols: set[str] = {symbol.strip().upper() for symbol in settings.symbols if symbol.strip()}
        self._clients = None
        self._on_bar = None
        self._on_quote = None
        self._on_news = None

    def add_symbol(self, symbol: str) -> None:
        normalized = symbol.strip().upper()
        if not normalized or normalized in self._symbols:
            return
        self._symbols.add(normalized)
        if self._clients is None or self._on_bar is None or self._on_quote is None:
            return
        self._clients.stream.subscribe_bars(self._on_bar, normalized)
        self._clients.stream.subscribe_quotes(self._on_quote, normalized)

    def remove_symbol(self, symbol: str) -> None:
        normalized = symbol.strip().upper()
        if not normalized or normalized not in self._symbols:
            return
        self._symbols.remove(normalized)
        if self._clients is None:
            return
        stream = self._clients.stream
        if hasattr(stream, "unsubscribe_bars"):
            stream.unsubscribe_bars(normalized)
        if hasattr(stream, "unsubscribe_quotes"):
            stream.unsubscribe_quotes(normalized)

    async def events(self) -> AsyncIterator[Bar | Heartbeat | Quote | NewsEvent]:
        queue: asyncio.Queue[Bar | Heartbeat | Quote | NewsEvent | BaseException | None] = asyncio.Queue()
        stream_lock = AlpacaStreamLock(self.settings)
        stream_lock.acquire()
        clients = make_clients(self.settings)
        self._clients = clients

        async def on_bar(raw_bar) -> None:
            await queue.put(to_bar(raw_bar))

        async def on_quote(raw_quote) -> None:
            await queue.put(to_quote(raw_quote))

        async def on_news(raw_news) -> None:
            symbols_raw = getattr(raw_news, "symbols", None)
            if isinstance(symbols_raw, str):
                symbols = tuple(part.strip().upper() for part in symbols_raw.replace(",", " ").split() if part.strip())
            elif isinstance(symbols_raw, (list, tuple, set)):
                symbols = tuple(str(part).strip().upper() for part in symbols_raw if str(part).strip())
            else:
                symbol = str(getattr(raw_news, "symbol", "")).strip().upper()
                symbols = (symbol,) if symbol else ()

            timestamp = (
                getattr(raw_news, "updated_at", None)
                or getattr(raw_news, "created_at", None)
                or getattr(raw_news, "timestamp", None)
            )
            timestamp_ms = int(time.time() * 1000)
            if timestamp is not None:
                try:
                    timestamp_ms = int(timestamp.timestamp() * 1000)
                except Exception:
                    timestamp_ms = int(time.time() * 1000)

            await queue.put(
                NewsEvent(
                    symbols=symbols,
                    timestamp_ms=timestamp_ms,
                    headline=str(getattr(raw_news, "headline", "") or ""),
                    summary=str(getattr(raw_news, "summary", "") or ""),
                    source=str(getattr(raw_news, "source", "") or ""),
                    url=str(getattr(raw_news, "url", "") or ""),
                )
            )

        self._on_bar = on_bar
        self._on_quote = on_quote
        self._on_news = on_news

        async def heartbeat() -> None:
            while self.settings.heartbeat_seconds > 0:
                await asyncio.sleep(self.settings.heartbeat_seconds)
                await queue.put(Heartbeat(timestamp_ms=int(time.time() * 1000)))

        for symbol in sorted(self._symbols):
            clients.stream.subscribe_bars(on_bar, symbol)
            clients.stream.subscribe_quotes(on_quote, symbol)
        clients.news_stream.subscribe_news(on_news, "*")

        stock_stream_task = asyncio.create_task(clients.stream._run_forever())
        stock_stream_task.set_name("alpaca_stock_stream")
        news_stream_task = asyncio.create_task(clients.news_stream._run_forever())
        news_stream_task.set_name("alpaca_news_stream")

        def on_stream_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                queue.put_nowait(exc)
            else:
                queue.put_nowait(AlpacaStreamEndedError(f"{task.get_name()} ended unexpectedly"))

        stock_stream_task.add_done_callback(on_stream_done)
        news_stream_task.add_done_callback(on_stream_done)
        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        except Exception as exc:
            if isinstance(exc, AlpacaConfigError):
                raise
            message = str(exc).lower()
            if "auth" in message or "forbidden" in message or "unauthorized" in message:
                raise AlpacaStreamAuthError(str(exc)) from exc
            raise
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(
                self._stop_ws(clients.stream, "stock"),
                self._stop_ws(clients.news_stream, "news"),
                return_exceptions=True,
            )
            for task in (stock_stream_task, news_stream_task):
                task.cancel()
            await asyncio.wait(
                (stock_stream_task, news_stream_task, heartbeat_task),
                timeout=5,
            )
            self._clients = None
            self._on_bar = None
            self._on_quote = None
            self._on_news = None
            stream_lock.release()

    async def _stop_ws(self, stream, name: str) -> None:
        try:
            await asyncio.wait_for(stream.stop_ws(), timeout=5)
        except asyncio.TimeoutError:
            raise AlpacaStreamEndedError(f"timed out stopping {name} Alpaca websocket")


def build_market_data_stream(settings: Settings) -> AlpacaStockStream | AlpacaRestPollingStream:
    if settings.alpaca_market_data_mode == "rest":
        return AlpacaRestPollingStream(settings)
    return AlpacaStockStream(settings)
