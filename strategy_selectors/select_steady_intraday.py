from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from statistics import mean

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
DEFAULT_PLAN_FILE = default_plan_file_for_strategy("steady_intraday")
MARKET_OPEN = time(9, 30)
DAILY_MIN_ATR_PCT = 0.012
DAILY_MAX_ATR_PCT = 0.09
DAILY_MIN_RANGE_PCT = 0.04
DAILY_MAX_DOWNSIDE_GAP_PCT = 0.003
DAILY_MIN_CLOSE_POSITION = 0.55


@dataclass(frozen=True)
class SteadyIntradayCandidate:
    symbol: str
    score: float
    selection_stage: str
    price: float
    spread_bps: float | None
    ema_fast: float
    ema_mid: float
    ema_slow: float
    ema_mid_slope_pct: float
    atr_pct: float
    range_pct: float
    volume_ratio: float
    vwap_distance_pct: float | None
    dollar_volume: float
    pullback_reclaim_ready: bool
    orb_continuation_ready: bool
    quality_flags: tuple[str, ...] = ()


def extract_json_object(text: str) -> dict[str, object]:
    decoder = json.JSONDecoder()
    stripped = text.lstrip()
    result, _ = decoder.raw_decode(stripped)
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object.")
    return result


def parse_symbols(raw: str) -> list[str]:
    return [part.strip().upper() for part in raw.replace("\n", ",").split(",") if part.strip()]


def load_universe(path: Path | None, raw_symbols: str = "") -> list[str]:
    symbols: list[str] = []
    if raw_symbols:
        symbols = parse_symbols(raw_symbols)
    elif path and path.exists():
        text = path.read_text(encoding="utf-8")
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0]
            for raw_symbol in re.split(r"[\s,]+", line):
                symbol = raw_symbol.strip().upper()
                if symbol:
                    symbols.append(symbol)
    else:
        raise FileNotFoundError(f"Missing universe file: {path}. Run strategy_selectors/select_market_universe.py first.")
    return list(dict.fromkeys(symbols))


def usable_quote(quote: Quote | None) -> Quote | None:
    if quote is None:
        return None
    if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
        return None
    return quote


def latest_price(bars: list[Bar], quote: Quote | None) -> float:
    valid_quote = usable_quote(quote)
    if valid_quote:
        return valid_quote.mid
    return bars[-1].close if bars else 0.0


