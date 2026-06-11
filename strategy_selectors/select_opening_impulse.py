import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_client import request_json_response
from config import Settings, load_settings
from strategy_selectors.cli import selector_argument_parser
from env_vars import format_symbols_env_line
from models import Bar, Quote
from opening_plan import default_plan_file_for_strategy


MARKET_TZ = ZoneInfo("America/New_York")
OPEN_TIME = time(9, 30)
DEFAULT_UNIVERSE_FILE = Path("data/opening_universe.txt")
DEFAULT_PLAN_FILE = default_plan_file_for_strategy("opening_impulse")


DEFAULT_UNIVERSE = [
    "AAPL",
    "ABNB",
    "ADBE",
    "AMD",
    "AMGN",
    "AMZN",
    "AVGO",
    "BA",
    "BABA",
    "BAC",
    "COIN",
    "COST",
    "CRM",
    "CRWD",
    "CVX",
    "DIS",
    "F",
    "GOOGL",
    "HD",
    "INTC",
    "JPM",
    "KO",
    "LLY",
    "MA",
    "META",
    "MRNA",
    "MSFT",
    "NFLX",
    "NKE",
    "NVDA",
    "ORCL",
    "PANW",
    "PEP",
    "PFE",
    "PLTR",
    "PYPL",
    "QCOM",
    "RIVN",
    "SHOP",
    "SNOW",
    "TSLA",
    "UBER",
    "UNH",
    "V",
    "WMT",
    "XOM",
]


def extract_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    stripped = text.lstrip()
    result, _ = decoder.raw_decode(stripped)
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object.")
    return result


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


