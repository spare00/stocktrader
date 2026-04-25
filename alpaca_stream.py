from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from alpaca_client import AlpacaConfigError, make_clients, to_bar, to_quote
from config import Settings
from models import Bar, Heartbeat, Quote


class AlpacaStreamAuthError(RuntimeError):
    pass


class AlpacaStockStream:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def events(self) -> AsyncIterator[Bar | Heartbeat | Quote]:
        queue: asyncio.Queue[Bar | Heartbeat | Quote | BaseException | None] = asyncio.Queue()
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
