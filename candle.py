from collections import deque
from dataclasses import dataclass, field

from models import Bar, Quote


@dataclass
class SymbolState:
    symbol: str
    bars: deque[Bar] = field(default_factory=lambda: deque(maxlen=600))
    quotes: deque[Quote] = field(default_factory=lambda: deque(maxlen=2400))
    quote: Quote | None = None
    last_news_ms: int | None = None
    last_news_price: float | None = None
    last_event_kind: str | None = None
    last_event_ms: int | None = None

    def add_bar(self, bar: Bar) -> None:
        self.bars.append(bar)
        self.last_event_kind = "bar"
        self.last_event_ms = bar.end_ms

    def update_quote(self, quote: Quote) -> None:
        self.quote = quote
        self.quotes.append(quote)
        self.last_event_kind = "quote"
        self.last_event_ms = quote.timestamp_ms

    def mark_news(self, timestamp_ms: int, price: float | None = None) -> None:
        self.last_news_ms = max(self.last_news_ms or 0, timestamp_ms)
        if price is not None and price > 0:
            self.last_news_price = price

    @property
    def last_price(self) -> float | None:
        if self.quote:
            return self.quote.mid
        return self.bars[-1].close if self.bars else None
