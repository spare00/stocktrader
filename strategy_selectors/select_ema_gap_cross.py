"""Select ema_gap_cross symbols from daily EMA5/EMA20 golden-cross alignment."""

from __future__ import annotations

import argparse
import json
import math
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
from strategies.ema_gap_cross import recent_ema_cross_above
from strategies.macd_early_impulse import _ema_series


DEFAULT_UNIVERSE_FILE = Path("data/opening_universe.txt")
DEFAULT_PLAN_FILE = default_plan_file_for_strategy("ema_gap_cross")
DEFAULT_DAILY_LOOKBACK_DAYS = 120
MIN_DAILY_BAR_COUNT = 30
DAILY_CROSS_LOOKBACK = 5
DAILY_EMA_FAST = 5
DAILY_EMA_MID = 10
DAILY_EMA_SLOW = 20
DAILY_VOLUME_LOOKBACK = 20
DAILY_VOLUME_RATIO_TARGET = 1.0
DAILY_ATR_PERIOD = 14
DAILY_RANGE_LOOKBACK = 20
DAILY_MEDIAN_DOLLAR_VOLUME_LOOKBACK = 20
DAILY_MIN_MEDIAN_DOLLAR_VOLUME = 2_000_000.0
DAILY_MIDCAP_SWEET_MIN = 5_000_000.0
DAILY_MIDCAP_SWEET_MAX = 80_000_000.0
DAILY_MEGA_CAP_PENALTY_START = 150_000_000.0
DAILY_MAX_GAP_EXTENSION_PCT = 0.12
AI_SCORE_DELTA_LIMIT = 12.0
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
class EmaGapCrossCandidate:
    symbol: str
    score: float
    setup_stage: str
    ema5: float
    ema10: float
    ema20: float
    ema_gap_pct: float
    bars_since_cross: int
    ema20_slope_pct: float
    ema5_above_ema10: bool
    daily_volume_ratio: float
    cross_day_volume_ratio: float
    daily_atr_pct: float
    daily_range_pct: float
    median_dollar_volume: float
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


def _volume_ratio_at_index(
    bars: list[Bar],
    index: int,
    *,
    lookback: int = DAILY_VOLUME_LOOKBACK,
) -> float:
    if index < 0 or index >= len(bars):
        return 0.0
    volume = bars[index].volume
    if volume <= 0:
        return 0.0
    start = max(0, index - lookback)
    baseline_items = [bar.volume for bar in bars[start:index] if bar.volume > 0]
    baseline = median(baseline_items or [0.0])
    return volume / baseline if baseline > 0 else 0.0


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


def _median_dollar_volume(bars: list[Bar], lookback: int = DAILY_MEDIAN_DOLLAR_VOLUME_LOOKBACK) -> float:
    window = bars[-max(1, lookback) :]
    dollar_volumes = [bar.close * bar.volume for bar in window if bar.close > 0 and bar.volume > 0]
    return float(median(dollar_volumes)) if dollar_volumes else 0.0


def _midcap_liquidity_score(median_dollar_volume: float) -> float:
    """Reward mid/small-cap turnover; penalize mega-cap liquidity."""
    if median_dollar_volume < DAILY_MIN_MEDIAN_DOLLAR_VOLUME:
        return -25.0
    if DAILY_MIDCAP_SWEET_MIN <= median_dollar_volume <= DAILY_MIDCAP_SWEET_MAX:
        center = math.sqrt(DAILY_MIDCAP_SWEET_MIN * DAILY_MIDCAP_SWEET_MAX)
        distance = abs(math.log10(max(median_dollar_volume, 1.0) / center))
        return max(10.0, 28.0 - distance * 14.0)
    if median_dollar_volume < DAILY_MIDCAP_SWEET_MIN:
        return _bounded((median_dollar_volume / DAILY_MIDCAP_SWEET_MIN) * 12.0, low=0.0, high=12.0)
    excess = (median_dollar_volume - DAILY_MIDCAP_SWEET_MAX) / DAILY_MIDCAP_SWEET_MAX
    return max(-20.0, 8.0 - excess * 22.0)


