from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Settings, load_settings
from market_hours import MARKET_TZ
from models import Bar, Quote
from opening_plan import default_plan_file_for_strategy


DEFAULT_UNIVERSE_FILE = Path("data/opening_universe.txt")
DEFAULT_OUTPUT_FILE = default_plan_file_for_strategy("maha7_pullback_reclaim")
MARKET_OPEN = time(9, 30)


@dataclass(frozen=True)
class Maha7Candidate:
    symbol: str
    score: float
    selection_stage: str
    price: float
    spread_bps: float | None
    ma7: float
    ma20: float
    ma7_slope_pct: float
    distance_to_ma7_pct: float
    extension_from_ma7_pct: float
    volume_ratio: float
    dollar_volume: float
    rsi: float | None
    prev_rsi: float | None
    vwap_distance_pct: float | None
    reclaim_score: float
    pullback_reaction: bool
    quality_flags: tuple[str, ...] = ()


def load_universe(path: Path) -> list[str]:
    symbols: list[str] = []
    if not path.exists():
        raise FileNotFoundError(f"Missing universe file: {path}. Run scripts/select_market_universe.py first.")

    text = path.read_text(encoding="utf-8")
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0]
        for raw_symbol in re.split(r"[\s,]+", line):
            symbol = raw_symbol.strip().upper()
            if symbol:
                symbols.append(symbol)
    return list(dict.fromkeys(symbols))


def _sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return mean(values[-window:])


def _rsi(closes: list[float], period: int) -> float | None:
    if len(closes) <= period:
        return None
    changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
    recent = changes[-period:]
    gains = [max(change, 0.0) for change in recent]
    losses = [abs(min(change, 0.0)) for change in recent]
    average_gain = mean(gains)
    average_loss = mean(losses)
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def usable_quote(quote: Quote | None) -> Quote | None:
    if quote is None:
        return None
    if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
        return None
    return quote


def _latest_price(bars: list[Bar], quote: Quote | None) -> float:
    valid_quote = usable_quote(quote)
    if valid_quote:
        return valid_quote.mid
    return bars[-1].close if bars else 0.0


def _session_vwap(bars: list[Bar]) -> float | None:
    total_volume = sum(bar.volume for bar in bars if bar.volume > 0)
    if total_volume <= 0:
        return None
    total_value = sum(bar.vwap * bar.volume for bar in bars if bar.volume > 0)
    return total_value / total_volume if total_value > 0 else None


def _strong_high_structure(bars: list[Bar]) -> bool:
    if len(bars) < 6:
        return False
    highs = [bar.high for bar in bars[-6:]]
    return highs[-1] > highs[-2] > highs[-3]


def _volume_ratio(bars: list[Bar], lookback: int = 10) -> float:
    if len(bars) < 2:
        return 0.0
    baseline = [bar.volume for bar in bars[-lookback - 1 : -1] if bar.volume > 0]
    if not baseline:
        return 0.0
    return bars[-1].volume / mean(baseline)


def _recent_pullback_reaction(bars: list[Bar]) -> bool:
    if len(bars) < 3:
        return False
    recent_lows = [bar.low for bar in bars[-3:]]
    return recent_lows[-1] != recent_lows[-2]


