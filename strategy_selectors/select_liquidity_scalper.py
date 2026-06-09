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

from ai_client import request_json_response
from config import Settings, load_settings
from env_vars import format_symbols_env_line
from models import Bar, Quote
from opening_plan import default_plan_file_for_strategy


MARKET_TZ = ZoneInfo("America/New_York")
PREMARKET_OPEN = time(4, 0)
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
DEFAULT_UNIVERSE_FILE = Path("data/opening_universe.txt")
DEFAULT_PLAN_FILE = default_plan_file_for_strategy("liquidity_scalper")
# Basic IEX allows 30 trade+quote channels (~15 symbols with liquidity_scalper).
DEFAULT_STREAM_SYMBOL_LIMIT = 15
DEFAULT_TOP = 12
DEFAULT_DAYS = 3
MIN_SESSION_BARS = 30
AI_SCORE_DELTA_LIMIT = 2.0
# Selector thresholds are softer than live strategy gates; runtime still uses LIQUIDITY_SCALPER_* env.
DEFAULT_SELECTOR_MIN_BAR_DOLLAR_VOLUME = 50_000.0
DEFAULT_SELECTOR_MIN_SESSION_DOLLAR_VOLUME = 50_000.0
DEFAULT_SELECTOR_MIN_RANGE_PCT = 0.010
DEFAULT_SELECTOR_MAX_SPREAD_BPS = 100.0


def extract_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    stripped = text.lstrip()
    result, _ = decoder.raw_decode(stripped)
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object.")
    return result


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
    premarket_dollar_volume: float
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


def bar_timestamp(bar: Bar) -> datetime:
    return datetime.fromtimestamp(bar.start_ms / 1000, tz=timezone.utc).astimezone(MARKET_TZ)


def bar_session_date(bar: Bar) -> date:
    return bar_timestamp(bar).date()


def in_regular_session(bar: Bar) -> bool:
    timestamp = bar_timestamp(bar)
    if timestamp.weekday() >= 5:
        return False
    open_at = datetime.combine(timestamp.date(), MARKET_OPEN, tzinfo=MARKET_TZ)
    close_at = datetime.combine(timestamp.date(), MARKET_CLOSE, tzinfo=MARKET_TZ)
    return open_at <= timestamp < close_at


def in_premarket_session(bar: Bar, session_date: date) -> bool:
    timestamp = bar_timestamp(bar)
    if timestamp.date() != session_date or timestamp.weekday() >= 5:
        return False
    return PREMARKET_OPEN <= timestamp.time() < MARKET_OPEN


def usable_quote(quote: Quote | None) -> Quote | None:
    if quote is None:
        return None
    if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
        return None
    return quote


def selector_price_context(
    quote: Quote | None,
    fallback_bars: list[Bar],
) -> tuple[float, float | None, int, list[str]]:
    """Premarket-friendly price/spread like opening_impulse: bar fallback when quote is one-sided."""
    quality_flags: list[str] = []
    valid_quote = usable_quote(quote)
    if valid_quote is not None:
        return (
            valid_quote.mid,
            valid_quote.spread_bps,
            int(valid_quote.bid_size + valid_quote.ask_size),
            quality_flags,
        )

    if quote is not None and quote.bid > 0 and quote.ask <= 0:
        quality_flags.append("one-sided premarket quote (bid only)")
        return quote.bid, None, int(quote.bid_size), quality_flags
    if quote is not None and quote.ask > 0 and quote.bid <= 0:
        quality_flags.append("one-sided premarket quote (ask only)")
        return quote.ask, None, int(quote.ask_size), quality_flags

    ordered = sorted(fallback_bars, key=lambda item: item.start_ms)
    if ordered and ordered[-1].close > 0:
        quality_flags.append("missing two-sided quote; using last bar close")
        return ordered[-1].close, None, 0, quality_flags

    return 0.0, None, 0, ["no quote or bar price"]


