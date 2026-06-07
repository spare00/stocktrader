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
MIN_SETUP_BARS = 21
BASE_LOOKBACK_DAYS = 15
PRIOR_HIGH_LOOKBACK = 20
DEFAULT_MAX_BASE_RANGE_PCT = 0.15
DEFAULT_MIN_BREAKOUT_DAY_PCT = 0.001
DEFAULT_MIN_VOLUME_RATIO = 0.85
DEFAULT_MAX_EXTENSION_FROM_BASE_PCT = 0.15
CLOSE_NEAR_HIGH_MIN = 0.50
BASE_BREAK_TOLERANCE = 0.998
PRIOR_HIGH_BREAK_TOLERANCE = 0.995
SETUP_GATE_KEYS = (
    "compressed",
    "breakout",
    "close_near_high",
    "volume_ok",
    "daily_change_ok",
    "price_above_ema20",
    "ema_aligned",
    "not_overextended",
)


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


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values or period <= 0:
        return []
    alpha = 2.0 / (period + 1)
    out: list[float] = []
    ema = values[0]
    for value in values:
        ema = alpha * value + (1.0 - alpha) * ema
        out.append(ema)
    return out


def breakout_setup_metrics(
    bars: list[Bar],
    *,
    base_lookback_days: int = BASE_LOOKBACK_DAYS,
    prior_high_lookback: int = PRIOR_HIGH_LOOKBACK,
    max_base_range_pct: float = DEFAULT_MAX_BASE_RANGE_PCT,
    min_breakout_day_pct: float = DEFAULT_MIN_BREAKOUT_DAY_PCT,
    min_volume_ratio: float = DEFAULT_MIN_VOLUME_RATIO,
    max_extension_from_base_pct: float = DEFAULT_MAX_EXTENSION_FROM_BASE_PCT,
    require_ema_stack: bool = False,
) -> dict | None:
    """Daily setup for next-session day trades: tight base then fresh breakout (SOFI May-28 style)."""
    ordered = sorted((bar for bar in bars if bar.close > 0), key=lambda item: item.start_ms)
    if len(ordered) < MIN_SETUP_BARS:
        return None

    latest = ordered[-1]
    if len(ordered) < 2:
        return None

    base = ordered[-(base_lookback_days + 1) : -1]
    if len(base) < 5:
        return None

    base_high = max(float(bar.high) for bar in base)
    base_low = min(float(bar.low) for bar in base)
    if base_low <= 0:
        return None

    base_range_pct = (base_high - base_low) / base_low
    compressed = base_range_pct <= max_base_range_pct

    prior_window = ordered[-(prior_high_lookback + 1) : -1]
    prior_high = max(float(bar.high) for bar in prior_window) if prior_window else base_high

    prior_close = float(ordered[-2].close)
    daily_change_pct = (float(latest.close) - prior_close) / prior_close if prior_close > 0 else 0.0
    day_range = float(latest.high) - float(latest.low)
    close_near_high = ((float(latest.close) - float(latest.low)) / day_range) >= CLOSE_NEAR_HIGH_MIN if day_range > 0 else False

    base_volumes = [float(bar.volume) for bar in base if bar.volume > 0]
    avg_base_volume = sum(base_volumes) / len(base_volumes) if base_volumes else 0.0
    volume_ratio = float(latest.volume) / avg_base_volume if avg_base_volume > 0 else 0.0

    broke_base = float(latest.close) > base_high * BASE_BREAK_TOLERANCE
    broke_prior_high = float(latest.close) >= prior_high * PRIOR_HIGH_BREAK_TOLERANCE

    closes = [float(bar.close) for bar in ordered]
    ema5_series = _ema_series(closes, 5)
    ema10_series = _ema_series(closes, 10)
    ema20_series = _ema_series(closes, 20)
    ema5_now = ema5_series[-1]
    ema10_now = ema10_series[-1]
    ema20_now = ema20_series[-1]

    ema_stack = ema5_now > ema10_now > ema20_now
    price_above_ema20 = float(latest.close) > ema20_now
    recent_cross = False
    if len(ema5_series) >= 6 and len(ema20_series) >= 6:
        was_below = any(ema5_series[-1 - offset] <= ema20_series[-1 - offset] for offset in range(1, 6))
        recent_cross = was_below and ema5_now > ema20_now

    ema_gap_now = (ema5_now - ema20_now) / ema20_now if ema20_now > 0 else 0.0
    ema_gap_prev = (ema5_series[-2] - ema20_series[-2]) / ema20_series[-2] if ema20_series[-2] > 0 else 0.0
    ema_gap_expanding = ema_gap_now > ema_gap_prev and ema_gap_now > 0

    extension_from_base = (float(latest.close) - base_low) / base_low
    not_overextended = extension_from_base <= max_extension_from_base_pct

    ema_aligned = ema_stack or recent_cross or (price_above_ema20 and ema5_now > ema20_now)
    if require_ema_stack:
        ema_aligned = ema_stack

    setup_ok = (
        (broke_base or broke_prior_high)
        and close_near_high
        and volume_ratio >= min_volume_ratio
        and daily_change_pct >= min_breakout_day_pct
        and price_above_ema20
        and ema_aligned
        and not_overextended
    )

    return {
        "setup_ok": setup_ok,
        "setup_ideal": setup_ok and compressed,
        "compressed": compressed,
        "broke_base": broke_base,
        "broke_prior_high": broke_prior_high,
        "close_near_high": close_near_high,
        "volume_ratio": volume_ratio,
        "daily_change_pct": daily_change_pct,
        "base_range_pct": base_range_pct,
        "extension_from_base": extension_from_base,
        "ema_stack": ema_stack,
        "recent_cross": recent_cross,
        "ema_aligned": ema_aligned,
        "ema_gap_expanding": ema_gap_expanding,
        "price_above_ema20": price_above_ema20,
        "not_overextended": not_overextended,
    }