def parse_as_of(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(tz=MARKET_TZ)
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        value = value.replace(tzinfo=MARKET_TZ)
    return value.astimezone(MARKET_TZ)


def previous_session_dates(as_of: datetime, count: int) -> list[date]:
    sessions = []
    current = as_of.astimezone(MARKET_TZ).date() - timedelta(days=1)
    while len(sessions) < count:
        if current.weekday() < 5:
            sessions.append(current)
        current -= timedelta(days=1)
    return sorted(sessions)


def opening_bounds(session_dates: list[date], opening_minutes: int) -> tuple[datetime, datetime]:
    if not session_dates:
        raise ValueError("At least one prior session is required.")
    start = datetime.combine(session_dates[0], OPEN_TIME, tzinfo=MARKET_TZ)
    end = datetime.combine(session_dates[-1], OPEN_TIME, tzinfo=MARKET_TZ) + timedelta(minutes=opening_minutes)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def bar_session_date(bar: Bar) -> date:
    return datetime.fromtimestamp(bar.start_ms / 1000, tz=timezone.utc).astimezone(MARKET_TZ).date()


def in_opening_window(bar: Bar, opening_minutes: int) -> bool:
    timestamp = datetime.fromtimestamp(bar.start_ms / 1000, tz=timezone.utc).astimezone(MARKET_TZ)
    open_at = datetime.combine(timestamp.date(), OPEN_TIME, tzinfo=MARKET_TZ)
    return open_at <= timestamp < open_at + timedelta(minutes=opening_minutes)


def opening_session_metrics(bars: list[Bar], opening_minutes: int) -> list[dict]:
    grouped: dict[date, list[Bar]] = defaultdict(list)
    for bar in bars:
        if in_opening_window(bar, opening_minutes):
            grouped[bar_session_date(bar)].append(bar)

    sessions = []
    for session_date, session_bars in grouped.items():
        ordered = sorted(session_bars, key=lambda item: item.start_ms)
        first = ordered[0]
        if first.open <= 0:
            continue
        high = max(bar.high for bar in ordered)
        low = min(bar.low for bar in ordered)
        close = ordered[-1].close
        dollar_volume = sum(bar.close * bar.volume for bar in ordered if bar.close > 0 and bar.volume > 0)
        sessions.append(
            {
                "date": session_date.isoformat(),
                "open": first.open,
                "close": close,
                "high_move_bps": ((high - first.open) / first.open) * 10_000,
                "close_move_bps": ((close - first.open) / first.open) * 10_000,
                "opening_range_bps": ((high - low) / first.open) * 10_000,
                "dollar_volume": dollar_volume,
                "bar_count": len(ordered),
            }
        )
    return sorted(sessions, key=lambda item: item["date"])


def daily_context_metrics(
    bars: list[Bar],
    last_price: float,
    trend_lookback_days: int,
    min_trend_bps: float,
    min_reversal_bps: float,
) -> dict:
    ordered = sorted(bars, key=lambda item: item.start_ms)
    recent = ordered[-trend_lookback_days:] if trend_lookback_days > 0 else ordered
    if len(recent) < 2 or last_price <= 0:
        return {
            "daily_context": "unknown",
            "daily_trend_bps": 0.0,
            "rebound_from_low_bps": 0.0,
            "distance_from_high_bps": 0.0,
            "daily_context_score": 0.0,
        }

    first_close = recent[0].close
    recent_low = min(bar.low for bar in recent)
    recent_high = max(bar.high for bar in recent)
    daily_trend_bps = ((last_price - first_close) / first_close) * 10_000 if first_close > 0 else 0.0
    rebound_from_low_bps = ((last_price - recent_low) / recent_low) * 10_000 if recent_low > 0 else 0.0
    distance_from_high_bps = ((recent_high - last_price) / recent_high) * 10_000 if recent_high > 0 else 0.0

    is_uptrend = daily_trend_bps >= min_trend_bps
    is_reversal = rebound_from_low_bps >= min_reversal_bps and daily_trend_bps > -min_trend_bps
    if is_uptrend:
        context = "uptrend"
    elif is_reversal:
        context = "bottom_reversal"
    else:
        context = "weak"

    trend_score = max(0.0, min(daily_trend_bps / max(min_trend_bps, 1.0), 2.0))
    reversal_score = max(0.0, min(rebound_from_low_bps / max(min_reversal_bps, 1.0), 2.0))
    return {
        "daily_context": context,
        "daily_trend_bps": daily_trend_bps,
        "rebound_from_low_bps": rebound_from_low_bps,
        "distance_from_high_bps": distance_from_high_bps,
        "daily_context_score": max(trend_score, reversal_score),
    }


def recent_compression_score(bars: list[Bar]) -> float:
    ordered = sorted(bars, key=lambda item: item.start_ms)
    ranges = [((bar.high - bar.low) / bar.close) for bar in ordered[-4:-1] if bar.close > 0]
    if len(ranges) >= 3 and ranges[-1] < ranges[-2] < ranges[-3]:
        return 2.0
    return 0.0


def daily_gap_score(bars: list[Bar]) -> float:
    ordered = sorted(bars, key=lambda item: item.start_ms)
    if len(ordered) < 2 or ordered[-2].close <= 0:
        return 0.0
    gap_pct = (ordered[-1].open - ordered[-2].close) / ordered[-2].close
    if gap_pct > 0.02:
        return 3.0
    if gap_pct < -0.02:
        return 2.0
    return 0.0


def usable_quote(quote: Quote | None) -> Quote | None:
    if quote is None:
        return None
    if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
        return None
    if quote.bid_size <= 0 or quote.ask_size <= 0:
        return None
    return quote


def score_candidate(
    symbol: str,
    bars: list[Bar],
    daily_bars: list[Bar],
    quote: Quote | None,
    opening_minutes: int,
    min_price: float,
    max_price: float,
    min_opening_days: int,
    min_opening_dollar_volume: float,
    min_impulse_bps: float,
    min_opening_range_bps: float,
    max_spread_bps: float,
    trend_lookback_days: int,
    min_trend_bps: float,
    min_reversal_bps: float,
    require_daily_context: bool,
    min_close_capture_ratio: float = 0.1,
    min_positive_close_day_ratio: float = 0.5,
    min_median_opening_close_bps: float = 0.0,
) -> dict | None:
    sessions = opening_session_metrics(bars, opening_minutes)

    valid_quote = usable_quote(quote)
    fallback_price = bars[-1].close if bars else daily_bars[-1].close if daily_bars else 0.0
    last_price = valid_quote.mid if valid_quote else fallback_price
    quality_flags = []
    if len(sessions) < min_opening_days:
        quality_flags.append(f"opening_days {len(sessions)} < {min_opening_days}")
    price_penalty = 0.0
    if last_price < min_price or last_price > max_price:
        quality_flags.append(f"price {last_price:.2f} outside {min_price:.2f}-{max_price:.2f}")
        price_penalty = 1.5

    daily_context = daily_context_metrics(
        daily_bars,
        last_price,
        trend_lookback_days=trend_lookback_days,
        min_trend_bps=min_trend_bps,
        min_reversal_bps=min_reversal_bps,
    )
    daily_context_penalty = 0.0
    if require_daily_context and daily_context["daily_context"] not in {"uptrend", "bottom_reversal"}:
        quality_flags.append(f"weak daily context: {daily_context['daily_context']}")
        daily_context_penalty = 1.5

    dollar_volumes = [session["dollar_volume"] for session in sessions if session["dollar_volume"] > 0]
    median_opening_dollar_volume = median(dollar_volumes) if dollar_volumes else 0.0
    liquidity_shortfall_penalty = 0.0
    if median_opening_dollar_volume < min_opening_dollar_volume:
        quality_flags.append(
            f"opening dollar volume {median_opening_dollar_volume:.0f} < {min_opening_dollar_volume:.0f}"
        )
        liquidity_shortfall_penalty = min(
            (min_opening_dollar_volume - median_opening_dollar_volume) / max(min_opening_dollar_volume, 1.0),
            1.0,
        ) * 1.5

    high_moves = [session["high_move_bps"] for session in sessions]
    close_moves = [session["close_move_bps"] for session in sessions]
    opening_ranges = [session["opening_range_bps"] for session in sessions]
    median_high_move_bps = median(high_moves) if high_moves else 0.0
    median_close_move_bps = median(close_moves) if close_moves else 0.0
    median_opening_range_bps = median(opening_ranges) if opening_ranges else 0.0
    opening_range_shortfall_penalty = 0.0
    if median_opening_range_bps < min_opening_range_bps:
        quality_flags.append(
            f"opening range {median_opening_range_bps:.1f} bps < {min_opening_range_bps:.1f} bps"
        )
        opening_range_shortfall_penalty = min(
            (min_opening_range_bps - median_opening_range_bps) / max(min_opening_range_bps, 1.0),
            1.0,
        )

    impulse_day_ratio = sum(move >= min_impulse_bps for move in high_moves) / len(high_moves) if high_moves else 0.0
    range_day_ratio = (
        sum(move >= min_opening_range_bps for move in opening_ranges) / len(opening_ranges) if opening_ranges else 0.0
    )
    positive_close_day_ratio = sum(move > 0 for move in close_moves) / len(close_moves) if close_moves else 0.0
    close_capture_ratio = median_close_move_bps / median_high_move_bps if median_high_move_bps > 0 else 0.0
    fade_bps = max(0.0, median_high_move_bps - median_close_move_bps)
    if median_close_move_bps < min_median_opening_close_bps:
        quality_flags.append(
            f"median_opening_close_move_bps {median_close_move_bps:.1f} < {min_median_opening_close_bps:.1f}"
        )
    if close_capture_ratio < min_close_capture_ratio:
        quality_flags.append(f"close_capture_ratio {close_capture_ratio:.3f} < {min_close_capture_ratio:.3f}")
    if positive_close_day_ratio < min_positive_close_day_ratio:
        quality_flags.append(
            f"positive_close_day_ratio {positive_close_day_ratio:.3f} < {min_positive_close_day_ratio:.3f}"
        )

    spread_bps = valid_quote.spread_bps if valid_quote else max_spread_bps
    spread_penalty = 0.0
    if spread_bps > max_spread_bps:
        quality_flags.append(f"spread {spread_bps:.1f} bps > {max_spread_bps:.1f} bps")
        spread_penalty = min(spread_bps / max(max_spread_bps, 1.0), 4.0) - 1.0

    quote_size = min(valid_quote.bid_size, valid_quote.ask_size) if valid_quote else 0

    liquidity_score = min(math.log10(median_opening_dollar_volume / min_opening_dollar_volume + 1), 3.0)
    spread_score = max(0.0, 1.0 - (spread_bps / max_spread_bps))
    movement_score = min(max(median_high_move_bps, 0.0) / max(min_impulse_bps, 1.0), 3.0)
    range_score = min(median_opening_range_bps / max(min_opening_range_bps, 1.0), 2.0)
    follow_through_score = max(-1.0, min(close_capture_ratio, 1.5))
    fade_penalty = min(fade_bps / max(min_opening_range_bps, 1.0), 2.0)
    consistency_score = impulse_day_ratio * 2.0
    size_score = min(quote_size / 100.0, 1.0)
    pattern_score = 0.0
    if daily_context["daily_trend_bps"] < -300:
        pattern_score += 3.0
    if daily_context["daily_trend_bps"] > 300:
        pattern_score += 3.0
    if daily_context["rebound_from_low_bps"] > 200:
        pattern_score += 2.0
    pattern_score += daily_gap_score(daily_bars)
    pattern_score += recent_compression_score(daily_bars)
    if median_high_move_bps > 80 and median_opening_range_bps > 50:
        pattern_score += 2.0
    follow_through_shortfall_penalty = 0.0
    if median_close_move_bps < min_median_opening_close_bps:
        follow_through_shortfall_penalty += min(
            (min_median_opening_close_bps - median_close_move_bps) / max(min_opening_range_bps, 1.0),
            1.0,
        )
    if close_capture_ratio < min_close_capture_ratio:
        follow_through_shortfall_penalty += min(
            (min_close_capture_ratio - close_capture_ratio) / max(abs(min_close_capture_ratio), 0.1),
            1.0,
        )
    if positive_close_day_ratio < min_positive_close_day_ratio:
        follow_through_shortfall_penalty += min(
            (min_positive_close_day_ratio - positive_close_day_ratio) / max(min_positive_close_day_ratio, 0.1),
            1.0,
        )
    score = (
        (liquidity_score * 3.0)
        + (spread_score * 2.0)
        + (movement_score * 2.0)
        + (range_score * 2.0)
        + (daily_context["daily_context_score"] * 1.5)
        + (follow_through_score * 1.5)
        + consistency_score
        + size_score
        + (pattern_score * 3.0)
        - fade_penalty
        - price_penalty
        - liquidity_shortfall_penalty
        - daily_context_penalty
        - opening_range_shortfall_penalty
        - follow_through_shortfall_penalty
        - spread_penalty
    )

    return {
        "symbol": symbol,
        "score": round(score, 3),
        "price": round(last_price, 2),
        "spread_bps": round(spread_bps, 2),
        "opening_days": len(sessions),
        "median_opening_dollar_volume": round(median_opening_dollar_volume, 2),
        "median_opening_high_move_bps": round(median_high_move_bps, 2),
        "median_opening_close_move_bps": round(median_close_move_bps, 2),
        "median_opening_range_bps": round(median_opening_range_bps, 2),
        "impulse_day_ratio": round(impulse_day_ratio, 3),
        "range_day_ratio": round(range_day_ratio, 3),
        "positive_close_day_ratio": round(positive_close_day_ratio, 3),
        "close_capture_ratio": round(close_capture_ratio, 3),
        "fade_bps": round(fade_bps, 2),
        "daily_context": daily_context["daily_context"],
        "daily_trend_bps": round(daily_context["daily_trend_bps"], 2),
        "rebound_from_low_bps": round(daily_context["rebound_from_low_bps"], 2),
        "distance_from_high_bps": round(daily_context["distance_from_high_bps"], 2),
        "quote_size": quote_size,
        "pattern_score": round(pattern_score, 3),
        "quality_flags": quality_flags,
    }


def get_opening_bars(
    settings: Settings,
    symbols: list[str],
    session_dates: list[date],
    opening_minutes: int,
) -> dict[str, list[Bar]]:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca_client import make_clients, to_bar

    clients = make_clients(settings)
    start, end = opening_bounds(session_dates, opening_minutes)
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
        results[symbol] = [
            bar
            for bar in bars
            if bar_session_date(bar) in requested_dates and in_opening_window(bar, opening_minutes)
        ]
    return results


def get_daily_bars(
    settings: Settings,
    symbols: list[str],
    session_dates: list[date],
    trend_lookback_days: int,
) -> dict[str, list[Bar]]:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca_client import make_clients, to_bar

    clients = make_clients(settings)
    start_date = session_dates[0] - timedelta(days=trend_lookback_days + 5)
    end_date = session_dates[-1] + timedelta(days=1)
    start = datetime.combine(start_date, time.min, tzinfo=MARKET_TZ).astimezone(timezone.utc)
    end = datetime.combine(end_date, time.min, tzinfo=MARKET_TZ).astimezone(timezone.utc)
    results: dict[str, list[Bar]] = {}

    for symbol in symbols:
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed=clients.feed,
        )
        response = clients.historical.get_stock_bars(request)
        results[symbol] = [to_bar(item) for item in response.data.get(symbol, [])]
    return results