def premarket_dollar_volume(bars: list[Bar], session_date: date) -> float:
    total = 0.0
    for bar in bars:
        if not in_premarket_session(bar, session_date):
            continue
        if bar.close > 0 and bar.volume > 0:
            total += bar.close * bar.volume
    return total


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
    *,
    premarket_bars: list[Bar],
    as_of: datetime,
    min_price: float,
    max_price: float,
    min_session_days: int,
    min_session_dollar_volume: float,
    min_bar_dollar_volume: float,
    min_range_pct: float,
    max_spread_bps: float,
) -> LiquidityScalperCandidate | None:
    fallback_bars = sorted(premarket_bars, key=lambda item: item.start_ms)
    price, spread_bps, quote_size, quote_flags = selector_price_context(quote, fallback_bars)
    if price <= 0:
        return None

    quality_flags = list(quote_flags)
    hard_reject = False

    if price < min_price:
        quality_flags.append(f"price {price:.2f} < {min_price:.2f}")
        hard_reject = True
    if price > max_price:
        quality_flags.append(f"price {price:.2f} > {max_price:.2f}")
        hard_reject = True
    if spread_bps is not None and spread_bps > max_spread_bps:
        quality_flags.append(f"spread {spread_bps:.2f}bps > {max_spread_bps:.2f}bps")
        hard_reject = True

    if len(session_rows) < min_session_days:
        quality_flags.append(f"session history {len(session_rows)} < {min_session_days}")

    median_session_dv = median(row["session_dollar_volume"] for row in session_rows) if session_rows else 0.0
    median_range_pct = median(row["range_pct"] for row in session_rows) if session_rows else 0.0
    median_bar_dv = median(row["median_bar_dollar_volume"] for row in session_rows) if session_rows else 0.0
    p75_bar_dv = median(row["p75_bar_dollar_volume"] for row in session_rows) if session_rows else 0.0
    median_minute_dv = median(row["median_minute_dollar_volume"] for row in session_rows) if session_rows else 0.0
    today_premarket_dv = premarket_dollar_volume(premarket_bars, as_of.date())

    liquidity_penalty = 0.0
    if median_session_dv < min_session_dollar_volume:
        quality_flags.append(
            f"median session_dv ${median_session_dv:,.0f} < ${min_session_dollar_volume:,.0f}"
        )
        liquidity_penalty += min(
            (min_session_dollar_volume - median_session_dv) / max(min_session_dollar_volume, 1.0),
            1.0,
        ) * 2.0
    if median_range_pct < min_range_pct:
        quality_flags.append(f"median range {median_range_pct:.2%} < {min_range_pct:.2%}")
        liquidity_penalty += min((min_range_pct - median_range_pct) / max(min_range_pct, 1e-6), 1.0) * 1.5
    if median_bar_dv < min_bar_dollar_volume:
        quality_flags.append(f"median bar_dv ${median_bar_dv:,.0f} < ${min_bar_dollar_volume:,.0f}")
        liquidity_penalty += min(
            (min_bar_dollar_volume - median_bar_dv) / max(min_bar_dollar_volume, 1.0),
            1.0,
        ) * 2.0
    if len(session_rows) < min_session_days:
        liquidity_penalty += 1.0

    liquidity_score = math.log10(max(median_session_dv, 1.0) / 1_000_000.0) * 4.0
    range_score = median_range_pct * 200.0
    tape_proxy = math.log10(max(p75_bar_dv, 1.0) / 1_000.0) * 2.5
    premarket_score = min(math.log10(max(today_premarket_dv, 1.0) / 100_000.0), 2.5)
    spread_penalty = (spread_bps / max(max_spread_bps, 0.1)) if spread_bps is not None else 0.5
    quote_depth_bonus = min(quote_size / 200.0, 2.0)
    reject_penalty = 8.0 if hard_reject else 0.0
    score = (
        liquidity_score
        + range_score
        + tape_proxy
        + premarket_score
        + quote_depth_bonus
        - spread_penalty
        - liquidity_penalty
        - reject_penalty
    )

    return LiquidityScalperCandidate(
        symbol=symbol,
        score=round(score, 3),
        price=round(price, 2),
        spread_bps=round(spread_bps, 2) if spread_bps is not None else None,
        session_days=len(session_rows),
        median_session_dollar_volume=round(median_session_dv, 2),
        median_range_pct=round(median_range_pct, 5),
        median_bar_dollar_volume=round(median_bar_dv, 2),
        p75_bar_dollar_volume=round(p75_bar_dv, 2),
        median_minute_dollar_volume=round(median_minute_dv, 2),
        premarket_dollar_volume=round(today_premarket_dv, 2),
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
        "premarket_dollar_volume": candidate.premarket_dollar_volume,
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


def get_premarket_bars(
    settings: Settings,
    symbols: list[str],
    as_of: datetime,
) -> dict[str, list[Bar]]:
    from alpaca.data.timeframe import TimeFrame
    from alpaca_client import get_bars_between, make_clients

    as_of = as_of.astimezone(MARKET_TZ)
    start = datetime.combine(as_of.date(), PREMARKET_OPEN, tzinfo=MARKET_TZ)
    if as_of <= start:
        return {symbol: [] for symbol in symbols}

    clients = make_clients(settings)
    raw = get_bars_between(clients, symbols, TimeFrame.Minute, start, as_of)
    session_date = as_of.date()
    return {
        symbol: [bar for bar in bars if in_premarket_session(bar, session_date)]
        for symbol, bars in raw.items()
    }


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
    premarket_by_symbol = get_premarket_bars(settings, universe, as_of)
    quotes = get_latest_quotes(settings, universe)
    session_date_set = set(session_dates)
    top_limit = effective_top_limit(args.top, args.stream_symbol_limit)
    max_spread_bps = args.max_spread_bps

    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for symbol in universe:
        session_rows = regular_session_metrics_by_day(bars_by_symbol.get(symbol, []), session_date_set)
        candidate = score_liquidity_scalper_candidate(
            symbol,
            session_rows,
            quotes.get(symbol),
            premarket_bars=premarket_by_symbol.get(symbol, []),
            as_of=as_of,
            min_price=args.min_price,
            max_price=args.max_price,
            min_session_days=args.min_session_days,
            min_session_dollar_volume=args.min_session_dollar_volume,
            min_bar_dollar_volume=args.min_bar_dollar_volume,
            min_range_pct=args.min_range_pct,
            max_spread_bps=max_spread_bps,
        )
        if candidate is None:
            rejected.append({"symbol": symbol, "code": "quote", "detail": "no quote or bar price"})
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
        "selection_stage": "pre_market",
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
            "min_session_dollar_volume": args.min_session_dollar_volume,
            "min_bar_dollar_volume": args.min_bar_dollar_volume,
            "min_range_pct": args.min_range_pct,
            "max_spread_bps": max_spread_bps,
            "runtime_strategy_thresholds": {
                "min_session_dollar_volume": settings.liquidity_scalper_min_session_dollar_volume,
                "min_bar_dollar_volume": settings.liquidity_scalper_min_bar_dollar_volume,
                "min_range_pct": settings.liquidity_scalper_min_range_pct,
            },
        },
    }


