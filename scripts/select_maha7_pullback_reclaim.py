from __future__ import annotations

import argparse
import json
from pathlib import Path


DEFAULT_UNIVERSE_FILE = Path("data/opening_universe.txt")
DEFAULT_OUTPUT_FILE = Path("data/maha7_pullback_reclaim_plan.json")


def load_universe(path: Path) -> list[str]:
    symbols: list[str] = []
    if not path.exists():
        raise FileNotFoundError(f"Missing universe file: {path}. Run scripts/select_market_universe.py first.")

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        symbol = raw_line.strip().upper()
        if symbol and not symbol.startswith("#"):
            symbols.append(symbol)
    return list(dict.fromkeys(symbols))


def build_plan(symbols: list[str], top: int) -> dict:
    if top <= 0:
        raise ValueError("--top must be positive")
    selected = symbols[:top]
    if not selected:
        raise ValueError("Universe did not contain any symbols")
    return {
        "strategy": "maha7_pullback_reclaim",
        "symbols": selected,
        "settings": {
            "TRADE_COOLDOWN_SECONDS": 600,
        },
    }


def write_plan(plan: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a Maha7 pullback reclaim strategy plan from the liquid universe.")
    parser.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--top", type=int, default=12)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> dict:
    args = parse_args(argv)
    symbols = load_universe(args.universe)
    plan = build_plan(symbols, args.top)
    write_plan(plan, args.output)
    print(f"Wrote {len(plan['symbols'])} Maha7 pullback reclaim symbols to {args.output}")
    return plan


if __name__ == "__main__":
    main()
