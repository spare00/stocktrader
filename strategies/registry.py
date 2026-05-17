"""Single registration point for strategies — add a new strategy class to `_STRATEGY_CLASSES` only."""

from __future__ import annotations

from pathlib import Path

from strategies.base import Strategy
from strategies.gap_and_go import GapAndGoStrategy
from strategies.macd_early_impulse import MACDEarlyImpulseStrategy
from strategies.maha7 import Maha7Strategy
from strategies.opening_impulse import OpeningImpulseStrategy
from strategies.spike import SpikeStrategy
from strategies.steady_intraday import SteadyIntradayStrategy
from strategies.stoch_macd_reversal import StochMACDReversalStrategy
from order_prefixes import validate_strategy_order_prefixes

# Registration order (used by available_strategy_names); lookup is by `name`.
_STRATEGY_CLASSES: tuple[type[Strategy], ...] = (
    GapAndGoStrategy,
    MACDEarlyImpulseStrategy,
    StochMACDReversalStrategy,
    Maha7Strategy,
    SteadyIntradayStrategy,
    SpikeStrategy,
    OpeningImpulseStrategy,
)

STRATEGY_REGISTRY: dict[str, type[Strategy]] = {cls.name: cls for cls in _STRATEGY_CLASSES}


def strategy_environment_specs() -> dict[str, tuple]:
    """EnvVar tuples keyed by strategy `name` (used by config.load_settings)."""
    return {cls.name: cls.env_specs for cls in _STRATEGY_CLASSES}


def diagnostic_loggers_for(strategy_names: list[str]) -> tuple[str, ...]:
    names: list[str] = []
    for raw in strategy_names:
        n = raw.strip().lower()
        cls = STRATEGY_REGISTRY.get(n)
        if cls and cls.diagnostic_loggers:
            names.extend(cls.diagnostic_loggers)
    return tuple(dict.fromkeys(names))


def merge_strategy_runtime_snapshots(settings: "Settings") -> dict[str, dict]:
    out: dict[str, dict] = {}
    for n in settings.strategy_names:
        cls = STRATEGY_REGISTRY.get(n.strip().lower())
        if not cls:
            continue
        section = cls.runtime_settings_section(settings)
        if section is not None:
            out[cls.name] = section
    return out


def default_plan_path_for_strategy(strategy_name: str) -> Path:
    cls = STRATEGY_REGISTRY.get(strategy_name.strip().lower())
    if cls and cls.plan_file is not None:
        return cls.plan_file
    return Path(f"data/{strategy_name.strip().lower()}_plan.json")


def selector_command_hint(strategy_name: str) -> str:
    key = strategy_name.strip().lower()
    cls = STRATEGY_REGISTRY.get(key)
    if cls and cls.selector_command:
        return cls.selector_command
    return f".venv/bin/python strategy_selectors/select_{key}.py"


def build_strategies(settings: "Settings"):
    validate_strategy_order_prefixes(settings.strategy_names)
    strategies = []
    for name in settings.strategy_names:
        try:
            strategy_cls = STRATEGY_REGISTRY[name.strip().lower()]
        except KeyError as exc:
            raise ValueError(f"Unknown strategy: {name}") from exc
        strategies.append(strategy_cls(settings))
    return strategies


def available_strategy_names() -> list[str]:
    return [cls.name for cls in _STRATEGY_CLASSES]


def strategies_requiring_plan(strategy_names: list[str]) -> list[str]:
    required: list[str] = []
    for raw in strategy_names:
        name = raw.strip().lower()
        cls = STRATEGY_REGISTRY.get(name)
        if cls and cls.requires_plan:
            required.append(name)
    return required