def breakout_setup_score(setup: dict) -> float:
    score = 0.0
    if setup["compressed"]:
        tightness = max(0.0, DEFAULT_MAX_BASE_RANGE_PCT - setup["base_range_pct"]) / DEFAULT_MAX_BASE_RANGE_PCT
        score += 1.5 + tightness
    if setup["broke_base"]:
        score += 2.5
    if setup["broke_prior_high"]:
        score += 1.5
    if setup["close_near_high"]:
        score += 1.5
    score += min(max(setup["volume_ratio"] - 1.0, 0.0), 2.5)
    score += min(max(setup["daily_change_pct"] * 25.0, 0.0), 3.0)
    if setup["ema_stack"]:
        score += 2.0
    if setup["recent_cross"]:
        score += 2.5
    if setup["ema_gap_expanding"]:
        score += 1.5
    if setup["price_above_ema20"]:
        score += 0.5
    if not setup["not_overextended"]:
        score -= 2.0
    if setup["setup_ideal"]:
        score += 1.5
    return score


def setup_gate_checks(setup: dict, *, min_volume_ratio: float, min_breakout_day_pct: float) -> dict[str, bool]:
    return {
        "compressed": setup["compressed"],
        "breakout": setup["broke_base"] or setup["broke_prior_high"],
        "close_near_high": setup["close_near_high"],
        "volume_ok": setup["volume_ratio"] >= min_volume_ratio,
        "daily_change_ok": setup["daily_change_pct"] >= min_breakout_day_pct,
        "price_above_ema20": setup["price_above_ema20"],
        "ema_aligned": setup["ema_aligned"],
        "not_overextended": setup["not_overextended"],
    }


def summarize_setup_gate_failures(candidates: list[dict]) -> dict[str, int]:
    with_checks = [item for item in candidates if item.get("setup_checks")]
    if not with_checks:
        return {}
    summary: dict[str, int] = {}
    for key in SETUP_GATE_KEYS:
        summary[key] = sum(1 for item in with_checks if not item["setup_checks"].get(key))
    return summary


