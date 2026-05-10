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

from ai_client import request_json_response
from alpaca.data.timeframe import TimeFrame

from alpaca_client import get_bars_between, get_latest_quotes, make_clients
from candle import SymbolState
from config import Settings, load_settings
from env_vars import format_symbols_env_line
from market_hours import MARKET_TZ
from opening_plan import default_plan_file_for_strategy


MARKET_OPEN = time(9, 30)
PREMARKET_OPEN = time(4, 0)
PREMARKET_VOLUME_FIXED_WINDOW_MINUTES = 60
VWAP_MIN_DISTANCE_PCT = 0.002
GAP_EXHAUSTION_PCT = 0.08
DEFAULT_UNIVERSE_FILE = Path("data/opening_universe.txt")
DEFAULT_PLAN_FILE = default_plan_file_for_strategy("gap_and_go")
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


def extract_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    stripped = text.lstrip()
    result, _ = decoder.raw_decode(stripped)
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object.")
    return result


@dataclass(frozen=True)
class GapAndGoCandidate:
    symbol: str
    score: float
    gap_pct: float
    premarket_volume_ratio: float
    spread_bps: float
    last_price: float
    prev_close: float
    open_price: float
    premarket_high: float
    has_news: bool
    quality_flags: tuple[str, ...] = ()
    vwap: float | None = None
    vwap_distance_pct: float | None = None
    prev_range_pct: float | None = None
    breakout_pct: float | None = None
    exhaustion_flag: bool = False
    hard_reject: bool = False


@dataclass(frozen=True)
class GapAndGoDecision:
    candidate: GapAndGoCandidate | None
    code: str
    detail: str


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


def latest_valid_quote(state: SymbolState):
    quote = state.quote or (state.quotes[-1] if state.quotes else None)
    if quote is None or quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
        return None
    return quote


def session_date(state: SymbolState):
    timestamp_ms = state.last_event_ms
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=MARKET_TZ).date()


def premarket_bars(state: SymbolState):
    current_session = session_date(state)
    if current_session is None:
        return []
    bars = []
    for bar in state.bars:
        current = datetime.fromtimestamp(bar.start_ms / 1000, tz=MARKET_TZ)
        if current.date() != current_session:
            continue
        if PREMARKET_OPEN <= current.time() < MARKET_OPEN:
            bars.append(bar)
    return bars


def regular_bars(state: SymbolState):
    current_session = session_date(state)
    if current_session is None:
        return []
    bars = []
    for bar in state.bars:
        current = datetime.fromtimestamp(bar.start_ms / 1000, tz=MARKET_TZ)
        if current.date() != current_session:
            continue
        if current.time() >= MARKET_OPEN:
            bars.append(bar)
    return bars


def previous_regular_bars(state: SymbolState):
    current_session = session_date(state)
    if current_session is None:
        return []
    bars = []
    for bar in state.bars:
        current = datetime.fromtimestamp(bar.start_ms / 1000, tz=MARKET_TZ)
        if current.date() >= current_session:
            continue
        if current.time() >= MARKET_OPEN:
            bars.append(bar)
    return bars


def infer_previous_close(state: SymbolState) -> float | None:
    previous_close = None
    current_session = session_date(state)
    if current_session is None:
        return None
    for bar in state.bars:
        current = datetime.fromtimestamp(bar.start_ms / 1000, tz=MARKET_TZ)
        if current.date() < current_session and current.time() >= MARKET_OPEN:
            previous_close = bar.close
    return previous_close


def regular_open_price(state: SymbolState) -> float | None:
    bars = regular_bars(state)
    return bars[0].open if bars else None


def premarket_high_price(state: SymbolState) -> float | None:
    bars = premarket_bars(state)
    return max((bar.high for bar in bars), default=None)


def premarket_vwap(state: SymbolState) -> float | None:
    bars = premarket_bars(state)
    total_volume = sum(bar.volume for bar in bars if bar.volume > 0)
    if total_volume <= 0:
        return None
    total_value = sum(bar.vwap * bar.volume for bar in bars if bar.volume > 0)
    return total_value / total_volume if total_value > 0 else None


def previous_day_range_pct(state: SymbolState, previous_close: float | None = None) -> float | None:
    bars = previous_regular_bars(state)
    if not bars:
        return None
    close = previous_close or bars[-1].close
    if close <= 0:
        return None
    return (max(bar.high for bar in bars) - min(bar.low for bar in bars)) / close


