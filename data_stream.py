import asyncio
import json
import logging
from collections.abc import AsyncIterator

import websockets

from config import Settings
from models import Bar, Quote


LOG = logging.getLogger(__name__)


class MassiveStreamAuthError(RuntimeError):
    pass


class MassiveStockStream:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def events(self) -> AsyncIterator[Bar | Quote]:
        backoff_seconds = 1
        while True:
            try:
                async with websockets.connect(self.settings.websocket_url, ping_interval=20) as ws:
                    await self._authenticate(ws)
                    await self._subscribe(ws)
                    backoff_seconds = 1

                    async for raw in ws:
                        for item in self._decode(raw):
                            yield item
            except asyncio.CancelledError:
                raise
            except MassiveStreamAuthError:
                raise
            except Exception:
                LOG.exception("Massive stream disconnected; reconnecting in %ss", backoff_seconds)
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 30)

    async def _authenticate(self, ws) -> None:
        await ws.send(json.dumps({"action": "auth", "params": self.settings.massive_api_key}))

    async def _subscribe(self, ws) -> None:
        aggregate_channels = [f"A.{symbol}" for symbol in self.settings.symbols]
        quote_channels = [f"Q.{symbol}" for symbol in self.settings.symbols]
        params = ",".join(aggregate_channels + quote_channels)
        await ws.send(json.dumps({"action": "subscribe", "params": params}))
        LOG.info("Subscribed to Massive channels: %s", params)

    def _decode(self, raw: str) -> list[Bar | Quote]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            LOG.warning("Ignoring non-JSON websocket payload: %r", raw[:120])
            return []

        messages = payload if isinstance(payload, list) else [payload]
        events: list[Bar | Quote] = []

        for msg in messages:
            event_type = msg.get("ev")
            if event_type == "status" and msg.get("status") == "auth_failed":
                raise MassiveStreamAuthError(msg.get("message", "Massive websocket authentication failed"))

            if event_type == "A":
                events.append(
                    Bar(
                        symbol=msg["sym"],
                        open=float(msg["o"]),
                        high=float(msg["h"]),
                        low=float(msg["l"]),
                        close=float(msg["c"]),
                        volume=float(msg.get("v") or msg.get("dv") or 0),
                        vwap=float(msg.get("vw") or msg["c"]),
                        start_ms=int(msg["s"]),
                        end_ms=int(msg["e"]),
                    )
                )
            elif event_type == "Q" and msg.get("bp") and msg.get("ap"):
                events.append(
                    Quote(
                        symbol=msg["sym"],
                        bid=float(msg["bp"]),
                        ask=float(msg["ap"]),
                        bid_size=int(msg.get("bs") or 0),
                        ask_size=int(msg.get("as") or 0),
                        timestamp_ms=int(msg["t"]),
                    )
                )
            elif event_type in {"status", "auth_success", "success"}:
                LOG.info("Massive status: %s", msg)

        return events