def near_miss_candidates(candidates: list[dict], limit: int = 10) -> list[dict]:
    misses = [item for item in candidates if item.get("setup_checks") and not item.get("breakout_setup_ok")]
    misses.sort(key=lambda item: item.get("setup_score", 0.0), reverse=True)
    trimmed = []
    for item in misses[:limit]:
        trimmed.append(
            {
                "symbol": item["symbol"],
                "setup_score": item.get("setup_score"),
                "setup_checks": item.get("setup_checks"),
                "daily_change_pct": item.get("daily_change_pct"),
                "volume_ratio": item.get("volume_ratio"),
            }
        )
    return trimmed


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
    *,
    base_lookback_days: int = BASE_LOOKBACK_DAYS,
    max_base_range_pct: float = DEFAULT_MAX_BASE_RANGE_PCT,
    min_breakout_day_pct: float = DEFAULT_MIN_BREAKOUT_DAY_PCT,
    min_volume_ratio: float = DEFAULT_MIN_VOLUME_RATIO,
    max_extension_from_base_pct: float = DEFAULT_MAX_EXTENSION_FROM_BASE_PCT,
    require_ema_stack: bool = False,
) -> dict | None:
    metrics = daily_metrics(bars)
    if not metrics:
        return None
    if metrics["price"] < min_price or metrics["price"] > max_price:
        return None
    if metrics["average_volume"] < min_average_volume:
        return None

    setup = breakout_setup_metrics(
        bars,
        base_lookback_days=base_lookback_days,
        max_base_range_pct=max_base_range_pct,
        min_breakout_day_pct=min_breakout_day_pct,
        min_volume_ratio=min_volume_ratio,
        max_extension_from_base_pct=max_extension_from_base_pct,
        require_ema_stack=require_ema_stack,
    )

    valid_quote = usable_quote(quote)
    spread_bps = valid_quote.spread_bps if valid_quote else None

    liquidity_score = min(math.log10(metrics["average_volume"] / min_average_volume + 1), 6.0)
    quote_score = 0.5 if spread_bps is not None and spread_bps <= max_spread_bps else 0.0
    setup_score = breakout_setup_score(setup) if setup else 0.0
    score = liquidity_score + quote_score + setup_score

    result = {
        "symbol": symbol,
        "score": round(score, 3),
        "price": round(metrics["price"], 2),
        "average_volume": round(metrics["average_volume"], 2),
        "median_dollar_volume": round(metrics["median_dollar_volume"], 2),
        "spread_bps": round(spread_bps, 2) if spread_bps is not None else None,
        "setup_score": round(setup_score, 3),
        "breakout_setup_ok": bool(setup and setup["setup_ok"]),
    }
    if setup:
        result["setup_ideal"] = setup["setup_ideal"]
        result["setup_checks"] = setup_gate_checks(
            setup,
            min_volume_ratio=min_volume_ratio,
            min_breakout_day_pct=min_breakout_day_pct,
        )
        result["daily_change_pct"] = round(setup["daily_change_pct"] * 100, 2)
        result["volume_ratio"] = round(setup["volume_ratio"], 2)
        result["base_range_pct"] = round(setup["base_range_pct"] * 100, 2)
        result["ema_stack"] = setup["ema_stack"]
        result["recent_cross"] = setup["recent_cross"]
    return result


def select_top_candidates(
    candidates: list[dict],
    top: int,
    *,
    prefer_breakout_setup: bool,
    strict: bool = False,
) -> list[dict]:
    ranked = sorted(candidates, key=lambda item: item["score"], reverse=True)
    if strict:
        strict_ranked = [item for item in ranked if item.get("breakout_setup_ok")]
        strict_ranked.sort(
            key=lambda item: (bool(item.get("setup_ideal")), item.get("setup_score", 0.0), item["score"]),
            reverse=True,
        )
        return strict_ranked[:top]
    if not prefer_breakout_setup:
        return ranked[:top]

    setup_first = [item for item in ranked if item.get("breakout_setup_ok")]
    setup_first.sort(
        key=lambda item: (bool(item.get("setup_ideal")), item.get("setup_score", 0.0), item["score"]),
        reverse=True,
    )
    remainder = [item for item in ranked if not item.get("breakout_setup_ok")]
    selected = setup_first[:top]
    if len(selected) < top:
        selected.extend(remainder[: top - len(selected)])
    return selected


