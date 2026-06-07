from collections import deque
from dataclasses import dataclass, field

from models import Bar, Quote, Trade


@dataclass
class SymbolState:
    symbol: str
    indicator_max_bars: int = 3000
    quotes: deque[Quote] = field(default_factory=lambda: deque(maxlen=2400))
    trades: deque[Trade] = field(default_factory=lambda: deque(maxlen=4800))
    quote: Quote | None = None
    trade: Trade | None = None
    last_news_ms: int | None = None
    last_news_price: float | None = None
    last_news_sentiment: int = 0
    last_news_impact: float = 0.0
    is_high_impact_news: bool = False
    last_event_kind: str | None = None
    last_event_ms: int | None = None
    bars: deque[Bar] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        cap = max(1, int(self.indicator_max_bars))
        object.__setattr__(self, "bars", deque(maxlen=cap))

    def add_bar(self, bar: Bar) -> None:
        self.bars.append(bar)
        cap = max(1, int(self.indicator_max_bars))
        while len(self.bars) > cap:
            self.bars.popleft()
        self.last_event_kind = "bar"
        self.last_event_ms = bar.end_ms

    def update_quote(self, quote: Quote) -> None:
        self.quote = quote
        self.quotes.append(quote)
        self.last_event_kind = "quote"
        self.last_event_ms = quote.timestamp_ms
        if self.last_news_ms is not None and self.last_news_price is None:
            self.last_news_price = quote.mid

    def update_trade(self, trade: Trade) -> None:
        self.trade = trade
        self.trades.append(trade)
        self.last_event_kind = "trade"
        self.last_event_ms = trade.timestamp_ms

    def mark_news(
        self,
        timestamp_ms: int,
        price: float | None = None,
        *,
        sentiment: int = 1,
        impact: float = 1.0,
    ) -> None:
        self.last_news_ms = max(self.last_news_ms or 0, timestamp_ms)
        self.last_news_sentiment = sentiment
        self.last_news_impact = impact
        self.is_high_impact_news = impact >= 0.5 and sentiment > 0
        if price is not None and price > 0:
            self.last_news_price = price

    @property
    def last_price(self) -> float | None:
        if self.quote:
            return self.quote.mid
        if self.trade:
            return self.trade.price
        return self.bars[-1].close if self.bars else None