def _volatility_score(atr_pct: float, range_pct: float) -> float:
    """Higher daily ATR and range earn more points."""
    atr_component = _bounded(atr_pct * 520.0, low=0.0, high=28.0)
    range_component = _bounded(range_pct * 140.0, low=0.0, high=22.0)
    return atr_component + range_component


def _volume_score(daily_volume_ratio: float, cross_day_volume_ratio: float) -> float:
    latest = min(max(daily_volume_ratio, 0.0), 4.0) * 5.0
    cross_day = min(max(cross_day_volume_ratio, 0.0), 4.0) * 6.0
    target_bonus = 4.0 if daily_volume_ratio >= DAILY_VOLUME_RATIO_TARGET else 0.0
    cross_bonus = 4.0 if cross_day_volume_ratio >= DAILY_VOLUME_RATIO_TARGET else 0.0
    return latest + cross_day + target_bonus + cross_bonus


def _bounded(value: float, *, low: float, high: float) -> float:
    return max(low, min(high, value))


def _setup_stage(*, bars_since_cross: int, ema5_above_ema10: bool, ema20_slope_pct: float) -> str:
    if bars_since_cross <= 1 and ema5_above_ema10 and ema20_slope_pct > 0:
        return "fresh_cross"
    if bars_since_cross <= DAILY_CROSS_LOOKBACK and ema5_above_ema10:
        return "recent_cross"
    return "holding"


