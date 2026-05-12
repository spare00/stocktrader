from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable

from candle import SymbolState


LOG = logging.getLogger(__name__)


def _normalize_symbols(symbols: Iterable[str]) -> set[str]:
    return {str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}


class SymbolManager:
    """Owns logical symbol universes while keeping one shared physical stream."""

    def __init__(
        self,
        states: dict[str, SymbolState],
        stream=None,
        strategies=None,
        *,
        global_symbols: Iterable[str] = (),
        settings=None,
    ):
        self.states = states
        self.stream = stream
        self.strategies = list(strategies or [])
        self.settings = settings
        self.global_symbols: set[str] = set()
        self.strategy_symbols: dict[str, set[str]] = {}
        self.symbol_refcount: Counter[str] = Counter()

        for strategy in self.strategies:
            strategy.set_symbol_manager(self)
        if global_symbols:
            self.add_global_symbols(global_symbols)

    def register_strategy_symbols(self, strategy: str, symbols: Iterable[str]) -> set[str]:
        name = strategy.strip().lower()
        new_symbols = _normalize_symbols(symbols)
        old_symbols = self.strategy_symbols.get(name, set())
        added = new_symbols - old_symbols
        removed = old_symbols - new_symbols
        self.strategy_symbols[name] = new_symbols
        for symbol in added:
            self._retain_symbol(symbol)
        for symbol in removed:
            self._release_symbol(symbol)
        return set(new_symbols)

    def add_global_symbols(self, symbols: Iterable[str]) -> set[str]:
        added_symbols: set[str] = set()
        for symbol in _normalize_symbols(symbols):
            if symbol in self.global_symbols:
                continue
            self.global_symbols.add(symbol)
            self._retain_symbol(symbol)
            added_symbols.add(symbol)
        return added_symbols

    def remove_global_symbols(self, symbols: Iterable[str]) -> set[str]:
        removed_symbols: set[str] = set()
        for symbol in _normalize_symbols(symbols):
            if symbol not in self.global_symbols:
                continue
            self.global_symbols.remove(symbol)
            self._release_symbol(symbol)
            removed_symbols.add(symbol)
        return removed_symbols

    def effective_symbols(self, strategy: str) -> set[str]:
        name = strategy.strip().lower()
        return set(self.global_symbols) | set(self.strategy_symbols.get(name, set()))

    def all_symbols(self) -> set[str]:
        symbols = set(self.global_symbols)
        for local_symbols in self.strategy_symbols.values():
            symbols.update(local_symbols)
        return symbols

    def symbol_refcount_for(self, symbol: str) -> int:
        return int(self.symbol_refcount.get(symbol.strip().upper(), 0))

    def add_symbol(self, symbol: str) -> bool:
        return bool(self.add_global_symbols([symbol]))

    def _retain_symbol(self, symbol: str) -> None:
        previous = self.symbol_refcount.get(symbol, 0)
        self.symbol_refcount[symbol] = previous + 1
        if symbol not in self.states:
            max_bars = 3000
            if self.settings is not None:
                max_bars = int(self.settings.indicator_max_bars_per_symbol)
            self.states[symbol] = SymbolState(symbol, indicator_max_bars=max_bars)
        if previous == 0:
            self.subscribe_symbol(symbol)
            self._bootstrap_symbol(symbol)

    def _release_symbol(self, symbol: str) -> None:
        previous = self.symbol_refcount.get(symbol, 0)
        if previous <= 0:
            return
        if previous == 1:
            self.symbol_refcount.pop(symbol, None)
            self.unsubscribe_symbol(symbol)
            return
        self.symbol_refcount[symbol] = previous - 1

    def subscribe_symbol(self, symbol: str) -> None:
        if self.stream is not None and hasattr(self.stream, "add_symbol"):
            self.stream.add_symbol(symbol)

    def unsubscribe_symbol(self, symbol: str) -> None:
        if self.stream is not None and hasattr(self.stream, "remove_symbol"):
            self.stream.remove_symbol(symbol)

    def _bootstrap_symbol(self, symbol: str) -> None:
        if symbol not in self.states:
            return
        single_state = {symbol: self.states[symbol]}
        for strategy in self.strategies:
            try:
                strategy.bootstrap_states(single_state)
            except Exception:
                LOG.exception("Strategy %s failed to bootstrap %s", getattr(strategy, "name", "?"), symbol)
