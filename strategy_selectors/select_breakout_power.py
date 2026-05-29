"""Select breakout_power symbols from a daily BreakOut Power alignment setup."""

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
from env_vars import format_symbols_env_line
from market_hours import MARKET_TZ
from models import Bar
from opening_plan import default_plan_file_for_strategy
from strategies.breakout_power import (
    compute_breakout_power_series,
    latest_breakout_power_details,
    recent_breakout_power_cross,
)


DEFAULT_UNIVERSE_FILE = Path("data/opening_universe.txt")
DEFAULT_PLAN_FILE = default_plan_file_for_strategy("breakout_power")
DEFAULT_DAILY_LOOKBACK_DAYS = 140
MIN_DAILY_BAR_COUNT = 45
DAILY_CROSS_LOOKBACK = 5
DAILY_GREEN_THRESHOLD = 65.0
DAILY_TREND_LINE = 50.0
DAILY_VOLUME_LOOKBACK = 20
DAILY_VOLUME_RATIO_MIN = 0.8
DAILY_EMA_FAST = 20
DAILY_EMA_SLOW = 50
DAILY_MAX_EMA_EXTENSION_PCT = 0.18
DAILY_ATR_PERIOD = 14
DAILY_MIN_ATR_PCT = 0.015
DAILY_MAX_ATR_PCT = 0.120
DAILY_RANGE_LOOKBACK = 20
DAILY_MIN_RANGE_PCT = 0.040
AI_SCORE_DELTA_LIMIT = 15.0
DEFAULT_UNIVERSE = [
    "AAPL",
    "AMD",
    "AMZN",
    "AVGO",
    "INTC",
    "META",
    "MRVL",
    "MSFT",
    "NVDA",
    "QCOM",
    "QQQ",
    "SMCI",
    "TSLA",
]


@dataclass(frozen=True)
class BreakoutPowerCandidate:
    symbol: str
    score: float
    setup_stage: str
    bp_score: float
    prev_bp_score: float
    avg_momentum: float
    momentum: float
    is_green: bool
    recent_cross_above_trend: bool
    avg_momentum_rising: bool
    macd_above_signal: bool
    macd_positive: bool
    ao_positive: bool
    ema5_above_ema20: bool
    breakout_high: bool
    daily_volume_ratio: float
    daily_atr_pct: float
    daily_range_pct: float
    ema_fast: float
    ema_slow: float
    ema_extension_pct: float
    last_price: float
    last_daily_change_pct: float
    quality_flags: tuple[str, ...]
    days: int
    selection_stage: str = "ranked"


@dataclass(frozen=True)
class CandidateReject:
    symbol: str
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


def _daily_volume_ratio(bars: list[Bar], lookback: int = DAILY_VOLUME_LOOKBACK) -> float:
    if len(bars) < 2:
        return 0.0
    baseline_items = [bar.volume for bar in bars[-(lookback + 1) : -1] if bar.volume > 0]
    baseline = median(baseline_items or [0.0])
    return bars[-1].volume / baseline if baseline > 0 else 0.0


def _latest_daily_change_pct(closes: list[float]) -> float:
    if len(closes) < 2 or closes[-2] <= 0:
        return 0.0
    return ((closes[-1] - closes[-2]) / closes[-2]) * 100.0


def _atr_pct(bars: list[Bar], period: int = DAILY_ATR_PERIOD) -> float:
    if period <= 0 or len(bars) < period + 1:
        return 0.0
    window = bars[-(period + 1) :]
    true_ranges: list[float] = []
    for index, bar in enumerate(window):
        if index == 0:
            continue
        prev_close = window[index - 1].close
        true_ranges.append(
            max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            )
        )
    price = bars[-1].close
    return (sum(true_ranges) / len(true_ranges)) / price if true_ranges and price > 0 else 0.0


def _range_pct(bars: list[Bar], lookback: int = DAILY_RANGE_LOOKBACK) -> float:
    window = bars[-max(1, lookback) :]
    if not window:
        return 0.0
    high = max(bar.high for bar in window)
    low = min(bar.low for bar in window)
    price = window[-1].close
    return (high - low) / price if price > 0 else 0.0


def _bounded(value: float, *, low: float, high: float) -> float:
    return max(low, min(high, value))


def _selector_settings() -> Settings:
    try:
        return load_settings(strategy_names=["breakout_power"], validate=False)
    except Exception:
        return Settings(
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
            strategy_names=["breakout_power"],
        )


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


