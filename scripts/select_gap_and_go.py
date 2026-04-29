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
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from alpaca_client import get_latest_quotes, make_clients, to_bar
from candle import SymbolState
from config import Settings
from market_hours import MARKET_TZ


MARKET_OPEN = time(9, 30)
PREMARKET_OPEN = time(4, 0)
DEFAULT_UNIVERSE_FILE = Path("data/opening_universe.txt")
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


def premarket_volume_ratio(state: SymbolState) -> float:
    premarket = premarket_bars(state)
    if not premarket:
        return 0.0
    total = sum(bar.volume for bar in premarket if bar.volume > 0)
    baseline = [bar.volume for bar in previous_regular_bars(state)[-30:] if bar.volume > 0]
    if not baseline:
        return 0.0
    expected = median(baseline) * len(premarket)
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
    )
    return GapAndGoDecision(candidate, "accepted", "accepted")


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
        decision = inspect_gap_and_go_candidate(
            state,
            settings,
            previous_close=previous_closes.get(symbol),
            has_news=news_flags.get(symbol, False),
        )
        if decision.candidate is not None:
            ranked.append(decision.candidate)
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
            "Include keys: strategy, symbols, ranked, rejected, risk_note. "
            "symbols must be an array of ticker strings ranked for a conservative long-only gap_and_go shortlist."
        ),
        payload,
    )
    if response_text is None:
        return None
    return extract_json_object(response_text)


def validated_gap_and_go_selection(plan: dict[str, Any], candidates: list[GapAndGoCandidate], limit: int) -> dict[str, Any]:
    available = {candidate.symbol: candidate for candidate in candidates}
    fallback_ranked = [asdict(candidate) for candidate in candidates]
    selected = []

    for raw_symbol in plan.get("symbols") or []:
        symbol = str(raw_symbol).upper()
        if symbol and symbol in available and symbol not in selected:
            selected.append(symbol)
        if len(selected) >= limit:
            break

    for candidate in candidates:
        if len(selected) >= limit:
            break
        if candidate.symbol not in selected:
            selected.append(candidate.symbol)

    ranked_by_symbol = {item["symbol"]: item for item in fallback_ranked}
    return {
        "strategy": "gap_and_go",
        "symbols": selected,
        "ranked": [ranked_by_symbol[symbol] for symbol in selected if symbol in ranked_by_symbol],
        "rejected": [item for item in plan.get("rejected", []) if str(item).upper() in available],
        "risk_note": str(plan.get("risk_note") or "Embedded AI ranking over deterministic gap-and-go candidates."),
    }


def load_states(settings: Settings, symbols: list[str]) -> tuple[dict[str, SymbolState], dict[str, float]]:
    clients = make_clients(settings)
    now = datetime.now(tz=MARKET_TZ)
    start_of_day = datetime.combine(now.date(), PREMARKET_OPEN, tzinfo=MARKET_TZ)
    previous_start = datetime.combine((now - timedelta(days=5)).date(), time.min, tzinfo=MARKET_TZ)

    intraday_request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Minute,
        start=start_of_day,
        end=now,
        feed=clients.feed,
    )
    daily_request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=previous_start,
        end=now + timedelta(days=1),
        feed=clients.feed,
    )
    intraday_response = clients.historical.get_stock_bars(intraday_request)
    daily_response = clients.historical.get_stock_bars(daily_request)
    quotes = get_latest_quotes(settings, symbols)

    states = {symbol: SymbolState(symbol) for symbol in symbols}
    previous_closes = {}
    for symbol in symbols:
        state = states[symbol]
        for raw_bar in intraday_response.data.get(symbol, []):
            state.add_bar(to_bar(raw_bar))
        quote = quotes.get(symbol)
        if quote is not None:
            state.update_quote(quote)

        previous_close = None
        for raw_bar in daily_response.data.get(symbol, []):
            bar_date = raw_bar.timestamp.astimezone(MARKET_TZ).date()
            if bar_date < now.date():
                previous_close = float(raw_bar.close)
        if previous_close and previous_close > 0:
            previous_closes[symbol] = previous_close

    return states, previous_closes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rank gap-and-go candidates from the current universe.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols. Overrides --universe-file when set.")
    parser.add_argument(
        "--universe-file",
        type=Path,
        default=DEFAULT_UNIVERSE_FILE,
        help="File with comma/newline separated symbols. Defaults to data/opening_universe.txt when present.",
    )
    parser.add_argument("--top", type=int, default=5, help="Maximum number of ranked symbols to return.")
    parser.add_argument("--use-ai", action="store_true", help="Use OpenAI to refine the final ranked symbol list.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    symbols = load_universe(args.universe_file, args.symbols)
    settings = Settings()
    states, previous_closes = load_states(settings, symbols)
    candidates = rank_gap_and_go_candidates(states, settings, previous_closes=previous_closes, top_n=args.top)
    result: dict[str, Any] = {
        "strategy": "gap_and_go",
        "selected_symbols": [candidate.symbol for candidate in candidates],
        "export": f"export SYMBOLS={','.join(candidate.symbol for candidate in candidates)}",
        "candidates": [asdict(candidate) for candidate in candidates],
        "ai_enabled": args.use_ai,
    }
    if args.use_ai:
        plan = ai_gap_and_go_selection(settings, candidates, args.top)
        if plan is None:
            result["ai_selection"] = None
            result["ai_error"] = "OpenAI not configured or client unavailable."
        else:
            validated = validated_gap_and_go_selection(plan, candidates, args.top)
            result["ai_selection"] = validated
            result["selected_symbols"] = validated["symbols"]
            result["export"] = f"export SYMBOLS={','.join(validated['symbols'])}"
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
