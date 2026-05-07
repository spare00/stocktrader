from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from candle import SymbolState
from env_vars import EnvSpec
from models import ExitDecision, Signal

if TYPE_CHECKING:
    from execution import Position


class Strategy(ABC):
    """Concrete strategies set `name`, optional `env_specs`, and plugin hooks."""

    name: str
    env_specs: ClassVar[tuple[EnvSpec, ...]] = ()
    diagnostic_loggers: ClassVar[tuple[str, ...]] = ()
    plan_file: ClassVar[Path | None] = None
    selector_command: ClassVar[str | None] = None
    requires_plan: ClassVar[bool] = True

    @classmethod
    def runtime_settings_section(cls, settings: Any) -> dict[str, Any] | None:
        """Return log snapshot dict when this strategy is active; default is no extra section."""
        return None

    @abstractmethod
    def evaluate(self, state: SymbolState) -> Signal | None:
        raise NotImplementedError

    def bootstrap_states(self, states: dict[str, SymbolState]) -> None:
        return None

    def should_exit(self, state: SymbolState, position: "Position") -> ExitDecision | None:
        return None

    def exit_activation_delay_seconds(self, position: "Position") -> int:
        return 0

    def use_fixed_target_exit(self, position: "Position") -> bool:
        return True
