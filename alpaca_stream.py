from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path

from alpaca_client import AlpacaConfigError, make_clients, to_bar, to_quote
from config import Settings
from models import Bar, Heartbeat, Quote


class AlpacaStreamAuthError(RuntimeError):
    pass


class AlpacaStreamConnectionLimitError(RuntimeError):
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


class AlpacaStockStream:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def events(self) -> AsyncIterator[Bar | Heartbeat | Quote]:
        queue: asyncio.Queue[Bar | Heartbeat | Quote | BaseException | None] = asyncio.Queue()
        stream_lock = AlpacaStreamLock(self.settings)
        stream_lock.acquire()
        clients = make_clients(self.settings)

        async def on_bar(raw_bar) -> None:
            await queue.put(to_bar(raw_bar))

        async def on_quote(raw_quote) -> None:
            await queue.put(to_quote(raw_quote))

        async def heartbeat() -> None:
            while self.settings.heartbeat_seconds > 0:
                await asyncio.sleep(self.settings.heartbeat_seconds)
                await queue.put(Heartbeat(timestamp_ms=int(time.time() * 1000)))

        for symbol in self.settings.symbols:
            clients.stream.subscribe_bars(on_bar, symbol)
            clients.stream.subscribe_quotes(on_quote, symbol)

        stream_task = asyncio.create_task(clients.stream._run_forever())

        def on_stream_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                queue.put_nowait(exc)
            else:
                queue.put_nowait(None)

        stream_task.add_done_callback(on_stream_done)
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
            await clients.stream.stop_ws()
            await asyncio.gather(stream_task, heartbeat_task, return_exceptions=True)
            stream_lock.release()
