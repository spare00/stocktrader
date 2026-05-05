"""Build data/news_impulse_plan.json — liquid names with intraday volume spike vs baseline."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from alpaca.data.timeframe import TimeFrame

from alpaca_client import get_bars_between, get_latest_quotes, make_clients
from config import Settings, load_settings
from env_vars import format_symbols_env_line
from market_hours import MARKET_TZ
from models import Bar, Quote
from opening_plan import default_plan_file_for_strategy


PREMARKET_OPEN = time(4, 0)
DEFAULT_UNIVERSE_FILE = Path("data/opening_universe.txt")
DEFAULT_PLAN_FILE = default_plan_file_for_strategy("news_impulse")

DEFAULT_UNIVERSE = [
    "AAPL",
    "AMD",
    "AMZN",
    "CRWD",
    "META",
    "MSFT",
    "NVDA",
    "PLTR",
    "QQQ",
    "SNOW",
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


def usable_quote(quote: Quote | None) -> Quote | None:
    if quote is None or quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
        return None
    return quote


@dataclass(frozen=True)
class NewsImpulseCandidate:
    symbol: str
    score: float
    volume_ratio: float
    change_pct: float
    spread_bps: float | None
    last_price: float | None
    bar_count: int


def _volume_ratio_and_change(bars: list[Bar]) -> tuple[float, float]:
    if len(bars) < 2:
        return 0.0, 0.0
    ordered = sorted(bars, key=lambda bar: bar.end_ms)
    latest = ordered[-1]
    baseline = median([bar.volume for bar in ordered[:-1] if bar.volume > 0] or [1])
    volume_ratio = latest.volume / baseline if baseline else 0.0
    first_open = ordered[0].open or ordered[0].close
    if first_open <= 0:
        return volume_ratio, 0.0
    change_pct = (latest.close - first_open) / first_open
    return volume_ratio, change_pct


def score_symbol(
    symbol: str,
    bars: list[Bar],
    quote: Quote | None,
    *,
    min_price: float,
    max_spread_bps: float,
) -> NewsImpulseCandidate | None:
    volume_ratio, change_pct = _volume_ratio_and_change(bars)
    valid = usable_quote(quote)
    spread_bps = valid.spread_bps if valid else None
    last_price = valid.ask if valid else None

    if valid is not None and last_price is not None and last_price < min_price:
        return None
    if spread_bps is not None and spread_bps > max_spread_bps:
        return None

    # Favor volume spike; mild boost for same-direction move (long-only strategy at runtime).
    score = volume_ratio * (1.0 + max(0.0, change_pct) * 5.0)
    return NewsImpulseCandidate(
        symbol=symbol,
        score=round(score, 4),
        volume_ratio=round(volume_ratio, 4),
        change_pct=round(change_pct, 6),
        spread_bps=round(spread_bps, 2) if spread_bps is not None else None,
        last_price=round(last_price, 4) if last_price is not None else None,
        bar_count=len(bars),
    )


def load_bars_and_quotes(settings: Settings, symbols: list[str]) -> tuple[dict[str, list[Bar]], dict[str, Quote]]:
    clients = make_clients(settings)
    now = datetime.now(tz=MARKET_TZ)
    start_of_day = datetime.combine(now.date(), PREMARKET_OPEN, tzinfo=MARKET_TZ)
    intraday = get_bars_between(clients, symbols, TimeFrame.Minute, start_of_day, now)
    quotes = get_latest_quotes(settings, symbols)
    return intraday, quotes


def rank_candidates(
    symbols: list[str],
    bars_by_symbol: dict[str, list[Bar]],
    quotes: dict[str, Quote],
    *,
    min_price: float,
    max_spread_bps: float,
) -> list[NewsImpulseCandidate]:
    out: list[NewsImpulseCandidate] = []
    for symbol in symbols:
        row = score_symbol(
            symbol,
            bars_by_symbol.get(symbol, []),
            quotes.get(symbol),
            min_price=min_price,
            max_spread_bps=max_spread_bps,
        )
        if row is not None:
            out.append(row)
    out.sort(key=lambda item: item.score, reverse=True)
    return out


def fallback_candidates(symbols: list[str], limit: int) -> list[NewsImpulseCandidate]:
    return [
        NewsImpulseCandidate(
            symbol=symbol,
            score=0.0,
            volume_ratio=0.0,
            change_pct=0.0,
            spread_bps=None,
            last_price=None,
            bar_count=0,
        )
        for symbol in symbols[:limit]
    ]


def build_plan(candidates: list[NewsImpulseCandidate], limit: int) -> dict[str, Any]:
    top = candidates[:limit]
    selected = [row.symbol for row in top]
    return {
        "strategy": "news_impulse",
        "selection_stage": "intraday_volume_screen",
        "symbols": selected,
        "ranked": [asdict(row) for row in top],
        "rejected": [],
        "settings": {},
        "risk_note": (
            "Universe screen for liquid symbols with elevated latest-bar volume vs session baseline; "
            "runtime entries still require high-impact news and strategy filters."
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rank symbols for news_impulse watchlist (volume spike + spread/price screen)."
    )
    parser.add_argument("--symbols", default="", help="Comma-separated symbols; overrides --universe-file when set.")
    parser.add_argument(
        "--universe-file",
        type=Path,
        default=DEFAULT_UNIVERSE_FILE,
        help="Universe file (comma/newline separated). Defaults to built-in liquid list if missing.",
    )
    parser.add_argument("--top", type=int, default=12, help="Max symbols to write into the plan.")
    parser.add_argument("--min-price", type=float, default=5.0, help="Minimum ask (when quote available).")
    parser.add_argument("--max-spread-bps", type=float, default=20.0, help="Reject wide spreads.")
    parser.add_argument(
        "--plan-output",
        type=Path,
        default=DEFAULT_PLAN_FILE,
        help="Strategy plan path consumed by main.py (default data/news_impulse_plan.json).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    symbols = load_universe(args.universe_file, args.symbols)
    settings = load_settings(strategy_names=["news_impulse"], validate=False)
    bars_by_symbol, quotes = load_bars_and_quotes(settings, symbols)
    candidates = rank_candidates(
        symbols,
        bars_by_symbol,
        quotes,
        min_price=args.min_price,
        max_spread_bps=args.max_spread_bps,
    )
    if not candidates:
        candidates = fallback_candidates(symbols, args.top)

    plan = build_plan(candidates, args.top)
    args.plan_output.parent.mkdir(parents=True, exist_ok=True)
    args.plan_output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")

    result: dict[str, Any] = {
        "strategy": "news_impulse",
        "selected_symbols": plan["symbols"],
        "symbols_env_line": format_symbols_env_line(plan["symbols"]),
        "selection_plan": plan,
        "plan_output": str(args.plan_output),
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