def score_maha7_candidate(
    symbol: str,
    bars: list[Bar],
    quote: Quote | None,
    settings: Settings,
    *,
    min_price: float,
    max_price: float,
    max_spread_bps: float,
    min_dollar_volume: float,
    pullback_max_distance_pct: float,
    max_extension_pct: float,
    stage: str,
) -> Maha7Candidate | None:
    ordered = sorted(bars, key=lambda item: item.start_ms)
    if len(ordered) < 21:
        return None

    closes = [bar.close for bar in ordered]
    ma7 = _sma(closes, 7)
    ma20 = _sma(closes, 20)
    prev_ma7 = _sma(closes[:-1], 7)
    if ma7 is None or ma20 is None or prev_ma7 is None or prev_ma7 <= 0:
        return None

    price = _latest_price(ordered, quote)
    if price <= 0:
        return None

    valid_quote = usable_quote(quote)
    spread_bps = valid_quote.spread_bps if valid_quote else None
    ma7_slope_pct = (ma7 - prev_ma7) / prev_ma7
    distance_to_ma7_pct = abs(price - ma7) / ma7 if ma7 else float("inf")
    extension_from_ma7_pct = max(0.0, (price - ma7) / ma7) if ma7 else 0.0
    dollar_volume = sum(bar.close * bar.volume for bar in ordered[-20:] if bar.close > 0 and bar.volume > 0)
    volume_ratio = _volume_ratio(ordered)
    rsi = _rsi(closes, settings.maha7_pullback_reclaim_rsi_period)
    prev_rsi = _rsi(closes[:-1], settings.maha7_pullback_reclaim_rsi_period)
    vwap = _session_vwap(ordered)
    vwap_distance_pct = (price - vwap) / vwap if vwap else None
    pullback_reaction = _recent_pullback_reaction(ordered)
    quality_flags: list[str] = []

    if price < min_price or price > max_price:
        quality_flags.append(f"price {price:.2f} outside {min_price:.2f}-{max_price:.2f}")
    if spread_bps is None:
        quality_flags.append("missing quote")
    elif spread_bps > max_spread_bps:
        quality_flags.append(f"spread {spread_bps:.2f}bps > {max_spread_bps:.2f}bps")
    if dollar_volume < min_dollar_volume:
        quality_flags.append(f"dollar_volume {dollar_volume:.0f} < {min_dollar_volume:.0f}")
    if ma7 <= ma20:
        quality_flags.append("MA7 <= MA20")
    if ma7_slope_pct <= 0:
        quality_flags.append("MA7 slope not positive")
    if distance_to_ma7_pct > pullback_max_distance_pct:
        quality_flags.append(f"distance_to_MA7 {distance_to_ma7_pct:.2%} > {pullback_max_distance_pct:.2%}")
    if extension_from_ma7_pct > max_extension_pct:
        quality_flags.append(f"extended_from_MA7 {extension_from_ma7_pct:.2%} > {max_extension_pct:.2%}")
    if volume_ratio < settings.maha7_pullback_reclaim_volume_min_ratio:
        quality_flags.append(f"volume_ratio {volume_ratio:.2f}x < {settings.maha7_pullback_reclaim_volume_min_ratio:.2f}x")
    if vwap_distance_pct is None:
        quality_flags.append("missing VWAP")
    elif vwap_distance_pct < settings.maha7_pullback_reclaim_vwap_min_distance_pct:
        quality_flags.append("too close to VWAP")
    if not pullback_reaction:
        quality_flags.append("no recent pullback reaction")

    trend_score = 3.0 if ma7 > ma20 else -2.0
    slope_score = max(-2.0, min(ma7_slope_pct * 800.0, 3.0))
    pullback_score = max(0.0, 3.0 - (distance_to_ma7_pct / max(pullback_max_distance_pct, 0.0001)) * 3.0)
    extension_penalty = max(0.0, (extension_from_ma7_pct - max_extension_pct) / max(max_extension_pct, 0.0001)) * 3.0
    structure_score = 2.0 if _strong_high_structure(ordered) else 0.0
    volume_score = min(volume_ratio, 2.0)
    liquidity_score = min(math.log10(dollar_volume / max(min_dollar_volume, 1.0) + 1.0), 2.0)
    spread_score = 1.0 if spread_bps is None else max(0.0, 1.0 - (spread_bps / max(max_spread_bps, 0.1)))
    rsi_score = 0.0
    reclaim_score = 0.0
    if rsi is not None and prev_rsi is not None:
        if prev_rsi < 55 and rsi > 55:
            rsi_score = 3.0
        elif 40 <= rsi <= 75:
            rsi_score = 1.0
        else:
            rsi_score = -1.0
        if 50 < prev_rsi < 55 and rsi > 55:
            reclaim_score = 2.0
    elif rsi is not None:
        if 45 <= rsi <= 65:
            rsi_score = 1.0
        else:
            rsi_score = -1.0

    vwap_score = 0.0
    if vwap_distance_pct is not None and vwap_distance_pct >= settings.maha7_pullback_reclaim_vwap_min_distance_pct:
        vwap_score = min(vwap_distance_pct * 500.0, 2.0)
    reaction_score = 1.0 if pullback_reaction else 0.0

    penalty = 0.4 * len(quality_flags)
    score = (
        trend_score
        + slope_score
        + pullback_score
        + structure_score
        + volume_score
        + liquidity_score
        + spread_score
        + rsi_score
        + reclaim_score
        + vwap_score
        + reaction_score
        - extension_penalty
        - penalty
    )

    return Maha7Candidate(
        symbol=symbol,
        score=round(score, 3),
        selection_stage=stage,
        price=round(price, 2),
        spread_bps=round(spread_bps, 2) if spread_bps is not None else None,
        ma7=round(ma7, 3),
        ma20=round(ma20, 3),
        ma7_slope_pct=round(ma7_slope_pct, 5),
        distance_to_ma7_pct=round(distance_to_ma7_pct, 5),
        extension_from_ma7_pct=round(extension_from_ma7_pct, 5),
        volume_ratio=round(volume_ratio, 3),
        dollar_volume=round(dollar_volume, 2),
        rsi=round(rsi, 2) if rsi is not None else None,
        prev_rsi=round(prev_rsi, 2) if prev_rsi is not None else None,
        vwap_distance_pct=round(vwap_distance_pct, 5) if vwap_distance_pct is not None else None,
        reclaim_score=round(reclaim_score, 3),
        pullback_reaction=pullback_reaction,
        quality_flags=tuple(quality_flags),
    )


