from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from candle import SymbolState
from env_vars import EnvSpec
from models import ExitDecision, Signal

if TYPE_CHECKING:
    from execution import Fill, Position


class Strategy(ABC):
    """Concrete strategies set `name`, optional `env_specs`, and plugin hooks."""

    name: str
    env_specs: ClassVar[tuple[EnvSpec, ...]] = ()
    diagnostic_loggers: ClassVar[tuple[str, ...]] = ()
    plan_file: ClassVar[Path | None] = None
    selector_command: ClassVar[str | None] = None
    requires_plan: ClassVar[bool] = True
    requires_trade_ticks: ClassVar[bool] = False
    _symbol_manager: Any = None
    _market_regime: Any = None

    @classmethod
    def runtime_settings_section(cls, settings: Any) -> dict[str, Any] | None:
        """Return log snapshot dict when this strategy is active; default is no extra section."""
        return None

    @abstractmethod
    def evaluate(self, state: SymbolState) -> Signal | None:
        raise NotImplementedError

    @property
    def allowed_symbols(self) -> set[str]:
        manager = getattr(self, "_symbol_manager", None)
        if manager is not None:
            return manager.effective_symbols(self.name)
        settings = getattr(self, "settings", None)
        return {str(symbol).strip().upper() for symbol in getattr(settings, "symbols", []) if str(symbol).strip()}

    def set_symbol_manager(self, manager: Any) -> None:
        self._symbol_manager = manager

    def set_market_regime(self, regime: Any) -> None:
        self._market_regime = regime

    def is_symbol_allowed(self, symbol: str) -> bool:
        normalized = symbol.strip().upper()
        manager = getattr(self, "_symbol_manager", None)
        if manager is not None:
            return normalized in manager.effective_symbols(self.name)
        allowed = self.allowed_symbols
        return not allowed or normalized in allowed

    def bootstrap_states(self, states: dict[str, SymbolState]) -> None:
        return None

    def on_entry_fill(self, fill: "Fill") -> None:
        return None

    def should_exit(self, state: SymbolState, position: "Position") -> ExitDecision | None:
        return None

    def exit_activation_delay_seconds(self, position: "Position") -> int:
        return 0

    def delay_stop_loss_until_exit_activation(self, position: "Position") -> bool:
        return False

    def use_fixed_target_exit(self, position: "Position") -> bool:
        return True

    def allow_max_hold_exit(self, state: SymbolState, position: "Position", age_seconds: float, pnl_pct: float) -> bool:
        return True
