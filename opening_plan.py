import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from config import Settings


DEFAULT_PLAN_FILES = {
    "opening_impulse": Path("data/opening_impulse_plan.json"),
    "gap_and_go": Path("data/gap_and_go_plan.json"),
    "maha7_pullback_reclaim": Path("data/maha7_pullback_reclaim_plan.json"),
}
DEFAULT_OPENING_PLAN_FILE = DEFAULT_PLAN_FILES["opening_impulse"]
SELECTOR_COMMANDS = {
    "opening_impulse": "venv/bin/python scripts/select_opening_impulse.py --top 12",
    "gap_and_go": "venv/bin/python scripts/select_gap_and_go.py --top 5",
    "maha7_pullback_reclaim": "venv/bin/python scripts/select_maha7_pullback_reclaim.py --top 12",
}


PLAN_SETTING_MAP = {
    "OPENING_IMPULSE_CHANGE_PCT": "opening_impulse_change_pct",
    "OPENING_IMPULSE_VOLUME_RATIO": "opening_impulse_volume_ratio",
    "MAX_OPEN_POSITIONS": "max_open_positions",
    "MAX_POSITION_VALUE": "max_position_value",
    "TARGET_PROFIT_PCT": "target_profit_pct",
    "STOP_LOSS_PCT": "stop_loss_pct",
}

SETTING_BOUNDS = {
    "opening_impulse_change_pct": (0.003, 0.02),
    "opening_impulse_volume_ratio": (1.5, 5.0),
    "target_profit_pct": (0.003, 0.02),
    "stop_loss_pct": (0.002, 0.02),
}


def default_plan_file_for_strategy(strategy_name: str) -> Path:
    return DEFAULT_PLAN_FILES.get(strategy_name, Path(f"data/{strategy_name}_plan.json"))


def default_plan_file_for_settings(settings: Settings) -> Path:
    if settings.strategy_names:
        return default_plan_file_for_strategy(settings.strategy_names[0])
    return DEFAULT_OPENING_PLAN_FILE


def selector_command_for_strategy(strategy_name: str) -> str:
    return SELECTOR_COMMANDS.get(strategy_name, f"venv/bin/python scripts/select_{strategy_name}.py")


def load_opening_plan(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


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
    if symbols:
        overrides["symbols"] = symbols

    plan_settings = plan.get("settings") or {}
    for external_name, field_name in PLAN_SETTING_MAP.items():
        if external_name not in plan_settings:
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
        return settings
    return replace(settings, **overrides)
