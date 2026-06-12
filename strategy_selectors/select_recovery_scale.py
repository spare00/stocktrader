from __future__ import annotations

import argparse
import json
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
from strategy_selectors.cli import selector_argument_parser
from market_hours import MARKET_TZ
from models import Bar, Quote
from opening_plan import default_plan_file_for_strategy

DEFAULT_UNIVERSE_FILE = Path("data/opening_universe.txt")
DEFAULT_OUTPUT_FILE = default_plan_file_for_strategy("recovery_scale")
DEFAULT_SELECTOR_MIN_DAILY_DOLLAR_VOLUME = 1_000_000.0
MARKET_OPEN = time(9, 30)


@dataclass(frozen=True)
class RecoveryScaleCandidate:
    symbol: str
    score: float
    selection_stage: str
    price: float
    spread_bps: float | None
    dollar_volume: float
    avg_daily_volume: float
    ema40: float
    ema60: float
    daily_trend_quality: str
    intraday_decline_pct: float
    recent_bounce_pct: float
    distance_from_ema60_pct: float
    rsi: float | None
    atr_pct: float | None
    quality_flags: tuple[str, ...] = ()


def load_universe(path: Path) -> list[str]:
    symbols: list[str] = []
    if not path.exists():
        raise FileNotFoundError(f"Missing universe file: {path}. Run strategy_selectors/select_market_universe.py first.")

    text = path.read_text(encoding="utf-8")
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0]
        for raw_symbol in re.split(r"[\s,]+", line):
            symbol = raw_symbol.strip().upper()
            if symbol:
                symbols.append(symbol)
    return list(dict.fromkeys(symbols))


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = values[0]
    for value in values[1:]:
        ema = (value - ema) * multiplier + ema
    return ema


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = changes[-period:]
    gains = [max(c, 0.0) for c in recent]
    losses = [abs(min(c, 0.0)) for c in recent]
    avg_gain = mean(gains)
    avg_loss = mean(losses)
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _atr(bars: list[Bar], period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    true_ranges = []
    for i in range(1, len(bars)):
        prev_close = bars[i-1].close
        current = bars[i]
        tr = max(
            current.high - current.low,
            abs(current.high - prev_close),
            abs(current.low - prev_close)
        )
        true_ranges.append(tr)
    return mean(true_ranges[-period:])


def usable_quote(quote: Quote | None) -> Quote | None:
    if quote is None:
        return None
    if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
        return None
    return quote


def score_recovery_scale_candidate(
    symbol: str,
    bars: list[Bar],
    daily_bars: list[Bar],
    quote: Quote | None,
    settings: Settings,
    *,
    min_price: float,
    max_spread_bps: float,
    min_liquidity: float,
) -> RecoveryScaleCandidate | None:
    """Score symbol for recovery_scale suitability."""
    if not daily_bars or len(daily_bars) < 60:
        return None

    valid_quote = usable_quote(quote)
    price = valid_quote.mid if valid_quote else (bars[-1].close if bars else daily_bars[-1].close)
    spread_bps = valid_quote.spread_bps if valid_quote else None

    # Price filter
    if price < min_price:
        return None

    # Spread filter
    if spread_bps is not None and spread_bps > max_spread_bps:
        return None

    # Liquidity filter: use daily liquidity as the hard pre-session gate. Runtime
    # still checks intraday liquidity before entries/adds.
    avg_daily_volume = mean([bar.volume for bar in daily_bars[-20:]])
    avg_daily_dollar_volume = mean([bar.close * bar.volume for bar in daily_bars[-20:]])
    if avg_daily_dollar_volume < min_liquidity:
        return None
    recent_bars = bars[-20:] if bars else []
    dollar_volume = mean([bar.close * bar.volume for bar in recent_bars]) if recent_bars else avg_daily_dollar_volume / 390

    # Daily structure check
    daily_closes = [bar.close for bar in daily_bars]
    ema40 = _ema(daily_closes, 40)
    ema60 = _ema(daily_closes, 60)

    if ema40 is None or ema60 is None or ema40 <= 0 or ema60 <= 0:
        return None

    # Trend quality
    if ema40 >= ema60:
        trend_quality = "uptrend"
    else:
        distance_from_ema60 = abs(price - ema60) / ema60
        if distance_from_ema60 < 0.05:
            trend_quality = "recovery"
        else:
            trend_quality = "downtrend"

    distance_from_ema60_pct = (price - ema60) / ema60

    # Intraday decline is a score bonus, not a hard selector gate. The live
    # strategy waits for the actual scale-in decline before trading.
    intraday_bars = bars[-10:] if bars else []
    if intraday_bars:
        recent_high = max(bar.high for bar in intraday_bars)
        current_price = bars[-1].close
        decline_pct = (recent_high - current_price) / recent_high if recent_high > 0 else 0.0
    else:
        decline_pct = 0.0

    bounce_pct = 0.0
    for i in range(1, len(intraday_bars)):
        if intraday_bars[i].close > intraday_bars[i-1].close:
            bounce = (intraday_bars[i].close - intraday_bars[i].low) / intraday_bars[i].low
            bounce_pct = max(bounce_pct, bounce)

    # Additional quality indicators
    intraday_closes = [bar.close for bar in bars[-20:]] if bars else []
    rsi = _rsi(intraday_closes)
    atr = _atr(bars) if bars else None
    atr_pct = (atr / price) if atr and price > 0 else None

    # Score components
    score = 0.0
    quality_flags = []

    # Higher score for good trend structure
    if trend_quality == "uptrend":
        score += 30.0
        quality_flags.append("uptrend")
    elif trend_quality == "recovery":
        score += 20.0
        quality_flags.append("recovery")
    else:
        score += 5.0
        quality_flags.append("weak_daily_trend")

    # Score liquidity
    liquidity_score = min(30.0, (avg_daily_dollar_volume / min_liquidity) * 10.0)
    score += liquidity_score

    # Score current decline magnitude when the selector runs during market hours.
    if 0.02 <= decline_pct <= 0.05:
        score += 20.0
        quality_flags.append("ideal_decline")
    elif 0.01 <= decline_pct <= 0.08:
        score += 10.0
        quality_flags.append("active_decline")
    else:
        quality_flags.append("watch_for_decline")

    # Score bounce presence
    if bounce_pct >= 0.003:
        score += 15.0
        quality_flags.append("bounce")

    # Score RSI (prefer oversold but not extreme)
    if rsi is not None:
        if 30 <= rsi <= 45:
            score += 10.0
            quality_flags.append("oversold")
        elif 45 < rsi <= 55:
            score += 5.0

    # Score distance from EMA60
    if abs(distance_from_ema60_pct) < 0.10:
        score += 5.0
        quality_flags.append("near_ema60")

    # Tight spread bonus
    if spread_bps is not None and spread_bps < max_spread_bps / 2:
        score += 5.0
        quality_flags.append("tight_spread")

    return RecoveryScaleCandidate(
        symbol=symbol,
        score=score,
        selection_stage="recovery_scale_selector",
        price=price,
        spread_bps=spread_bps,
        dollar_volume=dollar_volume,
        avg_daily_volume=avg_daily_volume,
        ema40=ema40,
        ema60=ema60,
        daily_trend_quality=trend_quality,
        intraday_decline_pct=decline_pct,
        recent_bounce_pct=bounce_pct,
        distance_from_ema60_pct=distance_from_ema60_pct,
        rsi=rsi,
        atr_pct=atr_pct,
        quality_flags=tuple(quality_flags),
    )


def main():
    parser = selector_argument_parser(description="Select symbols for recovery_scale strategy.")
    parser.add_argument(
        "--universe",
        "--universe-file",
        dest="universe",
        type=Path,
        default=DEFAULT_UNIVERSE_FILE,
        help=f"File with comma/newline separated symbols. Defaults to {DEFAULT_UNIVERSE_FILE}.",
    )
    parser.add_argument("--top", type=int, default=8, help="Maximum number of ranked symbols to return.")
    parser.add_argument(
        "--min-daily-dollar-volume",
        type=float,
        default=DEFAULT_SELECTOR_MIN_DAILY_DOLLAR_VOLUME,
        help="Selector floor for average daily dollar volume. Runtime liquidity checks remain stricter.",
    )
    parser.add_argument(
        "--output",
        "--plan-output",
        dest="output",
        type=Path,
        default=DEFAULT_OUTPUT_FILE,
        help="Write the strategy plan that main.py can consume directly.",
    )
    args = parser.parse_args()

    settings = load_settings(validate=False)
    universe = load_universe(args.universe)
    print(f"Loaded {len(universe)} symbols from {args.universe}")

    # Get market data
    try:
        from alpaca.data.timeframe import TimeFrame
        from alpaca_client import get_latest_quotes, get_recent_bars, make_clients

        clients = make_clients(settings)
        now = datetime.now(tz=MARKET_TZ)

        # Intraday bars
        bars_dict = get_recent_bars(settings, universe, limit=100)

        # Daily bars
        start_date = now - timedelta(days=90)
        end_date = now
        daily_bars_dict = {}
        for symbol in universe:
            try:
                from alpaca_client import get_bars_between
                daily_bars = get_bars_between(clients, [symbol], TimeFrame.Day, start_date, end_date)
                if symbol in daily_bars:
                    daily_bars_dict[symbol] = daily_bars[symbol]
            except Exception:
                pass

        quotes = get_latest_quotes(settings, universe)

    except Exception as e:
        print(f"Failed to load market data: {e}")
        return 1

    # Score candidates
    candidates = []
    for symbol in universe:
        bars = bars_dict.get(symbol, [])
        daily_bars = daily_bars_dict.get(symbol, [])
        quote = quotes.get(symbol)

        candidate = score_recovery_scale_candidate(
            symbol,
            bars,
            daily_bars,
            quote,
            settings,
            min_price=settings.recovery_scale_min_price,
            max_spread_bps=settings.recovery_scale_max_spread_bps,
            min_liquidity=args.min_daily_dollar_volume,
        )

        if candidate:
            candidates.append(candidate)

    # Sort by score
    candidates.sort(key=lambda c: c.score, reverse=True)

    # Take top N
    top_candidates = candidates[:args.top]

    # Write output
    plan = {
        "strategy": "recovery_scale",
        "generated_at": datetime.now(tz=MARKET_TZ).isoformat(),
        "universe_size": len(universe),
        "candidates_scored": len(candidates),
        "symbols": [c.symbol for c in top_candidates],
        "details": [asdict(c) for c in top_candidates],
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")

    print(f"\nTop {len(top_candidates)} recovery_scale candidates:")
    for i, c in enumerate(top_candidates, 1):
        print(
            f"{i:2d}. {c.symbol:6s} score={c.score:5.1f} price=${c.price:7.2f} "
            f"decline={c.intraday_decline_pct:5.2%} trend={c.daily_trend_quality} "
            f"flags={','.join(c.quality_flags)}"
        )

    print(f"\nWrote {len(top_candidates)} symbols to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
