import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import Settings


class MassiveApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class MassiveRestClient:
    settings: Settings
    base_url: str = "https://api.massive.com"
    timeout_seconds: int = 15

    def get_single_ticker_snapshot(self, symbol: str) -> dict[str, Any]:
        return self._get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{symbol.upper()}")

    def get_top_movers(self, direction: str = "gainers") -> dict[str, Any]:
        if direction not in {"gainers", "losers"}:
            raise ValueError("direction must be 'gainers' or 'losers'")
        return self._get(f"/v2/snapshot/locale/us/markets/stocks/{direction}")

    def get_market_status(self) -> dict[str, Any]:
        return self._get("/v1/marketstatus/now")

    def get_previous_day_bar(self, symbol: str) -> dict[str, Any]:
        return self._get(f"/v2/aggs/ticker/{symbol.upper()}/prev")

    def get_last_trade(self, symbol: str) -> dict[str, Any]:
        return self._get(f"/v2/last/trade/{symbol.upper()}")

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = dict(params or {})
        query["apiKey"] = self.settings.massive_api_key
        url = f"{self.base_url}{path}?{urlencode(query)}"
        request = Request(url, headers={"Accept": "application/json"})

        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise MassiveApiError(f"Massive REST HTTP {exc.code}: {body[:500]}") from exc
        except OSError as exc:
            raise MassiveApiError(f"Massive REST request failed: {exc}") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MassiveApiError(f"Massive REST returned non-JSON: {raw[:500]}") from exc

        if isinstance(payload, dict) and payload.get("status") in {"ERROR", "NOT_AUTHORIZED"}:
            raise MassiveApiError(f"Massive REST error: {payload}")

        return payload
