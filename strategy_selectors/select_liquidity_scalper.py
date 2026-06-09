"""Pre-session REST screener for liquidity_scalper stream symbols."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Settings, load_settings
from env_vars import format_symbols_env_line
from models import Bar, Quote
from opening_plan import default_plan_file_for_strategy


MARKET_TZ = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
DEFAULT_UNIVERSE_FILE = Path("data/opening_universe.txt")
DEFAULT_PLAN_FILE = default_plan_file_for_strategy("liquidity_scalper")
# Basic IEX allows 30 trade+quote channels (~15 symbols with liquidity_scalper).
DEFAULT_STREAM_SYMBOL_LIMIT = 15
DEFAULT_TOP = 12
DEFAULT_DAYS = 3
MIN_SESSION_BARS = 30


@dataclass(frozen=True)
class LiquidityScalperCandidate:
    symbol: str
    score: float
    price: float
    spread_bps: float | None
    session_days: int
    median_session_dollar_volume: float
    median_range_pct: float
    median_bar_dollar_volume: float
    p75_bar_dollar_volume: float
    median_minute_dollar_volume: float
    quote_size: int
    quality_flags: tuple[str, ...] = ()
    hard_reject: bool = False


def parse_symbols(raw: str) -> list[str]:
    return [part.strip().upper() for part in raw.replace("\n", ",").split(",") if part.strip()]


def load_universe(path: Path | None, raw_symbols: str) -> list[str]:
    if raw_symbols:
        symbols = parse_symbols(raw_symbols)
    elif path and path.exists():
        symbols = parse_symbols(path.read_text())
    else:
        raise FileNotFoundError(
            f"Missing universe file: {path}. Run strategy_selectors/select_market_universe.py first."
        )
    return sorted(dict.fromkeys(symbols))


def parse_as_of(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(tz=MARKET_TZ)
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        value = value.replace(tzinfo=MARKET_TZ)
    return value.astimezone(MARKET_TZ)


def previous_session_dates(as_of: datetime, count: int) -> list[date]:
    sessions: list[date] = []
    current = as_of.astimezone(MARKET_TZ).date() - timedelta(days=1)
    while len(sessions) < count:
        if current.weekday() < 5:
            sessions.append(current)
        current -= timedelta(days=1)
    return sorted(sessions)


def bar_session_date(bar: Bar) -> date:
    return datetime.fromtimestamp(bar.start_ms / 1000, tz=timezone.utc).astimezone(MARKET_TZ).date()


def in_regular_session(bar: Bar) -> bool:
    timestamp = datetime.fromtimestamp(bar.start_ms / 1000, tz=timezone.utc).astimezone(MARKET_TZ)
    if timestamp.weekday() >= 5:
        return False
    open_at = datetime.combine(timestamp.date(), MARKET_OPEN, tzinfo=MARKET_TZ)
    close_at = datetime.combine(timestamp.date(), MARKET_CLOSE, tzinfo=MARKET_TZ)
    return open_at <= timestamp < close_at


def usable_quote(quote: Quote | None) -> Quote | None:
    if quote is None:
        return None
    if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
        return None
    return quote


def session_metrics(bars: list[Bar]) -> dict[str, float | int] | None:
    ordered = sorted(bars, key=lambda item: item.start_ms)
    if len(ordered) < MIN_SESSION_BARS:
        return None
    low = min(bar.low for bar in ordered)
    high = max(bar.high for bar in ordered)
    if low <= 0:
        return None
    bar_dollar_volumes = [bar.close * bar.volume for bar in ordered if bar.close > 0 and bar.volume > 0]
    if not bar_dollar_volumes:
        return None
    session_dollar_volume = sum(bar_dollar_volumes)
    sorted_bar_dvs = sorted(bar_dollar_volumes)
    p75_index = max(0, min(len(sorted_bar_dvs) - 1, int(len(sorted_bar_dvs) * 0.75) - 1))
    return {
        "session_dollar_volume": session_dollar_volume,
        "range_pct": (high - low) / low,
        "median_bar_dollar_volume": median(sorted_bar_dvs),
        "p75_bar_dollar_volume": sorted_bar_dvs[p75_index],
        "median_minute_dollar_volume": median(sorted_bar_dvs),
        "bar_count": len(ordered),
    }


def regular_session_metrics_by_day(bars: list[Bar], session_dates: set[date]) -> list[dict[str, float | int]]:
    grouped: dict[date, list[Bar]] = defaultdict(list)
    for bar in bars:
        if not in_regular_session(bar):
            continue
        session_date = bar_session_date(bar)
        if session_date in session_dates:
            grouped[session_date].append(bar)

    metrics = []
    for session_date in sorted(grouped):
        session = session_metrics(grouped[session_date])
        if session is not None:
            metrics.append(session)
    return metrics


def score_liquidity_scalper_candidate(
    symbol: str,
    session_rows: list[dict[str, float | int]],
    quote: Quote | None,
    settings: Settings,
    *,
    min_price: float,
    max_price: float,
    min_session_days: int,
) -> LiquidityScalperCandidate | None:
    valid_quote = usable_quote(quote)
    quality_flags: list[str] = []
    hard_reject = False

    if valid_quote is None:
        return None

    price = valid_quote.mid
    spread_bps = valid_quote.spread_bps
    quote_size = int(valid_quote.bid_size + valid_quote.ask_size)

    if price < min_price:
        quality_flags.append(f"price {price:.2f} < {min_price:.2f}")
        hard_reject = True
    if price > max_price:
        quality_flags.append(f"price {price:.2f} > {max_price:.2f}")
        hard_reject = True
    if spread_bps > settings.liquidity_scalper_max_spread_bps:
        quality_flags.append(
            f"spread {spread_bps:.2f}bps > {settings.liquidity_scalper_max_spread_bps:.2f}bps"
        )
        hard_reject = True

    if len(session_rows) < min_session_days:
        quality_flags.append(f"session history {len(session_rows)} < {min_session_days}")
        hard_reject = True

    median_session_dv = median(row["session_dollar_volume"] for row in session_rows) if session_rows else 0.0
    median_range_pct = median(row["range_pct"] for row in session_rows) if session_rows else 0.0
    median_bar_dv = median(row["median_bar_dollar_volume"] for row in session_rows) if session_rows else 0.0
    p75_bar_dv = median(row["p75_bar_dollar_volume"] for row in session_rows) if session_rows else 0.0
    median_minute_dv = median(row["median_minute_dollar_volume"] for row in session_rows) if session_rows else 0.0

    if median_session_dv < settings.liquidity_scalper_min_session_dollar_volume:
        quality_flags.append(
            f"median session_dv ${median_session_dv:,.0f} < "
            f"${settings.liquidity_scalper_min_session_dollar_volume:,.0f}"
        )
        hard_reject = True
    if median_range_pct < settings.liquidity_scalper_min_range_pct:
        quality_flags.append(
            f"median range {median_range_pct:.2%} < {settings.liquidity_scalper_min_range_pct:.2%}"
        )
        hard_reject = True
    if median_bar_dv < settings.liquidity_scalper_min_bar_dollar_volume:
        quality_flags.append(
            f"median bar_dv ${median_bar_dv:,.0f} < ${settings.liquidity_scalper_min_bar_dollar_volume:,.0f}"
        )
        hard_reject = True

    liquidity_score = math.log10(max(median_session_dv, 1.0) / 1_000_000.0) * 4.0
    range_score = median_range_pct * 200.0
    tape_proxy = math.log10(max(p75_bar_dv, 1.0) / 1_000.0) * 2.5
    spread_penalty = spread_bps / max(settings.liquidity_scalper_max_spread_bps, 0.1)
    quote_depth_bonus = min(quote_size / 200.0, 2.0)
    reject_penalty = 8.0 if hard_reject else 0.0
    score = liquidity_score + range_score + tape_proxy + quote_depth_bonus - spread_penalty - reject_penalty

    return LiquidityScalperCandidate(
        symbol=symbol,
        score=round(score, 3),
        price=round(price, 2),
        spread_bps=round(spread_bps, 2),
        session_days=len(session_rows),
        median_session_dollar_volume=round(median_session_dv, 2),
        median_range_pct=round(median_range_pct, 5),
        median_bar_dollar_volume=round(median_bar_dv, 2),
        p75_bar_dollar_volume=round(p75_bar_dv, 2),
        median_minute_dollar_volume=round(median_minute_dv, 2),
        quote_size=quote_size,
        quality_flags=tuple(quality_flags),
        hard_reject=hard_reject,
    )


def candidate_to_dict(candidate: LiquidityScalperCandidate) -> dict[str, Any]:
    return {
        "symbol": candidate.symbol,
        "score": candidate.score,
        "price": candidate.price,
        "spread_bps": candidate.spread_bps,
        "session_days": candidate.session_days,
        "median_session_dollar_volume": candidate.median_session_dollar_volume,
        "median_range_pct": candidate.median_range_pct,
        "median_bar_dollar_volume": candidate.median_bar_dollar_volume,
        "p75_bar_dollar_volume": candidate.p75_bar_dollar_volume,
        "median_minute_dollar_volume": candidate.median_minute_dollar_volume,
        "quote_size": candidate.quote_size,
        "quality_flags": list(candidate.quality_flags),
        "hard_reject": candidate.hard_reject,
    }


def get_regular_session_bars(
    settings: Settings,
    symbols: list[str],
    session_dates: list[date],
) -> dict[str, list[Bar]]:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca_client import make_clients, to_bar

    if not session_dates:
        return {symbol: [] for symbol in symbols}

    clients = make_clients(settings)
    start = datetime.combine(session_dates[0], MARKET_OPEN, tzinfo=MARKET_TZ).astimezone(timezone.utc)
    end = datetime.combine(session_dates[-1], MARKET_CLOSE, tzinfo=MARKET_TZ).astimezone(timezone.utc)
    requested_dates = set(session_dates)
    results: dict[str, list[Bar]] = {}

    for symbol in symbols:
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
            feed=clients.feed,
        )
        response = clients.historical.get_stock_bars(request)
        bars = [to_bar(item) for item in response.data.get(symbol, [])]
        results[symbol] = [bar for bar in bars if bar_session_date(bar) in requested_dates and in_regular_session(bar)]
    return results


def effective_top_limit(requested_top: int, stream_symbol_limit: int) -> int:
    if stream_symbol_limit <= 0:
        return requested_top
    return min(requested_top, stream_symbol_limit)


def screen(args: argparse.Namespace) -> dict[str, Any]:
    from alpaca_client import get_latest_quotes

    if args.days < 1:
        raise ValueError("--days must be at least 1.")
    if args.min_session_days < 1:
        raise ValueError("--min-session-days must be at least 1.")

    settings_kwargs: dict[str, Any] = {}
    if args.alpaca_api_key:
        settings_kwargs["alpaca_api_key"] = args.alpaca_api_key
    if args.alpaca_secret_key:
        settings_kwargs["alpaca_secret_key"] = args.alpaca_secret_key

    settings = load_settings(strategy_names=["liquidity_scalper"], validate=False)
    if settings_kwargs:
        settings = Settings(**{**settings.__dict__, **settings_kwargs})

    universe = load_universe(args.universe_file, args.symbols)
    as_of = parse_as_of(args.as_of)
    session_dates = previous_session_dates(as_of, args.days)
    bars_by_symbol = get_regular_session_bars(settings, universe, session_dates)
    quotes = get_latest_quotes(settings, universe)
    session_date_set = set(session_dates)
    top_limit = effective_top_limit(args.top, args.stream_symbol_limit)

    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for symbol in universe:
        session_rows = regular_session_metrics_by_day(bars_by_symbol.get(symbol, []), session_date_set)
        candidate = score_liquidity_scalper_candidate(
            symbol,
            session_rows,
            quotes.get(symbol),
            settings,
            min_price=args.min_price,
            max_price=args.max_price,
            min_session_days=args.min_session_days,
        )
        if candidate is None:
            rejected.append({"symbol": symbol, "code": "quote", "detail": "invalid or missing latest quote"})
            continue
        row = candidate_to_dict(candidate)
        if candidate.hard_reject:
            rejected.append(
                {
                    "symbol": symbol,
                    "code": "filter",
                    "detail": "; ".join(candidate.quality_flags) or "failed liquidity filters",
                }
            )
        candidates.append(row)

    selectable = [row for row in candidates if not row["hard_reject"]]
    selectable.sort(key=lambda item: item["score"], reverse=True)
    selected = selectable[:top_limit]

    return {
        "selected_symbols": [item["symbol"] for item in selected],
        "symbols_env_line": format_symbols_env_line([item["symbol"] for item in selected]),
        "candidates": selectable,
        "rejected": rejected,
        "screened": len(universe),
        "session_dates": [value.isoformat() for value in session_dates],
        "as_of": as_of.isoformat(),
        "requested_top": args.top,
        "effective_top": top_limit,
        "stream_symbol_limit": args.stream_symbol_limit,
        "thresholds": {
            "min_session_dollar_volume": settings.liquidity_scalper_min_session_dollar_volume,
            "min_bar_dollar_volume": settings.liquidity_scalper_min_bar_dollar_volume,
            "min_range_pct": settings.liquidity_scalper_min_range_pct,
            "max_spread_bps": settings.liquidity_scalper_max_spread_bps,
        },
    }


def deterministic_plan(screen_result: dict[str, Any], limit: int) -> dict[str, Any]:
    ranked = [
        {
            "symbol": str(row["symbol"]).upper(),
            "score": round(float(row.get("score", 0.0) or 0.0), 3),
            "notes": list(row.get("quality_flags") or []),
        }
        for row in screen_result.get("candidates") or []
        if str(row.get("symbol", "")).strip()
    ]
    ranked.sort(key=lambda item: item["score"], reverse=True)
    selected = [item["symbol"] for item in ranked[:limit]]
    return {
        "date": str(screen_result.get("as_of", date.today().isoformat()))[:10],
        "strategy": "liquidity_scalper",
        "symbols": selected,
        "ranked": ranked[:limit],
        "rejected": list(screen_result.get("rejected") or []),
        "settings": {
            "ALPACA_MARKET_DATA_MODE": "stream",
        },
        "risk_note": (
            "Deterministic liquidity_scalper selection ranked by prior-session dollar volume, "
            "intraday range, and minute-bar liquidity. Keep symbol count within the Alpaca Basic "
            "IEX trade+quote channel budget (~15 symbols)."
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Pre-session REST screener for liquidity_scalper. "
            "Ranks liquid names using prior regular-session minute bars and a quote snapshot; "
            "it does not open a live stream."
        )
    )
    parser.add_argument("--symbols", default="", help="Comma-separated universe override.")
    parser.add_argument(
        "--universe-file",
        type=Path,
        default=DEFAULT_UNIVERSE_FILE,
        help=f"Universe file (default {DEFAULT_UNIVERSE_FILE}).",
    )
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="Prior regular sessions to inspect.")
    parser.add_argument(
        "--min-session-days",
        type=int,
        default=2,
        help="Minimum completed sessions required per symbol.",
    )
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, help="Symbols to select (default 12).")
    parser.add_argument(
        "--stream-symbol-limit",
        type=int,
        default=DEFAULT_STREAM_SYMBOL_LIMIT,
        help="Cap selected symbols for Basic IEX quote+trade channels (0 disables cap).",
    )
    parser.add_argument("--min-price", type=float, default=5.0, help="Minimum last/mid price.")
    parser.add_argument("--max-price", type=float, default=500.0, help="Maximum last/mid price.")
    parser.add_argument("--as-of", default="", help="Reference timestamp (ISO-8601, America/New_York assumed if naive).")
    parser.add_argument(
        "--plan-output",
        type=Path,
        default=DEFAULT_PLAN_FILE,
        help=f"Output JSON path (default {DEFAULT_PLAN_FILE}).",
    )
    parser.add_argument("--alpaca-api-key", default="", help="Optional API key override.")
    parser.add_argument("--alpaca-secret-key", default="", help="Optional secret key override.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = screen(args)
    top_limit = int(result["effective_top"])
    plan = deterministic_plan(result, top_limit)
    args.plan_output.parent.mkdir(parents=True, exist_ok=True)
    args.plan_output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    result["selection_plan"] = plan
    result["plan_output"] = str(args.plan_output)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
