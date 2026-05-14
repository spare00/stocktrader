import argparse
import json
import math
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Settings, load_settings
from env_vars import format_symbols_env_line
from models import Bar, Quote
from strategy_selectors.select_opening_impulse import usable_quote


MARKET_TZ = ZoneInfo("America/New_York")


def batched(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def parse_symbols(raw: str) -> list[str]:
    return [part.strip().upper() for part in raw.replace("\n", ",").split(",") if part.strip()]


def enum_value(value) -> str:
    return str(getattr(value, "value", value)).upper()


def get_active_tradable_symbols(settings: Settings, exchanges: set[str] | None = None) -> list[str]:
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest
    from alpaca_client import make_clients

    clients = make_clients(settings)
    request = GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
    assets = clients.trading.get_all_assets(request)
    symbols = []
    for asset in assets:
        symbol = str(getattr(asset, "symbol", "")).upper()
        exchange = enum_value(getattr(asset, "exchange", ""))
        if not symbol or "/" in symbol:
            continue
        if exchanges and exchange not in exchanges:
            continue
        if not bool(getattr(asset, "tradable", False)):
            continue
        symbols.append(symbol)
    return sorted(dict.fromkeys(symbols))


def get_daily_bars(
    settings: Settings,
    symbols: list[str],
    lookback_days: int,
    batch_size: int,
    *,
    as_of: date | None = None,
) -> dict[str, list[Bar]]:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca_client import make_clients, to_bar

    clients = make_clients(settings)
    if as_of is not None:
        end = datetime.combine(as_of, time.min, tzinfo=MARKET_TZ) + timedelta(days=1)
    else:
        end = datetime.now(tz=MARKET_TZ).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    start = end - timedelta(days=lookback_days * 2 + 10)
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
            bars = [to_bar(item) for item in response.data.get(symbol, [])]
            results[symbol] = sorted(bars, key=lambda item: item.start_ms)[-lookback_days:]
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


def daily_metrics(bars: list[Bar]) -> dict | None:
    ordered = sorted(bars, key=lambda item: item.start_ms)
    if len(ordered) < 2:
        return None

    dollar_volumes = [bar.close * bar.volume for bar in ordered if bar.close > 0 and bar.volume > 0]
    volumes = [bar.volume for bar in ordered if bar.volume > 0]
    if not dollar_volumes or not volumes:
        return None

    latest = ordered[-1]
    prior = ordered[-2]
    first = ordered[0]
    if latest.close <= 0 or prior.close <= 0 or first.close <= 0:
        return None

    recent_low = min(bar.low for bar in ordered)
    recent_high = max(bar.high for bar in ordered)
    gap_bps = ((latest.open - prior.close) / prior.close) * 10_000 if prior.close > 0 else 0.0
    trend_bps = ((latest.close - first.close) / first.close) * 10_000
    rebound_from_low_bps = ((latest.close - recent_low) / recent_low) * 10_000 if recent_low > 0 else 0.0
    distance_from_high_bps = ((recent_high - latest.close) / recent_high) * 10_000 if recent_high > 0 else 0.0

    return {
        "price": latest.close,
        "average_volume": sum(volumes) / len(volumes),
        "median_dollar_volume": median(dollar_volumes),
        "latest_gap_bps": gap_bps,
        "trend_bps": trend_bps,
        "rebound_from_low_bps": rebound_from_low_bps,
        "distance_from_high_bps": distance_from_high_bps,
    }


def score_symbol(
    symbol: str,
    bars: list[Bar],
    quote: Quote | None,
    min_price: float,
    max_price: float,
    min_average_volume: float,
    max_spread_bps: float,
) -> dict | None:
    metrics = daily_metrics(bars)
    if not metrics:
        return None
    if metrics["price"] < min_price or metrics["price"] > max_price:
        return None
    if metrics["average_volume"] < min_average_volume:
        return None

    valid_quote = usable_quote(quote)
    spread_bps = valid_quote.spread_bps if valid_quote else None

    liquidity_score = min(math.log10(metrics["average_volume"] / min_average_volume + 1), 6.0)
    quote_score = 0.5 if spread_bps is not None and spread_bps <= max_spread_bps else 0.0
    score = liquidity_score + quote_score

    return {
        "symbol": symbol,
        "score": round(score, 3),
        "price": round(metrics["price"], 2),
        "average_volume": round(metrics["average_volume"], 2),
        "median_dollar_volume": round(metrics["median_dollar_volume"], 2),
        "spread_bps": round(spread_bps, 2) if spread_bps is not None else None,
    }


def build_universe(args: argparse.Namespace) -> dict:
    if args.top < 1:
        raise ValueError("--top must be at least 1.")
    if args.lookback_days < 2:
        raise ValueError("--lookback-days must be at least 2.")
    if args.batch_size < 1:
        raise ValueError("--batch-size must be at least 1.")

    settings_kwargs = {}
    if args.alpaca_api_key:
        settings_kwargs["alpaca_api_key"] = args.alpaca_api_key
    if args.alpaca_secret_key:
        settings_kwargs["alpaca_secret_key"] = args.alpaca_secret_key
    settings = load_settings(strategy_names=[], validate=False)
    if settings_kwargs:
        settings = Settings(**{**settings.__dict__, **settings_kwargs})

    as_of: date | None = None
    as_of_date_arg = getattr(args, "as_of_date", None)
    if as_of_date_arg and str(as_of_date_arg).strip():
        try:
            as_of = date.fromisoformat(str(as_of_date_arg).strip())
        except ValueError as exc:
            raise ValueError("--as-of-date must be YYYY-MM-DD.") from exc

    exchanges = {value.upper() for value in parse_symbols(args.exchanges)} if args.exchanges else None
    symbols = get_active_tradable_symbols(settings, exchanges=exchanges)
    if as_of is not None:
        bars_by_symbol = get_daily_bars(settings, symbols, args.lookback_days, args.batch_size, as_of=as_of)
    else:
        bars_by_symbol = get_daily_bars(settings, symbols, args.lookback_days, args.batch_size)
    skip_quotes = bool(getattr(args, "skip_quotes", False))
    quotes = get_latest_quotes(settings, symbols, args.batch_size) if not skip_quotes else {}

    candidates = []
    for symbol in symbols:
        result = score_symbol(
            symbol=symbol,
            bars=bars_by_symbol.get(symbol, []),
            quote=quotes.get(symbol),
            min_price=args.min_price,
            max_price=args.max_price,
            min_average_volume=args.min_average_volume,
            max_spread_bps=args.max_spread_bps,
        )
        if result:
            candidates.append(result)

    candidates.sort(key=lambda item: item["score"], reverse=True)
    selected = candidates[: args.top]
    selected_symbols = [item["symbol"] for item in selected]
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(",".join(selected_symbols) + "\n")

    return {
        "selected_symbols": selected_symbols,
        "symbols_env_line": format_symbols_env_line(selected_symbols),
        "output": str(args.output) if args.output else None,
        "candidates": selected,
        "screened": len(symbols),
        "passed": len(candidates),
        "lookback_days": args.lookback_days,
        "min_average_volume": args.min_average_volume,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "REST-only broad market selector. "
            "Run periodically, then pass the output file to a per-strategy selector."
        )
    )
    parser.add_argument("--top", type=int, default=300)
    parser.add_argument("--output", type=Path, default=Path("data/opening_universe.txt"))
    parser.add_argument("--lookback-days", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--exchanges", default="NASDAQ,NYSE,ARCA", help="Comma-separated asset exchanges to include.")
    parser.add_argument("--min-price", type=float, default=5.0)
    parser.add_argument("--max-price", type=float, default=500.0)
    parser.add_argument("--min-average-volume", type=float, default=1_000_000.0)
    parser.add_argument("--max-spread-bps", type=float, default=12.0)
    parser.add_argument(
        "--skip-quotes",
        action="store_true",
        help=(
            "Skip latest quote checks (spread score only). Use with alpaca_mock_server replay "
            "or very large asset lists if quote fetches are too slow."
        ),
    )
    parser.add_argument(
        "--as-of-date",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "US/Eastern calendar day to anchor daily bars (inclusive); exclusive end is midnight "
            "at the start of the following day. Set to the same calendar day as the mock's "
            "--alpaca-date when you want an explicit bar anchor (optional if you rely on the mock's "
            "replay remap from wall-clock requests)."
        ),
    )
    parser.add_argument("--alpaca-api-key", default=None)
    parser.add_argument("--alpaca-secret-key", default=None)
    return parser.parse_args()


def main() -> None:
    result = build_universe(parse_args())
    print(json.dumps(result, indent=2, sort_keys=True))
    line = result.get("symbols_env_line") or ""
    if line:
        print(
            "# Paste into profiles/*.env or `.env` — do not `export` (pollutes shell/tmux).",
            file=sys.stderr,
        )
        print(line)


if __name__ == "__main__":
    main()
