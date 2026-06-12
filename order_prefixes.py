from __future__ import annotations

import re


CLIENT_ORDER_ID_ROOT = "bk"

STRATEGY_ORDER_PREFIXES: dict[str, str] = {
    "breakout_power": "bop",
    "ema_gap_cross": "egc",
    "gap_and_go": "gag",
    "macd_early_impulse": "mei",
    "stoch_macd_reversal": "smr",
    "maha7": "mh7",
    "recovery_scale": "rsc",
    "steady_intraday": "si",
    "spike": "spk",
    "liquidity_scalper": "lqs",
    "opening_impulse": "oi",
    "reconciled": "rec",
}

_PREFIX_RE = re.compile(r"^[a-z0-9]+$")


def validate_strategy_order_prefixes(strategy_names: list[str] | tuple[str, ...] | None = None) -> None:
    names = [name.strip().lower() for name in strategy_names] if strategy_names is not None else list(STRATEGY_ORDER_PREFIXES)
    missing = sorted(name for name in names if name and name not in STRATEGY_ORDER_PREFIXES)
    if missing:
        raise ValueError(f"Missing client_order_id strategy prefix for: {', '.join(missing)}")

    seen: dict[str, str] = {}
    for strategy, prefix in STRATEGY_ORDER_PREFIXES.items():
        normalized = str(prefix).strip().lower()
        if not normalized:
            raise ValueError(f"Empty client_order_id strategy prefix for: {strategy}")
        if not _PREFIX_RE.fullmatch(normalized):
            raise ValueError(f"Invalid client_order_id strategy prefix for {strategy}: {prefix!r}")
        other = seen.get(normalized)
        if other is not None:
            raise ValueError(
                f"Duplicate client_order_id strategy prefix {normalized!r} for {other} and {strategy}"
            )
        seen[normalized] = strategy


def strategy_order_prefix(strategy_name: str) -> str:
    validate_strategy_order_prefixes()
    key = strategy_name.strip().lower()
    try:
        return STRATEGY_ORDER_PREFIXES[key]
    except KeyError as exc:
        raise ValueError(f"Missing client_order_id strategy prefix for: {key}") from exc