def premarket_volume_ratio(state: SymbolState) -> float:
    premarket = premarket_bars(state)
    if not premarket:
        return 0.0
    total = sum(bar.volume for bar in premarket if bar.volume > 0)
    baseline = [bar.volume for bar in previous_regular_bars(state)[-30:] if bar.volume > 0]
    if not baseline:
        return 0.0
    expected = median(baseline) * PREMARKET_VOLUME_FIXED_WINDOW_MINUTES
    if expected <= 0:
        return 0.0
    return total / expected


def inspect_gap_and_go_candidate(
    state: SymbolState,
    settings: Settings,
    previous_close: float | None = None,
    has_news: bool = False,
) -> GapAndGoDecision:
    quote = latest_valid_quote(state)
    if quote is None:
        return GapAndGoDecision(None, "quote", "invalid or missing latest quote")

    if quote.ask < settings.gap_and_go_min_price:
        return GapAndGoDecision(None, "price", f"price {quote.ask:.2f} < {settings.gap_and_go_min_price:.2f}")

    if quote.spread_bps > settings.gap_and_go_max_spread_bps:
        return GapAndGoDecision(
            None,
            "spread",
            f"spread {quote.spread_bps:.2f}bps > {settings.gap_and_go_max_spread_bps:.2f}bps",
        )

    previous_close = previous_close or infer_previous_close(state)
    if not previous_close:
        return GapAndGoDecision(None, "prev_close", "missing previous close")

    open_price = regular_open_price(state)
    if not open_price:
        return GapAndGoDecision(None, "open", "missing regular session open")

    gap_pct = (open_price - previous_close) / previous_close
    if gap_pct < settings.gap_and_go_min_gap_pct:
        return GapAndGoDecision(None, "gap", f"gap {gap_pct:.3%} < {settings.gap_and_go_min_gap_pct:.3%}")

    high = premarket_high_price(state)
    if not high:
        return GapAndGoDecision(None, "premarket", "missing premarket high")

    volume_ratio = premarket_volume_ratio(state)
    if volume_ratio < settings.gap_and_go_premarket_volume_ratio:
        return GapAndGoDecision(
            None,
            "volume",
            f"premarket volume {volume_ratio:.2f}x < {settings.gap_and_go_premarket_volume_ratio:.2f}x",
        )

    vwap = premarket_vwap(state)
    if not vwap:
        return GapAndGoDecision(None, "vwap", "missing premarket VWAP")
    vwap_distance_pct = (quote.ask - vwap) / vwap if vwap > 0 else 0.0
    if quote.ask <= vwap or vwap_distance_pct < VWAP_MIN_DISTANCE_PCT:
        return GapAndGoDecision(None, "vwap", f"price {quote.ask:.2f} too close to or below VWAP {vwap:.2f}")

    breakout_pct = (quote.ask - high) / high if high > 0 else 0.0
    if breakout_pct <= 0:
        return GapAndGoDecision(None, "breakout", f"price {quote.ask:.2f} below premarket high {high:.2f}")

    score = (gap_pct * 100.0) + volume_ratio + (1.0 if has_news else 0.0)
    candidate = GapAndGoCandidate(
        symbol=state.symbol,
        score=round(score, 4),
        gap_pct=gap_pct,
        premarket_volume_ratio=volume_ratio,
        spread_bps=quote.spread_bps,
        last_price=quote.ask,
        prev_close=previous_close,
        open_price=open_price,
        premarket_high=high,
        has_news=has_news,
        vwap=vwap,
        vwap_distance_pct=vwap_distance_pct,
        prev_range_pct=previous_day_range_pct(state, previous_close),
        breakout_pct=breakout_pct,
        exhaustion_flag=gap_pct > GAP_EXHAUSTION_PCT,
    )
    return GapAndGoDecision(candidate, "accepted", "accepted")


