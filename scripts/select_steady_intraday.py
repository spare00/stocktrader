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

from config import Settings, load_settings
from env_vars import format_symbols_env_line
from market_hours import MARKET_TZ
from models import Bar, Quote
from opening_plan import default_plan_file_for_strategy


DEFAULT_UNIVERSE_FILE = Path("data/opening_universe.txt")
DEFAULT_PLAN_FILE = default_plan_file_for_strategy("steady_intraday")
MARKET_OPEN = time(9, 30)


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
        raise FileNotFoundError(f"Missing universe file: {path}. Run scripts/select_market_universe.py first.")
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

    quality_flags: list[str] = []
    if len(ordered) < max(settings.steady_intraday_min_bars, settings.steady_intraday_ema_slow):
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
    if atr_pct < settings.steady_intraday_min_atr_pct:
        quality_flags.append("ATR too low")
    if atr_pct > settings.steady_intraday_max_atr_pct:
        quality_flags.append("ATR too high")
    if recent_range_pct < settings.steady_intraday_min_range_pct:
        quality_flags.append("range too compressed")
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
    atr_mid = (settings.steady_intraday_min_atr_pct + settings.steady_intraday_max_atr_pct) / 2
    atr_span = max(settings.steady_intraday_max_atr_pct - settings.steady_intraday_min_atr_pct, 0.0001)
    atr_score = max(0.0, 2.0 - abs(atr_pct - atr_mid) / atr_span * 2.0)
    range_score = min(recent_range_pct / max(settings.steady_intraday_min_range_pct, 0.0001), 2.0)
    volume_score = min(vol_ratio, 2.5)
    liquidity_score = min(math.log10(dollar_volume / max(min_dollar_volume, 1.0) + 1.0), 2.0)
    spread_score = 0.0 if spread_bps is None else max(0.0, 1.0 - spread_bps / max(max_spread_bps, 0.1))
    vwap_score = 0.0
    if vwap_distance_pct is not None:
        if vwap_distance_pct > 0:
            vwap_score = min(vwap_distance_pct * 250.0, 2.0)
        vwap_score -= max(0.0, vwap_distance_pct - settings.steady_intraday_max_vwap_extension_pct) * 80.0
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
    return candidates[:top]


def deterministic_plan(candidates: list[SteadyIntradayCandidate], top: int) -> dict:
    selected = [candidate.symbol for candidate in candidates[:top]]
    if not selected:
        raise ValueError("No steady_intraday candidates could be ranked from the available market data")
    return {
        "strategy": "steady_intraday",
        "selection_stage": candidates[0].selection_stage,
        "symbols": selected,
        "ranked": [asdict(candidate) for candidate in candidates[:top]],
        "settings": {
            "MAX_OPEN_POSITIONS": 2,
            "TRADE_COOLDOWN_SECONDS": 300,
        },
        "risk_note": (
            "Deterministic steady_intraday selection ranked by EMA/VWAP trend, ATR range, "
            "volume, liquidity, spread, and near-trigger readiness."
        ),
    }


def build_plan(
    symbols: list[str],
    top: int,
    *,
    bars_by_symbol: dict[str, list[Bar]] | None = None,
    quotes: dict[str, Quote] | None = None,
    settings: Settings | None = None,
    stage: str = "intraday",
    min_price: float = 5.0,
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
    parser.add_argument("--min-price", type=float, default=5.0)
    parser.add_argument("--max-price", type=float, default=500.0)
    parser.add_argument("--max-spread-bps", type=float, default=12.0)
    parser.add_argument("--min-dollar-volume", type=float, default=5_000_000.0)
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
            if len(regular_session_bars(bars)) >= max(settings.steady_intraday_min_bars, settings.steady_intraday_ema_slow)
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
    }
    write_plan(plan, args.output)
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


if __name__ == "__main__":
    main()