def _setup_stage(
    details,
    *,
    recent_cross: bool,
    trend_line: float = DAILY_TREND_LINE,
    green_threshold: float = DAILY_GREEN_THRESHOLD,
) -> str:
    is_green = details.is_green(threshold=green_threshold)
    if recent_cross and is_green:
        return "green_cross"
    if details.score > trend_line and is_green:
        return "green_above_trend"
    if details.score >= trend_line - 10 and details.avg_momentum_rising():
        return "building"
    return "not_ready"


def evaluate_symbol(
    symbol: str,
    bars: list[Bar],
    *,
    stage_counts: dict[str, int] | None = None,
) -> tuple[BreakoutPowerCandidate | None, CandidateReject | None]:
    def _bump(key: str) -> None:
        if stage_counts is not None:
            stage_counts[key] = stage_counts.get(key, 0) + 1

    ordered = sorted((bar for bar in bars if bar.close > 0), key=lambda item: item.start_ms)
    if len(ordered) < MIN_DAILY_BAR_COUNT:
        return None, CandidateReject(symbol, "bars", f"need >= {MIN_DAILY_BAR_COUNT} daily bars, got {len(ordered)}")

    details = latest_breakout_power_details(ordered)
    series = compute_breakout_power_series(ordered)
    if details is None or not series.scores:
        return None, CandidateReject(symbol, "indicators", "insufficient daily BreakOut Power points")

    _bump("passed_indicator_data")

    closes = [float(bar.close) for bar in ordered]
    price = closes[-1]
    prev_bp_score = float(details.prev_score or details.score)
    is_green = details.is_green(threshold=DAILY_GREEN_THRESHOLD)
    recent_cross = recent_breakout_power_cross(
        series.scores,
        trend_line=DAILY_TREND_LINE,
        lookback=DAILY_CROSS_LOOKBACK,
    )
    avg_momentum_rising = details.avg_momentum_rising()

    quality_flags: list[str] = []
    if details.score > DAILY_TREND_LINE:
        _bump("passed_above_trend")
    else:
        quality_flags.append(f"BP score {details.score:.0f} <= {DAILY_TREND_LINE:.0f}")
    if is_green:
        _bump("passed_green_momentum")
    else:
        quality_flags.append(f"avg_momentum {details.avg_momentum:.1f} < {DAILY_GREEN_THRESHOLD:.0f}")
    if recent_cross:
        _bump("passed_recent_cross")
    else:
        quality_flags.append(f"no BP cross above {DAILY_TREND_LINE:.0f} in last {DAILY_CROSS_LOOKBACK} bars")
    if avg_momentum_rising:
        _bump("passed_avg_momentum_rising")
    else:
        quality_flags.append("avg_momentum not rising")
    if details.macd_above_signal:
        _bump("passed_macd_above_signal")
    else:
        quality_flags.append("MACD not above signal")
    if details.macd_positive:
        _bump("passed_macd_positive")
    else:
        quality_flags.append("MACD not positive")
    if details.ao_positive:
        _bump("passed_ao_positive")
    else:
        quality_flags.append("AO not positive")
    if details.ema5_above_ema20:
        _bump("passed_ema_alignment")
    else:
        quality_flags.append("EMA5 not above EMA20")
    if details.breakout_high:
        _bump("passed_breakout_high")
    else:
        quality_flags.append("no 20-bar high breakout")

    daily_volume_ratio = _daily_volume_ratio(ordered)
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
    ema_extension_pct = (price - ema_fast) / ema_fast if ema_fast > 0 else 0.0
    if ema_extension_pct <= DAILY_MAX_EMA_EXTENSION_PCT:
        _bump("passed_not_overextended")
    else:
        quality_flags.append(f"price extension {ema_extension_pct:.2%} > {DAILY_MAX_EMA_EXTENSION_PCT:.2%}")

    setup_stage = _setup_stage(details, recent_cross=recent_cross)
    bp_score_component = details.score * 0.55
    green_score = 18.0 if is_green else -12.0
    cross_score = 12.0 if recent_cross else -6.0
    momentum_rise_score = 8.0 if avg_momentum_rising else -6.0
    alignment_score = (
        (10.0 if details.macd_above_signal else -8.0)
        + (8.0 if details.macd_positive else -6.0)
        + (8.0 if details.ao_positive else -6.0)
        + (8.0 if details.ema5_above_ema20 else -6.0)
        + (10.0 if details.breakout_high else -4.0)
    )
    volume_score = min(daily_volume_ratio, 3.0) * 4.0
    atr_score = _bounded((daily_atr_pct - DAILY_MIN_ATR_PCT) * 250.0, low=-8.0, high=10.0)
    range_score = _bounded((daily_range_pct - DAILY_MIN_RANGE_PCT) * 120.0, low=-8.0, high=10.0)
    extension_penalty = max(0.0, ema_extension_pct - DAILY_MAX_EMA_EXTENSION_PCT) * 100.0
    below_trend_penalty = max(0.0, DAILY_TREND_LINE - details.score) * 0.35
    score = (
        bp_score_component
        + green_score
        + cross_score
        + momentum_rise_score
        + alignment_score
        + volume_score
        + atr_score
        + range_score
        - extension_penalty
        - below_trend_penalty
    )

    candidate = BreakoutPowerCandidate(
        symbol=symbol,
        score=round(score, 6),
        setup_stage=setup_stage,
        bp_score=round(details.score, 4),
        prev_bp_score=round(prev_bp_score, 4),
        avg_momentum=round(details.avg_momentum, 4),
        momentum=round(details.momentum, 4),
        is_green=is_green,
        recent_cross_above_trend=recent_cross,
        avg_momentum_rising=avg_momentum_rising,
        macd_above_signal=details.macd_above_signal,
        macd_positive=details.macd_positive,
        ao_positive=details.ao_positive,
        ema5_above_ema20=details.ema5_above_ema20,
        breakout_high=details.breakout_high,
        daily_volume_ratio=round(daily_volume_ratio, 4),
        daily_atr_pct=round(daily_atr_pct, 6),
        daily_range_pct=round(daily_range_pct, 6),
        ema_fast=round(ema_fast, 4),
        ema_slow=round(ema_slow, 4),
        ema_extension_pct=round(ema_extension_pct, 6),
        last_price=round(price, 4),
        last_daily_change_pct=round(_latest_daily_change_pct(closes), 4),
        quality_flags=tuple(quality_flags),
        days=len(ordered),
    )
    return candidate, None


