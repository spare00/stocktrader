from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from models import NewsEvent as RawNewsEvent

POSITIVE_TERMS: tuple[str, ...] = (
    "beats",
    "beats estimate",
    "beats estimates",
    "beat",
    "beat estimate",
    "beat estimates",
    "raises guidance",
    "raises price target",
    "raised price target",
    "price target raised",
    "maintains outperform",
    "maintains overweight",
    "reiterates outperform",
    "reiterates buy",
    "outperform",
    "overweight",
    "affirms guidance",
    "affirms fy",
    "upgrades",
    "upgrade",
    "surge",
    "jumps",
    "soars",
    "acquisition",
    "contract win",
    "approval",
    "launches",
    "strong earnings",
    "record revenue",
)
NEGATIVE_TERMS: tuple[str, ...] = (
    "misses",
    "miss",
    "cuts guidance",
    "lowers price target",
    "lowered price target",
    "price target lowered",
    "underperform",
    "underweight",
    "downgrade",
    "downgrades",
    "offering",
    "dilution",
    "investigation",
    "lawsuit",
    "recall",
    "bankruptcy",
    "plunges",
    "falls",
    "weak earnings",
    "contract terminated",
    "terminated",
)
HIGH_IMPACT_TERMS: tuple[str, ...] = (
    "earnings",
    "guidance",
    "acquisition",
    "merger",
    "approval",
    "contract",
    "investigation",
    "lawsuit",
    "bankruptcy",
)


@dataclass(frozen=True)
class NewsEvent:
    symbol: str
    timestamp_ms: int
    sentiment: int
    impact: float
    headline: str = ""
    summary: str = ""


class NewsListener:
    """Classifies raw feed events into symbol-scoped news signals."""

    def __init__(
        self,
        *,
        symbol_cooldown_seconds: int = 120,
        min_impact: float = 0.5,
        positive_only: bool = True,
    ):
        self.symbol_cooldown_ms = max(0, symbol_cooldown_seconds) * 1000
        self.min_impact = max(0.0, min_impact)
        self.positive_only = positive_only
        self._last_symbol_news_ms: dict[str, int] = {}

    def process(self, event: RawNewsEvent) -> list[NewsEvent]:
        sentiment = self._sentiment(event.headline, event.summary)
        impact = self._impact(event.headline, event.summary)
        events: list[NewsEvent] = []
        for symbol in self._extract_symbols(event.symbols):
            if not self._passes_filters(symbol, event.timestamp_ms, sentiment, impact):
                continue
            self._last_symbol_news_ms[symbol] = event.timestamp_ms
            events.append(
                NewsEvent(
                    symbol=symbol,
                    timestamp_ms=event.timestamp_ms,
                    sentiment=sentiment,
                    impact=impact,
                    headline=event.headline,
                    summary=event.summary,
                )
            )
        return events

    @staticmethod
    def _extract_symbols(symbols: tuple[str, ...]) -> list[str]:
        return [symbol.strip().upper() for symbol in symbols if symbol and symbol.strip()]

    def _passes_filters(self, symbol: str, timestamp_ms: int, sentiment: int, impact: float) -> bool:
        if self.positive_only and sentiment <= 0:
            return False
        if impact < self.min_impact:
            return False
        if self.symbol_cooldown_ms <= 0:
            return True
        last_ms = self._last_symbol_news_ms.get(symbol)
        if last_ms is None:
            return True
        return (timestamp_ms - last_ms) >= self.symbol_cooldown_ms

    @staticmethod
    def _sentiment(headline: str, summary: str) -> int:
        text = f"{headline} {summary}".lower()
        if any(term in text for term in NEGATIVE_TERMS):
            return -1
        if any(term in text for term in POSITIVE_TERMS):
            return 1
        return 0

    @staticmethod
    def _impact(headline: str, summary: str) -> float:
        text = f"{headline} {summary}".lower()
        if not text.strip():
            return 0.0
        score = 0.2
        if any(term in text for term in HIGH_IMPACT_TERMS):
            score += 0.5
        if any(term in text for term in POSITIVE_TERMS + NEGATIVE_TERMS):
            score += 0.3
        return min(score, 1.0)

    async def mock_events(self, events: Iterable[NewsEvent]):
        for event in events:
            yield event
