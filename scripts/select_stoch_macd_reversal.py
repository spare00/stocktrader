"""Select STOCH/MACD reversal symbols from a daily confirmation setup."""

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


DEFAULT_UNIVERSE_FILE = Path("data/opening_universe.txt")
DEFAULT_PLAN_FILE = default_plan_file_for_strategy("stoch_macd_reversal")
DEFAULT_DAILY_LOOKBACK_DAYS = 140
MIN_DAILY_BAR_COUNT = 45
STOCH_PERIOD = 14
STOCH_D_PERIOD = 3
STOCH_SMOOTH_K = 3
DAILY_EMA_CONFIRM = 5
SUPERTREND_PERIOD = 7
SUPERTREND_MULTIPLIER = 3.0
DAILY_VOLUME_LOOKBACK = 20
DAILY_VOLUME_RATIO_MIN = 0.8
DAILY_EMA_FAST = 20
DAILY_EMA_SLOW = 50
DAILY_MAX_EMA_EXTENSION_PCT = 0.18
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
class StochMACDReversalCandidate:
    symbol: str
    score: float
    setup_stage: str
    stoch_k: float
    stoch_d: float
    daily_macd: float
    daily_signal: float
    daily_hist: float
    ema_confirm: float
    supertrend: float
    supertrend_bullish: bool
    daily_volume_ratio: float
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


def _sma(values: list[float], period: int) -> list[float]:
    if not values or period <= 1:
        return values[:]
    out: list[float] = []
    for index in range(len(values)):
        start = max(0, index - period + 1)
        window = values[start : index + 1]
        out.append(sum(window) / len(window))
    return out


def _macd_components(values: list[float]) -> tuple[list[float], list[float], list[float]]:
    ema12 = _ema_series(values, 12)
    ema26 = _ema_series(values, 26)
    macd_line = [a - b for a, b in zip(ema12, ema26)]
    signal_line = _ema_series(macd_line, 9)
    hist = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, hist


def _stoch_components(bars: list[Bar]) -> tuple[list[float], list[float]]:
    if len(bars) < STOCH_PERIOD + STOCH_SMOOTH_K + STOCH_D_PERIOD:
        return [], []
    raw_k: list[float] = []
    for index in range(STOCH_PERIOD - 1, len(bars)):
        window = bars[index - STOCH_PERIOD + 1 : index + 1]
        high = max(bar.high for bar in window)
        low = min(bar.low for bar in window)
        if high <= low:
            raw_k.append(50.0)
        else:
            raw_k.append(((bars[index].close - low) / (high - low)) * 100.0)
    k_values = _sma(raw_k, STOCH_SMOOTH_K)
    d_values = _sma(k_values, STOCH_D_PERIOD)
    return k_values, d_values


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


def _bounded(value: float, *, low: float, high: float) -> float:
    return max(low, min(high, value))


def _supertrend(bars: list[Bar], period: int = SUPERTREND_PERIOD, multiplier: float = SUPERTREND_MULTIPLIER) -> tuple[float, bool] | None:
    if period <= 0 or multiplier <= 0 or len(bars) < period + 1:
        return None

    true_ranges: list[float] = []
    for index, bar in enumerate(bars):
        if index == 0:
            true_ranges.append(bar.high - bar.low)
            continue
        prev_close = bars[index - 1].close
        true_ranges.append(
            max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            )
        )

    atr_values: list[float | None] = []
    for index in range(len(true_ranges)):
        if index + 1 < period:
            atr_values.append(None)
        elif index + 1 == period:
            atr_values.append(sum(true_ranges[:period]) / period)
        else:
            prev_atr = atr_values[-1]
            if prev_atr is None:
                return None
            atr_values.append(((prev_atr * (period - 1)) + true_ranges[index]) / period)

    first_atr_index = next((index for index, value in enumerate(atr_values) if value is not None), None)
    if first_atr_index is None:
        return None

    first_bar = bars[first_atr_index]
    first_atr = atr_values[first_atr_index]
    if first_atr is None:
        return None
    hl2 = (first_bar.high + first_bar.low) / 2
    final_upper = hl2 + multiplier * first_atr
    final_lower = hl2 - multiplier * first_atr
    bullish = first_bar.close >= hl2
    supertrend = final_lower if bullish else final_upper

    for index in range(first_atr_index + 1, len(bars)):
        bar = bars[index]
        atr = atr_values[index]
        if atr is None:
            continue
        basic_upper = ((bar.high + bar.low) / 2) + multiplier * atr
        basic_lower = ((bar.high + bar.low) / 2) - multiplier * atr
        prev_close = bars[index - 1].close
        if basic_upper < final_upper or prev_close > final_upper:
            final_upper = basic_upper
        if basic_lower > final_lower or prev_close < final_lower:
            final_lower = basic_lower
        if bar.close > final_upper:
            bullish = True
        elif bar.close < final_lower:
            bullish = False
        supertrend = final_lower if bullish else final_upper

    return supertrend, bullish