def rank_candidates(
    symbols: list[str],
    bars_by_symbol: dict[str, list[Bar]],
) -> tuple[list[BreakoutPowerCandidate], list[CandidateReject], dict[str, int]]:
    ranked: list[BreakoutPowerCandidate] = []
    rejected: list[CandidateReject] = []
    stage_counts: dict[str, int] = {
        "universe_symbols": len(symbols),
        "passed_indicator_data": 0,
        "passed_above_trend": 0,
        "passed_green_momentum": 0,
        "passed_recent_cross": 0,
        "passed_avg_momentum_rising": 0,
        "passed_macd_above_signal": 0,
        "passed_macd_positive": 0,
        "passed_ao_positive": 0,
        "passed_ema_alignment": 0,
        "passed_breakout_high": 0,
        "passed_volume": 0,
        "passed_daily_atr": 0,
        "passed_daily_range": 0,
        "passed_not_overextended": 0,
    }
    for symbol in symbols:
        candidate, reject = evaluate_symbol(symbol, bars_by_symbol.get(symbol, []), stage_counts=stage_counts)
        if candidate is not None:
            ranked.append(candidate)
        elif reject is not None:
            rejected.append(reject)
    ranked.sort(key=lambda row: row.score, reverse=True)
    return ranked, rejected, stage_counts


