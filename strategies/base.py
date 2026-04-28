from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from candle import SymbolState
from models import ExitDecision, Signal

if TYPE_CHECKING:
    from execution import Position


class Strategy(ABC):
    name: str

    @abstractmethod
    def evaluate(self, state: SymbolState) -> Signal | None:
        raise NotImplementedError

    def should_exit(self, state: SymbolState, position: "Position") -> ExitDecision | None:
        return None

    def exit_activation_delay_seconds(self, position: "Position") -> int:
        return 0
