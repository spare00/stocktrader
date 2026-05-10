from __future__ import annotations

from dataclasses import replace

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
            settings = getattr(strategy, "settings", None)
            if settings is not None and normalized not in settings.symbols:
                strategy.settings = replace(settings, symbols=[*settings.symbols, normalized])
            strategy.bootstrap_states(self.states)
        return True