def deterministic_plan(
    candidates: list[BreakoutPowerCandidate],
    rejected: list[CandidateReject],
    strategy: str,
    limit: int,
    *,
    filter_stage_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    top = candidates[:limit]
    settings: dict[str, Any] = {}
    if filter_stage_counts:
        settings["filter_stage_counts"] = dict(filter_stage_counts)
        settings["filter_thresholds"] = {
            "min_daily_bar_count": MIN_DAILY_BAR_COUNT,
            "daily_cross_lookback": DAILY_CROSS_LOOKBACK,
            "daily_green_threshold": DAILY_GREEN_THRESHOLD,
            "daily_trend_line": DAILY_TREND_LINE,
            "daily_volume_ratio_min": DAILY_VOLUME_RATIO_MIN,
            "daily_volume_lookback": DAILY_VOLUME_LOOKBACK,
            "ema_fast": DAILY_EMA_FAST,
            "ema_slow": DAILY_EMA_SLOW,
            "max_ema_extension_pct": DAILY_MAX_EMA_EXTENSION_PCT,
            "daily_atr_period": DAILY_ATR_PERIOD,
            "daily_min_atr_pct": DAILY_MIN_ATR_PCT,
            "daily_max_atr_pct": DAILY_MAX_ATR_PCT,
            "daily_range_lookback": DAILY_RANGE_LOOKBACK,
            "daily_min_range_pct": DAILY_MIN_RANGE_PCT,
            "indicator_input": "daily OHLCV bars",
        }
    return {
        "strategy": strategy,
        "selection_stage": "ranked",
        "note": (
            "Daily BreakOut Power ranker: scores BP score, green avg_momentum, recent cross above 50, "
            "MACD/AO/EMA alignment, breakout-high participation, daily volume, volatility, range, and overextension."
        ),
        "symbols": [row.symbol for row in top],
        "ranked": [asdict(row) for row in top],
        "rejected": [asdict(row) for row in rejected],
        "settings": settings,
        "risk_note": (
            "Selector uses daily bars to build a watchlist; breakout_power still waits for minute-bar "
            "BP cross above 50 with green avg_momentum before trading."
        ),
    }


def ai_breakout_power_selection(ranked: list[dict[str, Any]], limit: int) -> dict[str, Any] | None:
    settings = _selector_settings()
    payload = {
        "strategy": "breakout_power",
        "selection_rules": {
            "must_choose_from_ranked": True,
            "focus": (
                "prefer daily BP score above 50, green avg_momentum, recent BP crosses, rising avg_momentum, "
                "aligned MACD/AO/EMA, tradable daily volatility/range, and avoid overextended names"
            ),
        },
        "ranked": ranked,
        "limit": limit,
    }
    response_text = request_json_response(
        settings,
        (
            "Review the breakout_power ranked candidates and return only JSON. "
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


def validated_breakout_power_selection(plan: dict[str, Any], ranked: list[dict[str, Any]], limit: int) -> dict[str, Any]:
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
        ai_delta = max(-AI_SCORE_DELTA_LIMIT, min(AI_SCORE_DELTA_LIMIT, float(adjustment.get("ai_score_delta", 0.0) or 0.0)))
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
    return {
        "strategy": "breakout_power",
        "selection_stage": "ranked",
        "symbols": selected,
        "ranked": normalized_ranked[:limit],
        "rejected": [item for item in (plan.get("rejected") or []) if str(item).upper() in available],
        "settings": plan.get("settings") if isinstance(plan.get("settings"), dict) else {},
        "risk_note": str(plan.get("risk_note") or "Embedded AI ranking over deterministic BreakOut Power candidates."),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build breakout_power plan from daily BreakOut Power candidates.")
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
        help="Output JSON path (default data/breakout_power_plan.json).",
    )
    parser.add_argument("--use-ai", action="store_true", help="Use OpenAI to refine the final ranked symbol list.")
    args = parser.parse_args()

    symbols = load_universe(args.universe_file, args.symbols)
    settings = _selector_settings()
    if args.daily_lookback_days < MIN_DAILY_BAR_COUNT:
        raise ValueError(f"--daily-lookback-days must be at least {MIN_DAILY_BAR_COUNT}")
    bars_by_symbol = load_market_data(settings, symbols, args.daily_lookback_days)
    candidates, rejected, stage_counts = rank_candidates(symbols, bars_by_symbol)
    plan = deterministic_plan(candidates, rejected, "breakout_power", args.top, filter_stage_counts=stage_counts)
    result: dict[str, Any] = {
        "strategy": "breakout_power",
        "selected_symbols": list(plan["symbols"]),
        "symbols_env_line": format_symbols_env_line(plan["symbols"]),
        "selection_plan": plan,
        "filter_stage_counts": stage_counts,
        "ai_enabled": args.use_ai,
        "plan_output": str(args.plan_output),
    }
    args.plan_output.parent.mkdir(parents=True, exist_ok=True)
    args.plan_output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    if args.use_ai:
        ai_plan = ai_breakout_power_selection(plan["ranked"], args.top)
        if ai_plan is None:
            result["ai_selection"] = None
            result["ai_error"] = "OpenAI not configured or client unavailable."
        else:
            validated = validated_breakout_power_selection(ai_plan, plan["ranked"], args.top)
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