def rank_candidates(
    symbols: list[str],
    bars_by_symbol: dict[str, list[Bar]],
    quotes: dict[str, Quote],
    settings: Settings,
    *,
    top: int,
    min_price: float,
    max_price: float,
    max_spread_bps: float,
    min_dollar_volume: float,
    pullback_max_distance_pct: float,
    max_extension_pct: float,
    stage: str,
) -> list[Maha7Candidate]:
    candidates = []
    for symbol in symbols:
        candidate = score_maha7_candidate(
            symbol,
            bars_by_symbol.get(symbol, []),
            quotes.get(symbol),
            settings,
            min_price=min_price,
            max_price=max_price,
            max_spread_bps=max_spread_bps,
            min_dollar_volume=min_dollar_volume,
            pullback_max_distance_pct=pullback_max_distance_pct,
            max_extension_pct=max_extension_pct,
            stage=stage,
        )
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates[:top]


def deterministic_plan(candidates: list[Maha7Candidate], top: int) -> dict:
    selected = [candidate.symbol for candidate in candidates[:top]]
    if not selected:
        raise ValueError("No Maha7 candidates could be ranked from the available market data")
    return {
        "strategy": "maha7_pullback_reclaim",
        "selection_stage": candidates[0].selection_stage,
        "symbols": selected,
        "ranked": [asdict(candidate) for candidate in candidates[:top]],
        "settings": {
            "TRADE_COOLDOWN_SECONDS": 600,
        },
        "risk_note": "Deterministic Maha7 selection ranked by MA7/MA20 trend, pullback proximity, liquidity, spread, volume, and RSI context.",
    }


def get_recent_daily_bars(settings: Settings, symbols: list[str], lookback_days: int) -> dict[str, list[Bar]]:
    from alpaca.data.timeframe import TimeFrame
    from alpaca_client import get_bars_between, make_clients

    clients = make_clients(settings)
    start = datetime.now(tz=MARKET_TZ) - timedelta(days=lookback_days * 2)
    end = datetime.now(tz=MARKET_TZ) + timedelta(days=1)
    return get_bars_between(clients, symbols, TimeFrame.Day, start, end)


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


def get_latest_quotes_for_symbols(settings: Settings, symbols: list[str]) -> dict[str, Quote]:
    from alpaca_client import get_latest_quotes

    return get_latest_quotes(settings, symbols)


def build_plan(
    symbols: list[str],
    top: int,
    *,
    bars_by_symbol: dict[str, list[Bar]] | None = None,
    quotes: dict[str, Quote] | None = None,
    settings: Settings | None = None,
    stage: str = "daily",
    min_price: float = 5.0,
    max_price: float = 500.0,
    max_spread_bps: float = 12.0,
    min_dollar_volume: float = 5_000_000.0,
    pullback_max_distance_pct: float = 0.03,
    max_extension_pct: float = 0.08,
) -> dict:
    if top <= 0:
        raise ValueError("--top must be positive")
    settings = settings or load_settings(strategy_names=["maha7_pullback_reclaim"], validate=False)
    candidates = rank_candidates(
        symbols,
        bars_by_symbol or {},
        quotes or {},
        settings,
        top=top,
        min_price=min_price,
        max_price=max_price,
        max_spread_bps=max_spread_bps,
        min_dollar_volume=min_dollar_volume,
        pullback_max_distance_pct=pullback_max_distance_pct,
        max_extension_pct=max_extension_pct,
        stage=stage,
    )
    return deterministic_plan(candidates, top)


def write_plan(plan: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rank Maha7 pullback reclaim candidates from the liquid universe.")
    parser.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--daily-lookback-days", type=int, default=45)
    parser.add_argument("--min-price", type=float, default=5.0)
    parser.add_argument("--max-price", type=float, default=500.0)
    parser.add_argument("--max-spread-bps", type=float, default=12.0)
    parser.add_argument("--min-dollar-volume", type=float, default=5_000_000.0)
    parser.add_argument("--pullback-max-distance-pct", type=float, default=0.03)
    parser.add_argument("--max-extension-pct", type=float, default=0.08)
    parser.add_argument(
        "--force-daily",
        action="store_true",
        help="Ignore today's minute bars and rank only daily setup context.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> dict:
    args = parse_args(argv)
    if args.daily_lookback_days < 21:
        raise ValueError("--daily-lookback-days must be at least 21")

    settings = load_settings(strategy_names=["maha7_pullback_reclaim"], validate=False)
    symbols = load_universe(args.universe)
    quotes = get_latest_quotes_for_symbols(settings, symbols)
    stage = "daily"
    bars_by_symbol: dict[str, list[Bar]] = {}

    if not args.force_daily:
        intraday_bars = get_today_minute_bars(settings, symbols)
        intraday_ready = {symbol: bars for symbol, bars in intraday_bars.items() if len(bars) >= 21}
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
        pullback_max_distance_pct=args.pullback_max_distance_pct,
        max_extension_pct=args.max_extension_pct,
    )
    write_plan(plan, args.output)
    print(json.dumps({"selected_symbols": plan["symbols"], "selection_stage": stage, "ranked": plan["ranked"]}, indent=2, sort_keys=True))
    return plan


if __name__ == "__main__":
    main()