def screen(args: argparse.Namespace) -> dict:
    from alpaca_client import get_latest_quotes

    if args.days < 1:
        raise ValueError("--days must be at least 1.")
    if args.opening_minutes < 1:
        raise ValueError("--opening-minutes must be at least 1.")
    if args.min_opening_days < 1:
        raise ValueError("--min-opening-days must be at least 1.")
    if args.trend_lookback_days < 2:
        raise ValueError("--trend-lookback-days must be at least 2.")

    settings_kwargs = {}
    if args.alpaca_api_key:
        settings_kwargs["alpaca_api_key"] = args.alpaca_api_key
    if args.alpaca_secret_key:
        settings_kwargs["alpaca_secret_key"] = args.alpaca_secret_key

    settings = load_settings(strategy_names=["opening_impulse"], validate=False)
    if settings_kwargs:
        settings = Settings(**{**settings.__dict__, **settings_kwargs})
    min_opening_range_bps = (
        args.min_opening_range_pct * 10_000
        if args.min_opening_range_pct is not None
        else (settings.target_profit_pct + min(settings.target_profit_pct, args.opening_range_buffer_pct)) * 10_000
    )
    universe = load_universe(args.universe_file, args.symbols)
    as_of = parse_as_of(args.as_of)
    session_dates = previous_session_dates(as_of, args.days)
    bars_by_symbol = get_opening_bars(settings, universe, session_dates, args.opening_minutes)
    daily_bars_by_symbol = get_daily_bars(settings, universe, session_dates, args.trend_lookback_days)
    quotes = get_latest_quotes(settings, universe)

    candidates = []
    for symbol in universe:
        result = score_candidate(
            symbol=symbol,
            bars=bars_by_symbol.get(symbol, []),
            daily_bars=daily_bars_by_symbol.get(symbol, []),
            quote=quotes.get(symbol),
            opening_minutes=args.opening_minutes,
            min_price=args.min_price,
            max_price=args.max_price,
            min_opening_days=args.min_opening_days,
            min_opening_dollar_volume=args.min_opening_dollar_volume,
            min_impulse_bps=args.min_impulse_bps,
            min_opening_range_bps=min_opening_range_bps,
            max_spread_bps=args.max_spread_bps,
            min_close_capture_ratio=args.min_close_capture_ratio,
            min_positive_close_day_ratio=args.min_positive_close_day_ratio,
            min_median_opening_close_bps=args.min_median_opening_close_bps,
            trend_lookback_days=args.trend_lookback_days,
            min_trend_bps=args.min_trend_bps,
            min_reversal_bps=args.min_reversal_bps,
            require_daily_context=not args.no_daily_context_filter,
        )
        if result:
            candidates.append(result)

    candidates.sort(key=lambda item: item["score"], reverse=True)
    selected = candidates[: args.top]
    return {
        "selected_symbols": [item["symbol"] for item in selected],
        "symbols_env_line": format_symbols_env_line([item["symbol"] for item in selected]),
        "candidates": selected,
        "screened": len(universe),
        "session_dates": [value.isoformat() for value in session_dates],
        "opening_window": f"09:30-{(datetime.combine(date.today(), OPEN_TIME) + timedelta(minutes=args.opening_minutes)).time().strftime('%H:%M')} America/New_York",
        "target_profit_pct": settings.target_profit_pct,
        "min_opening_range_pct": round(min_opening_range_bps / 10_000, 4),
        "min_close_capture_ratio": args.min_close_capture_ratio,
        "min_positive_close_day_ratio": args.min_positive_close_day_ratio,
        "min_median_opening_close_bps": args.min_median_opening_close_bps,
        "daily_context_filter": not args.no_daily_context_filter,
        "as_of": as_of.isoformat(),
    }