def deterministic_plan(screen_result: dict[str, Any], limit: int) -> dict[str, Any]:
    ranked = ranked_liquidity_scalper_candidates(list(screen_result.get("candidates") or []))
    selected = [item["symbol"] for item in ranked[:limit]]
    return {
        "date": str(screen_result.get("as_of", date.today().isoformat()))[:10],
        "strategy": "liquidity_scalper",
        "selection_stage": "pre_market",
        "symbols": selected,
        "ranked": ranked[:limit],
        "rejected": list(screen_result.get("rejected") or []),
        "settings": {
            "ALPACA_MARKET_DATA_MODE": "stream",
        },
        "risk_note": (
            "Pre-market liquidity_scalper selection from prior regular sessions, today's premarket "
            "activity, and a quote/bar price snapshot. Selector thresholds are softer than live runtime "
            "gates. Keep symbol count within the Alpaca Basic IEX trade+quote channel budget (~15 symbols)."
        ),
    }


def ranked_liquidity_scalper_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for row in candidates:
        symbol = str(row.get("symbol", "")).upper()
        if not symbol:
            continue
        ranked.append(
            {
                "symbol": symbol,
                "score": round(float(row.get("score", 0.0) or 0.0), 3),
                "notes": list(row.get("quality_flags") or []),
                "median_session_dollar_volume": row.get("median_session_dollar_volume"),
                "median_range_pct": row.get("median_range_pct"),
                "p75_bar_dollar_volume": row.get("p75_bar_dollar_volume"),
                "premarket_dollar_volume": row.get("premarket_dollar_volume"),
                "spread_bps": row.get("spread_bps"),
            }
        )
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked


def _bounded_ai_delta(raw_value: Any) -> float:
    return round(max(-AI_SCORE_DELTA_LIMIT, min(AI_SCORE_DELTA_LIMIT, float(raw_value))), 3)


def ai_liquidity_scalper_selection(
    settings: Settings,
    screen_result: dict[str, Any],
    universe_symbols: list[str],
    limit: int,
) -> dict[str, Any] | None:
    payload = {
        "strategy": "liquidity_scalper",
        "selection_rules": {
            "focus": "ultra-short stream scalping with live trade ticks and tight spreads",
            "style": "rank only from screened candidates; do not invent symbols",
            "must_choose_from_candidates": True,
            "selection_stage": "pre_market",
            "stream_symbol_limit": screen_result.get("stream_symbol_limit"),
            "effective_top": screen_result.get("effective_top"),
        },
        "screen": screen_result,
        "universe_symbols": universe_symbols,
        "limit": limit,
        "allowed_settings": {
            "ALPACA_MARKET_DATA_MODE": ["stream"],
            "MAX_OPEN_POSITIONS": [0, settings.max_open_positions],
            "MAX_POSITION_VALUE": [0, settings.max_position_value],
        },
    }
    response_text = request_json_response(
        settings,
        (
            "Review the liquidity_scalper candidates and return only JSON. "
            "Choose only from screen.candidates. Do not invent symbols. "
            "Include keys: date, strategy, adjustments, rejected, settings, risk_note. "
            "adjustments must be an object keyed by symbol. Each value may include ai_score_delta and ai_reason. "
            f"Keep ai_score_delta bounded between -{AI_SCORE_DELTA_LIMIT:.1f} and {AI_SCORE_DELTA_LIMIT:.1f}, "
            "and use 0 when no adjustment is needed. "
            "Prefer names with high session dollar volume, strong minute-bar liquidity, "
            "healthy intraday range, and tight spreads suitable for tape scalping. "
            "Respect the stream symbol limit and keep the final list conservative."
        ),
        payload,
    )
    if response_text is None:
        return None
    return extract_json_object(response_text)


def validated_liquidity_scalper_selection(
    plan: dict[str, Any],
    screen_result: dict[str, Any],
    limit: int,
) -> dict[str, Any]:
    candidates = {
        str(candidate.get("symbol", "")).upper(): candidate
        for candidate in screen_result.get("candidates") or []
    }
    fallback_ranked = ranked_liquidity_scalper_candidates(list(screen_result.get("candidates") or []))
    raw_adjustments = plan.get("adjustments") if isinstance(plan.get("adjustments"), dict) else {}
    ranked: list[dict[str, Any]] = []
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
        ranked_item["score"] = round(float(ranked_item["base_score"]) + ai_delta, 3)
        if ai_reason:
            ranked_item["ai_reason"] = ai_reason
        ranked.append(ranked_item)

    ranked.sort(key=lambda row: row["score"], reverse=True)
    selected = [item["symbol"] for item in ranked[:limit]]
    validated = dict(plan)
    validated["date"] = str(screen_result.get("as_of", date.today().isoformat()))[:10]
    validated["strategy"] = "liquidity_scalper"
    validated["selection_stage"] = "pre_market"
    validated["symbols"] = selected
    validated["ranked"] = ranked[:limit]
    validated["rejected"] = list(screen_result.get("rejected") or [])
    if not isinstance(validated.get("settings"), dict):
        validated["settings"] = {"ALPACA_MARKET_DATA_MODE": "stream"}
    else:
        validated["settings"] = {**{"ALPACA_MARKET_DATA_MODE": "stream"}, **validated["settings"]}
    validated["risk_note"] = str(
        plan.get("risk_note") or "Embedded AI ranking over deterministic liquidity_scalper candidates."
    )
    ai_rejected = [str(item).upper() for item in (plan.get("rejected") or []) if str(item).upper() in candidates]
    if ai_rejected:
        validated["ai_rejected"] = ai_rejected
    return validated