def score_gap_and_go_candidate(
    state: SymbolState,
    settings: Settings,
    previous_close: float | None = None,
    has_news: bool = False,
) -> GapAndGoCandidate | None:
    quote = latest_valid_quote(state)
    if quote is None:
        return None

    previous_close = previous_close or infer_previous_close(state)
    quality_flags: list[str] = []
    if not previous_close or previous_close <= 0:
        previous_close = quote.ask
        quality_flags.append("missing previous close")

    high = premarket_high_price(state)
    if not high or high <= 0:
        high = quote.ask
        quality_flags.append("missing premarket high")

    # This selector runs before the bell, so we rank names using the current
    # premarket quote as a projected open rather than any post-open price.
    projected_open_price = quote.ask
    gap_pct = (projected_open_price - previous_close) / previous_close if previous_close > 0 else 0.0
    volume_ratio = premarket_volume_ratio(state)
    vwap = premarket_vwap(state)
    vwap_distance_pct = (quote.ask - vwap) / vwap if vwap else None
    prev_range_pct = previous_day_range_pct(state, previous_close)
    breakout_pct = ((quote.ask - high) / high) if high > 0 else 0.0
    spread_penalty = max(0.0, (quote.spread_bps / max(settings.gap_and_go_max_spread_bps, 0.1)) - 1.0)
    price_penalty = 0.0
    missing_data_penalty = 0.0
    hard_reject = False

    if quote.ask < settings.gap_and_go_min_price:
        quality_flags.append(f"price {quote.ask:.2f} < {settings.gap_and_go_min_price:.2f}")
        price_penalty = 2.0

    if quote.spread_bps > settings.gap_and_go_max_spread_bps:
        quality_flags.append(
            f"spread {quote.spread_bps:.2f}bps > {settings.gap_and_go_max_spread_bps:.2f}bps"
        )
        hard_reject = True

    if gap_pct < settings.gap_and_go_min_gap_pct:
        quality_flags.append(f"gap {gap_pct:.3%} < {settings.gap_and_go_min_gap_pct:.3%}")
        hard_reject = True

    exhaustion_flag = gap_pct > GAP_EXHAUSTION_PCT
    exhaustion_penalty = 0.0
    if exhaustion_flag:
        quality_flags.append("overextended gap")
        exhaustion_penalty = 2.0

    if prev_range_pct is not None and gap_pct < prev_range_pct:
        quality_flags.append("weak gap vs prior range")

    if volume_ratio < settings.gap_and_go_premarket_volume_ratio:
        quality_flags.append(
            f"premarket volume {volume_ratio:.2f}x < {settings.gap_and_go_premarket_volume_ratio:.2f}x"
        )
        hard_reject = True

    if breakout_pct <= 0:
        quality_flags.append(f"price {quote.ask:.2f} below premarket high {high:.2f}")
        hard_reject = True
        if gap_pct < settings.gap_and_go_min_gap_pct:
            quality_flags.append("failed breakout with small gap")

    if vwap is None:
        quality_flags.append("missing premarket VWAP")
        hard_reject = True
    elif quote.ask <= vwap:
        quality_flags.append("price below VWAP")
        hard_reject = True
    elif vwap_distance_pct is not None and vwap_distance_pct < VWAP_MIN_DISTANCE_PCT:
        quality_flags.append("too close to VWAP")
        hard_reject = True

    if "missing previous close" in quality_flags:
        missing_data_penalty += 3.0
    if "missing premarket high" in quality_flags:
        missing_data_penalty += 2.0

    gap_score = min(gap_pct * 100.0, 5.0)
    volume_score = min(volume_ratio, 6.0)
    breakout_score = min(max(breakout_pct, 0.0) * 200.0, 4.0)
    vwap_score = 0.0
    if vwap_distance_pct is not None and vwap_distance_pct >= VWAP_MIN_DISTANCE_PCT:
        vwap_score = min(vwap_distance_pct * 500.0, 2.0)
    vwap_hold_bonus = 0.0
    if vwap is not None:
        recent_closes = [bar.close for bar in premarket_bars(state)[-5:]]
        if len(recent_closes) >= 3 and all(close > vwap for close in recent_closes[-3:]):
            vwap_hold_bonus = 1.5
    quality_flag_penalty = len(quality_flags) * 0.4
    hard_reject_penalty = 5.0 if hard_reject else 0.0
    score = (
        gap_score
        + volume_score
        + breakout_score
        + vwap_score
        + vwap_hold_bonus
        + (1.0 if has_news else 0.0)
        - min(spread_penalty, 4.0)
        - exhaustion_penalty
        - quality_flag_penalty
        - price_penalty
        - missing_data_penalty
        - hard_reject_penalty
    )

    return GapAndGoCandidate(
        symbol=state.symbol,
        score=round(score, 4),
        gap_pct=gap_pct,
        premarket_volume_ratio=volume_ratio,
        spread_bps=quote.spread_bps,
        last_price=quote.ask,
        prev_close=previous_close,
        open_price=projected_open_price,
        premarket_high=high,
        has_news=has_news,
        quality_flags=tuple(quality_flags),
        vwap=round(vwap, 4) if vwap is not None else None,
        vwap_distance_pct=round(vwap_distance_pct, 5) if vwap_distance_pct is not None else None,
        prev_range_pct=round(prev_range_pct, 5) if prev_range_pct is not None else None,
        breakout_pct=round(breakout_pct, 5),
        exhaustion_flag=exhaustion_flag,
        hard_reject=hard_reject,
    )