def _selector_settings() -> Settings:
    try:
        return load_settings(strategy_names=["ema_gap_cross"], validate=False)
    except Exception:
        return Settings(
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
            strategy_names=["ema_gap_cross"],
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
) -> tuple[EmaGapCrossCandidate | None, CandidateReject | None]:
    def _bump(key: str) -> None:
        if stage_counts is not None:
            stage_counts[key] = stage_counts.get(key, 0) + 1

    ordered = sorted((bar for bar in bars if bar.close > 0), key=lambda item: item.start_ms)
    if len(ordered) < MIN_DAILY_BAR_COUNT:
        return None, CandidateReject(symbol, "bars", f"need >= {MIN_DAILY_BAR_COUNT} daily bars, got {len(ordered)}")

    closes = [float(bar.close) for bar in ordered]
    price = closes[-1]
    ema5 = _ema_series(closes, DAILY_EMA_FAST)
    ema10 = _ema_series(closes, DAILY_EMA_MID)
    ema20 = _ema_series(closes, DAILY_EMA_SLOW)
    if len(ema5) < 2 or len(ema10) < 2 or len(ema20) < 2:
        return None, CandidateReject(symbol, "ema", "insufficient daily EMA history")

    _bump("passed_indicator_data")

    recent_cross, bars_since_cross = recent_ema_cross_above(
        ema5,
        ema20,
        lookback=DAILY_CROSS_LOOKBACK,
    )
    if not recent_cross or bars_since_cross is None:
        return None, CandidateReject(
            symbol,
            "golden_cross",
            f"no daily EMA{DAILY_EMA_FAST}/EMA{DAILY_EMA_SLOW} cross in last {DAILY_CROSS_LOOKBACK} bars",
        )
    _bump("passed_recent_golden_cross")

    ema5_now = ema5[-1]
    ema10_now = ema10[-1]
    ema20_now = ema20[-1]
    if ema5_now <= ema20_now:
        return None, CandidateReject(
            symbol,
            "alignment",
            f"EMA{DAILY_EMA_FAST} {ema5_now:.4f} not above EMA{DAILY_EMA_SLOW} {ema20_now:.4f}",
        )
    _bump("passed_ema5_above_ema20")

    ema5_above_ema10 = ema5_now > ema10_now
    ema20_prev = ema20[-2]
    ema20_slope_pct = (ema20_now - ema20_prev) / ema20_prev if ema20_prev > 0 else 0.0
    ema_gap_pct = (ema5_now - ema20_now) / ema20_now if ema20_now > 0 else 0.0
    daily_volume_ratio = _daily_volume_ratio(ordered)
    cross_index = len(ordered) - 1 - bars_since_cross
    cross_day_volume_ratio = _volume_ratio_at_index(ordered, cross_index)
    daily_atr_pct = _atr_pct(ordered)
    daily_range_pct = _range_pct(ordered)
    median_dollar_volume = _median_dollar_volume(ordered)
    last_daily_change_pct = _latest_daily_change_pct(closes)

    if median_dollar_volume < DAILY_MIN_MEDIAN_DOLLAR_VOLUME:
        return None, CandidateReject(
            symbol,
            "liquidity",
            (
                f"median dollar volume {median_dollar_volume:.0f} "
                f"< {DAILY_MIN_MEDIAN_DOLLAR_VOLUME:.0f}"
            ),
        )
    _bump("passed_liquidity_floor")

    quality_flags: list[str] = []
    if ema5_above_ema10:
        _bump("passed_ema5_above_ema10")
    else:
        quality_flags.append(f"EMA{DAILY_EMA_FAST} not above EMA{DAILY_EMA_MID}")
    if ema20_slope_pct > 0:
        _bump("passed_ema20_rising")
    else:
        quality_flags.append("EMA20 not rising on latest bar")
    if daily_volume_ratio >= DAILY_VOLUME_RATIO_TARGET:
        _bump("passed_volume_target")
    else:
        quality_flags.append(
            f"daily volume ratio {daily_volume_ratio:.2f} < target {DAILY_VOLUME_RATIO_TARGET:.2f}"
        )
    if cross_day_volume_ratio >= DAILY_VOLUME_RATIO_TARGET:
        _bump("passed_cross_day_volume")
    else:
        quality_flags.append(
            f"cross-day volume ratio {cross_day_volume_ratio:.2f} < target {DAILY_VOLUME_RATIO_TARGET:.2f}"
        )
    if last_daily_change_pct > 0:
        _bump("passed_positive_daily_change")
    else:
        quality_flags.append(f"latest daily change {last_daily_change_pct:.2f}% <= 0")
    if DAILY_MIDCAP_SWEET_MIN <= median_dollar_volume <= DAILY_MIDCAP_SWEET_MAX:
        _bump("passed_midcap_liquidity_tier")
    elif median_dollar_volume > DAILY_MEGA_CAP_PENALTY_START:
        quality_flags.append(
            f"mega-cap liquidity {median_dollar_volume:.0f} > {DAILY_MEGA_CAP_PENALTY_START:.0f}"
        )
    else:
        quality_flags.append(
            f"median dollar volume {median_dollar_volume:.0f} outside mid-cap sweet spot "
            f"{DAILY_MIDCAP_SWEET_MIN:.0f}-{DAILY_MIDCAP_SWEET_MAX:.0f}"
        )
    if daily_atr_pct >= 0.025:
        _bump("passed_high_atr")
    if daily_range_pct >= 0.05:
        _bump("passed_high_range")
    if ema_gap_pct <= DAILY_MAX_GAP_EXTENSION_PCT:
        _bump("passed_gap_not_extended")
    else:
        quality_flags.append(f"EMA gap {ema_gap_pct:.2%} > {DAILY_MAX_GAP_EXTENSION_PCT:.2%}")

    recency_score = max(0.0, 24.0 - (bars_since_cross * 4.0))
    alignment_score = 10.0 if ema5_above_ema10 else -8.0
    slope_score = _bounded(ema20_slope_pct * 400.0, low=-6.0, high=10.0)
    gap_score = _bounded(ema_gap_pct * 160.0, low=0.0, high=14.0)
    volume_component = _volume_score(daily_volume_ratio, cross_day_volume_ratio)
    volatility_component = _volatility_score(daily_atr_pct, daily_range_pct)
    momentum_score = _bounded(last_daily_change_pct * 2.0, low=-8.0, high=14.0)
    midcap_component = _midcap_liquidity_score(median_dollar_volume)
    mega_cap_penalty = 0.0
    if median_dollar_volume > DAILY_MEGA_CAP_PENALTY_START:
        mega_cap_penalty = (
            (median_dollar_volume - DAILY_MEGA_CAP_PENALTY_START) / DAILY_MEGA_CAP_PENALTY_START
        ) * 18.0
    extension_penalty = max(0.0, ema_gap_pct - DAILY_MAX_GAP_EXTENSION_PCT) * 120.0
    score = (
        recency_score
        + alignment_score
        + slope_score
        + gap_score
        + volume_component
        + volatility_component
        + momentum_score
        + midcap_component
        - mega_cap_penalty
        - extension_penalty
    )

    setup_stage = _setup_stage(
        bars_since_cross=bars_since_cross,
        ema5_above_ema10=ema5_above_ema10,
        ema20_slope_pct=ema20_slope_pct,
    )

    candidate = EmaGapCrossCandidate(
        symbol=symbol,
        score=round(score, 6),
        setup_stage=setup_stage,
        ema5=round(ema5_now, 4),
        ema10=round(ema10_now, 4),
        ema20=round(ema20_now, 4),
        ema_gap_pct=round(ema_gap_pct, 6),
        bars_since_cross=bars_since_cross,
        ema20_slope_pct=round(ema20_slope_pct, 6),
        ema5_above_ema10=ema5_above_ema10,
        daily_volume_ratio=round(daily_volume_ratio, 4),
        cross_day_volume_ratio=round(cross_day_volume_ratio, 4),
        daily_atr_pct=round(daily_atr_pct, 6),
        daily_range_pct=round(daily_range_pct, 6),
        median_dollar_volume=round(median_dollar_volume, 2),
        last_price=round(price, 4),
        last_daily_change_pct=round(last_daily_change_pct, 4),
        quality_flags=tuple(quality_flags),
        days=len(ordered),
    )
    return candidate, None