def maybe_apply_ai_selection(
    result: dict[str, Any],
    args: argparse.Namespace,
    settings: Settings,
    universe_symbols: list[str],
    top_limit: int,
) -> dict[str, Any]:
    plan = deterministic_plan(result, top_limit)
    result["selection_plan"] = plan
    result["selected_symbols"] = plan["symbols"]
    result["symbols_env_line"] = format_symbols_env_line(plan["symbols"])
    result["ai_enabled"] = bool(args.use_ai)
    args.plan_output.parent.mkdir(parents=True, exist_ok=True)
    args.plan_output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")

    if not args.use_ai:
        return result

    ai_plan = ai_liquidity_scalper_selection(settings, result, universe_symbols, top_limit)
    if ai_plan is None:
        result["ai_selection"] = None
        result["ai_error"] = "OpenAI not configured or client unavailable."
        return result

    validated = validated_liquidity_scalper_selection(ai_plan, result, top_limit)
    result["ai_selection"] = validated
    result["selection_plan"] = validated
    result["selected_symbols"] = validated["symbols"]
    result["symbols_env_line"] = format_symbols_env_line(validated["symbols"])
    args.plan_output.write_text(json.dumps(validated, indent=2, sort_keys=True) + "\n")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Pre-market REST screener for liquidity_scalper. "
            "Ranks liquid names using prior regular sessions, today's premarket bars, "
            "and a quote/bar price snapshot; it does not open a live stream."
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
    parser.add_argument(
        "--min-session-dollar-volume",
        type=float,
        default=DEFAULT_SELECTOR_MIN_SESSION_DOLLAR_VOLUME,
        help="Selector floor for median prior-session dollar volume (default 50K; runtime gate is higher).",
    )
    parser.add_argument(
        "--min-bar-dollar-volume",
        type=float,
        default=DEFAULT_SELECTOR_MIN_BAR_DOLLAR_VOLUME,
        help="Selector floor for median minute-bar dollar volume (default 50K).",
    )
    parser.add_argument(
        "--min-range-pct",
        type=float,
        default=DEFAULT_SELECTOR_MIN_RANGE_PCT,
        help="Selector floor for median prior-session range (default 1%%).",
    )
    parser.add_argument(
        "--max-spread-bps",
        type=float,
        default=DEFAULT_SELECTOR_MAX_SPREAD_BPS,
        help="Hard reject when a two-sided premarket quote spread exceeds this (default 100 bps).",
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
    parser.add_argument("--use-ai", action="store_true", help="Use OpenAI to refine the final ranked symbol list.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = load_settings(strategy_names=["liquidity_scalper"], validate=False)
    if args.alpaca_api_key or args.alpaca_secret_key:
        overrides: dict[str, Any] = {}
        if args.alpaca_api_key:
            overrides["alpaca_api_key"] = args.alpaca_api_key
        if args.alpaca_secret_key:
            overrides["alpaca_secret_key"] = args.alpaca_secret_key
        settings = Settings(**{**settings.__dict__, **overrides})

    universe = load_universe(args.universe_file, args.symbols)
    result = screen(args)
    top_limit = int(result["effective_top"])
    result = maybe_apply_ai_selection(result, args, settings, universe, top_limit)
    result["plan_output"] = str(args.plan_output)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