def penalty_gap_and_go_candidate(
    symbol: str,
    reason: str,
    previous_close: float = 0.0,
    has_news: bool = False,
) -> GapAndGoCandidate:
    return GapAndGoCandidate(
        symbol=symbol,
        score=-999.0,
        gap_pct=0.0,
        premarket_volume_ratio=0.0,
        spread_bps=0.0,
        last_price=0.0,
        prev_close=previous_close,
        open_price=0.0,
        premarket_high=0.0,
        has_news=has_news,
        quality_flags=(reason,),
        hard_reject=True,
    )


def rank_gap_and_go_candidates(
    states: dict[str, SymbolState],
    settings: Settings,
    previous_closes: dict[str, float] | None = None,
    news_flags: dict[str, bool] | None = None,
    top_n: int = 5,
) -> list[GapAndGoCandidate]:
    previous_closes = previous_closes or {}
    news_flags = news_flags or {}
    ranked = []
    for symbol, state in states.items():
        candidate = score_gap_and_go_candidate(
            state,
            settings,
            previous_close=previous_closes.get(symbol),
            has_news=news_flags.get(symbol, False),
        )
        if candidate is None:
            candidate = penalty_gap_and_go_candidate(
                symbol,
                "insufficient quote or market data",
                previous_close=previous_closes.get(symbol, 0.0),
                has_news=news_flags.get(symbol, False),
            )
        ranked.append(candidate)
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked[:top_n]


def ai_gap_and_go_selection(settings: Settings, candidates: list[GapAndGoCandidate], limit: int) -> dict[str, Any] | None:
    payload = {
        "strategy": "gap_and_go",
        "selection_rules": {
            "focus": "strong upward gap continuation after open",
            "must_choose_from_candidates": True,
            "prefer": [
                "larger positive gap",
                "stronger premarket volume ratio",
                "tighter spread",
                "cleaner breakout candidates",
            ],
        },
        "candidates": [asdict(candidate) for candidate in candidates],
        "limit": limit,
    }
    response_text = request_json_response(
        settings,
        (
            "Review the gap_and_go candidates and return only JSON. "
            "Choose only from candidates. Do not invent symbols. "
            "Include keys: strategy, adjustments, rejected, risk_note. "
            "adjustments must be an object keyed by symbol. Each value may include ai_score_delta and ai_reason. "
            "Keep ai_score_delta bounded between -2.0 and 2.0, and use 0 when no adjustment is needed."
        ),
        payload,
    )
    if response_text is None:
        return None
    return extract_json_object(response_text)


def validated_gap_and_go_selection(plan: dict[str, Any], candidates: list[GapAndGoCandidate], limit: int) -> dict[str, Any]:
    available = {candidate.symbol: candidate for candidate in candidates}
    fallback_ranked = [asdict(candidate) for candidate in candidates]
    raw_adjustments = plan.get("adjustments") if isinstance(plan.get("adjustments"), dict) else {}
    ranked = []
    for item in fallback_ranked:
        symbol = item["symbol"]
        adjustment = raw_adjustments.get(symbol) or raw_adjustments.get(symbol.lower()) or {}
        if not isinstance(adjustment, dict):
            adjustment = {}
        ai_delta = max(-2.0, min(2.0, float(adjustment.get("ai_score_delta", 0.0))))
        ai_reason = str(adjustment.get("ai_reason", "")).strip()
        ranked_item = dict(item)
        ranked_item["base_score"] = ranked_item["score"]
        ranked_item["ai_score_delta"] = round(ai_delta, 3)
        ranked_item["score"] = round(ranked_item["base_score"] + ranked_item["ai_score_delta"], 3)
        if ai_reason:
            ranked_item["ai_reason"] = ai_reason
        ranked.append(ranked_item)

    ranked.sort(key=lambda row: row["score"], reverse=True)
    selected = [item["symbol"] for item in ranked[:limit]]
    return {
        "strategy": "gap_and_go",
        "symbols": selected,
        "ranked": ranked[:limit],
        "rejected": [item for item in plan.get("rejected", []) if str(item).upper() in available],
        "risk_note": str(plan.get("risk_note") or "Embedded AI ranking over deterministic gap-and-go candidates."),
    }


