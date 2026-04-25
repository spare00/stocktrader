from dataclasses import dataclass


@dataclass(frozen=True)
class Bar:
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    bid_size: int
    ask_size: int
    timestamp_ms: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> float:
        return ((self.ask - self.bid) / self.mid) * 10_000 if self.mid else float("inf")


@dataclass(frozen=True)
class ExitDecision:
    reason: str


@dataclass(frozen=True)
class Signal:
    strategy: str
    symbol: str
    side: str
    price: float
    timestamp_ms: int
    change_pct: float
    volume_ratio: float
    spread_bps: float | None
    reason: str