def _selector_settings() -> Settings:
    try:
        return load_settings(strategy_names=["stoch_macd_reversal"], validate=False)
    except Exception:
        return Settings(
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
            strategy_names=["stoch_macd_reversal"],
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


def evaluate_symbol(
    symbol: str,
    bars: list[Bar],
    *,
    stage_counts: dict[str, int] | None = None,
) -> tuple[StochMACDReversalCandidate | None, CandidateReject | None]:
    def _bump(key: str) -> None:
        if stage_counts is not None:
            stage_counts[key] = stage_counts.get(key, 0) + 1

    ordered = sorted((bar for bar in bars if bar.close > 0), key=lambda item: item.start_ms)
    if len(ordered) < MIN_DAILY_BAR_COUNT:
        return None, CandidateReject(symbol, "bars", f"need >= {MIN_DAILY_BAR_COUNT} daily bars, got {len(ordered)}")

    closes = [float(bar.close) for bar in ordered]
    price = closes[-1]
    k_values, d_values = _stoch_components(ordered)
    macd_line, signal_line, hist = _macd_components(closes)
    ema_confirm_series = _ema_series(closes, DAILY_EMA_CONFIRM)
    supertrend = _supertrend(ordered)
    if not k_values or not d_values or not hist or not macd_line or not signal_line or not ema_confirm_series or supertrend is None:
        return None, CandidateReject(symbol, "indicators", "insufficient daily EMA/SuperTrend/STOCH/MACD points")

    _bump("passed_indicator_data")

    quality_flags: list[str] = []
    k_now = k_values[-1]
    d_now = d_values[-1]
    stoch_bullish = k_now > d_now
    if stoch_bullish:
        _bump("passed_stoch_bullish")
    else:
        quality_flags.append(f"STOCH not bullish k={k_now:.1f} d={d_now:.1f}")

    ccc = macd_line[-1]
    macd_signal = signal_line[-1]
    macd_confirmed = ccc > macd_signal and ccc >= 0
    if macd_confirmed:
        _bump("passed_macd_confirmed")
    else:
        quality_flags.append(f"MACD/CCC not confirmed ccc={ccc:.4f} signal={macd_signal:.4f}")

    ema_confirm = ema_confirm_series[-1]
    supertrend_value, supertrend_bullish = supertrend
    trend_confirmed = supertrend_bullish and ema_confirm > supertrend_value
    if trend_confirmed:
        _bump("passed_trend_confirmed")
    else:
        quality_flags.append(f"EMA{DAILY_EMA_CONFIRM} not above bullish SuperTrend")

    daily_volume_ratio = _daily_volume_ratio(ordered)
    if daily_volume_ratio >= DAILY_VOLUME_RATIO_MIN:
        _bump("passed_volume")
    else:
        quality_flags.append(f"daily volume ratio {daily_volume_ratio:.2f} < {DAILY_VOLUME_RATIO_MIN:.2f}")

    ema_fast_series = _ema_series(closes, DAILY_EMA_FAST)
    ema_slow_series = _ema_series(closes, DAILY_EMA_SLOW)
    ema_fast = ema_fast_series[-1] if ema_fast_series else 0.0
    ema_slow = ema_slow_series[-1] if ema_slow_series else 0.0
    ema_extension_pct = (price - ema_fast) / ema_fast if ema_fast > 0 else 0.0
    not_overextended = ema_extension_pct <= DAILY_MAX_EMA_EXTENSION_PCT
    if not_overextended:
        _bump("passed_not_overextended")
    else:
        quality_flags.append(f"price extension {ema_extension_pct:.2%} > {DAILY_MAX_EMA_EXTENSION_PCT:.2%}")

    setup_stage = "confirmed_stack" if trend_confirmed and macd_confirmed and stoch_bullish else "not_confirmed"
    trend_score = 26.0 if trend_confirmed else -18.0
    macd_score = 26.0 if macd_confirmed else -18.0
    stoch_score = 18.0 if stoch_bullish else -12.0
    macd_strength_score = _bounded((ccc / price if price > 0 else 0.0) * 2000.0, low=0.0, high=22.0)
    hist_score = _bounded((hist[-1] / price if price > 0 else 0.0) * 2000.0, low=-8.0, high=16.0)
    trend_distance_score = _bounded(((ema_confirm - supertrend_value) / price if price > 0 else 0.0) * 1000.0, low=-12.0, high=18.0)
    volume_score = min(daily_volume_ratio, 3.0) * 4.0
    extension_penalty = max(0.0, ema_extension_pct - DAILY_MAX_EMA_EXTENSION_PCT) * 100.0
    score = (
        trend_score
        + macd_score
        + stoch_score
        + macd_strength_score
        + hist_score
        + trend_distance_score
        + volume_score
        - extension_penalty
    )

    candidate = StochMACDReversalCandidate(
        symbol=symbol,
        score=round(score, 6),
        setup_stage=setup_stage,
        stoch_k=round(k_now, 4),
        stoch_d=round(d_now, 4),
        daily_macd=round(ccc, 6),
        daily_signal=round(macd_signal, 6),
        daily_hist=round(hist[-1], 6),
        ema_confirm=round(ema_confirm, 4),
        supertrend=round(supertrend_value, 4),
        supertrend_bullish=supertrend_bullish,
        daily_volume_ratio=round(daily_volume_ratio, 4),
        ema_fast=round(ema_fast, 4),
        ema_slow=round(ema_slow, 4),
        ema_extension_pct=round(ema_extension_pct, 6),
        last_price=round(price, 4),
        last_daily_change_pct=round(_latest_daily_change_pct(closes), 4),
        quality_flags=tuple(quality_flags),
        days=len(ordered),
    )
    return candidate, None


def rank_candidates(symbols: list[str], bars_by_symbol: dict[str, list[Bar]]) -> tuple[list[StochMACDReversalCandidate], list[CandidateReject], dict[str, int]]:
    ranked: list[StochMACDReversalCandidate] = []
    rejected: list[CandidateReject] = []
    stage_counts: dict[str, int] = {
        "universe_symbols": len(symbols),
        "passed_indicator_data": 0,
        "passed_trend_confirmed": 0,
        "passed_macd_confirmed": 0,
        "passed_stoch_bullish": 0,
        "passed_volume": 0,
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
    candidates: list[StochMACDReversalCandidate],
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
            "stoch_period": STOCH_PERIOD,
            "stoch_d_period": STOCH_D_PERIOD,
            "stoch_smooth_k": STOCH_SMOOTH_K,
            "ema_confirm": DAILY_EMA_CONFIRM,
            "supertrend_period": SUPERTREND_PERIOD,
            "supertrend_multiplier": SUPERTREND_MULTIPLIER,
            "daily_volume_ratio_min": DAILY_VOLUME_RATIO_MIN,
            "daily_volume_lookback": DAILY_VOLUME_LOOKBACK,
            "ema_fast": DAILY_EMA_FAST,
            "ema_slow": DAILY_EMA_SLOW,
            "max_ema_extension_pct": DAILY_MAX_EMA_EXTENSION_PCT,
            "indicator_input": "daily OHLCV bars",
        }
    return {
        "strategy": strategy,
        "selection_stage": "ranked",
        "note": "Daily STOCH/MACD ranker: scores the same confirmation stack as the handler using daily bars.",
        "symbols": [row.symbol for row in top],
        "ranked": [asdict(row) for row in top],
        "rejected": [asdict(row) for row in rejected],
        "settings": settings,
        "risk_note": "Selector uses daily bars to build a watchlist; stoch_macd_reversal still waits for minute EMA/SuperTrend/MACD/STOCH confirmation before trading.",
    }


def ai_stoch_macd_selection(ranked: list[dict[str, Any]], limit: int) -> dict[str, Any] | None:
    settings = _selector_settings()
    payload = {
        "strategy": "stoch_macd_reversal",
        "selection_rules": {
            "must_choose_from_ranked": True,
            "focus": "prefer daily EMA/SuperTrend, MACD/CCC, and STOCH confirmation stacks",
        },
        "ranked": ranked,
        "limit": limit,
    }
    response_text = request_json_response(
        settings,
        (
            "Review the stoch_macd_reversal ranked candidates and return only JSON. "
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


def validated_stoch_macd_selection(plan: dict[str, Any], ranked: list[dict[str, Any]], limit: int) -> dict[str, Any]:
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
    return {
        "strategy": "stoch_macd_reversal",
        "selection_stage": "ranked",
        "symbols": selected,
        "ranked": normalized_ranked[:limit],
        "rejected": [item for item in (plan.get("rejected") or []) if str(item).upper() in available],
        "settings": plan.get("settings") if isinstance(plan.get("settings"), dict) else {},
        "risk_note": str(plan.get("risk_note") or "Embedded AI ranking over deterministic STOCH/MACD candidates."),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build stoch_macd_reversal plan from daily confirmation-stack candidates.")
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
        help="Output JSON path (default data/stoch_macd_reversal_plan.json).",
    )
    parser.add_argument("--use-ai", action="store_true", help="Use OpenAI to refine the final ranked symbol list.")
    args = parser.parse_args()

    symbols = load_universe(args.universe_file, args.symbols)
    settings = _selector_settings()
    if args.daily_lookback_days < MIN_DAILY_BAR_COUNT:
        raise ValueError(f"--daily-lookback-days must be at least {MIN_DAILY_BAR_COUNT}")
    bars_by_symbol = load_market_data(settings, symbols, args.daily_lookback_days)
    candidates, rejected, stage_counts = rank_candidates(symbols, bars_by_symbol)
    plan = deterministic_plan(candidates, rejected, "stoch_macd_reversal", args.top, filter_stage_counts=stage_counts)
    result: dict[str, Any] = {
        "strategy": "stoch_macd_reversal",
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
        ai_plan = ai_stoch_macd_selection(plan["ranked"], args.top)
        if ai_plan is None:
            result["ai_selection"] = None
            result["ai_error"] = "OpenAI not configured or client unavailable."
        else:
            validated = validated_stoch_macd_selection(ai_plan, plan["ranked"], args.top)
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
