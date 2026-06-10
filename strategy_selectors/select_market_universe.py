import argparse
import json
import math
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Settings, load_settings
from env_vars import format_symbols_env_line
from models import Bar, Quote
from strategy_selectors.select_opening_impulse import usable_quote


MARKET_TZ = ZoneInfo("America/New_York")
DEFAULT_LOOKBACK_DAYS = 80
DEFAULT_MIN_EMA20_SLOPE_BPS = 1.0
DEFAULT_MIN_EMA40_SLOPE_BPS = 0.0
DEFAULT_MIN_EMA60_SLOPE_BPS = 0.0
DEFAULT_EMA60_RECOVERY_TOLERANCE = 0.985
DEFAULT_EMA_GAP_SHRINK_TOLERANCE = 0.02
DEFAULT_ROLLOVER_LOOKBACK_DAYS = 10
DEFAULT_MIN_AVERAGE_VOLUME = 500_000.0
DEFAULT_MIN_MEDIAN_DOLLAR_VOLUME = 20_000_000.0
DEFAULT_LIMIT_UP_PCT = 0.295
DEFAULT_LIMIT_DOWN_PCT = -0.295
DEFAULT_LIMIT_CLOSE_NEAR_HIGH_MIN = 0.95
DEFAULT_LIMIT_CLOSE_NEAR_LOW_MAX = 0.05
DEFAULT_MIN_PREVIOUS_DAY_VOLUME = 500_000.0
MODE_CHOICES = {"liquid", "limit-up", "limit-down"}
LEGACY_PREVIOUS_DAY_FILTER_MODE = {
    "none": "liquid",
    "limit-up": "limit-up",
    "sharp-drop": "limit-down",
}