def ranked_opening_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = []
    for candidate in candidates:
        symbol = str(candidate.get("symbol", "")).upper()
        if not symbol:
            continue
        ranked.append(
            {
                "symbol": symbol,
                "score": round(float(candidate.get("score", 0.0) or 0.0), 3),
                "notes": list(candidate.get("quality_flags") or []),
            }
        )
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked


def deterministic_opening_plan(screen_result: dict[str, Any], limit: int) -> dict[str, Any]:
    ranked = ranked_opening_candidates(list(screen_result.get("candidates") or []))
    selected = [item["symbol"] for item in ranked[:limit]]
    settings = {
        "MAX_OPEN_POSITIONS": 1 if len(selected) <= 2 else 2,
    }
    return {
        "date": str(screen_result.get("as_of", date.today().isoformat()))[:10],
        "strategy": "opening_impulse",
        "symbols": selected,
        "ranked": ranked[:limit],
        "rejected": [],
        "settings": settings,
        "risk_note": "Deterministic opening_impulse selection.",
    }


def _bounded_ai_delta(raw_value: Any) -> float:
    return round(max(-2.0, min(2.0, float(raw_value))), 3)


def ai_opening_plan(settings: Settings, screen_result: dict[str, Any], universe_symbols: list[str], limit: int) -> dict[str, Any] | None:
    payload = {
        "strategy": "opening_impulse",
        "selection_rules": {
            "focus": "opening momentum and follow-through",
            "entry_window": "09:30-10:00 America/New_York",
            "style": "rank, do not guess hidden data",
            "must_choose_from_candidates": True,
        },
        "screen": screen_result,
        "universe_symbols": universe_symbols,
        "limit": limit,
        "allowed_settings": {
            "MAX_OPEN_POSITIONS": [0, settings.max_open_positions],
            "MAX_POSITION_VALUE": [0, settings.max_position_value],
            "TARGET_PROFIT_PCT": [0.003, 0.02],
            "STOP_LOSS_PCT": [0.002, settings.stop_loss_pct],
        },
    }
    response_text = request_json_response(
        settings,
        (
            "Review the opening_impulse candidates and return only JSON. "
            "Choose only from screen.candidates. Do not invent symbols. "
            "Include keys: date, strategy, adjustments, rejected, settings, risk_note. "
            "adjustments must be an object keyed by symbol. Each value may include ai_score_delta and ai_reason. "
            "Keep ai_score_delta bounded between -2.0 and 2.0, and use 0 when no adjustment is needed. "
            "Use the provided candidate metrics and notes to prefer liquid names with better opening follow-through. "
            "Keep the result conservative and bounded by allowed_settings."
        ),
        payload,
    )
    if response_text is None:
        return None
    return extract_json_object(response_text)