def rank_candidates(
    symbols: list[str],
    bars_by_symbol: dict[str, list[Bar]],
) -> tuple[list[EmaGapCrossCandidate], list[CandidateReject], dict[str, int]]:
    ranked: list[EmaGapCrossCandidate] = []
    rejected: list[CandidateReject] = []
    stage_counts: dict[str, int] = {
        "universe_symbols": len(symbols),
        "passed_indicator_data": 0,
        "passed_recent_golden_cross": 0,
        "passed_ema5_above_ema20": 0,
        "passed_ema5_above_ema10": 0,
        "passed_ema20_rising": 0,
        "passed_liquidity_floor": 0,
        "passed_volume_target": 0,
        "passed_cross_day_volume": 0,
        "passed_positive_daily_change": 0,
        "passed_midcap_liquidity_tier": 0,
        "passed_high_atr": 0,
        "passed_high_range": 0,
        "passed_gap_not_extended": 0,
        "ranked_candidates": 0,
    }
    for symbol in symbols:
        candidate, reject = evaluate_symbol(symbol, bars_by_symbol.get(symbol, []), stage_counts=stage_counts)
        if candidate is not None:
            ranked.append(candidate)
        elif reject is not None:
            rejected.append(reject)
    ranked.sort(key=lambda row: row.score, reverse=True)
    if stage_counts is not None:
        stage_counts["ranked_candidates"] = len(ranked)
    return ranked, rejected, stage_counts


def deterministic_plan(
    candidates: list[EmaGapCrossCandidate],
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
            "ema_fast": DAILY_EMA_FAST,
            "ema_mid": DAILY_EMA_MID,
            "ema_slow": DAILY_EMA_SLOW,
            "selection_mode": "score_ranked_top_n",
            "daily_volume_ratio_target": DAILY_VOLUME_RATIO_TARGET,
            "daily_range_lookback": DAILY_RANGE_LOOKBACK,
            "daily_min_median_dollar_volume": DAILY_MIN_MEDIAN_DOLLAR_VOLUME,
            "daily_midcap_sweet_min": DAILY_MIDCAP_SWEET_MIN,
            "daily_midcap_sweet_max": DAILY_MIDCAP_SWEET_MAX,
            "daily_mega_cap_penalty_start": DAILY_MEGA_CAP_PENALTY_START,
            "max_gap_extension_pct": DAILY_MAX_GAP_EXTENSION_PCT,
            "indicator_input": "daily OHLCV bars",
        }
    return {
        "strategy": strategy,
        "selection_stage": "ranked",
        "note": (
            "Daily EMA gap ranker: requires EMA5 golden cross above EMA20 within the last "
            f"{DAILY_CROSS_LOOKBACK} sessions and EMA5 still above EMA20, then ranks all "
            "passers by score. Favors mid/small-cap liquidity, high ATR/range, cross-day volume, "
            "fresh crosses, and positive daily change; penalizes mega-cap names and overextension."
        ),
        "symbols": [row.symbol for row in top],
        "ranked": [asdict(row) for row in top],
        "rejected": [asdict(row) for row in rejected],
        "settings": settings,
        "risk_note": (
            "Selector uses daily EMA alignment for the watchlist; ema_gap_cross still waits for "
            "minute-bar EMA5 golden cross right after two bars below EMA20 before entry."
        ),
    }


