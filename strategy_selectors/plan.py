"""Shared strategy plan JSON shape for selector scripts and main.py."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


def normalize_ranked_rows(ranked: list[Any]) -> list[dict[str, Any]]:
    """Convert ranked candidate rows to JSON-serializable dicts."""
    normalized: list[dict[str, Any]] = []
    for row in ranked:
        if isinstance(row, dict):
            normalized.append(dict(row))
        elif is_dataclass(row):
            normalized.append(asdict(row))
        else:
            raise TypeError(f"ranked rows must be dicts or dataclasses, got {type(row)!r}")
    return normalized


def normalize_symbol_list(symbols: list[Any]) -> list[str]:
    out: list[str] = []
    for raw_symbol in symbols:
        if isinstance(raw_symbol, dict):
            raw_symbol = raw_symbol.get("symbol", "")
        symbol = str(raw_symbol).strip().upper()
        if symbol:
            out.append(symbol)
    return list(dict.fromkeys(out))


def build_strategy_plan(
    *,
    strategy: str,
    symbols: list[Any],
    ranked: list[Any],
    selection_stage: str,
    settings: dict[str, Any] | None = None,
    risk_note: str = "",
    rejected: list[Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a plan dict that main.py can load via parse_plan_symbols()."""
    plan: dict[str, Any] = {
        "strategy": strategy.strip().lower(),
        "selection_stage": selection_stage,
        "symbols": normalize_symbol_list(symbols),
        "ranked": normalize_ranked_rows(ranked),
        "rejected": normalize_ranked_rows(rejected or []),
        "settings": dict(settings or {}),
        "risk_note": risk_note,
    }
    if extra:
        plan.update(extra)
    return plan


def write_strategy_plan(path: Path, plan: dict[str, Any]) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
