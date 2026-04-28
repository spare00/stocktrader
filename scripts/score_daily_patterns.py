import argparse
import json
import math
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Settings
from models import Bar, Quote
from scripts.build_opening_universe import batched, parse_symbols
from scripts.screen_opening_impulse import usable_quote


MARKET_TZ = ZoneInfo("America/New_York")
DEFAULT_UNIVERSE_FILE = Path("data/opening_universe.txt")
DEFAULT_OUTPUT_FILE = Path("data/daily_pattern_candidates.json")
DEFAULT_JOURNAL_FILE = Path("data/trade_candidates.jsonl")
PREMARKET_START = time(4, 0)
REGULAR_OPEN = time(9, 30)


def load_universe(path: Path, raw_symbols: str) -> list[str]:
    if raw_symbols:
        symbols = parse_symbols(raw_symbols)
    elif path.exists():
        symbols = parse_symbols(path.read_text())
    else:
        symbols = []
    return sorted(dict.fromkeys(symbols))


def parse_as_of(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(tz=MARKET_TZ)
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        value = value.replace(tzinfo=MARKET_TZ)
    return value.astimezone(MARKET_TZ)


def pct_change(new: float, old: float) -> float:
    return (new - old) / old if old > 0 else 0.0


def bar_range(bar: Bar) -> float:
    return max(0.0, bar.high - bar.low)


def close_location(bar: Bar) -> float:
    span = bar_range(bar)
    if span <= 0:
        return 0.5
    return (bar.close - bar.low) / span


def previous_daily_volume(daily_bars: list[Bar]) -> float:
    prior = [bar.volume for bar in daily_bars[:-1] if bar.volume > 0]
    return mean(prior) if prior else 0.0


def minute_volume_baseline(daily_bars: list[Bar]) -> float:
    avg_volume = previous_daily_volume(daily_bars)
    return avg_volume / 390 if avg_volume > 0 else 0.0


def mean_reversion_score(daily_bars: list[Bar]) -> tuple[float, list[str]]:
    if len(daily_bars) < 2:
        return 0.0, ["insufficient daily bars"]
    latest = daily_bars[-1]
    prior = daily_bars[-2]
    avg_volume = previous_daily_volume(daily_bars)
    score = 0.0
    reasons = []

    prev_return = pct_change(latest.close, prior.close)
    if prev_return <= -0.03:
        score += 3
        reasons.append(f"prev_day_return={prev_return:.2%}")
    if close_location(latest) <= 0.25:
        score += 2
        reasons.append("close_near_low")
    if avg_volume > 0 and latest.volume >= avg_volume * 1.5:
        score += 2
        reasons.append("volume_spike")
    return score, reasons


def compression_score(daily_bars: list[Bar]) -> tuple[float, list[str]]:
    if len(daily_bars) < 5:
        return 0.0, ["insufficient daily bars"]
    recent = daily_bars[-5:]
    last_three = recent[-3:]
    ranges = [bar_range(bar) for bar in last_three]
    volumes = [bar.volume for bar in last_three]
    latest = recent[-1]
    resistance = max(bar.high for bar in recent[:-1])
    score = 0.0
    reasons = []

    if ranges[0] > ranges[1] > ranges[2]:
        score += 3
        reasons.append("range_decreasing_3_days")
    if volumes[0] > volumes[1] > volumes[2]:
        score += 2
        reasons.append("volume_declining")
    if resistance > 0 and latest.close >= resistance * 0.99:
        score += 2
        reasons.append("near_resistance")
    return score, reasons


def trend_continuation_score(daily_bars: list[Bar]) -> tuple[float, list[str]]:
    if len(daily_bars) < 2:
        return 0.0, ["insufficient daily bars"]
    latest = daily_bars[-1]
    prior = daily_bars[-2]
    avg_volume = previous_daily_volume(daily_bars)
    score = 0.0
    reasons = []

    prev_return = pct_change(latest.close, prior.close)
    if prev_return >= 0.03:
        score += 3
        reasons.append(f"prev_day_return={prev_return:.2%}")
    if close_location(latest) >= 0.75:
        score += 2
        reasons.append("close_near_high")
    if avg_volume > 0 and latest.volume >= avg_volume * 1.2:
        score += 2
        reasons.append("strong_volume")
    return score, reasons


def session_minutes(minute_bars: list[Bar], as_of: datetime) -> list[Bar]:
    session_date = as_of.astimezone(MARKET_TZ).date()
    return [
        bar
        for bar in sorted(minute_bars, key=lambda item: item.start_ms)
        if datetime.fromtimestamp(bar.start_ms / 1000, tz=timezone.utc).astimezone(MARKET_TZ).date() == session_date
    ]


def gap_and_go_score(daily_bars: list[Bar], minute_bars: list[Bar], as_of: datetime) -> tuple[float, list[str]]:
    if not daily_bars or not minute_bars:
        return 0.0, ["missing daily or minute bars"]
    minutes = session_minutes(minute_bars, as_of)
    if not minutes:
        return 0.0, ["no current-session minute bars"]
    prev_close = daily_bars[-1].close
    first = minutes[0]
    latest = minutes[-1]
    premarket = [
        bar
        for bar in minutes
        if datetime.fromtimestamp(bar.start_ms / 1000, tz=timezone.utc).astimezone(MARKET_TZ).time() < REGULAR_OPEN
    ]
    score = 0.0
    reasons = []

    gap_up = pct_change(first.open, prev_close)
    if gap_up >= 0.02:
        score += 3
        reasons.append(f"gap_up={gap_up:.2%}")
    baseline = minute_volume_baseline(daily_bars)
    premarket_volume = sum(bar.volume for bar in premarket)
    if baseline > 0 and premarket_volume >= baseline * 30:
        score += 2
        reasons.append("premarket_volume_high")
    if latest.close >= first.open:
        score += 2
        reasons.append("holding_above_open")
    return score, reasons


def opening_flush_reversal_score(daily_bars: list[Bar], minute_bars: list[Bar], as_of: datetime) -> tuple[float, list[str]]:
    minutes = [
        bar
        for bar in session_minutes(minute_bars, as_of)
        if datetime.fromtimestamp(bar.start_ms / 1000, tz=timezone.utc).astimezone(MARKET_TZ).time() >= REGULAR_OPEN
    ]
    if not minutes:
        return 0.0, ["no regular-session minute bars"]
    first_five = minutes[:5]
    first = first_five[0]
    open_price = first.open
    low = min(bar.low for bar in first_five)
    close = first_five[-1].close
    high = max(bar.high for bar in first_five)
    score = 0.0
    reasons = []

    first_5min_drop = pct_change(low, open_price)
    if first_5min_drop <= -0.02:
        score += 3
        reasons.append(f"first_5min_drop={first_5min_drop:.2%}")
    baseline = minute_volume_baseline(daily_bars)
    if baseline > 0 and sum(bar.volume for bar in first_five) >= baseline * 5 * 1.5:
        score += 2
        reasons.append("volume_spike")
    body_low = min(first.open, close)
    full_range = high - low
    lower_wick = body_low - low
    if full_range > 0 and lower_wick / full_range >= 0.4 and close > low:
        score += 2
        reasons.append("lower_wick_detected")
    return score, reasons


def score_patterns(
    symbol: str,
    daily_bars: list[Bar],
    minute_bars: list[Bar],
    quote: Quote | None,
    as_of: datetime,
    max_spread_bps: float,
) -> dict:
    ordered_daily = sorted(daily_bars, key=lambda item: item.start_ms)
    ordered_minutes = sorted(minute_bars, key=lambda item: item.start_ms)
    pattern_results = {
        "mean_reversion": mean_reversion_score(ordered_daily),
        "compression": compression_score(ordered_daily),
        "trend_continuation": trend_continuation_score(ordered_daily),
        "gap_and_go": gap_and_go_score(ordered_daily, ordered_minutes, as_of),
        "opening_flush_reversal": opening_flush_reversal_score(ordered_daily, ordered_minutes, as_of),
    }
    pattern_scores = {name: score for name, (score, _reasons) in pattern_results.items()}
    best_pattern = max(pattern_scores, key=lambda name: pattern_scores[name])
    final_score = pattern_scores[best_pattern]

    valid_quote = usable_quote(quote)
    spread_bps = valid_quote.spread_bps if valid_quote else None
    spread_penalty = -2.0 if spread_bps is not None and spread_bps > max_spread_bps else 0.0
    final_score += spread_penalty

    return {
        "symbol": symbol,
        "score": round(final_score, 3),
        "pattern": best_pattern,
        "pattern_scores": {name: round(score, 3) for name, score in pattern_scores.items()},
        "reasons": pattern_results[best_pattern][1],
        "spread_bps": round(spread_bps, 2) if spread_bps is not None and math.isfinite(spread_bps) else None,
        "spread_penalty": spread_penalty,
    }


def get_daily_bars(settings: Settings, symbols: list[str], days: int, batch_size: int) -> dict[str, list[Bar]]:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca_client import make_clients, to_bar

    clients = make_clients(settings)
    end = datetime.now(tz=MARKET_TZ).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    start = end - timedelta(days=days * 2 + 5)
    results: dict[str, list[Bar]] = {}
    for chunk in batched(symbols, batch_size):
        request = StockBarsRequest(
            symbol_or_symbols=chunk,
            timeframe=TimeFrame.Day,
            start=start.astimezone(timezone.utc),
            end=end.astimezone(timezone.utc),
            feed=clients.feed,
        )
        response = clients.historical.get_stock_bars(request)
        for symbol in chunk:
            results[symbol] = [to_bar(item) for item in response.data.get(symbol, [])][-days:]
    return results


def get_minute_bars(
    settings: Settings,
    symbols: list[str],
    as_of: datetime,
    batch_size: int,
) -> dict[str, list[Bar]]:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca_client import make_clients, to_bar

    clients = make_clients(settings)
    session_date = as_of.astimezone(MARKET_TZ).date()
    start = datetime.combine(session_date, PREMARKET_START, tzinfo=MARKET_TZ)
    end = as_of.astimezone(MARKET_TZ)
    results: dict[str, list[Bar]] = {}
    for chunk in batched(symbols, batch_size):
        request = StockBarsRequest(
            symbol_or_symbols=chunk,
            timeframe=TimeFrame.Minute,
            start=start.astimezone(timezone.utc),
            end=end.astimezone(timezone.utc),
            feed=clients.feed,
        )
        response = clients.historical.get_stock_bars(request)
        for symbol in chunk:
            results[symbol] = [to_bar(item) for item in response.data.get(symbol, [])]
    return results


def get_latest_quotes(settings: Settings, symbols: list[str], batch_size: int) -> dict[str, Quote]:
    from alpaca.data.requests import StockLatestQuoteRequest
    from alpaca_client import make_clients, to_quote

    clients = make_clients(settings)
    results: dict[str, Quote] = {}
    for chunk in batched(symbols, batch_size):
        request = StockLatestQuoteRequest(symbol_or_symbols=chunk, feed=clients.feed)
        response = clients.historical.get_stock_latest_quote(request)
        for symbol, quote in response.items():
            results[symbol] = to_quote(quote)
    return results


def append_journal(path: Path | None, result: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(result, sort_keys=True) + "\n")


def write_output(path: Path | None, result: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


def screen(args: argparse.Namespace) -> dict:
    if args.top < 1:
        raise ValueError("--top must be at least 1.")
    if args.daily_days < 2:
        raise ValueError("--daily-days must be at least 2.")
    if args.batch_size < 1:
        raise ValueError("--batch-size must be at least 1.")

    settings_kwargs = {}
    if args.alpaca_api_key:
        settings_kwargs["alpaca_api_key"] = args.alpaca_api_key
    if args.alpaca_secret_key:
        settings_kwargs["alpaca_secret_key"] = args.alpaca_secret_key
    settings = Settings(**settings_kwargs)

    symbols = load_universe(args.universe_file, args.symbols)
    as_of = parse_as_of(args.as_of)
    daily_by_symbol = get_daily_bars(settings, symbols, args.daily_days, args.batch_size)
    minute_by_symbol = get_minute_bars(settings, symbols, as_of, args.batch_size)
    quotes = get_latest_quotes(settings, symbols, args.batch_size) if not args.skip_quotes else {}

    candidates = [
        score_patterns(
            symbol=symbol,
            daily_bars=daily_by_symbol.get(symbol, []),
            minute_bars=minute_by_symbol.get(symbol, []),
            quote=quotes.get(symbol),
            as_of=as_of,
            max_spread_bps=args.max_spread_bps,
        )
        for symbol in symbols
    ]
    candidates.sort(key=lambda item: item["score"], reverse=True)
    selected = candidates[: args.top]
    result = {
        "date": as_of.date().isoformat(),
        "as_of": as_of.isoformat(),
        "selected_symbols": [item["symbol"] for item in selected],
        "export": f"export SYMBOLS={','.join(item['symbol'] for item in selected)}",
        "candidates": selected,
        "all_candidates": candidates,
        "screened": len(symbols),
        "top": args.top,
    }
    write_output(args.output, result)
    append_journal(args.journal, result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Daily state-based pattern scorer for intraday candidates.")
    parser.add_argument("--symbols", default="", help="Comma-separated universe override.")
    parser.add_argument("--universe-file", type=Path, default=DEFAULT_UNIVERSE_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL_FILE)
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--daily-days", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--max-spread-bps", type=float, default=100.0)
    parser.add_argument("--skip-quotes", action="store_true")
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--alpaca-api-key", default=None)
    parser.add_argument("--alpaca-secret-key", default=None)
    return parser.parse_args()


def main() -> None:
    result = screen(parse_args())
    print(json.dumps({"selected_symbols": result["selected_symbols"], "candidates": result["candidates"]}, indent=2))
    print(result["export"])


if __name__ == "__main__":
    main()
