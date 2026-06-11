"""Select MACD early-impulse symbols from a daily close MACD reclaim setup."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_client import request_json_response
from config import Settings, load_settings
from strategy_selectors.cli import selector_argument_parser
from env_vars import format_symbols_env_line
from market_hours import MARKET_TZ
from models import Bar
from opening_plan import default_plan_file_for_strategy


DEFAULT_UNIVERSE_FILE = Path("data/opening_universe.txt")
DEFAULT_PLAN_FILE = default_plan_file_for_strategy("macd_early_impulse")
DEFAULT_DAILY_LOOKBACK_DAYS = 120
AI_SCORE_DELTA_LIMIT = 15.0
MIN_DAILY_BAR_COUNT = 35
DEEP_NEGATIVE_LOOKBACK = 20
DEEP_NEGATIVE_MACD_NORM_MAX = -0.003
GOLDEN_CROSS_LOOKBACK = 20
RISING_LOOKBACK = 3
DAILY_VOLUME_LOOKBACK = 20
DAILY_VOLUME_RATIO_MIN = 1.0
DAILY_EMA_FAST = 20
DAILY_EMA_SLOW = 50
DAILY_MAX_EMA_EXTENSION_PCT = 0.25
DAILY_PREFERRED_EMA_EXTENSION_PCT = 0.18
DAILY_ATR_PERIOD = 14
DAILY_MIN_ATR_PCT = 0.015
DAILY_MAX_ATR_PCT = 0.120
DAILY_RANGE_LOOKBACK = 20
DAILY_MIN_RANGE_PCT = 0.040
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
    daily_macd: float
    daily_signal: float
    daily_hist: float
    daily_macd_norm: float
    daily_hist_norm: float
    recent_negative_low: float
    recent_negative_low_norm: float
    recovery_from_low_norm: float
    hist_growth_norm: float
    macd_zone: str
    daily_volume_ratio: float
    daily_atr_pct: float
    daily_range_pct: float
    ema_fast: float
    ema_slow: float
    ema_extension_pct: float
    above_key_ma_structure: bool
    quality_flags: tuple[str, ...]
    last_price: float
    last_daily_change_pct: float
    days: int
    selection_stage: str = "ranked"


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


def _macd_components(values: list[float]) -> tuple[list[float], list[float], list[float]]:
    ema12 = _ema_series(values, 12)
    ema26 = _ema_series(values, 26)
    macd_line = [a - b for a, b in zip(ema12, ema26)]
    signal_line = _ema_series(macd_line, 9)
    hist = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, hist


def _is_rising(values: list[float]) -> bool:
    return len(values) >= 2 and all(values[index] > values[index - 1] for index in range(1, len(values)))


def _daily_closes(bars: list[Bar]) -> list[float]:
    return [float(bar.close) for bar in sorted((bar for bar in bars if bar.close > 0), key=lambda item: item.start_ms)]


def _recent_cross_above(values: list[float], threshold: float = 0.0, lookback: int = GOLDEN_CROSS_LOOKBACK) -> bool:
    start = max(1, len(values) - max(1, lookback))
    return any(values[index - 1] <= threshold < values[index] for index in range(start, len(values)))


def _is_improving(values: list[float], lookback: int = RISING_LOOKBACK) -> bool:
    if len(values) < lookback + 1:
        return False
    recent = values[-lookback:]
    previous = values[-(lookback + 1) : -1]
    return values[-1] > values[-2] and sum(recent) / len(recent) > sum(previous) / len(previous)


def _latest_daily_change_pct(closes: list[float]) -> float:
    if len(closes) < 2 or closes[-2] <= 0:
        return 0.0
    return ((closes[-1] - closes[-2]) / closes[-2]) * 100.0


def _volume_ratio(bars: list[Bar], lookback: int = DAILY_VOLUME_LOOKBACK) -> float:
    if len(bars) < 2:
        return 0.0
    latest = bars[-1].volume
    baseline_items = [bar.volume for bar in bars[-(lookback + 1) : -1] if bar.volume > 0]
    baseline = median(baseline_items or [0.0])
    return latest / baseline if baseline > 0 else 0.0


def _atr_pct(bars: list[Bar], period: int = DAILY_ATR_PERIOD) -> float:
    if period <= 0 or len(bars) < period + 1:
        return 0.0
    window = bars[-(period + 1) :]
    true_ranges: list[float] = []
    for previous, current in zip(window, window[1:]):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    price = bars[-1].close
    return (sum(true_ranges) / len(true_ranges)) / price if true_ranges and price > 0 else 0.0


def _range_pct(bars: list[Bar], lookback: int = DAILY_RANGE_LOOKBACK) -> float:
    window = bars[-max(1, lookback) :]
    if not window:
        return 0.0
    low = min(bar.low for bar in window)
    high = max(bar.high for bar in window)
    price = window[-1].close
    return (high - low) / price if price > 0 else 0.0


def _macd_zone(macd_value: float, crossed_zero_recently: bool) -> str:
    if macd_value < 0:
        return "negative_reclaim"
    if crossed_zero_recently:
        return "zero_reclaim"
    return "positive_impulse"


def _recent_golden_cross(hist: list[float], lookback: int = GOLDEN_CROSS_LOOKBACK) -> bool:
    start = max(1, len(hist) - max(1, lookback))
    return any(hist[index - 1] <= 0 < hist[index] for index in range(start, len(hist)))


def _bounded_score(value: float, *, scale: float, maximum: float) -> float:
    return max(0.0, min(maximum, value * scale))


def load_market_data(settings: Settings, symbols: list[str], lookback_days: int) -> dict[str, list[Bar]]:
    try:
        from alpaca.data.timeframe import TimeFrame

        from alpaca_client import get_bars_between, make_clients
    except Exception as exc:
        raise RuntimeError("Alpaca market-data dependencies unavailable.") from exc

    clients = make_clients(settings)
    end = datetime.now(tz=MARKET_TZ) + timedelta(days=1)
    start = end - timedelta(days=max(lookback_days * 2, lookback_days + 30))
    daily = get_bars_between(clients, symbols, TimeFrame.Day, start, end)
    return {symbol: sorted(bars, key=lambda item: item.start_ms)[-lookback_days:] for symbol, bars in daily.items()}


def _selector_settings() -> Settings:
    try:
        return load_settings(strategy_names=["macd_early_impulse"], validate=False)
    except Exception:
        return Settings(
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
            strategy_names=["macd_early_impulse"],
        )


def evaluate_symbol(
    symbol: str,
    bars: list[Bar],
    *,
    stage_counts: dict[str, int] | None = None,
) -> tuple[MACDEarlyImpulseCandidate | None, CandidateReject | None]:
    def _bump(key: str) -> None:
        if stage_counts is not None:
            stage_counts[key] = stage_counts.get(key, 0) + 1

    ordered = sorted((bar for bar in bars if bar.close > 0), key=lambda item: item.start_ms)
    closes = _daily_closes(ordered)
    if len(closes) < MIN_DAILY_BAR_COUNT:
        return None, CandidateReject(symbol, "bars", f"need >= {MIN_DAILY_BAR_COUNT} daily bars, got {len(closes)}")
    price = closes[-1]

    _bump("passed_macd_data")

    macd_line, signal_line, hist = _macd_components(closes)
    if len(hist) < max(MIN_DAILY_BAR_COUNT, GOLDEN_CROSS_LOOKBACK + 1, RISING_LOOKBACK + 1):
        return None, CandidateReject(symbol, "macd", "insufficient daily MACD points")

    quality_flags: list[str] = []
    recent_macd = macd_line[-DEEP_NEGATIVE_LOOKBACK:]
    recent_negative_low = min(recent_macd)
    recent_negative_low_norm = recent_negative_low / price if price > 0 else 0.0
    deep_negative = recent_negative_low_norm <= DEEP_NEGATIVE_MACD_NORM_MAX
    if deep_negative:
        _bump("passed_deep_negative")
    else:
        quality_flags.append(
            f"shallow MACD washout ({recent_negative_low_norm:.5f} > {DEEP_NEGATIVE_MACD_NORM_MAX:.5f})"
        )

    golden_cross = _recent_golden_cross(hist)
    if golden_cross:
        _bump("passed_golden_cross")
    else:
        quality_flags.append("no recent bullish histogram cross")

    recent_hist = hist[-RISING_LOOKBACK:]
    recent_line = macd_line[-RISING_LOOKBACK:]
    hist_expanding = _is_improving(hist)
    macd_line_rising = _is_rising(recent_line)
    if hist_expanding and macd_line_rising:
        _bump("passed_rising")
    else:
        if not hist_expanding:
            quality_flags.append("daily histogram not expanding")
        if not macd_line_rising:
            quality_flags.append("daily MACD line not rising")

    daily_volume_ratio = _volume_ratio(ordered)
    if daily_volume_ratio >= DAILY_VOLUME_RATIO_MIN:
        _bump("passed_volume")
    else:
        quality_flags.append(f"daily volume ratio {daily_volume_ratio:.2f} < {DAILY_VOLUME_RATIO_MIN:.2f}")

    daily_atr_pct = _atr_pct(ordered)
    daily_range_pct = _range_pct(ordered)
    if DAILY_MIN_ATR_PCT <= daily_atr_pct <= DAILY_MAX_ATR_PCT:
        _bump("passed_daily_atr")
    elif daily_atr_pct < DAILY_MIN_ATR_PCT:
        quality_flags.append(f"daily ATR {daily_atr_pct:.2%} < {DAILY_MIN_ATR_PCT:.2%}")
    else:
        quality_flags.append(f"daily ATR {daily_atr_pct:.2%} > {DAILY_MAX_ATR_PCT:.2%}")
    if daily_range_pct >= DAILY_MIN_RANGE_PCT:
        _bump("passed_daily_range")
    else:
        quality_flags.append(f"{DAILY_RANGE_LOOKBACK}-day range {daily_range_pct:.2%} < {DAILY_MIN_RANGE_PCT:.2%}")

    ema_fast_series = _ema_series(closes, DAILY_EMA_FAST)
    ema_slow_series = _ema_series(closes, DAILY_EMA_SLOW)
    ema_fast = ema_fast_series[-1] if ema_fast_series else 0.0
    ema_slow = ema_slow_series[-1] if ema_slow_series else 0.0
    above_key_ma_structure = price > ema_fast and (ema_slow <= 0 or price >= ema_slow * 0.98)
    if above_key_ma_structure:
        _bump("passed_ma_structure")
    else:
        quality_flags.append(f"price {price:.2f} not above EMA{DAILY_EMA_FAST}/near EMA{DAILY_EMA_SLOW}")

    ema_extension_pct = (price - ema_fast) / ema_fast if ema_fast > 0 else 0.0
    not_overextended = ema_extension_pct <= DAILY_MAX_EMA_EXTENSION_PCT
    if not_overextended:
        _bump("passed_not_overextended")
    else:
        quality_flags.append(f"price extension {ema_extension_pct:.2%} > {DAILY_MAX_EMA_EXTENSION_PCT:.2%}")

    crossed_zero_recently = _recent_cross_above(macd_line, 0.0, lookback=GOLDEN_CROSS_LOOKBACK)
    recovery_from_low_norm = (macd_line[-1] - recent_negative_low) / price if price > 0 else 0.0
    daily_macd_norm = macd_line[-1] / price if price > 0 else 0.0
    daily_hist_norm = hist[-1] / price if price > 0 else 0.0
    hist_growth_norm = (hist[-1] - hist[-RISING_LOOKBACK]) / price if price > 0 and len(hist) >= RISING_LOOKBACK else 0.0
    zero_bonus = 8.0 if crossed_zero_recently else 0.0
    negative_reclaim_bonus = 4.0 if macd_line[-1] < 0 and hist[-1] > 0 else 0.0
    hist_positive_bonus = 8.0 if hist[-1] > 0 else -8.0
    cross_bonus = 14.0 if golden_cross else -12.0
    rising_bonus = 10.0 if macd_line_rising else -8.0
    hist_expansion_bonus = 12.0 if hist_expanding else -10.0
    volume_score = min(daily_volume_ratio, 3.0) * 5.0
    volume_penalty = max(0.0, DAILY_VOLUME_RATIO_MIN - daily_volume_ratio) * 10.0
    ma_score = 10.0 if above_key_ma_structure else -10.0
    extension_penalty = max(0.0, ema_extension_pct - DAILY_MAX_EMA_EXTENSION_PCT) * 100.0
    preferred_extension_penalty = max(0.0, ema_extension_pct - DAILY_PREFERRED_EMA_EXTENSION_PCT) * 60.0
    atr_score = max(-8.0, min(10.0, (daily_atr_pct - DAILY_MIN_ATR_PCT) * 220.0))
    range_score = max(-8.0, min(10.0, (daily_range_pct - DAILY_MIN_RANGE_PCT) * 100.0))
    score = (
        _bounded_score(abs(recent_negative_low_norm), scale=1000.0, maximum=25.0)
        + _bounded_score(recovery_from_low_norm, scale=1000.0, maximum=25.0)
        + _bounded_score(max(0.0, daily_hist_norm), scale=1000.0, maximum=18.0)
        + _bounded_score(max(0.0, hist_growth_norm), scale=1000.0, maximum=12.0)
        + zero_bonus
        + negative_reclaim_bonus
        + hist_positive_bonus
        + cross_bonus
        + rising_bonus
        + hist_expansion_bonus
        + volume_score
        + ma_score
        + atr_score
        + range_score
        - volume_penalty
        - preferred_extension_penalty
        - extension_penalty
    )
    candidate = MACDEarlyImpulseCandidate(
        symbol=symbol,
        score=round(score, 6),
        daily_macd=round(macd_line[-1], 6),
        daily_signal=round(signal_line[-1], 6),
        daily_hist=round(hist[-1], 6),
        daily_macd_norm=round(daily_macd_norm, 6),
        daily_hist_norm=round(daily_hist_norm, 6),
        recent_negative_low=round(recent_negative_low, 6),
        recent_negative_low_norm=round(recent_negative_low_norm, 6),
        recovery_from_low_norm=round(recovery_from_low_norm, 6),
        hist_growth_norm=round(hist_growth_norm, 6),
        macd_zone=_macd_zone(macd_line[-1], crossed_zero_recently),
        daily_volume_ratio=round(daily_volume_ratio, 4),
        daily_atr_pct=round(daily_atr_pct, 6),
        daily_range_pct=round(daily_range_pct, 6),
        ema_fast=round(ema_fast, 4),
        ema_slow=round(ema_slow, 4),
        ema_extension_pct=round(ema_extension_pct, 6),
        above_key_ma_structure=above_key_ma_structure,
        quality_flags=tuple(quality_flags),
        last_price=round(price, 4),
        last_daily_change_pct=round(_latest_daily_change_pct(closes), 4),
        days=len(ordered),
    )
    return candidate, None


def rank_candidates(symbols: list[str], bars_by_symbol: dict[str, list[Bar]]) -> tuple[list[MACDEarlyImpulseCandidate], list[CandidateReject], dict[str, int]]:
    ranked: list[MACDEarlyImpulseCandidate] = []
    rejected: list[CandidateReject] = []
    stage_counts: dict[str, int] = {
        "universe_symbols": len(symbols),
        "passed_macd_data": 0,
        "passed_deep_negative": 0,
        "passed_golden_cross": 0,
        "passed_rising": 0,
        "passed_volume": 0,
        "passed_daily_atr": 0,
        "passed_daily_range": 0,
        "passed_ma_structure": 0,
        "passed_not_overextended": 0,
    }
    for symbol in symbols:
        candidate, reject = evaluate_symbol(
            symbol,
            bars_by_symbol.get(symbol, []),
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
            "min_daily_bar_count": MIN_DAILY_BAR_COUNT,
            "deep_negative_lookback": DEEP_NEGATIVE_LOOKBACK,
            "deep_negative_macd_norm_max": DEEP_NEGATIVE_MACD_NORM_MAX,
            "golden_cross_lookback": GOLDEN_CROSS_LOOKBACK,
            "rising_lookback": RISING_LOOKBACK,
            "daily_volume_ratio_min": DAILY_VOLUME_RATIO_MIN,
            "daily_volume_lookback": DAILY_VOLUME_LOOKBACK,
            "ema_fast": DAILY_EMA_FAST,
            "ema_slow": DAILY_EMA_SLOW,
            "max_ema_extension_pct": DAILY_MAX_EMA_EXTENSION_PCT,
            "preferred_ema_extension_pct": DAILY_PREFERRED_EMA_EXTENSION_PCT,
            "daily_atr_period": DAILY_ATR_PERIOD,
            "daily_min_atr_pct": DAILY_MIN_ATR_PCT,
            "daily_max_atr_pct": DAILY_MAX_ATR_PCT,
            "daily_range_lookback": DAILY_RANGE_LOOKBACK,
            "daily_min_range_pct": DAILY_MIN_RANGE_PCT,
            "macd_input": "daily closes",
        }
    return {
        "strategy": strategy,
        "selection_stage": "ranked",
        "note": "Daily MACD ranker: scores reclaim quality, expanding histogram, rising line, daily volume, daily volatility/range, MA structure, and overextension without using intraday bars.",
        "symbols": selected,
        "ranked": ranked,
        "rejected": [asdict(row) for row in rejected],
        "settings": settings,
        "risk_note": "Selector returns the top ranked daily MACD watchlist; macd_early_impulse still waits for intraday MACD/volume/structure before trading.",
    }


def ai_macd_selection(ranked: list[dict[str, Any]], limit: int) -> dict[str, Any] | None:
    settings = _selector_settings()
    payload = {
        "strategy": "macd_early_impulse",
        "selection_rules": {
            "must_choose_from_ranked": True,
            "focus": "liquidity, tradable daily volatility/range, fresh bullish histogram crosses, rising MACD/histogram, and avoiding overextended names",
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
            f"Keep ai_score_delta bounded between -{AI_SCORE_DELTA_LIMIT:.1f} and {AI_SCORE_DELTA_LIMIT:.1f}, "
            "and use 0 when no adjustment is needed."
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
        ai_delta = max(
            -AI_SCORE_DELTA_LIMIT,
            min(AI_SCORE_DELTA_LIMIT, float(adjustment.get("ai_score_delta", 0.0) or 0.0)),
        )
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
    selection_stage = "ranked" if ranked else str(plan.get("selection_stage") or "ranked")
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
    parser = selector_argument_parser(description="Build macd_early_impulse plan from daily close MACD reclaim candidates.")
    parser.add_argument(
        "--universe-file",
        type=Path,
        default=DEFAULT_UNIVERSE_FILE,
        help="Universe file (default data/opening_universe.txt when present).",
    )
    parser.add_argument("--symbols", default="", help="Comma-separated symbols; overrides universe file.")
    parser.add_argument("--top", type=int, default=12, help="Max symbols to include.")
    parser.add_argument("--daily-lookback-days", type=int, default=DEFAULT_DAILY_LOOKBACK_DAYS)
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
    if args.daily_lookback_days < MIN_DAILY_BAR_COUNT:
        raise ValueError(f"--daily-lookback-days must be at least {MIN_DAILY_BAR_COUNT}")
    bars_by_symbol = load_market_data(settings, symbols, args.daily_lookback_days)
    candidates, rejected, stage_counts = rank_candidates(symbols, bars_by_symbol)
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