def build_universe(args: argparse.Namespace) -> dict:
    if args.top < 1:
        raise ValueError("--top must be at least 1.")
    if args.lookback_days < 2:
        raise ValueError("--lookback-days must be at least 2.")
    if args.batch_size < 1:
        raise ValueError("--batch-size must be at least 1.")
    if args.lookback_days < MIN_SETUP_BARS:
        raise ValueError(f"--lookback-days must be at least {MIN_SETUP_BARS}.")
    base_lookback_days = int(getattr(args, "base_lookback_days", BASE_LOOKBACK_DAYS))
    if base_lookback_days < 5:
        raise ValueError("--base-lookback-days must be at least 5.")

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
            base_lookback_days=base_lookback_days,
            max_base_range_pct=args.max_base_range_pct,
            min_breakout_day_pct=args.min_breakout_day_pct,
            min_volume_ratio=args.min_volume_ratio,
            max_extension_from_base_pct=args.max_extension_from_base_pct,
            require_ema_stack=bool(getattr(args, "require_ema_stack", False)),
        )
        if result:
            candidates.append(result)

    prefer_breakout_setup = not bool(getattr(args, "liquidity_only_ranking", False))
    strict = bool(getattr(args, "strict", False))
    selected = select_top_candidates(
        candidates,
        args.top,
        prefer_breakout_setup=prefer_breakout_setup,
        strict=strict,
    )

    selected_symbols = [item["symbol"] for item in selected]
    setup_pass_count = sum(1 for item in candidates if item.get("breakout_setup_ok"))
    setup_failure_summary = summarize_setup_gate_failures(candidates) if candidates else {}
    near_misses = near_miss_candidates(candidates) if strict and not selected else []

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(",".join(selected_symbols) + ("\n" if selected_symbols else ""))

    return {
        "selected_symbols": selected_symbols,
        "symbols_env_line": format_symbols_env_line(selected_symbols),
        "output": str(args.output) if args.output else None,
        "candidates": selected,
        "screened": len(symbols),
        "passed": len(candidates),
        "setup_pass_count": setup_pass_count,
        "setup_failure_summary": setup_failure_summary,
        "near_misses": near_misses,
        "requested_top": args.top,
        "selected_count": len(selected_symbols),
        "as_of_date": as_of.isoformat() if as_of is not None else None,
        "lookback_days": args.lookback_days,
        "base_lookback_days": base_lookback_days,
        "prefer_breakout_setup": prefer_breakout_setup,
        "strict": strict,
        "max_base_range_pct": args.max_base_range_pct,
        "min_breakout_day_pct": args.min_breakout_day_pct,
        "min_volume_ratio": args.min_volume_ratio,
        "min_average_volume": args.min_average_volume,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "REST-only broad market selector for next-session day-trade monitoring. "
            "Ranks symbols whose latest daily bar looks like a fresh base breakout "
            "(tight range, strong close, volume expansion, EMA turn), then fills --top."
        )
    )
    parser.add_argument("--top", type=int, default=300)
    parser.add_argument("--output", type=Path, default=Path("data/opening_universe.txt"))
    parser.add_argument("--lookback-days", type=int, default=60)
    parser.add_argument("--base-lookback-days", type=int, default=BASE_LOOKBACK_DAYS, help="Consolidation window before the latest daily bar.")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--exchanges", default="NASDAQ,NYSE,ARCA", help="Comma-separated asset exchanges to include.")
    parser.add_argument("--min-price", type=float, default=5.0)
    parser.add_argument("--max-price", type=float, default=500.0)
    parser.add_argument("--min-average-volume", type=float, default=1_000_000.0)
    parser.add_argument("--max-spread-bps", type=float, default=12.0)
    parser.add_argument(
        "--max-base-range-pct",
        type=float,
        default=DEFAULT_MAX_BASE_RANGE_PCT,
        help="Maximum base range (fraction) over --base-lookback-days before breakout day.",
    )
    parser.add_argument(
        "--min-breakout-day-pct",
        type=float,
        default=DEFAULT_MIN_BREAKOUT_DAY_PCT,
        help="Minimum latest daily gain (fraction) for breakout_setup_ok tier.",
    )
    parser.add_argument(
        "--min-volume-ratio",
        type=float,
        default=DEFAULT_MIN_VOLUME_RATIO,
        help="Minimum latest-day volume vs average base volume for breakout_setup_ok tier.",
    )
    parser.add_argument(
        "--max-extension-from-base-pct",
        type=float,
        default=DEFAULT_MAX_EXTENSION_FROM_BASE_PCT,
        help="Maximum extension above base low on breakout day (avoid late chase).",
    )
    parser.add_argument(
        "--require-ema-stack",
        action="store_true",
        help="Require EMA5 > EMA10 > EMA20 for breakout_setup_ok tier (default allows fresh EMA5/20 cross).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Only include symbols passing breakout_setup_ok; do not backfill --top with filtered-out names.",
    )
    parser.add_argument(
        "--liquidity-only-ranking",
        action="store_true",
        help="Rank purely by liquidity/spread score without breakout-setup-first tiering.",
    )
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
    selected_count = int(result.get("selected_count") or 0)
    if selected_count == 0:
        print(
            "No symbols selected. "
            f"screened={result.get('screened')} passed={result.get('passed')} "
            f"setup_pass_count={result.get('setup_pass_count')} strict={result.get('strict')}",
            file=sys.stderr,
        )
        if result.get("setup_failure_summary"):
            print(
                "Setup gate failures (counts among liquid symbols with daily setup data): "
                f"{json.dumps(result['setup_failure_summary'], sort_keys=True)}",
                file=sys.stderr,
            )
        if result.get("near_misses"):
            print(
                "Near-miss symbols (highest setup_score, not strict-pass): "
                f"{json.dumps(result['near_misses'], sort_keys=True)}",
                file=sys.stderr,
            )
        if not result.get("passed"):
            print(
                "Hint: passed=0 usually means missing Alpaca credentials, empty bar history, "
                "or no symbols met liquidity filters. Use --as-of-date YYYY-MM-DD (not --as-of-te).",
                file=sys.stderr,
            )
        elif result.get("strict") and not result.get("setup_pass_count"):
            print(
                "Hint: --strict requires breakout_setup_ok. Try without --strict to backfill --top, "
                "or loosen --max-base-range-pct / --min-breakout-day-pct / --min-volume-ratio.",
                file=sys.stderr,
            )
    if line:
        print(
            "# Paste into profiles/*.env or `.env` — do not `export` (pollutes shell/tmux).",
            file=sys.stderr,
        )
        print(line)


if __name__ == "__main__":
    main()