def deterministic_gap_and_go_plan(candidates: list[GapAndGoCandidate], limit: int) -> dict[str, Any]:
    selected = [candidate.symbol for candidate in candidates[:limit]]
    return {
        "strategy": "gap_and_go",
        "selection_stage": "pre_market",
        "symbols": selected,
        "ranked": [asdict(candidate) for candidate in candidates[:limit]],
        "rejected": [],
        "settings": {},
        "risk_note": "Deterministic pre-market gap_and_go selection using only previous-day and premarket data.",
    }


def load_states(
    settings: Settings,
    symbols: list[str],
    now: datetime | None = None,
) -> tuple[dict[str, SymbolState], dict[str, float]]:
    clients = make_clients(settings)
    now = now.astimezone(MARKET_TZ) if now else datetime.now(tz=MARKET_TZ)
    previous_start = datetime.combine((now - timedelta(days=5)).date(), time.min, tzinfo=MARKET_TZ)

    start_of_day = datetime.combine(now.date(), PREMARKET_OPEN, tzinfo=MARKET_TZ)
    intraday_bars = get_bars_between(clients, symbols, TimeFrame.Minute, start_of_day, now)
    daily_bars = get_bars_between(clients, symbols, TimeFrame.Day, previous_start, now + timedelta(days=1))
    quotes = get_latest_quotes(settings, symbols)

    states = {symbol: SymbolState(symbol) for symbol in symbols}
    previous_closes = {}
    for symbol in symbols:
        state = states[symbol]
        for bar in intraday_bars.get(symbol, []):
            state.add_bar(bar)
        quote = quotes.get(symbol)
        if quote is not None:
            state.update_quote(quote)

        previous_close = None
        for bar in daily_bars.get(symbol, []):
            bar_date = datetime.fromtimestamp(bar.start_ms / 1000, tz=MARKET_TZ).date()
            if bar_date < now.date():
                previous_close = float(bar.close)
        if previous_close and previous_close > 0:
            previous_closes[symbol] = previous_close

    return states, previous_closes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rank pre-market gap-and-go candidates from the current universe."
    )
    parser.add_argument("--symbols", default="", help="Comma-separated symbols. Overrides --universe-file when set.")
    parser.add_argument(
        "--universe-file",
        type=Path,
        default=DEFAULT_UNIVERSE_FILE,
        help="File with comma/newline separated symbols. Defaults to data/opening_universe.txt when present.",
    )
    parser.add_argument("--top", type=int, default=5, help="Maximum number of ranked symbols to return.")
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
    symbols = load_universe(args.universe_file, args.symbols)
    settings = load_settings(strategy_names=["gap_and_go"], validate=False)
    states, previous_closes = load_states(settings, symbols)
    candidates = rank_gap_and_go_candidates(states, settings, previous_closes=previous_closes, top_n=args.top)
    if not candidates:
        candidates = [
            penalty_gap_and_go_candidate(symbol, "universe symbol could not be ranked")
            for symbol in symbols[: args.top]
        ]
    deterministic_plan = deterministic_gap_and_go_plan(candidates, args.top)
    result: dict[str, Any] = {
        "strategy": "gap_and_go",
        "selection_stage": "pre_market",
        "selected_symbols": [candidate.symbol for candidate in candidates],
        "symbols_env_line": format_symbols_env_line([candidate.symbol for candidate in candidates]),
        "candidates": [asdict(candidate) for candidate in candidates],
        "selection_plan": deterministic_plan,
        "ai_enabled": args.use_ai,
    }
    if args.plan_output:
        args.plan_output.parent.mkdir(parents=True, exist_ok=True)
        args.plan_output.write_text(json.dumps(deterministic_plan, indent=2, sort_keys=True) + "\n")
    if args.use_ai:
        plan = ai_gap_and_go_selection(settings, candidates, args.top)
        if plan is None:
            result["ai_selection"] = None
            result["ai_error"] = "OpenAI not configured or client unavailable."
        else:
            validated = validated_gap_and_go_selection(plan, candidates, args.top)
            result["ai_selection"] = validated
            result["selection_plan"] = validated
            result["selected_symbols"] = validated["symbols"]
            result["symbols_env_line"] = format_symbols_env_line(validated["symbols"])
            if args.plan_output:
                args.plan_output.parent.mkdir(parents=True, exist_ok=True)
                args.plan_output.write_text(json.dumps(validated, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
