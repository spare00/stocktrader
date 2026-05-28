from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from models import Bar, Quote, Trade


MARKET_TZ = ZoneInfo("America/New_York")


def parse_symbols(raw: str) -> list[str]:
    return [part.strip().upper() for part in raw.replace("\n", ",").split(",") if part.strip()]


def load_candidate_symbols(path: str | Path, limit: int) -> list[str]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    symbols = parse_symbols(file_path.read_text())
    if limit > 0:
        symbols = symbols[:limit]
    return list(dict.fromkeys(symbols))


@dataclass(frozen=True)
class DynamicExecutionSelection:
    symbol: str
    execution_strength: float
    dollar_volume: float
    dollar_volume_rank: int
    buy_volume: int
    sell_volume: int
    timestamp_ms: int


class DynamicExecutionStrengthSelector:
    """Selects liquid runtime symbols when trade aggression crosses a threshold."""

    def __init__(
        self,
        symbols: list[str],
        *,
        strength_threshold: float = 120.0,
        lookback_seconds: int = 60,
        top_dollar_volume_count: int = 30,
        min_dollar_volume: float = 500_000.0,
        cooldown_seconds: int = 600,
    ):
        self.symbols = {symbol.strip().upper() for symbol in symbols if symbol.strip()}
        self.strength_threshold = max(1.0, strength_threshold)
        self.lookback_ms = max(1, lookback_seconds) * 1000
        self.top_dollar_volume_count = max(1, top_dollar_volume_count)
        self.min_dollar_volume = max(0.0, min_dollar_volume)
        self.cooldown_ms = max(0, cooldown_seconds) * 1000
        self._quotes: dict[str, Quote] = {}
        self._last_trade_price: dict[str, float] = {}
        self._trade_windows: dict[str, deque[tuple[int, str, int]]] = {}
        self._last_strength: dict[str, float] = {}
        self._last_selected_ms: dict[str, int] = {}
        self._dollar_volume: dict[str, float] = {}
        self._volume_session_date: dict[str, str] = {}

    def record_quote(self, quote: Quote) -> None:
        symbol = quote.symbol.strip().upper()
        if symbol in self.symbols:
            self._quotes[symbol] = quote

    def record_bar(self, bar: Bar) -> None:
        symbol = bar.symbol.strip().upper()
        if symbol not in self.symbols:
            return
        session_date = datetime.fromtimestamp(bar.end_ms / 1000, tz=MARKET_TZ).date().isoformat()
        if self._volume_session_date.get(symbol) != session_date:
            self._dollar_volume[symbol] = 0.0
            self._volume_session_date[symbol] = session_date
        price = bar.vwap if bar.vwap > 0 else bar.close
        if price > 0 and bar.volume > 0:
            self._dollar_volume[symbol] = self._dollar_volume.get(symbol, 0.0) + price * bar.volume

    def record_trade(self, trade: Trade) -> DynamicExecutionSelection | None:
        symbol = trade.symbol.strip().upper()
        if symbol not in self.symbols or trade.size <= 0 or trade.price <= 0:
            return None

        side = self._classify_trade(symbol, trade)
        self._last_trade_price[symbol] = trade.price
        if side is None:
            return None

        window = self._trade_windows.setdefault(symbol, deque())
        window.append((trade.timestamp_ms, side, trade.size))
        cutoff = trade.timestamp_ms - self.lookback_ms
        while window and window[0][0] < cutoff:
            window.popleft()

        buy_volume = sum(size for _, item_side, size in window if item_side == "buy")
        sell_volume = sum(size for _, item_side, size in window if item_side == "sell")
        if sell_volume <= 0:
            return None

        strength = (buy_volume / sell_volume) * 100.0
        previous_strength = self._last_strength.get(symbol, 100.0)
        self._last_strength[symbol] = strength
        if previous_strength >= self.strength_threshold or strength < self.strength_threshold:
            return None

        dollar_volume = self._dollar_volume.get(symbol, 0.0)
        if dollar_volume < self.min_dollar_volume:
            return None

        rank = self._dollar_volume_rank(symbol)
        if rank is None or rank > self.top_dollar_volume_count:
            return None

        last_selected_ms = self._last_selected_ms.get(symbol)
        if last_selected_ms is not None and trade.timestamp_ms - last_selected_ms < self.cooldown_ms:
            return None
        self._last_selected_ms[symbol] = trade.timestamp_ms

        return DynamicExecutionSelection(
            symbol=symbol,
            execution_strength=round(strength, 2),
            dollar_volume=round(dollar_volume, 2),
            dollar_volume_rank=rank,
            buy_volume=buy_volume,
            sell_volume=sell_volume,
            timestamp_ms=trade.timestamp_ms,
        )

    def _classify_trade(self, symbol: str, trade: Trade) -> str | None:
        quote = self._quotes.get(symbol)
        if quote is not None and quote.bid > 0 and quote.ask > 0:
            if trade.price >= quote.ask:
                return "buy"
            if trade.price <= quote.bid:
                return "sell"
            return "buy" if trade.price >= quote.mid else "sell"

        previous = self._last_trade_price.get(symbol)
        if previous is None or trade.price == previous:
            return None
        return "buy" if trade.price > previous else "sell"

    def _dollar_volume_rank(self, symbol: str) -> int | None:
        ranked = sorted(
            ((item_symbol, volume) for item_symbol, volume in self._dollar_volume.items() if volume > 0),
            key=lambda item: item[1],
            reverse=True,
        )
        for index, (item_symbol, _volume) in enumerate(ranked, start=1):
            if item_symbol == symbol:
                return index
        return None