def ema(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    alpha = 2 / (period + 1)
    current = sum(values[:period]) / period
    for value in values[period:]:
        current = value * alpha + current * (1 - alpha)
    return current


def atr(bars: list[Bar], period: int) -> float | None:
    if period <= 0 or len(bars) < period + 1:
        return None
    true_ranges = []
    tail = bars[-(period + 1) :]
    for previous, current in zip(tail, tail[1:]):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return mean(true_ranges) if true_ranges else None


def session_vwap(bars: list[Bar]) -> float | None:
    total_volume = sum(bar.volume for bar in bars if bar.volume > 0)
    if total_volume <= 0:
        return None
    total_value = sum(bar.vwap * bar.volume for bar in bars if bar.volume > 0)
    return total_value / total_volume if total_value > 0 else None


def volume_ratio(bars: list[Bar], lookback: int = 10) -> float:
    if len(bars) < 2:
        return 0.0
    baseline = [bar.volume for bar in bars[-lookback - 1 : -1] if bar.volume > 0]
    if not baseline:
        return 0.0
    return bars[-1].volume / mean(baseline)


def range_pct(bars: list[Bar]) -> float:
    if not bars:
        return 0.0
    low = min(bar.low for bar in bars)
    high = max(bar.high for bar in bars)
    return (high - low) / low if low > 0 else 0.0


def close_position_in_range(bar: Bar) -> float:
    candle_range = bar.high - bar.low
    if candle_range <= 0:
        return 1.0 if bar.close >= bar.open else 0.0
    return max(0.0, min(1.0, (bar.close - bar.low) / candle_range))


def steady_atr_bounds(settings: Settings, stage: str) -> tuple[float, float]:
    if stage == "daily":
        return DAILY_MIN_ATR_PCT, DAILY_MAX_ATR_PCT
    return settings.steady_intraday_min_atr_pct, settings.steady_intraday_max_atr_pct


def steady_min_range_pct(settings: Settings, stage: str) -> float:
    if stage == "daily":
        return DAILY_MIN_RANGE_PCT
    return settings.steady_intraday_min_range_pct


def is_selectable_candidate(candidate: SteadyIntradayCandidate) -> bool:
    blocking_prefixes = (
        "bar_history",
        "price ",
        "spread ",
        "dollar_volume ",
        "daily ATR too high",
        "daily quote gap ",
        "daily close_position ",
    )
    blocking_flags = {
        "missing quote",
        "EMA stack not bullish",
        "EMA mid slope not positive",
        "daily close not positive",
        "missing VWAP",
        "ATR too high",
        "insufficient quote or market data",
    }
    for flag in candidate.quality_flags:
        if flag in blocking_flags or flag.startswith(blocking_prefixes):
            return False
    return True


def close_near_high(bar: Bar) -> bool:
    candle_range = bar.high - bar.low
    if candle_range <= 0:
        return bar.close >= bar.open
    return (bar.high - bar.close) / candle_range <= 0.35


def opening_range_high(bars: list[Bar], minutes: int) -> float | None:
    opening = []
    for bar in bars:
        current = datetime.fromtimestamp(bar.start_ms / 1000, tz=MARKET_TZ)
        elapsed = (current.hour * 60 + current.minute) - (MARKET_OPEN.hour * 60 + MARKET_OPEN.minute)
        if 0 <= elapsed < minutes:
            opening.append(bar)
    return max((bar.high for bar in opening), default=None)


def regular_session_bars(bars: list[Bar]) -> list[Bar]:
    regular = []
    for bar in bars:
        current = datetime.fromtimestamp(bar.start_ms / 1000, tz=MARKET_TZ)
        if current.weekday() >= 5:
            continue
        minute = current.hour * 60 + current.minute
        if (9 * 60 + 30) <= minute < (16 * 60):
            regular.append(bar)
    return regular


def score_steady_intraday_candidate(
    symbol: str,
    bars: list[Bar],
    quote: Quote | None,
    settings: Settings,
    *,
    stage: str,
    min_price: float,
    max_price: float,
    max_spread_bps: float,
    min_dollar_volume: float,
) -> SteadyIntradayCandidate | None:
    ordered = sorted(bars, key=lambda item: item.start_ms)
    if stage == "intraday":
        ordered = regular_session_bars(ordered)
    if not ordered:
        return None

    price = latest_price(ordered, quote)
    if price <= 0:
        return None

    closes = [bar.close for bar in ordered]
    fast = ema(closes, settings.steady_intraday_ema_fast) or price
    mid = ema(closes, settings.steady_intraday_ema_mid) or fast
    slow = ema(closes, settings.steady_intraday_ema_slow) or mid
    prev_mid = ema(closes[:-3], settings.steady_intraday_ema_mid) or mid
    mid_slope_pct = (mid - prev_mid) / prev_mid if prev_mid else 0.0

    valid_quote = usable_quote(quote)
    spread_bps = valid_quote.spread_bps if valid_quote else None
    raw_atr = atr(ordered, settings.steady_intraday_atr_period) or 0.0
    atr_pct = raw_atr / price if price > 0 else 0.0
    recent_range_pct = range_pct(ordered[-20:])
    vol_ratio = volume_ratio(ordered)
    vwap = session_vwap(ordered)
    vwap_distance_pct = (price - vwap) / vwap if vwap else None
    dollar_volume = sum(bar.close * bar.volume for bar in ordered[-20:] if bar.close > 0 and bar.volume > 0)
    last_close = ordered[-1].close
    previous_close = ordered[-2].close if len(ordered) >= 2 else last_close
    quote_gap_pct = (price - last_close) / last_close if stage == "daily" and last_close > 0 else 0.0
    daily_change_pct = (last_close - previous_close) / previous_close if previous_close > 0 else 0.0
    daily_close_position = close_position_in_range(ordered[-1]) if stage == "daily" else 1.0

    quality_flags: list[str] = []
    if len(ordered) < required_intraday_bar_count(settings):
        quality_flags.append(f"bar_history {len(ordered)} < steady minimum")
    if price < min_price or price > max_price:
        quality_flags.append(f"price {price:.2f} outside {min_price:.2f}-{max_price:.2f}")
    if spread_bps is None:
        quality_flags.append("missing quote")
    elif spread_bps > max_spread_bps:
        quality_flags.append(f"spread {spread_bps:.2f}bps > {max_spread_bps:.2f}bps")
    if dollar_volume < min_dollar_volume:
        quality_flags.append(f"dollar_volume {dollar_volume:.0f} < {min_dollar_volume:.0f}")
    if not (fast > mid > slow):
        quality_flags.append("EMA stack not bullish")
    if mid_slope_pct <= 0:
        quality_flags.append("EMA mid slope not positive")
    min_atr_pct, max_atr_pct = steady_atr_bounds(settings, stage)
    min_range_pct = steady_min_range_pct(settings, stage)
    atr_label_prefix = "daily " if stage == "daily" else ""
    range_label_prefix = "daily " if stage == "daily" else ""
    if atr_pct < min_atr_pct:
        quality_flags.append(f"{atr_label_prefix}ATR too low")
    if atr_pct > max_atr_pct:
        quality_flags.append(f"{atr_label_prefix}ATR too high")
    if recent_range_pct < min_range_pct:
        quality_flags.append(f"{range_label_prefix}range too compressed")
    if stage == "daily":
        if quote_gap_pct < -DAILY_MAX_DOWNSIDE_GAP_PCT:
            quality_flags.append(f"daily quote gap {quote_gap_pct:.2%} < -{DAILY_MAX_DOWNSIDE_GAP_PCT:.2%}")
        if daily_change_pct <= 0:
            quality_flags.append("daily close not positive")
        if daily_close_position < DAILY_MIN_CLOSE_POSITION:
            quality_flags.append(f"daily close_position {daily_close_position:.2f} < {DAILY_MIN_CLOSE_POSITION:.2f}")
    if vwap_distance_pct is None:
        quality_flags.append("missing VWAP")
    elif vwap_distance_pct <= settings.steady_intraday_vwap_buffer_pct:
        quality_flags.append("price not above VWAP buffer")

    pullback_reclaim_ready = False
    orb_continuation_ready = False
    if len(ordered) >= 2:
        latest = ordered[-1]
        previous = ordered[-2]
        bullish_close = latest.close > latest.open and close_near_high(latest)
        held_mid = latest.low >= min(mid, vwap or mid) * 0.997
        reclaimed_fast = previous.close <= fast * 1.002 and latest.close > max(previous.high, fast)
        pullback_reclaim_ready = (
            reclaimed_fast
            and held_mid
            and bullish_close
            and vol_ratio >= settings.steady_intraday_min_volume_ratio
        )
        opening_high = opening_range_high(ordered, settings.steady_intraday_orb_minutes)
        orb_continuation_ready = (
            opening_high is not None
            and latest.close > opening_high
            and previous.close <= opening_high * 1.002
            and bullish_close
            and vol_ratio >= settings.steady_intraday_breakout_volume_ratio
        )

    trend_score = 3.0 if fast > mid > slow else -2.5
    slope_score = max(-2.0, min(mid_slope_pct * 1000.0, 3.0))
    atr_mid = (min_atr_pct + max_atr_pct) / 2
    atr_span = max(max_atr_pct - min_atr_pct, 0.0001)
    atr_score = max(0.0, 2.0 - abs(atr_pct - atr_mid) / atr_span * 2.0)
    range_score = min(recent_range_pct / max(min_range_pct, 0.0001), 2.0)
    volume_score = min(vol_ratio, 2.5)
    liquidity_score = min(math.log10(dollar_volume / max(min_dollar_volume, 1.0) + 1.0), 2.0)
    spread_score = 0.0 if spread_bps is None else max(0.0, 1.0 - spread_bps / max(max_spread_bps, 0.1))
    vwap_score = 0.0
    if vwap_distance_pct is not None:
        if vwap_distance_pct > 0:
            vwap_score = min(vwap_distance_pct * 250.0, 2.0)
        vwap_score -= max(0.0, vwap_distance_pct - settings.steady_intraday_max_vwap_extension_pct) * 80.0
    daily_momentum_score = 0.0
    if stage == "daily":
        daily_momentum_score = max(-2.0, min(daily_change_pct * 100.0, 2.0))
        daily_momentum_score += max(-1.0, min(quote_gap_pct * 200.0, 1.5))
        daily_momentum_score += max(-1.0, min((daily_close_position - 0.5) * 3.0, 1.0))
    trigger_score = (3.0 if pullback_reclaim_ready else 0.0) + (2.0 if orb_continuation_ready else 0.0)
    history_penalty = 3.0 if "bar_history" in " ".join(quality_flags) else 0.0
    penalty = 0.35 * len(quality_flags) + history_penalty
    score = (
        trend_score
        + slope_score
        + atr_score
        + range_score
        + volume_score
        + liquidity_score
        + spread_score
        + vwap_score
        + daily_momentum_score
        + trigger_score
        - penalty
    )

    return SteadyIntradayCandidate(
        symbol=symbol,
        score=round(score, 3),
        selection_stage=stage,
        price=round(price, 2),
        spread_bps=round(spread_bps, 2) if spread_bps is not None else None,
        ema_fast=round(fast, 3),
        ema_mid=round(mid, 3),
        ema_slow=round(slow, 3),
        ema_mid_slope_pct=round(mid_slope_pct, 5),
        atr_pct=round(atr_pct, 5),
        range_pct=round(recent_range_pct, 5),
        volume_ratio=round(vol_ratio, 3),
        vwap_distance_pct=round(vwap_distance_pct, 5) if vwap_distance_pct is not None else None,
        dollar_volume=round(dollar_volume, 2),
        pullback_reclaim_ready=pullback_reclaim_ready,
        orb_continuation_ready=orb_continuation_ready,
        quality_flags=tuple(quality_flags),
    )


def rank_candidates(
    symbols: list[str],
    bars_by_symbol: dict[str, list[Bar]],
    quotes: dict[str, Quote],
    settings: Settings,
    *,
    top: int,
    stage: str,
    min_price: float,
    max_price: float,
    max_spread_bps: float,
    min_dollar_volume: float,
) -> list[SteadyIntradayCandidate]:
    candidates = []
    for symbol in symbols:
        candidate = score_steady_intraday_candidate(
            symbol,
            bars_by_symbol.get(symbol, []),
            quotes.get(symbol),
            settings,
            stage=stage,
            min_price=min_price,
            max_price=max_price,
            max_spread_bps=max_spread_bps,
            min_dollar_volume=min_dollar_volume,
        )
        if candidate is None:
            candidate = SteadyIntradayCandidate(
                symbol=symbol,
                score=-999.0,
                selection_stage=stage,
                price=0.0,
                spread_bps=None,
                ema_fast=0.0,
                ema_mid=0.0,
                ema_slow=0.0,
                ema_mid_slope_pct=0.0,
                atr_pct=0.0,
                range_pct=0.0,
                volume_ratio=0.0,
                vwap_distance_pct=None,
                dollar_volume=0.0,
                pullback_reclaim_ready=False,
                orb_continuation_ready=False,
                quality_flags=("insufficient quote or market data",),
            )
        candidates.append(candidate)
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates


def required_intraday_bar_count(settings: Settings) -> int:
    return max(settings.steady_intraday_min_bars, settings.steady_intraday_ema_slow + 5)


def deterministic_plan(candidates: list[SteadyIntradayCandidate], top: int) -> dict:
    selected_candidates = [candidate for candidate in candidates if is_selectable_candidate(candidate)]
    selected = [candidate.symbol for candidate in selected_candidates[:top]]
    if not selected:
        raise ValueError("No selectable steady_intraday candidates passed liquidity, trend, spread, volatility, and VWAP filters")
    screened_out = [candidate for candidate in candidates if not is_selectable_candidate(candidate)]
    return {
        "strategy": "steady_intraday",
        "selection_stage": candidates[0].selection_stage,
        "symbols": selected,
        "ranked": [asdict(candidate) for candidate in selected_candidates[:top]],
        "screened_out": [asdict(candidate) for candidate in screened_out[:top]],
        "settings": {
            "MAX_OPEN_POSITIONS": 2,
            "TRADE_COOLDOWN_SECONDS": 300,
        },
        "risk_note": (
            "Deterministic steady_intraday selection ranked by EMA/VWAP trend, ATR range, "
            "volume, liquidity, spread, and near-trigger readiness."
        ),
    }


def ai_steady_intraday_selection(
    settings: Settings,
    ranked: list[dict[str, object]],
    limit: int,
) -> dict[str, object] | None:
    payload = {
        "strategy": "steady_intraday",
        "selection_rules": {
            "must_choose_from_ranked": True,
            "focus": "same-day VWAP/EMA trend quality with controlled ATR risk",
            "prefer": [
                "clean EMA stack and rising EMA20",
                "price above VWAP without excessive extension",
                "ATR in a tradable but not chaotic range",
                "better liquidity and tighter spread",
                "pullback_reclaim_ready or orb_continuation_ready candidates",
            ],
            "avoid": [
                "quality flags showing poor liquidity, wide spread, weak trend, or missing VWAP",
                "overextended names far above VWAP",
                "symbols ranked only because of noisy volume without trend structure",
            ],
        },
        "ranked": ranked,
        "limit": limit,
    }
    response_text = request_json_response(
        settings,
        (
            "Review the steady_intraday ranked candidates and return only JSON. "
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


def validated_steady_intraday_selection(
    plan: dict[str, object],
    ranked: list[dict[str, object]],
    limit: int,
) -> dict[str, object]:
    available = {str(item.get("symbol", "")).upper() for item in ranked}
    raw_adjustments = plan.get("adjustments") if isinstance(plan.get("adjustments"), dict) else {}
    normalized_ranked: list[dict[str, object]] = []
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
        "strategy": "steady_intraday",
        "selection_stage": str(plan.get("selection_stage") or "intraday"),
        "symbols": selected,
        "ranked": normalized_ranked[:limit],
        "rejected": [item for item in (plan.get("rejected") or []) if str(item).upper() in available],
        "settings": plan.get("settings") if isinstance(plan.get("settings"), dict) else {},
        "risk_note": str(plan.get("risk_note") or "Embedded AI ranking over deterministic steady_intraday candidates."),
    }


def build_plan(
    symbols: list[str],
    top: int,
    *,
    bars_by_symbol: dict[str, list[Bar]] | None = None,
    quotes: dict[str, Quote] | None = None,
    settings: Settings | None = None,
    stage: str = "intraday",
    min_price: float = 10.0,
    max_price: float = 500.0,
    max_spread_bps: float = 12.0,
    min_dollar_volume: float = 5_000_000.0,
) -> dict:
    if top <= 0:
        raise ValueError("--top must be positive")
    settings = settings or load_settings(strategy_names=["steady_intraday"], validate=False)
    candidates = rank_candidates(
        symbols,
        bars_by_symbol or {},
        quotes or {},
        settings,
        top=top,
        stage=stage,
        min_price=min_price,
        max_price=max_price,
        max_spread_bps=max_spread_bps,
        min_dollar_volume=min_dollar_volume,
    )
    return deterministic_plan(candidates, top)


def get_today_minute_bars(
    settings: Settings,
    symbols: list[str],
    now: datetime | None = None,
) -> dict[str, list[Bar]]:
    from alpaca.data.timeframe import TimeFrame
    from alpaca_client import get_bars_between, make_clients

    now = now.astimezone(MARKET_TZ) if now else datetime.now(tz=MARKET_TZ)
    start = datetime.combine(now.date(), MARKET_OPEN, tzinfo=MARKET_TZ)
    clients = make_clients(settings)
    return get_bars_between(clients, symbols, TimeFrame.Minute, start, now)


def get_recent_daily_bars(settings: Settings, symbols: list[str], lookback_days: int) -> dict[str, list[Bar]]:
    from alpaca.data.timeframe import TimeFrame
    from alpaca_client import get_bars_between, make_clients

    clients = make_clients(settings)
    end = datetime.now(tz=MARKET_TZ) + timedelta(days=1)
    start = end - timedelta(days=lookback_days * 2)
    return get_bars_between(clients, symbols, TimeFrame.Day, start, end)


def get_latest_quotes_for_symbols(settings: Settings, symbols: list[str]) -> dict[str, Quote]:
    from alpaca_client import get_latest_quotes

    return get_latest_quotes(settings, symbols)


def write_plan(plan: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rank steady_intraday VWAP/EMA day-trading candidates.")
    parser.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE_FILE)
    parser.add_argument("--symbols", default="", help="Comma/newline separated symbols; overrides --universe.")
    parser.add_argument("--output", type=Path, default=DEFAULT_PLAN_FILE)
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--daily-lookback-days", type=int, default=90)
    parser.add_argument("--min-price", type=float, default=10.0)
    parser.add_argument("--max-price", type=float, default=500.0)
    parser.add_argument("--max-spread-bps", type=float, default=12.0)
    parser.add_argument("--min-dollar-volume", type=float, default=5_000_000.0)
    parser.add_argument("--use-ai", action="store_true", help="Use OpenAI to refine the final ranked symbol list.")
    parser.add_argument(
        "--force-daily",
        action="store_true",
        help="Ignore today's minute bars and rank only recent daily setup context.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> dict:
    args = parse_args(argv)
    if args.daily_lookback_days < 60:
        raise ValueError("--daily-lookback-days must be at least 60")

    settings = load_settings(strategy_names=["steady_intraday"], validate=False)
    symbols = load_universe(args.universe, args.symbols)
    quotes = get_latest_quotes_for_symbols(settings, symbols)
    stage = "daily"
    bars_by_symbol: dict[str, list[Bar]] = {}

    if not args.force_daily:
        intraday = get_today_minute_bars(settings, symbols)
        intraday_ready = {
            symbol: bars
            for symbol, bars in intraday.items()
            if len(regular_session_bars(bars)) >= required_intraday_bar_count(settings)
        }
        if intraday_ready:
            stage = "intraday"
            bars_by_symbol = intraday_ready

    if not bars_by_symbol:
        bars_by_symbol = get_recent_daily_bars(settings, symbols, args.daily_lookback_days)

    plan = build_plan(
        symbols,
        args.top,
        bars_by_symbol=bars_by_symbol,
        quotes=quotes,
        settings=settings,
        stage=stage,
        min_price=args.min_price,
        max_price=args.max_price,
        max_spread_bps=args.max_spread_bps,
        min_dollar_volume=args.min_dollar_volume,
    )
    result: dict[str, object] = {
        "selected_symbols": plan["symbols"],
        "symbols_env_line": format_symbols_env_line(plan["symbols"]),
        "selection_stage": stage,
        "ranked": plan["ranked"],
        "selection_plan": plan,
        "ai_enabled": args.use_ai,
    }
    write_plan(plan, args.output)
    if args.use_ai:
        ai_plan = ai_steady_intraday_selection(settings, plan["ranked"], args.top)
        if ai_plan is None:
            result["ai_selection"] = None
            result["ai_error"] = "OpenAI not configured or client unavailable."
        else:
            validated = validated_steady_intraday_selection(ai_plan, plan["ranked"], args.top)
            prev_settings = plan.get("settings") if isinstance(plan.get("settings"), dict) else {}
            if prev_settings and isinstance(validated.get("settings"), dict):
                validated["settings"] = {**prev_settings, **validated["settings"]}
            elif prev_settings:
                validated["settings"] = dict(prev_settings)
            result["ai_selection"] = validated
            result["selection_plan"] = validated
            result["selected_symbols"] = validated["symbols"]
            result["symbols_env_line"] = format_symbols_env_line(validated["symbols"])
            write_plan(validated, args.output)
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


if __name__ == "__main__":
    main()
