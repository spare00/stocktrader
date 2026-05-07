from __future__ import annotations

from candle import SymbolState


class SymbolManager:
    def __init__(self, states: dict[str, SymbolState], stream, strategies):
        self.states = states
        self.stream = stream
        self.strategies = strategies

    def add_symbol(self, symbol: str) -> bool:
        normalized = symbol.strip().upper()
        if not normalized:
            return False
        if normalized in self.states:
            return False
        self.states[normalized] = SymbolState(normalized)
        if hasattr(self.stream, "add_symbol"):
            self.stream.add_symbol(normalized)
        for strategy in self.strategies:
            strategy.bootstrap_states(self.states)
        return True
