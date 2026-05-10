import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from config import DEFAULT_SYMBOLS_SET, Settings


DEFAULT_OPENING_PLAN_FILE = Path("data/opening_impulse_plan.json")


PLAN_SETTING_MAP = {
    "OPENING_IMPULSE_CHANGE_PCT": "opening_impulse_change_pct",
    "OPENING_IMPULSE_VOLUME_RATIO": "opening_impulse_volume_ratio",
    "MAX_OPEN_POSITIONS": "max_open_positions",
    "MAX_POSITION_VALUE": "max_position_value",
    "TARGET_PROFIT_PCT": "target_profit_pct",
    "STOP_LOSS_PCT": "stop_loss_pct",
}

FIELD_ENV_MAP = {field_name: external_name for external_name, field_name in PLAN_SETTING_MAP.items()}

SETTING_BOUNDS = {
    "opening_impulse_change_pct": (0.003, 0.02),
    "opening_impulse_volume_ratio": (1.5, 5.0),
    "target_profit_pct": (0.003, 0.02),
    "stop_loss_pct": (0.002, 0.02),
}

# Kept local (no strategies.registry import) so scripts can call this while the
# registry is still importing strategy modules that depend on this file.
_SELECTOR_COMMAND_HINTS: dict[str, str] = {
    "opening_impulse": ".venv/bin/python scripts/select_opening_impulse.py --top 12",
    "gap_and_go": ".venv/bin/python scripts/select_gap_and_go.py --top 5",
    "maha7": ".venv/bin/python scripts/select_maha7.py --top 12",
    "steady_intraday": ".venv/bin/python scripts/select_steady_intraday.py --top 12",
    "macd_early_impulse": ".venv/bin/python scripts/select_macd_early_impulse.py --top 12",
    "stoch_macd_reversal": ".venv/bin/python scripts/select_stoch_macd_reversal.py --top 12",
}


def default_plan_file_for_strategy(strategy_name: str) -> Path:
    return Path(f"data/{strategy_name.strip().lower()}_plan.json")


def default_plan_file_for_settings(settings: Settings) -> Path:
    if settings.strategy_names:
        return default_plan_file_for_strategy(settings.strategy_names[0])
    return DEFAULT_OPENING_PLAN_FILE


def selector_command_for_strategy(strategy_name: str) -> str:
    key = strategy_name.strip().lower()
    return _SELECTOR_COMMAND_HINTS.get(key, f".venv/bin/python scripts/select_{key}.py")


def load_opening_plan(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def symbols_env_blocks_plan() -> bool:
    """True when SYMBOLS was set to an explicit non-default list (overrides plan tickers)."""
    raw = os.getenv("SYMBOLS")
    if raw is None or not raw.strip():
        return False
    tickers = frozenset(part.strip().upper() for part in raw.split(",") if part.strip())
    if not tickers:
        return False
    if tickers == DEFAULT_SYMBOLS_SET:
        return False
    return True


def parse_plan_symbols(plan: dict[str, Any]) -> list[str]:
    raw_symbols = plan.get("symbols") or plan.get("selected_symbols") or []
    symbols = []
    for raw_symbol in raw_symbols:
        if isinstance(raw_symbol, dict):
            raw_symbol = raw_symbol.get("symbol", "")
        symbol = str(raw_symbol).strip().upper()
        if symbol:
            symbols.append(symbol)
    return list(dict.fromkeys(symbols))


def bounded_float(value: Any, low: float, high: float) -> float:
    return min(max(float(value), low), high)


def bounded_int(value: Any, low: int, high: int) -> int:
    return min(max(int(value), low), high)


def plan_overrides(settings: Settings, plan: dict[str, Any]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    symbols = parse_plan_symbols(plan)
    # Plan tickers apply unless SYMBOLS names a non-default list (see symbols_env_blocks_plan).
    if symbols and not symbols_env_blocks_plan():
        overrides["symbols"] = symbols

    plan_settings = plan.get("settings") or {}
    for external_name, field_name in PLAN_SETTING_MAP.items():
        if external_name not in plan_settings:
            continue
        if os.getenv(FIELD_ENV_MAP[field_name]) is not None:
            continue
        value = plan_settings[external_name]

        if field_name == "max_open_positions":
            overrides[field_name] = bounded_int(value, 0, settings.max_open_positions)
        elif field_name == "max_position_value":
            overrides[field_name] = bounded_float(value, 0.0, settings.max_position_value)
        elif field_name == "stop_loss_pct":
            low, high = SETTING_BOUNDS[field_name]
            overrides[field_name] = min(bounded_float(value, low, high), settings.stop_loss_pct)
        elif field_name in SETTING_BOUNDS:
            low, high = SETTING_BOUNDS[field_name]
            overrides[field_name] = bounded_float(value, low, high)

    return overrides


def apply_opening_plan(settings: Settings, path: Path) -> Settings:
    plan = load_opening_plan(path)
    overrides = plan_overrides(settings, plan)
    if not overrides:
        # Safety net: plan tickers alone must still apply when SYMBOLS does not block the plan
        # (avoids returning unchanged settings if overrides dict was unexpectedly empty).
        plan_syms = parse_plan_symbols(plan)
        if plan_syms and not symbols_env_blocks_plan():
            return replace(settings, symbols=plan_syms)
        return settings
    return replace(settings, **overrides)