def validated_opening_plan(plan: dict[str, Any], screen_result: dict[str, Any], limit: int) -> dict[str, Any]:
    candidates = {str(candidate.get("symbol", "")).upper(): candidate for candidate in screen_result.get("candidates") or []}
    fallback_ranked = ranked_opening_candidates(list(screen_result.get("candidates") or []))
    raw_adjustments = plan.get("adjustments") if isinstance(plan.get("adjustments"), dict) else {}
    ranked = []
    for item in fallback_ranked:
        symbol = item["symbol"]
        adjustment = raw_adjustments.get(symbol) or raw_adjustments.get(symbol.lower()) or {}
        if not isinstance(adjustment, dict):
            adjustment = {}
        ai_delta = _bounded_ai_delta(adjustment.get("ai_score_delta", 0.0))
        ai_reason = str(adjustment.get("ai_reason", "")).strip()
        ranked_item = dict(item)
        ranked_item["base_score"] = ranked_item["score"]
        ranked_item["ai_score_delta"] = ai_delta
        ranked_item["score"] = round(ranked_item["base_score"] + ai_delta, 3)
        if ai_reason:
            ranked_item["ai_reason"] = ai_reason
        ranked.append(ranked_item)

    ranked.sort(key=lambda row: row["score"], reverse=True)
    selected = [item["symbol"] for item in ranked[:limit]]
    plan["date"] = str(screen_result.get("as_of", date.today().isoformat()))[:10]
    plan["strategy"] = "opening_impulse"
    plan["symbols"] = selected
    plan["ranked"] = ranked[:limit]
    plan["rejected"] = [item for item in plan.get("rejected", []) if str(item).upper() in candidates]
    plan["risk_note"] = str(plan.get("risk_note") or "Embedded AI ranking over deterministic opening candidates.")
    if not isinstance(plan.get("settings"), dict):
        plan["settings"] = {}
    return plan