def ai_ema_gap_cross_selection(ranked: list[dict[str, Any]], limit: int) -> dict[str, Any] | None:
    settings = _selector_settings()
    payload = {
        "strategy": "ema_gap_cross",
        "selection_rules": {
            "must_choose_from_ranked": True,
            "focus": (
                "prefer fresher daily golden crosses, EMA5 above EMA10, rising EMA20, "
                "strong cross-day volume, positive daily change, higher ATR/range, "
                "mid-cap liquidity over mega-cap names, and avoid names already too extended above EMA20"
            ),
        },
        "ranked": ranked,
        "limit": limit,
    }
    response_text = request_json_response(
        settings,
        (
            "Review the ema_gap_cross ranked candidates and return only JSON. "
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


def validated_ema_gap_cross_selection(plan: dict[str, Any], ranked: list[dict[str, Any]], limit: int) -> dict[str, Any]:
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
        "strategy": "ema_gap_cross",
        "selection_stage": "ranked",
        "symbols": selected,
        "ranked": normalized_ranked[:limit],
        "rejected": [item for item in (plan.get("rejected") or []) if str(item).upper() in available],
        "settings": plan.get("settings") if isinstance(plan.get("settings"), dict) else {},
        "risk_note": str(plan.get("risk_note") or "Embedded AI ranking over deterministic EMA gap candidates."),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build ema_gap_cross plan from daily EMA golden-cross candidates.")
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
        help="Output JSON path (default data/ema_gap_cross_plan.json).",
    )
    parser.add_argument("--use-ai", action="store_true", help="Use OpenAI to refine the final ranked symbol list.")
    args = parser.parse_args()

    symbols = load_universe(args.universe_file, args.symbols)
    settings = _selector_settings()
    if args.daily_lookback_days < MIN_DAILY_BAR_COUNT:
        raise ValueError(f"--daily-lookback-days must be at least {MIN_DAILY_BAR_COUNT}")
    bars_by_symbol = load_market_data(settings, symbols, args.daily_lookback_days)
    candidates, rejected, stage_counts = rank_candidates(symbols, bars_by_symbol)
    plan = deterministic_plan(candidates, rejected, "ema_gap_cross", args.top, filter_stage_counts=stage_counts)
    result: dict[str, Any] = {
        "strategy": "ema_gap_cross",
        "selected_symbols": list(plan["symbols"]),
        "symbols_env_line": format_symbols_env_line(plan["symbols"]),
        "selection_plan": plan,
        "filter_stage_counts": stage_counts,
        "ranked_candidates": stage_counts.get("ranked_candidates", len(candidates)),
        "selected_count": len(plan["symbols"]),
        "requested_top": args.top,
        "ai_enabled": args.use_ai,
        "plan_output": str(args.plan_output),
    }
    args.plan_output.parent.mkdir(parents=True, exist_ok=True)
    args.plan_output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    if args.use_ai:
        ai_plan = ai_ema_gap_cross_selection(plan["ranked"], args.top)
        if ai_plan is None:
            result["ai_selection"] = None
            result["ai_error"] = "OpenAI not configured or client unavailable."
        else:
            validated = validated_ema_gap_cross_selection(ai_plan, plan["ranked"], args.top)
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
