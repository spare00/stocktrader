"""Stub selector: writes data/macd_early_impulse_plan.json from opening_universe (no heavy filters)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, time
from pathlib import Path
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_client import request_json_response
from config import Settings, load_settings
from env_vars import format_symbols_env_line
from market_hours import MARKET_TZ
from models import Bar, Quote
from opening_plan import default_plan_file_for_strategy


DEFAULT_UNIVERSE_FILE = Path("data/opening_universe.txt")
DEFAULT_PLAN_FILE = default_plan_file_for_strategy("macd_early_impulse")
PREMARKET_OPEN = time(4, 0)
HIST_NORM_MIN = 0.0005
NEAR_HIGH_TOLERANCE_PCT = 0.005
CHOP_DELTA_MULTIPLIER = 0.0035
MIN_VOLUME_RATIO = 1.2
MIN_BAR_COUNT = 20
RECENT_HIGH_LOOKBACK = 20
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


@dataclass(frozen=True)
class MACDEarlyImpulseCandidate:
    symbol: str
    score: float
    hist_norm: float
    volume_ratio: float
    last_price: float
    session_high: float
    macd_hist: float
    selection_stage: str = "filtered"


@dataclass(frozen=True)
class CandidateReject:
    symbol: str
    code: str
    detail: str


def extract_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    stripped = text.lstrip()
    result, _ = decoder.raw_decode(stripped)
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object.")
    return result


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values or period <= 0:
        return []
    alpha = 2.0 / (period + 1)
    ema = values[0]
    out: list[float] = []
    for value in values:
        ema = alpha * value + (1.0 - alpha) * ema
        out.append(ema)
    return out


def _macd_histogram(closes: list[float]) -> list[float]:
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    macd_line = [a - b for a, b in zip(ema12, ema26)]
    signal_line = _ema_series(macd_line, 9)
    return [m - s for m, s in zip(macd_line, signal_line)]


def load_market_data(settings: Settings, symbols: list[str]) -> tuple[dict[str, list[Bar]], dict[str, Quote]]:
    try:
        from alpaca.data.timeframe import TimeFrame

        from alpaca_client import get_bars_between, get_latest_quotes, make_clients
    except Exception as exc:
        raise RuntimeError("Alpaca market-data dependencies unavailable.") from exc

    clients = make_clients(settings)
    now = datetime.now(tz=MARKET_TZ)
    start_of_day = datetime.combine(now.date(), PREMARKET_OPEN, tzinfo=MARKET_TZ)
    intraday = get_bars_between(clients, symbols, TimeFrame.Minute, start_of_day, now)
    quotes = get_latest_quotes(settings, symbols)
    return intraday, quotes


def _selector_settings() -> Settings:
    try:
        return load_settings(strategy_names=["macd_early_impulse"], validate=False)
    except Exception:
        return Settings(
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
            strategy_names=["macd_early_impulse"],
        )


def _usable_price(quote: Quote | None, bars: list[Bar]) -> float:
    if quote is not None and quote.ask > 0:
        return quote.ask
    if bars:
        return bars[-1].close
    return 0.0


def evaluate_symbol(
    symbol: str,
    bars: list[Bar],
    quote: Quote | None,
    *,
    stage_counts: dict[str, int] | None = None,
) -> tuple[MACDEarlyImpulseCandidate | None, CandidateReject | None]:
    def _bump(key: str) -> None:
        if stage_counts is not None:
            stage_counts[key] = stage_counts.get(key, 0) + 1

    if len(bars) < MIN_BAR_COUNT:
        return None, CandidateReject(symbol, "bars", f"need >= {MIN_BAR_COUNT} bars, got {len(bars)}")

    closes = [float(bar.close) for bar in bars if bar.close > 0]
    if len(closes) < MIN_BAR_COUNT:
        return None, CandidateReject(symbol, "close", "insufficient positive closes")

    hist = _macd_histogram(closes)
    if len(hist) < 2:
        return None, CandidateReject(symbol, "macd", "insufficient histogram points")

    price = _usable_price(quote, bars)
    if price <= 0:
        return None, CandidateReject(symbol, "price", "invalid last price")

    _bump("passed_macd_data")

    hist_norm = hist[-1] / price
    if abs(hist_norm) < HIST_NORM_MIN:
        return None, CandidateReject(symbol, "weak_macd", f"hist_norm {hist_norm:.5f} < {HIST_NORM_MIN}")

    _bump("passed_hist_norm")

    if not (closes[-3] < closes[-2] < closes[-1]):
        return None, CandidateReject(symbol, "momentum", "last 3 closes not strictly increasing")

    _bump("passed_momentum")

    recent_bars = bars[-RECENT_HIGH_LOOKBACK:]
    recent_high = max((bar.high for bar in recent_bars if bar.high > 0), default=0.0)
    if recent_high <= 0:
        return None, CandidateReject(symbol, "high", "invalid recent high")
    if price < recent_high * (1.0 - NEAR_HIGH_TOLERANCE_PCT):
        return None, CandidateReject(symbol, "near_high", "price too far below recent high")

    _bump("passed_near_high")

    chop_delta = abs(hist[-1] - hist[-2])
    if chop_delta < price * CHOP_DELTA_MULTIPLIER:
        return None, CandidateReject(symbol, "chop", f"macd delta {chop_delta:.5f} below threshold")

    _bump("passed_chop")

    latest_volume = bars[-1].volume
    baseline = median([bar.volume for bar in bars[:-1] if bar.volume > 0] or [0.0])
    volume_ratio = (latest_volume / baseline) if baseline > 0 else 0.0
    if volume_ratio < MIN_VOLUME_RATIO:
        return None, CandidateReject(symbol, "volume", f"volume_ratio {volume_ratio:.2f} < {MIN_VOLUME_RATIO:.2f}")

    _bump("passed_volume")

    breakout_score = ((price - recent_high) / price) * 100.0 if price > 0 else 0.0
    score = (hist_norm * 1000.0) + (volume_ratio * 2.0) + breakout_score
    candidate = MACDEarlyImpulseCandidate(
        symbol=symbol,
        score=round(score, 6),
        hist_norm=round(hist_norm, 6),
        volume_ratio=round(volume_ratio, 4),
        last_price=round(price, 4),
        session_high=round(recent_high, 4),
        macd_hist=round(hist[-1], 6),
    )
    return candidate, None


def rank_candidates(symbols: list[str], bars_by_symbol: dict[str, list[Bar]], quotes: dict[str, Quote]) -> tuple[list[MACDEarlyImpulseCandidate], list[CandidateReject], dict[str, int]]:
    ranked: list[MACDEarlyImpulseCandidate] = []
    rejected: list[CandidateReject] = []
    stage_counts: dict[str, int] = {
        "universe_symbols": len(symbols),
        "passed_macd_data": 0,
        "passed_hist_norm": 0,
        "passed_momentum": 0,
        "passed_near_high": 0,
        "passed_chop": 0,
        "passed_volume": 0,
    }
    for symbol in symbols:
        candidate, reject = evaluate_symbol(
            symbol,
            bars_by_symbol.get(symbol, []),
            quotes.get(symbol),
            stage_counts=stage_counts,
        )
        if candidate is not None:
            ranked.append(candidate)
        elif reject is not None:
            rejected.append(reject)
    ranked.sort(key=lambda row: row.score, reverse=True)
    return ranked, rejected, stage_counts


def deterministic_plan(
    candidates: list[MACDEarlyImpulseCandidate],
    rejected: list[CandidateReject],
    strategy: str,
    limit: int,
    *,
    filter_stage_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    top = candidates[:limit]
    selected = [row.symbol for row in top]
    ranked = [asdict(row) for row in top]
    settings: dict[str, Any] = {}
    if filter_stage_counts:
        settings["filter_stage_counts"] = dict(filter_stage_counts)
        settings["filter_thresholds"] = {
            "hist_norm_min": HIST_NORM_MIN,
            "min_bar_count": MIN_BAR_COUNT,
            "near_high_tolerance_pct": NEAR_HIGH_TOLERANCE_PCT,
            "chop_delta_multiplier": CHOP_DELTA_MULTIPLIER,
            "min_volume_ratio": MIN_VOLUME_RATIO,
            "recent_high_lookback_bars": RECENT_HIGH_LOOKBACK,
        }
    return {
        "strategy": strategy,
        "selection_stage": "filtered",
        "note": "MACD-based ranking with normalization, momentum continuation, chop, near-high, and volume filters.",
        "symbols": selected,
        "ranked": ranked,
        "rejected": [asdict(row) for row in rejected],
        "settings": settings,
        "risk_note": "MACD momentum + breakout + volume filtered candidates",
    }


def ai_macd_selection(ranked: list[dict[str, Any]], limit: int) -> dict[str, Any] | None:
    settings = _selector_settings()
    payload = {
        "strategy": "macd_early_impulse",
        "selection_rules": {
            "must_choose_from_ranked": True,
            "focus": "liquidity, tradability, and avoiding structurally weak names",
        },
        "ranked": ranked,
        "limit": limit,
    }
    response_text = request_json_response(
        settings,
        (
            "Review the macd_early_impulse ranked candidates and return only JSON. "
            "Choose only from ranked symbols. Do not invent symbols. "
            "Include keys: strategy, adjustments, rejected, risk_note. "
            "adjustments must be an object keyed by symbol. Each value may include ai_score_delta and ai_reason. "
            "Keep ai_score_delta bounded between -2.0 and 2.0, and use 0 when no adjustment is needed."
        ),
        payload,
    )
    if response_text is None:
        return None
    return extract_json_object(response_text)


def validated_macd_selection(plan: dict[str, Any], ranked: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    available = {str(item.get("symbol", "")).upper() for item in ranked}
    raw_adjustments = plan.get("adjustments") if isinstance(plan.get("adjustments"), dict) else {}
    normalized_ranked: list[dict[str, Any]] = []
    for item in ranked:
        symbol = str(item.get("symbol", "")).upper()
        if not symbol:
            continue
        adjustment = raw_adjustments.get(symbol) or raw_adjustments.get(symbol.lower()) or {}
        if not isinstance(adjustment, dict):
            adjustment = {}
        ai_delta = max(-2.0, min(2.0, float(adjustment.get("ai_score_delta", 0.0) or 0.0)))
        ai_reason = str(adjustment.get("ai_reason", "")).strip()
        ranked_item = dict(item)
        ranked_item["symbol"] = symbol
        ranked_item["base_score"] = float(ranked_item.get("score", 0.0) or 0.0)
        ranked_item["ai_score_delta"] = round(ai_delta, 3)
        ranked_item["score"] = round(float(ranked_item["base_score"]) + float(ranked_item["ai_score_delta"]), 3)
        if ai_reason:
            ranked_item["ai_reason"] = ai_reason
        normalized_ranked.append(ranked_item)

    normalized_ranked.sort(key=lambda row: float(row.get("score", 0.0) or 0.0), reverse=True)
    selected = [str(item.get("symbol", "")) for item in normalized_ranked[:limit] if str(item.get("symbol", ""))]
    selection_stage = "filtered" if ranked else str(plan.get("selection_stage") or "filtered")
    return {
        "strategy": "macd_early_impulse",
        "selection_stage": selection_stage,
        "symbols": selected,
        "ranked": normalized_ranked[:limit],
        "rejected": [item for item in (plan.get("rejected") or []) if str(item).upper() in available],
        "settings": plan.get("settings") if isinstance(plan.get("settings"), dict) else {},
        "risk_note": str(plan.get("risk_note") or "Embedded AI ranking over deterministic MACD candidates."),
    }


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
    parser.add_argument("--use-ai", action="store_true", help="Use OpenAI to refine the final ranked symbol list.")
    args = parser.parse_args()

    symbols = load_universe(args.universe_file, args.symbols)
    settings = _selector_settings()
    bars_by_symbol, quotes = load_market_data(settings, symbols)
    candidates, rejected, stage_counts = rank_candidates(symbols, bars_by_symbol, quotes)
    plan = deterministic_plan(candidates, rejected, "macd_early_impulse", args.top, filter_stage_counts=stage_counts)
    selected_symbols = list(plan["symbols"])
    result: dict[str, Any] = {
        "strategy": "macd_early_impulse",
        "selected_symbols": selected_symbols,
        "symbols_env_line": format_symbols_env_line(selected_symbols),
        "selection_plan": plan,
        "filter_stage_counts": stage_counts,
        "ai_enabled": args.use_ai,
        "plan_output": str(args.plan_output),
    }
    args.plan_output.parent.mkdir(parents=True, exist_ok=True)
    args.plan_output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    if args.use_ai:
        ai_plan = ai_macd_selection(plan["ranked"], args.top)
        if ai_plan is None:
            result["ai_selection"] = None
            result["ai_error"] = "OpenAI not configured or client unavailable."
        else:
            validated = validated_macd_selection(ai_plan, plan["ranked"], args.top)
            prev_settings = plan.get("settings") if isinstance(plan.get("settings"), dict) else {}
            if prev_settings and isinstance(validated.get("settings"), dict):
                validated["settings"] = {**prev_settings, **validated["settings"]}
            elif prev_settings:
                validated["settings"] = dict(prev_settings)
            result["ai_selection"] = validated
            result["selection_plan"] = validated
            result["selected_symbols"] = validated["symbols"]
            result["symbols_env_line"] = format_symbols_env_line(validated["symbols"])
            args.plan_output.write_text(json.dumps(validated, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