def maybe_apply_ai_selection(result: dict[str, Any], args: argparse.Namespace, settings: Settings, universe_symbols: list[str]) -> dict[str, Any]:
    deterministic_plan = deterministic_opening_plan(result, args.top)
    result["selection_plan"] = deterministic_plan
    if args.plan_output:
        args.plan_output.parent.mkdir(parents=True, exist_ok=True)
        args.plan_output.write_text(json.dumps(deterministic_plan, indent=2, sort_keys=True) + "\n")

    if not args.use_ai:
        return result

    plan = ai_opening_plan(settings, result, universe_symbols, args.top)
    if plan is None:
        result["ai_plan"] = None
        result["ai_enabled"] = True
        result["ai_error"] = "OpenAI not configured or client unavailable."
        return result

    validated = validated_opening_plan(plan, result, args.top)
    selected_symbols = validated["symbols"]
    result["selected_symbols"] = selected_symbols
    result["symbols_env_line"] = format_symbols_env_line(selected_symbols)
    result["ai_enabled"] = True
    result["ai_plan"] = validated
    result["selection_plan"] = validated

    if args.plan_output:
        args.plan_output.parent.mkdir(parents=True, exist_ok=True)
        args.plan_output.write_text(json.dumps(validated, indent=2, sort_keys=True) + "\n")

    return result


