"""Environment variable readers shared by config and strategy plugins (no strategy imports)."""

import os
from collections.abc import Sequence
from typing import Any, Callable

EnvReader = Callable[[str, Any], Any]
EnvSpec = tuple[str, str, EnvReader, Any]


def format_symbols_env_line(symbols: Sequence[str]) -> str:
    """One line for `.env` / profiles — not a shell `export` (avoids leaking into tmux/subshells)."""
    return "SYMBOLS=" + ",".join(s.strip().upper() for s in symbols if str(s).strip())


def csv_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [part.strip().upper() for part in raw.split(",") if part.strip()]


def float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value is None else float(value)


def int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None else int(value)


def optional_int_env(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    return default if value is None or value == "" else int(value)


def bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def strategy_names_csv(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def str_env(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)


def lower_env(name: str, default: str) -> str:
    return os.getenv(name, default).lower()


def read_env(specs: tuple[EnvSpec, ...]) -> dict[str, Any]:
    return {field_name: reader(env_name, default) for field_name, env_name, reader, default in specs}
