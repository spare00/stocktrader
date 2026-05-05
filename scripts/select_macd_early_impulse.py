"""Stub selector: writes data/macd_early_impulse_plan.json from opening_universe (no heavy filters)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from env_vars import format_symbols_env_line
from opening_plan import default_plan_file_for_strategy


DEFAULT_UNIVERSE_FILE = Path("data/opening_universe.txt")
DEFAULT_PLAN_FILE = default_plan_file_for_strategy("macd_early_impulse")
DEFAULT_UNIVERSE = [
    "AAPL",
    "AMD",
    "AMZN",
    "META",
    "MSFT",
    "NVDA",
    "QQQ",
    "TSLA",
]


def parse_symbols(raw: str) -> list[str]:
    return [part.strip().upper() for part in raw.replace("\n", ",").split(",") if part.strip()]


def load_universe(path: Path | None, raw_symbols: str) -> list[str]:
    if raw_symbols:
        symbols = parse_symbols(raw_symbols)
    elif path and path.exists():
        symbols = parse_symbols(path.read_text())
    else:
        symbols = DEFAULT_UNIVERSE
    return sorted(dict.fromkeys(symbols))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build macd_early_impulse plan from opening universe.")
    parser.add_argument(
        "--universe-file",
        type=Path,
        default=DEFAULT_UNIVERSE_FILE,
        help="Universe file (default data/opening_universe.txt when present).",
    )
    parser.add_argument("--symbols", default="", help="Comma-separated symbols; overrides universe file.")
    parser.add_argument("--top", type=int, default=12, help="Max symbols to include.")
    parser.add_argument(
        "--plan-output",
        type=Path,
        default=DEFAULT_PLAN_FILE,
        help="Output JSON path (default data/macd_early_impulse_plan.json).",
    )
    args = parser.parse_args()

    symbols = load_universe(args.universe_file, args.symbols)[: args.top]
    plan = {
        "strategy": "macd_early_impulse",
        "note": "Stub: sliced from opening_universe; add liquidity/MACD filters when needed.",
        "symbols": symbols,
        "selected_symbols": symbols,
        "symbols_env_line": format_symbols_env_line(symbols),
    }
    args.plan_output.parent.mkdir(parents=True, exist_ok=True)
    args.plan_output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"wrote": str(args.plan_output), "count": len(symbols)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