def parse_args() -> argparse.Namespace:
    parser = selector_argument_parser(
        description=(
            "Pre-session REST screener for opening_impulse candidates. "
            "Uses previous regular-session opening bars and one quote snapshot; "
            "it does not open a live stream."
        )
    )
    parser.add_argument("--symbols", default="", help="Comma-separated universe override.")
    parser.add_argument(
        "--universe-file",
        type=Path,
        default=DEFAULT_UNIVERSE_FILE,
        help="File with comma/newline separated symbols. Defaults to data/opening_universe.txt when present.",
    )
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--days", type=int, default=10, help="Prior weekday sessions to inspect.")
    parser.add_argument("--opening-minutes", type=int, default=30, help="Minutes after 09:30 ET to inspect.")
    parser.add_argument("--min-opening-days", type=int, default=4)
    parser.add_argument("--min-price", type=float, default=10.0)
    parser.add_argument("--max-price", type=float, default=900.0)
    parser.add_argument("--min-opening-dollar-volume", type=float, default=2_000_000.0)
    parser.add_argument("--min-impulse-bps", type=float, default=60.0)
    parser.add_argument(
        "--min-opening-range-pct",
        type=float,
        default=None,
        help="Minimum median opening-window range. Defaults to TARGET_PROFIT_PCT plus a capped cushion.",
    )
    parser.add_argument(
        "--opening-range-buffer-pct",
        type=float,
        default=0.01,
        help="Maximum extra opening range cushion when --min-opening-range-pct is not set.",
    )
    parser.add_argument("--max-spread-bps", type=float, default=8.0)
    parser.add_argument(
        "--min-close-capture-ratio",
        type=float,
        default=0.1,
        help="Preferred median opening close/high capture. Weak values lower the score.",
    )
    parser.add_argument(
        "--min-positive-close-day-ratio",
        type=float,
        default=0.5,
        help="Preferred share of sampled openings that closed above the opening price. Weak values lower the score.",
    )
    parser.add_argument(
        "--min-median-opening-close-bps",
        type=float,
        default=0.0,
        help="Preferred median move from opening price to opening-window close. Weak values lower the score.",
    )
    parser.add_argument("--trend-lookback-days", type=int, default=5)
    parser.add_argument("--min-trend-bps", type=float, default=50.0)
    parser.add_argument("--min-reversal-bps", type=float, default=100.0)
    parser.add_argument(
        "--no-daily-context-filter",
        action="store_true",
        help="Do not require a recent uptrend or bottom-reversal daily context.",
    )
    parser.add_argument("--as-of", default=None, help="ISO datetime/date in New York time for reproducible screens.")
    parser.add_argument("--alpaca-api-key", default=None)
    parser.add_argument("--alpaca-secret-key", default=None)
    parser.add_argument("--use-ai", action="store_true", help="Use OpenAI to refine the final ranked symbol list.")
    parser.add_argument(
        "--plan-output",
        type=Path,
        default=DEFAULT_PLAN_FILE,
        help="Write the strategy plan that main.py can consume directly.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = screen(args)
    settings_kwargs = {}
    if args.alpaca_api_key:
        settings_kwargs["alpaca_api_key"] = args.alpaca_api_key
    if args.alpaca_secret_key:
        settings_kwargs["alpaca_secret_key"] = args.alpaca_secret_key
    settings = load_settings(strategy_names=["opening_impulse"], validate=False)
    if settings_kwargs:
        settings = Settings(**{**settings.__dict__, **settings_kwargs})
    universe = load_universe(args.universe_file, args.symbols)
    result = maybe_apply_ai_selection(result, args, settings, universe)
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
