from collections import deque
from dataclasses import dataclass, field

from models import Bar, Quote


@dataclass
class SymbolState:
    symbol: str
    bars: deque[Bar] = field(default_factory=lambda: deque(maxlen=600))
    quote: Quote | None = None

    def add_bar(self, bar: Bar) -> None:
        self.bars.append(bar)

    def update_quote(self, quote: Quote) -> None:
        self.quote = quote

    @property
    def last_price(self) -> float | None:
        return self.bars[-1].close if self.bars else None
