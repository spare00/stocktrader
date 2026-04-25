from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from alpaca_client import AlpacaConfigError, make_clients, to_bar, to_quote
from config import Settings
from models import Bar, Quote


class AlpacaStreamAuthError(RuntimeError):
    pass


class AlpacaStockStream:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def events(self) -> AsyncIterator[Bar | Quote]:
        queue: asyncio.Queue[Bar | Quote | BaseException | None] = asyncio.Queue()
        clients = make_clients(self.settings)

        async def on_bar(raw_bar) -> None:
            await queue.put(to_bar(raw_bar))

        async def on_quote(raw_quote) -> None:
            await queue.put(to_quote(raw_quote))

        for symbol in self.settings.symbols:
            clients.stream.subscribe_bars(on_bar, symbol)
            clients.stream.subscribe_quotes(on_quote, symbol)

        stream_task = asyncio.create_task(clients.stream._run_forever())
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
            await clients.stream.stop_ws()
            await asyncio.gather(stream_task, return_exceptions=True)