def batched(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def parse_symbols(raw: str) -> list[str]:
    return [part.strip().upper() for part in raw.replace("\n", ",").split(",") if part.strip()]


def enum_value(value) -> str:
    return str(getattr(value, "value", value)).upper()


def get_active_tradable_symbols(settings: Settings, exchanges: set[str] | None = None) -> list[str]:
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest
    from alpaca_client import make_clients

    clients = make_clients(settings)
    request = GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
    assets = clients.trading.get_all_assets(request)
    symbols = []
    for asset in assets:
        symbol = str(getattr(asset, "symbol", "")).upper()
        exchange = enum_value(getattr(asset, "exchange", ""))
        if not symbol or "/" in symbol:
            continue
        if exchanges and exchange not in exchanges:
            continue
        if not bool(getattr(asset, "tradable", False)):
            continue
        symbols.append(symbol)
    return sorted(dict.fromkeys(symbols))


def get_daily_bars(
    settings: Settings,
    symbols: list[str],
    lookback_days: int,
    batch_size: int,
    *,
    as_of: date | None = None,
) -> dict[str, list[Bar]]:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca_client import make_clients, to_bar

    clients = make_clients(settings)
    if as_of is not None:
        end = datetime.combine(as_of, time.min, tzinfo=MARKET_TZ) + timedelta(days=1)
    else:
        end = datetime.now(tz=MARKET_TZ).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    start = end - timedelta(days=lookback_days * 2 + 10)
    results: dict[str, list[Bar]] = {}

    for chunk in batched(symbols, batch_size):
        request = StockBarsRequest(
            symbol_or_symbols=chunk,
            timeframe=TimeFrame.Day,
            start=start.astimezone(timezone.utc),
            end=end.astimezone(timezone.utc),
            feed=clients.feed,
        )
        response = clients.historical.get_stock_bars(request)
        for symbol in chunk:
            bars = [to_bar(item) for item in response.data.get(symbol, [])]
            results[symbol] = sorted(bars, key=lambda item: item.start_ms)[-lookback_days:]
    return results


def get_latest_quotes(settings: Settings, symbols: list[str], batch_size: int) -> dict[str, Quote]:
    from alpaca.data.requests import StockLatestQuoteRequest
    from alpaca_client import make_clients, to_quote

    clients = make_clients(settings)
    results: dict[str, Quote] = {}
    for chunk in batched(symbols, batch_size):
        request = StockLatestQuoteRequest(symbol_or_symbols=chunk, feed=clients.feed)
        response = clients.historical.get_stock_latest_quote(request)
        for symbol, quote in response.items():
            results[symbol] = to_quote(quote)
    return results


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values or period <= 0:
        return []
    alpha = 2.0 / (period + 1)
    out: list[float] = []
    ema = values[0]
    for value in values:
        ema = alpha * value + (1.0 - alpha) * ema
        out.append(ema)
    return out


def _lookback_change_bps(closes: list[float], bars_back: int) -> float | None:
    if len(closes) <= bars_back:
        return None
    prior = closes[-1 - bars_back]
    return ((closes[-1] - prior) / prior) * 10_000 if prior > 0 else None


def _ema_slope_bps(series: list[float], lookback: int = 10) -> float | None:
    if len(series) <= lookback or series[-1 - lookback] <= 0:
        return None
    return ((series[-1] - series[-1 - lookback]) / series[-1 - lookback]) * 10_000


def _relative_ema_gap(fast: float, slow: float) -> float | None:
    if slow <= 0:
        return None
    return (fast - slow) / slow


def daily_metrics(bars: list[Bar]) -> dict | None:
    ordered = sorted((bar for bar in bars if bar.close > 0 and bar.volume > 0), key=lambda item: item.start_ms)
    if len(ordered) < 61:
        return None

    latest = ordered[-1]
    prior = ordered[-2]
    closes = [float(bar.close) for bar in ordered]
    highs = [float(bar.high) for bar in ordered]
    lows = [float(bar.low) for bar in ordered]
    volumes = [float(bar.volume) for bar in ordered]
    dollar_volumes = [float(bar.close) * float(bar.volume) for bar in ordered]
    if latest.close <= 0 or prior.close <= 0:
        return None

    latest_range = float(latest.high) - float(latest.low)
    previous_day_change_pct = (float(latest.close) - float(prior.close)) / float(prior.close)
    previous_day_close_location = (
        (float(latest.close) - float(latest.low)) / latest_range if latest_range > 0 else 0.0
    )

    ema20_series = _ema_series(closes, 20)
    ema40_series = _ema_series(closes, 40)
    ema60_series = _ema_series(closes, 60)
    ema20 = ema20_series[-1]
    ema40 = ema40_series[-1]
    ema60 = ema60_series[-1]
    ema20_slope_bps = _ema_slope_bps(ema20_series)
    ema40_slope_bps = _ema_slope_bps(ema40_series)
    ema60_slope_bps = _ema_slope_bps(ema60_series)
    ema40_slope_prev_bps = (
        _ema_slope_bps(ema40_series[:-5], lookback=10) if len(ema40_series) > 15 else None
    )

    gap_20_40_now = _relative_ema_gap(ema20, ema40)
    gap_40_60_now = _relative_ema_gap(ema40, ema60)
    gap_20_40_prev = _relative_ema_gap(ema20_series[-6], ema40_series[-6]) if len(ema20_series) >= 6 else None
    gap_40_60_prev = _relative_ema_gap(ema40_series[-6], ema60_series[-6]) if len(ema40_series) >= 6 else None
    ema_gap_improving = (
        (gap_20_40_now is not None and gap_20_40_prev is not None and gap_20_40_now > gap_20_40_prev)
        or (gap_40_60_now is not None and gap_40_60_prev is not None and gap_40_60_now > gap_40_60_prev)
    )

    trend_20d_bps = _lookback_change_bps(closes, 20)
    trend_10d_bps = _lookback_change_bps(closes, 10)
    trend_5d_bps = _lookback_change_bps(closes, 5)
    trend_60d_bps = _lookback_change_bps(closes, 60)

    rollover_window = ordered[-DEFAULT_ROLLOVER_LOOKBACK_DAYS:]
    recent_peak = max(float(bar.high) for bar in rollover_window)
    pullback_from_peak_pct = (recent_peak - float(latest.close)) / recent_peak if recent_peak > 0 else 0.0
    recent_low = min(float(bar.low) for bar in rollover_window)
    recovery_from_low_pct = (float(latest.close) - recent_low) / recent_low if recent_low > 0 else 0.0

    price = float(latest.close)
    near_ema60 = price >= ema60 * DEFAULT_EMA60_RECOVERY_TOLERANCE

    return {
        "price": price,
        "average_volume": sum(volumes) / len(volumes),
        "median_dollar_volume": median(dollar_volumes),
        "average_dollar_volume": sum(dollar_volumes) / len(dollar_volumes),
        "previous_day_change_pct": previous_day_change_pct,
        "previous_day_close_location": previous_day_close_location,
        "previous_day_volume": float(latest.volume),
        "previous_day_dollar_volume": float(latest.close) * float(latest.volume),
        "trend_20d_bps": trend_20d_bps,
        "trend_10d_bps": trend_10d_bps,
        "trend_5d_bps": trend_5d_bps,
        "trend_60d_bps": trend_60d_bps,
        "ema20": ema20,
        "ema40": ema40,
        "ema60": ema60,
        "ema20_slope_bps": ema20_slope_bps,
        "ema40_slope_bps": ema40_slope_bps,
        "ema60_slope_bps": ema60_slope_bps,
        "ema40_slope_prev_bps": ema40_slope_prev_bps,
        "ema_stack": ema40 > ema60,
        "ema40_above_ema60": ema40 > ema60,
        "price_above_ema20": price > ema20,
        "price_above_ema40": price > ema40,
        "price_above_ema60": price > ema60,
        "near_ema60": near_ema60,
        "gap_20_40_now": gap_20_40_now,
        "gap_40_60_now": gap_40_60_now,
        "gap_20_40_prev": gap_20_40_prev,
        "gap_40_60_prev": gap_40_60_prev,
        "ema_gap_improving": ema_gap_improving,
        "pullback_from_peak_pct": pullback_from_peak_pct,
        "recovery_from_low_pct": recovery_from_low_pct,
    }


def passes_liquidity_filter(
    metrics: dict,
    *,
    min_price: float,
    max_price: float,
    min_average_volume: float,
    min_median_dollar_volume: float,
    min_previous_day_volume: float,
) -> bool:
    if metrics["price"] < min_price or metrics["price"] > max_price:
        return False
    if metrics["average_volume"] < min_average_volume:
        return False
    if metrics["median_dollar_volume"] < min_median_dollar_volume:
        return False
    if metrics["previous_day_volume"] < min_previous_day_volume:
        return False
    return True


def passes_established_uptrend(
    metrics: dict,
    *,
    min_ema20_slope_bps: float,
    min_ema40_slope_bps: float,
    min_ema60_slope_bps: float,
) -> bool:
    if not metrics.get("ema_stack"):
        return False

    ema20_slope_bps = metrics.get("ema20_slope_bps")
    ema40_slope_bps = metrics.get("ema40_slope_bps")
    ema60_slope_bps = metrics.get("ema60_slope_bps")
    if ema20_slope_bps is None or ema20_slope_bps <= min_ema20_slope_bps:
        return False
    if ema40_slope_bps is None or ema40_slope_bps <= min_ema40_slope_bps:
        return False
    if ema60_slope_bps is None or ema60_slope_bps < min_ema60_slope_bps:
        return False
    return True


def passes_recovery_uptrend(
    metrics: dict,
    *,
    min_ema20_slope_bps: float,
    ema60_recovery_tolerance: float = DEFAULT_EMA60_RECOVERY_TOLERANCE,
) -> bool:
    if not metrics.get("price_above_ema20"):
        return False
    if not metrics.get("price_above_ema40"):
        return False

    ema60 = metrics.get("ema60")
    if ema60 is None or metrics["price"] < ema60 * ema60_recovery_tolerance:
        return False

    ema20_slope_bps = metrics.get("ema20_slope_bps")
    if ema20_slope_bps is None or ema20_slope_bps <= min_ema20_slope_bps:
        return False

    ema40_slope_bps = metrics.get("ema40_slope_bps")
    ema40_slope_prev_bps = metrics.get("ema40_slope_prev_bps")
    ema40_turning_up = (
        ema40_slope_bps is not None
        and ema40_slope_bps > 0
        or (
            ema40_slope_bps is not None
            and ema40_slope_prev_bps is not None
            and ema40_slope_bps > ema40_slope_prev_bps
        )
    )
    if not ema40_turning_up:
        return False

    trend_5d_bps = metrics.get("trend_5d_bps")
    trend_10d_bps = metrics.get("trend_10d_bps")
    if trend_5d_bps is None or trend_5d_bps <= 0:
        return False
    if trend_10d_bps is None or trend_10d_bps <= 0:
        return False
    if not metrics.get("ema_gap_improving"):
        return False
    return True


def trend_track(metrics: dict, *, min_ema20_slope_bps: float, min_ema40_slope_bps: float, min_ema60_slope_bps: float) -> str | None:
    if passes_established_uptrend(
        metrics,
        min_ema20_slope_bps=min_ema20_slope_bps,
        min_ema40_slope_bps=min_ema40_slope_bps,
        min_ema60_slope_bps=min_ema60_slope_bps,
    ):
        return "established"
    if passes_recovery_uptrend(metrics, min_ema20_slope_bps=min_ema20_slope_bps):
        return "recovery"
    return None


def rejects_rollover(
    metrics: dict,
    *,
    ema_gap_shrink_tolerance: float = DEFAULT_EMA_GAP_SHRINK_TOLERANCE,
) -> bool:
    price = metrics["price"]
    ema20 = metrics.get("ema20")
    ema40 = metrics.get("ema40")
    ema20_slope_bps = metrics.get("ema20_slope_bps")
    ema40_slope_bps = metrics.get("ema40_slope_bps")

    if ema20 is not None and price < ema20 and (ema20_slope_bps is None or ema20_slope_bps <= 0):
        return True

    if (
        ema20_slope_bps is not None
        and ema20_slope_bps < 0
        and ema40_slope_bps is not None
        and ema40_slope_bps < 0
    ):
        return True

    trend_5d_bps = metrics.get("trend_5d_bps")
    trend_10d_bps = metrics.get("trend_10d_bps")
    if trend_5d_bps is not None and trend_10d_bps is not None and trend_5d_bps < 0 and trend_10d_bps < 0:
        return True

    if (
        ema20 is not None
        and ema40 is not None
        and price < ema20 * 0.995
        and price < ema40 * 0.995
    ):
        return True

    gap_20_40_now = metrics.get("gap_20_40_now")
    gap_20_40_prev = metrics.get("gap_20_40_prev")
    gap_40_60_now = metrics.get("gap_40_60_now")
    gap_40_60_prev = metrics.get("gap_40_60_prev")
    gap_20_40_shrinking = (
        gap_20_40_now is not None
        and gap_20_40_prev is not None
        and gap_20_40_prev > 0
        and gap_20_40_now < gap_20_40_prev * (1.0 - ema_gap_shrink_tolerance)
    )
    gap_40_60_shrinking = (
        gap_40_60_now is not None
        and gap_40_60_prev is not None
        and gap_40_60_prev > 0
        and gap_40_60_now < gap_40_60_prev * (1.0 - ema_gap_shrink_tolerance)
    )
    if ema20 is not None and price < ema20 and gap_20_40_shrinking and gap_40_60_shrinking:
        return True

    return False


def passes_trend_filter(
    metrics: dict,
    *,
    min_ema20_slope_bps: float,
    min_ema40_slope_bps: float,
    min_ema60_slope_bps: float,
    require_price_above_ema20: bool,
    require_ema_stack: bool,
    require_price_above_ema60: bool,
    apply_rollover_rejection: bool = True,
) -> bool:
    if require_price_above_ema20 and not metrics.get("price_above_ema20"):
        return False
    if require_price_above_ema60 and not metrics.get("price_above_ema60"):
        return False

    track = trend_track(
        metrics,
        min_ema20_slope_bps=min_ema20_slope_bps,
        min_ema40_slope_bps=min_ema40_slope_bps,
        min_ema60_slope_bps=min_ema60_slope_bps,
    )
    if track is None:
        return False
    if require_ema_stack and not metrics.get("ema_stack"):
        return False

    if apply_rollover_rejection and rejects_rollover(metrics):
        return False
    return True


def passes_mode_filter(
    metrics: dict,
    mode: str,
    *,
    limit_up_pct: float = DEFAULT_LIMIT_UP_PCT,
    limit_down_pct: float = DEFAULT_LIMIT_DOWN_PCT,
    limit_close_near_high_min: float = DEFAULT_LIMIT_CLOSE_NEAR_HIGH_MIN,
    limit_close_near_low_max: float = DEFAULT_LIMIT_CLOSE_NEAR_LOW_MAX,
    min_previous_day_volume: float = DEFAULT_MIN_PREVIOUS_DAY_VOLUME,
) -> bool:
    if mode == "liquid":
        return True
    if metrics["previous_day_volume"] < min_previous_day_volume:
        return False
    change_pct = float(metrics["previous_day_change_pct"])
    close_location = float(metrics["previous_day_close_location"])
    if mode == "limit-up":
        return change_pct >= limit_up_pct and close_location >= limit_close_near_high_min
    if mode == "limit-down":
        return change_pct <= limit_down_pct and close_location <= limit_close_near_low_max
    raise ValueError(f"Unsupported --mode: {mode}")


def _quote_spread_bps(quote: Quote | None) -> float | None:
    valid_quote = usable_quote(quote)
    return valid_quote.spread_bps if valid_quote else None


def trend_quality_score(metrics: dict) -> float:
    score = 0.0
    score += min(max((metrics.get("trend_60d_bps") or 0.0) / 1_000.0, 0.0), 4.0)
    score += min(max((metrics.get("trend_20d_bps") or 0.0) / 800.0, 0.0), 3.0)
    score += min(max((metrics.get("trend_5d_bps") or 0.0) / 400.0, 0.0), 2.0)
    if metrics.get("ema_stack"):
        score += 2.5
    if metrics.get("price_above_ema60"):
        score += 1.5
    elif metrics.get("near_ema60"):
        score += 0.75
    for slope_key in ("ema20_slope_bps", "ema40_slope_bps", "ema60_slope_bps"):
        slope = metrics.get(slope_key)
        if slope is not None and slope > 0:
            score += min(slope / 500.0, 1.0)
    if metrics.get("ema_gap_improving"):
        score += 1.0
    score += max(0.0, 1.5 - metrics.get("pullback_from_peak_pct", 0.0) * 15.0)
    if (metrics.get("trend_5d_bps") or 0.0) > 0 and (metrics.get("trend_10d_bps") or 0.0) > 0:
        score += min(metrics.get("recovery_from_low_pct", 0.0) * 5.0, 1.5)
    return score


def score_symbol(
    symbol: str,
    bars: list[Bar],
    quote: Quote | None,
    *,
    min_price: float,
    max_price: float,
    min_average_volume: float,
    min_median_dollar_volume: float,
    max_spread_bps: float,
    min_previous_day_volume: float,
    min_ema20_slope_bps: float,
    min_ema40_slope_bps: float,
    min_ema60_slope_bps: float,
    require_price_above_ema20: bool,
    require_ema_stack: bool,
    require_price_above_ema60: bool,
    reject_wide_spread: bool,
    apply_trend_filter: bool = True,
    apply_rollover_rejection: bool = True,
) -> dict | None:
    metrics = daily_metrics(bars)
    if not metrics:
        return None
    if not passes_liquidity_filter(
        metrics,
        min_price=min_price,
        max_price=max_price,
        min_average_volume=min_average_volume,
        min_median_dollar_volume=min_median_dollar_volume,
        min_previous_day_volume=min_previous_day_volume,
    ):
        return None
    if apply_trend_filter and not passes_trend_filter(
        metrics,
        min_ema20_slope_bps=min_ema20_slope_bps,
        min_ema40_slope_bps=min_ema40_slope_bps,
        min_ema60_slope_bps=min_ema60_slope_bps,
        require_price_above_ema20=require_price_above_ema20,
        require_ema_stack=require_ema_stack,
        require_price_above_ema60=require_price_above_ema60,
        apply_rollover_rejection=apply_rollover_rejection,
    ):
        return None

    spread_bps = _quote_spread_bps(quote)
    if reject_wide_spread:
        if spread_bps is None or spread_bps > max_spread_bps:
            return None

    liquidity_score = min(math.log10(metrics["average_volume"] / min_average_volume + 1.0), 6.0)
    dollar_score = min(math.log10(metrics["median_dollar_volume"] / min_median_dollar_volume + 1.0), 6.0)
    quote_score = 0.5 if spread_bps is not None and spread_bps <= max_spread_bps else 0.0
    score = liquidity_score + dollar_score + trend_quality_score(metrics) + quote_score
    track = (
        trend_track(
            metrics,
            min_ema20_slope_bps=min_ema20_slope_bps,
            min_ema40_slope_bps=min_ema40_slope_bps,
            min_ema60_slope_bps=min_ema60_slope_bps,
        )
        if apply_trend_filter
        else None
    )

    return {
        "symbol": symbol,
        "score": round(score, 3),
        "trend_track": track,
        "price": round(metrics["price"], 2),
        "average_volume": round(metrics["average_volume"], 2),
        "median_dollar_volume": round(metrics["median_dollar_volume"], 2),
        "average_dollar_volume": round(metrics["average_dollar_volume"], 2),
        "spread_bps": round(spread_bps, 2) if spread_bps is not None else None,
        "previous_day_change_pct": round(metrics["previous_day_change_pct"], 6),
        "previous_day_close_location": round(metrics["previous_day_close_location"], 4),
        "previous_day_volume": round(metrics["previous_day_volume"], 2),
        "previous_day_dollar_volume": round(metrics["previous_day_dollar_volume"], 2),
        "trend_20d_bps": round(metrics["trend_20d_bps"], 1) if metrics["trend_20d_bps"] is not None else None,
        "trend_10d_bps": round(metrics["trend_10d_bps"], 1) if metrics["trend_10d_bps"] is not None else None,
        "trend_5d_bps": round(metrics["trend_5d_bps"], 1) if metrics["trend_5d_bps"] is not None else None,
        "trend_60d_bps": round(metrics["trend_60d_bps"], 1) if metrics["trend_60d_bps"] is not None else None,
        "ema20_slope_bps": round(metrics["ema20_slope_bps"], 1) if metrics["ema20_slope_bps"] is not None else None,
        "ema40_slope_bps": round(metrics["ema40_slope_bps"], 1) if metrics["ema40_slope_bps"] is not None else None,
        "ema60_slope_bps": round(metrics["ema60_slope_bps"], 1) if metrics["ema60_slope_bps"] is not None else None,
        "ema_stack": bool(metrics["ema_stack"]),
        "ema40_above_ema60": bool(metrics["ema40_above_ema60"]),
        "price_above_ema20": bool(metrics["price_above_ema20"]),
        "price_above_ema40": bool(metrics["price_above_ema40"]),
        "price_above_ema60": bool(metrics["price_above_ema60"]),
        "near_ema60": bool(metrics["near_ema60"]),
        "ema_gap_improving": bool(metrics["ema_gap_improving"]),
        "pullback_from_peak_pct": round(metrics["pullback_from_peak_pct"], 4),
    }


def select_top_candidates(candidates: list[dict], top: int) -> list[dict]:
    return sorted(
        candidates,
        key=lambda item: (
            item["score"],
            item["median_dollar_volume"],
            item["average_volume"],
            item.get("trend_60d_bps") or 0.0,
        ),
        reverse=True,
    )[:top]


def _resolved_mode(args: argparse.Namespace) -> str:
    mode = str(getattr(args, "mode", "liquid") or "liquid")
    legacy_filter = getattr(args, "previous_day_filter", None)
    if legacy_filter:
        legacy_mode = LEGACY_PREVIOUS_DAY_FILTER_MODE.get(str(legacy_filter))
        if legacy_mode is None:
            raise ValueError("--previous-day-filter must be one of: none, sharp-drop, limit-up.")
        mode = legacy_mode
    if mode not in MODE_CHOICES:
        raise ValueError(f"--mode must be one of: {', '.join(sorted(MODE_CHOICES))}.")
    return mode


def _resolved_flag(args: argparse.Namespace, name: str, *, default: bool) -> bool:
    explicit = getattr(args, name, None)
    if explicit is None:
        return default
    return bool(explicit)


def build_universe(args: argparse.Namespace) -> dict:
    if args.top < 1:
        raise ValueError("--top must be at least 1.")
    if args.lookback_days < 61:
        raise ValueError("--lookback-days must be at least 61 for the 60-day trend filter.")
    if args.batch_size < 1:
        raise ValueError("--batch-size must be at least 1.")

    mode = _resolved_mode(args)
    min_ema20_slope_bps = float(getattr(args, "min_ema20_slope_bps", DEFAULT_MIN_EMA20_SLOPE_BPS))
    min_ema40_slope_bps = float(getattr(args, "min_ema40_slope_bps", DEFAULT_MIN_EMA40_SLOPE_BPS))
    min_ema60_slope_bps = float(getattr(args, "min_ema60_slope_bps", DEFAULT_MIN_EMA60_SLOPE_BPS))
    min_median_dollar_volume = float(
        getattr(args, "min_median_dollar_volume", DEFAULT_MIN_MEDIAN_DOLLAR_VOLUME)
    )
    limit_up_pct = float(getattr(args, "limit_up_pct", DEFAULT_LIMIT_UP_PCT))
    limit_down_pct = float(getattr(args, "limit_down_pct", DEFAULT_LIMIT_DOWN_PCT))
    limit_close_near_high_min = float(getattr(args, "limit_close_near_high_min", DEFAULT_LIMIT_CLOSE_NEAR_HIGH_MIN))
    limit_close_near_low_max = float(getattr(args, "limit_close_near_low_max", DEFAULT_LIMIT_CLOSE_NEAR_LOW_MAX))
    min_previous_day_volume = float(getattr(args, "min_previous_day_volume", DEFAULT_MIN_PREVIOUS_DAY_VOLUME))
    require_price_above_ema20 = _resolved_flag(
        args, "require_price_above_ema20", default=mode != "limit-down"
    )
    require_ema_stack = _resolved_flag(args, "require_ema_stack", default=False)
    require_price_above_ema60 = _resolved_flag(args, "require_price_above_ema60", default=False)
    apply_trend_filter = mode != "limit-down"
    apply_rollover_rejection = mode != "limit-down"
    skip_quotes = bool(getattr(args, "skip_quotes", False))

    settings_kwargs = {}
    if args.alpaca_api_key:
        settings_kwargs["alpaca_api_key"] = args.alpaca_api_key
    if args.alpaca_secret_key:
        settings_kwargs["alpaca_secret_key"] = args.alpaca_secret_key
    settings = load_settings(strategy_names=[], validate=False)
    if settings_kwargs:
        settings = Settings(**{**settings.__dict__, **settings_kwargs})

    as_of: date | None = None
    as_of_date_arg = getattr(args, "as_of_date", None)
    if as_of_date_arg and str(as_of_date_arg).strip():
        try:
            as_of = date.fromisoformat(str(as_of_date_arg).strip())
        except ValueError as exc:
            raise ValueError("--as-of-date must be YYYY-MM-DD.") from exc

    exchanges = {value.upper() for value in parse_symbols(args.exchanges)} if args.exchanges else None
    symbols = get_active_tradable_symbols(settings, exchanges=exchanges)
    if as_of is not None:
        bars_by_symbol = get_daily_bars(settings, symbols, args.lookback_days, args.batch_size, as_of=as_of)
    else:
        bars_by_symbol = get_daily_bars(settings, symbols, args.lookback_days, args.batch_size)

    preliminary = []
    for symbol in symbols:
        metrics = daily_metrics(bars_by_symbol.get(symbol, []))
        if not metrics:
            continue
        if not passes_liquidity_filter(
            metrics,
            min_price=args.min_price,
            max_price=args.max_price,
            min_average_volume=args.min_average_volume,
            min_median_dollar_volume=min_median_dollar_volume,
            min_previous_day_volume=min_previous_day_volume,
        ):
            continue
        if apply_trend_filter and not passes_trend_filter(
            metrics,
            min_ema20_slope_bps=min_ema20_slope_bps,
            min_ema40_slope_bps=min_ema40_slope_bps,
            min_ema60_slope_bps=min_ema60_slope_bps,
            require_price_above_ema20=require_price_above_ema20,
            require_ema_stack=require_ema_stack,
            require_price_above_ema60=require_price_above_ema60,
            apply_rollover_rejection=apply_rollover_rejection,
        ):
            continue
        if not passes_mode_filter(
            metrics,
            mode,
            limit_up_pct=limit_up_pct,
            limit_down_pct=limit_down_pct,
            limit_close_near_high_min=limit_close_near_high_min,
            limit_close_near_low_max=limit_close_near_low_max,
            min_previous_day_volume=min_previous_day_volume,
        ):
            continue
        preliminary.append(symbol)

    quotes = get_latest_quotes(settings, preliminary, args.batch_size) if preliminary and not skip_quotes else {}

    candidates = []
    for symbol in preliminary:
        result = score_symbol(
            symbol,
            bars_by_symbol.get(symbol, []),
            quotes.get(symbol),
            min_price=args.min_price,
            max_price=args.max_price,
            min_average_volume=args.min_average_volume,
            min_median_dollar_volume=min_median_dollar_volume,
            max_spread_bps=args.max_spread_bps,
            min_previous_day_volume=min_previous_day_volume,
            min_ema20_slope_bps=min_ema20_slope_bps,
            min_ema40_slope_bps=min_ema40_slope_bps,
            min_ema60_slope_bps=min_ema60_slope_bps,
            require_price_above_ema20=require_price_above_ema20,
            require_ema_stack=require_ema_stack,
            require_price_above_ema60=require_price_above_ema60,
            reject_wide_spread=not skip_quotes,
            apply_trend_filter=apply_trend_filter,
            apply_rollover_rejection=apply_rollover_rejection,
        )
        if result:
            candidates.append(result)

    selected = select_top_candidates(candidates, args.top)
    selected_symbols = [item["symbol"] for item in selected]
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(",".join(selected_symbols) + ("\n" if selected_symbols else ""))

    return {
        "selected_symbols": selected_symbols,
        "symbols_env_line": format_symbols_env_line(selected_symbols),
        "output": str(args.output) if args.output else None,
        "candidates": selected,
        "screened": len(symbols),
        "passed": len(candidates),
        "requested_top": args.top,
        "selected_count": len(selected_symbols),
        "as_of_date": as_of.isoformat() if as_of is not None else None,
        "mode": mode,
        "lookback_days": args.lookback_days,
        "min_ema20_slope_bps": min_ema20_slope_bps,
        "min_ema40_slope_bps": min_ema40_slope_bps,
        "min_ema60_slope_bps": min_ema60_slope_bps,
        "require_price_above_ema20": require_price_above_ema20,
        "require_ema_stack": require_ema_stack,
        "require_price_above_ema60": require_price_above_ema60,
        "apply_trend_filter": apply_trend_filter,
        "min_average_volume": args.min_average_volume,
        "min_median_dollar_volume": min_median_dollar_volume,
        "limit_up_pct": limit_up_pct,
        "limit_down_pct": limit_down_pct,
        "limit_close_near_high_min": limit_close_near_high_min,
        "limit_close_near_low_max": limit_close_near_low_max,
        "min_previous_day_volume": min_previous_day_volume,
        "quotes_checked": not skip_quotes,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "REST-only broad market universe builder. Creates a liquid, tradable boundary pool "
            "with established or early-recovery daily uptrend structure for downstream strategy selectors."
        )
    )
    parser.add_argument("--top", type=int, default=300)
    parser.add_argument("--output", type=Path, default=Path("data/opening_universe.txt"))
    parser.add_argument("--mode", choices=sorted(MODE_CHOICES), default="liquid")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--exchanges", default="NASDAQ,NYSE,ARCA", help="Comma-separated asset exchanges to include.")
    parser.add_argument("--min-price", type=float, default=5.0)
    parser.add_argument("--max-price", type=float, default=500.0)
    parser.add_argument("--min-average-volume", type=float, default=DEFAULT_MIN_AVERAGE_VOLUME)
    parser.add_argument("--min-median-dollar-volume", type=float, default=DEFAULT_MIN_MEDIAN_DOLLAR_VOLUME)
    parser.add_argument("--max-spread-bps", type=float, default=12.0)
    parser.add_argument("--min-ema20-slope-bps", type=float, default=DEFAULT_MIN_EMA20_SLOPE_BPS)
    parser.add_argument("--min-ema40-slope-bps", type=float, default=DEFAULT_MIN_EMA40_SLOPE_BPS)
    parser.add_argument("--min-ema60-slope-bps", type=float, default=DEFAULT_MIN_EMA60_SLOPE_BPS)
    parser.add_argument("--min-trend-bps", type=float, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--min-20d-trend-bps", type=float, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--min-5d-trend-bps", type=float, default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--allow-below-ema20",
        dest="require_price_above_ema20",
        action="store_false",
        help="Do not require the latest close to be above EMA20.",
    )
    parser.set_defaults(require_price_above_ema20=None)
    parser.add_argument(
        "--require-ema-stack",
        dest="require_ema_stack",
        action="store_true",
        help="Require EMA40 > EMA60 for all candidates (default: scoring bonus only).",
    )
    parser.add_argument(
        "--allow-ema-stack-break",
        dest="require_ema_stack",
        action="store_false",
        help=argparse.SUPPRESS,
    )
    parser.set_defaults(require_ema_stack=None)
    parser.add_argument(
        "--require-price-above-ema60",
        dest="require_price_above_ema60",
        action="store_true",
        help="Require close above EMA60 (default False; recovery track allows near-EMA60).",
    )
    parser.add_argument(
        "--allow-below-ema60",
        dest="require_price_above_ema60",
        action="store_false",
        help=argparse.SUPPRESS,
    )
    parser.set_defaults(require_price_above_ema60=None)
    parser.add_argument("--limit-up-pct", type=float, default=DEFAULT_LIMIT_UP_PCT)
    parser.add_argument("--limit-down-pct", type=float, default=DEFAULT_LIMIT_DOWN_PCT)
    parser.add_argument("--limit-close-near-high-min", type=float, default=DEFAULT_LIMIT_CLOSE_NEAR_HIGH_MIN)
    parser.add_argument("--limit-close-near-low-max", type=float, default=DEFAULT_LIMIT_CLOSE_NEAR_LOW_MAX)
    parser.add_argument("--min-previous-day-volume", type=float, default=DEFAULT_MIN_PREVIOUS_DAY_VOLUME)
    parser.add_argument(
        "--previous-day-filter",
        choices=sorted(LEGACY_PREVIOUS_DAY_FILTER_MODE),
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--skip-quotes",
        action="store_true",
        help="Skip latest quote checks. Spread is not enforced; liquidity/trend filters still apply.",
    )
    parser.add_argument(
        "--as-of-date",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "US/Eastern calendar day to anchor daily bars. The exclusive end is midnight "
            "at the start of the following day."
        ),
    )
    parser.add_argument("--alpaca-api-key", default=None)
    parser.add_argument("--alpaca-secret-key", default=None)
    return parser.parse_args()


def main() -> None:
    result = build_universe(parse_args())
    print(json.dumps(result, indent=2, sort_keys=True))
    line = result.get("symbols_env_line") or ""
    selected_count = int(result.get("selected_count") or 0)
    if selected_count == 0:
        print(
            "No symbols selected. "
            f"screened={result.get('screened')} passed={result.get('passed')} mode={result.get('mode')}",
            file=sys.stderr,
        )
        if not result.get("passed"):
            print(
                "Hint: passed=0 usually means missing Alpaca credentials, empty bar history, "
                "or no symbols met liquidity/trend/mode filters.",
                file=sys.stderr,
            )
    if line:
        print(
            "# Paste into profiles/*.env or `.env` - do not `export` (pollutes shell/tmux).",
            file=sys.stderr,
        )
        print(line)


if __name__ == "__main__":
    main()
