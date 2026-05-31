import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_JOURNAL_FILE = ROOT / "logs" / "trade_journal.jsonl"
TRADING_TZ = ZoneInfo("America/New_York")
MARKET_REGIME_RE = re.compile(r"\bmarket_regime\s+([a-z_]+)\b")
STRATEGY_REGIME_RE = re.compile(r"\bregime=([a-z_]+)(?::[0-9]+(?:\.[0-9]+)?)?\b")
SIZE_MULT_RE = re.compile(r"\bsize_mult=([0-9]+(?:\.[0-9]+)?)\b")


@dataclass(frozen=True)
class TradeEvent:
    event: str
    symbol: str
    timestamp_ms: int
    shares: int
    price: float
    pnl: float
    strategy: str
    reason: str
    order_id: str
    r_multiple: float | None = None
    exit_stage: str = ""
    runner_r_multiple: float | None = None
    full_trade_r_multiple: float | None = None
    cumulative_daily_pnl: float | None = None


@dataclass(frozen=True)
class PositionRoundTrip:
    symbol: str
    strategy: str
    shares: int
    buy_timestamp_ms: int
    final_sell_timestamp_ms: int
    buy_price: float
    average_sell_price: float
    pnl: float
    pnl_pct: float
    max_price: float
    min_price: float
    mfe_pct: float
    mae_pct: float
    final_reason: str
    exit_reasons: tuple[str, ...]
    exit_stages: tuple[str, ...]
    hold_seconds: float
    legs: int
    full_trade_r_multiple: float | None = None
    buy_order_id: str = ""
    entry_market_regime: str = "unknown"
    entry_size_multiplier: float | None = None


@dataclass(frozen=True)
class RoundTrip:
    symbol: str
    strategy: str
    shares: int
    buy_timestamp_ms: int
    sell_timestamp_ms: int
    buy_price: float
    sell_price: float
    pnl: float
    pnl_pct: float
    max_price: float
    min_price: float
    mfe_pct: float
    mae_pct: float
    reason: str
    hold_seconds: float
    r_multiple: float | None = None
    exit_stage: str = ""
    runner_r_multiple: float | None = None
    full_trade_r_multiple: float | None = None
    cumulative_daily_pnl: float | None = None
    buy_order_id: str = ""
    sell_order_id: str = ""
    entry_reason: str = ""
    entry_market_regime: str = "unknown"
    entry_size_multiplier: float | None = None


def parse_event(row: dict) -> TradeEvent:
    return TradeEvent(
        event=str(row.get("event", "")).lower(),
        symbol=str(row.get("symbol", "")).upper(),
        timestamp_ms=int(row.get("timestamp_ms", 0)),
        shares=int(float(row.get("shares", 0) or 0)),
        price=float(row.get("price", 0.0) or 0.0),
        pnl=float(row.get("pnl", 0.0) or 0.0),
        strategy=str(row.get("strategy", "")),
        reason=str(row.get("reason", "")),
        order_id=str(row.get("order_id", "")),
        r_multiple=optional_float(row.get("r_multiple")),
        exit_stage=str(row.get("exit_stage", "")),
        runner_r_multiple=optional_float(row.get("runner_r_multiple")),
        full_trade_r_multiple=optional_float(row.get("full_trade_r_multiple")),
        cumulative_daily_pnl=optional_float(row.get("cumulative_daily_pnl")),
    )


def load_events(path: Path) -> list[TradeEvent]:
    if not path.exists():
        return []

    events = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(parse_event(json.loads(line)))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"Invalid journal row {line_number} in {path}: {exc}") from exc
    return sorted(events, key=lambda item: item.timestamp_ms)


def build_round_trips(events: list[TradeEvent]) -> tuple[list[RoundTrip], list[dict]]:
    open_lots: dict[str, list[TradeEvent]] = defaultdict(list)
    price_points_by_symbol = defaultdict(list)
    for event in events:
        if event.price > 0:
            price_points_by_symbol[event.symbol].append((event.timestamp_ms, event.price))

    round_trips = []
    unmatched = []

    for event in events:
        if event.event == "buy":
            open_lots[event.symbol].append(event)
            continue

        if event.event != "sell":
            if event.price <= 0:
                unmatched.append({"event": event.event, "symbol": event.symbol, "reason": "unsupported event"})
            continue

        remaining_shares = event.shares
        while remaining_shares > 0 and open_lots[event.symbol]:
            buy = open_lots[event.symbol][0]
            matched_shares = min(remaining_shares, buy.shares)
            allocated_pnl = event.pnl * (matched_shares / event.shares) if event.shares else event.pnl
            max_price, min_price = excursion_prices(
                price_points_by_symbol[event.symbol],
                buy.timestamp_ms,
                event.timestamp_ms,
                buy.price,
                event.price,
            )
            round_trips.append(
                RoundTrip(
                    symbol=event.symbol,
                    strategy=buy.strategy or event.strategy,
                    shares=matched_shares,
                    buy_timestamp_ms=buy.timestamp_ms,
                    sell_timestamp_ms=event.timestamp_ms,
                    buy_price=buy.price,
                    sell_price=event.price,
                    pnl=allocated_pnl,
                    pnl_pct=pct_change(event.price, buy.price),
                    max_price=max_price,
                    min_price=min_price,
                    mfe_pct=pct_change(max_price, buy.price),
                    mae_pct=pct_change(min_price, buy.price),
                    reason=clean_reason(event.reason),
                    hold_seconds=(event.timestamp_ms - buy.timestamp_ms) / 1000,
                    r_multiple=event.r_multiple,
                    exit_stage=event.exit_stage,
                    runner_r_multiple=event.runner_r_multiple,
                    full_trade_r_multiple=event.full_trade_r_multiple,
                    cumulative_daily_pnl=event.cumulative_daily_pnl,
                    buy_order_id=buy.order_id,
                    sell_order_id=event.order_id,
                    entry_reason=clean_reason(buy.reason),
                    entry_market_regime=market_regime_from_reason(buy.reason),
                    entry_size_multiplier=size_multiplier_from_reason(buy.reason),
                )
            )
            remaining_shares -= matched_shares

            if matched_shares >= buy.shares:
                open_lots[event.symbol].pop(0)
            else:
                open_lots[event.symbol][0] = TradeEvent(
                    event=buy.event,
                    symbol=buy.symbol,
                    timestamp_ms=buy.timestamp_ms,
                    shares=buy.shares - matched_shares,
                    price=buy.price,
                    pnl=buy.pnl,
                    strategy=buy.strategy,
                    reason=buy.reason,
                    order_id=buy.order_id,
                    r_multiple=buy.r_multiple,
                    exit_stage=buy.exit_stage,
                    runner_r_multiple=buy.runner_r_multiple,
                    full_trade_r_multiple=buy.full_trade_r_multiple,
                    cumulative_daily_pnl=buy.cumulative_daily_pnl,
                )

        if remaining_shares > 0:
            unmatched.append({"event": "sell", "symbol": event.symbol, "shares": remaining_shares, "reason": "sell without matching buy"})

    for symbol, buys in open_lots.items():
        for buy in buys:
            unmatched.append({"event": "buy", "symbol": symbol, "shares": buy.shares, "reason": "open position"})

    return round_trips, unmatched


def build_position_round_trips(round_trips: list[RoundTrip]) -> list[PositionRoundTrip]:
    groups: dict[tuple[str, str, int, str, float], list[RoundTrip]] = defaultdict(list)
    for trade in round_trips:
        key = (trade.buy_order_id, trade.symbol, trade.buy_timestamp_ms, trade.strategy, trade.buy_price)
        groups[key].append(trade)

    positions: list[PositionRoundTrip] = []
    for trades in groups.values():
        legs = sorted(trades, key=lambda trade: trade.sell_timestamp_ms)
        first = legs[0]
        final = legs[-1]
        shares = sum(trade.shares for trade in legs)
        pnl = sum(trade.pnl for trade in legs)
        sell_notional = sum(trade.sell_price * trade.shares for trade in legs)
        average_sell_price = sell_notional / shares if shares > 0 else 0.0
        full_r_values = [trade.full_trade_r_multiple for trade in legs if trade.full_trade_r_multiple is not None]
        positions.append(
            PositionRoundTrip(
                symbol=first.symbol,
                strategy=first.strategy,
                shares=shares,
                buy_timestamp_ms=first.buy_timestamp_ms,
                final_sell_timestamp_ms=final.sell_timestamp_ms,
                buy_price=first.buy_price,
                average_sell_price=average_sell_price,
                pnl=pnl,
                pnl_pct=pct_change(average_sell_price, first.buy_price),
                max_price=max(trade.max_price for trade in legs),
                min_price=min(trade.min_price for trade in legs),
                mfe_pct=max(trade.mfe_pct for trade in legs),
                mae_pct=min(trade.mae_pct for trade in legs),
                final_reason=final.reason,
                exit_reasons=tuple(trade.reason or "unknown" for trade in legs),
                exit_stages=tuple(trade.exit_stage or "unknown" for trade in legs),
                hold_seconds=(final.sell_timestamp_ms - first.buy_timestamp_ms) / 1000,
                legs=len(legs),
                full_trade_r_multiple=full_r_values[-1] if full_r_values else None,
                buy_order_id=first.buy_order_id,
                entry_market_regime=first.entry_market_regime,
                entry_size_multiplier=first.entry_size_multiplier,
            )
        )
    return sorted(positions, key=lambda position: position.final_sell_timestamp_ms)


def pct_change(price: float, entry_price: float) -> float:
    return (price - entry_price) / entry_price if entry_price > 0 else 0.0


def clean_reason(reason: str) -> str:
    return str(reason or "").split(" | ")[0]


def market_regime_from_reason(reason: str) -> str:
    reason_text = str(reason or "")
    match = MARKET_REGIME_RE.search(reason_text)
    if match:
        return match.group(1)
    match = STRATEGY_REGIME_RE.search(reason_text)
    return match.group(1) if match else "unknown"


# Highest risk first. Core ladder from market_regime.py; strategy tags appended below.
MARKET_REGIME_RISK_ORDER = {
    "panic": 0,
    "risk_off": 1,
    "neutral_hardened": 2,  # stoch_macd / macd_early_impulse in weak neutral
    "neutral": 3,
    "risk_on": 4,
    "bypassed": 5,
    "disabled": 6,
}
_MARKET_REGIME_RISK_UNKNOWN_RANK = 99


def market_regime_risk_sort_key(regime: str) -> tuple[int, str]:
    name = (regime or "unknown").strip().lower()
    return (MARKET_REGIME_RISK_ORDER.get(name, _MARKET_REGIME_RISK_UNKNOWN_RANK), name)


def sort_market_regime_groups(groups: dict) -> list[tuple[str, dict]]:
    return sorted(groups.items(), key=lambda pair: market_regime_risk_sort_key(pair[0]))


def size_multiplier_from_reason(reason: str) -> float | None:
    match = SIZE_MULT_RE.search(str(reason or ""))
    return float(match.group(1)) if match else None


def optional_float(value) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def excursion_prices(
    price_points: list[tuple[int, float]],
    entry_ms: int,
    exit_ms: int,
    entry_price: float,
    exit_price: float,
) -> tuple[float, float]:
    prices = [entry_price, exit_price]
    prices.extend(price for timestamp_ms, price in price_points if entry_ms <= timestamp_ms <= exit_ms and price > 0)
    return max(prices), min(prices)


def summarize(round_trips: list[RoundTrip], unmatched: list[dict]) -> dict:
    positions = build_position_round_trips(round_trips)
    wins = [trade for trade in round_trips if trade.pnl > 0]
    losses = [trade for trade in round_trips if trade.pnl < 0]
    flat = [trade for trade in round_trips if trade.pnl == 0]
    hold_times = [trade.hold_seconds for trade in round_trips]
    pnls = [trade.pnl for trade in round_trips]
    pnl_pcts = [trade.pnl_pct for trade in round_trips]
    mfe_pcts = [trade.mfe_pct for trade in round_trips]
    mae_pcts = [trade.mae_pct for trade in round_trips]
    missed_profit_pcts = [trade.mfe_pct - trade.pnl_pct for trade in round_trips]
    r_multiples = [trade.r_multiple for trade in round_trips if trade.r_multiple is not None]
    runner_r_multiples = [trade.runner_r_multiple for trade in round_trips if trade.runner_r_multiple is not None]
    full_trade_r_multiples = [trade.full_trade_r_multiple for trade in round_trips if trade.full_trade_r_multiple is not None]

    by_symbol = defaultdict(list)
    by_reason = defaultdict(list)
    by_strategy = defaultdict(list)
    by_day = defaultdict(list)
    by_day_strategy = defaultdict(lambda: defaultdict(list))
    by_entry_market_regime = defaultdict(list)
    for trade in round_trips:
        by_symbol[trade.symbol].append(trade)
        by_reason[trade.reason or "unknown"].append(trade)
        by_strategy[trade.strategy or "unknown"].append(trade)
        by_entry_market_regime[trade.entry_market_regime or "unknown"].append(trade)
        day = trade_day(trade)
        by_day[day].append(trade)
        by_day_strategy[day][trade.strategy or "unknown"].append(trade)

    return {
        "positions": summarize_positions(positions),
        "trades": len(round_trips),
        "wins": len(wins),
        "losses": len(losses),
        "flat": len(flat),
        "win_rate": round(len(wins) / len(round_trips), 4) if round_trips else 0.0,
        "total_pnl": round(sum(pnls), 4),
        "average_pnl": round(mean(pnls), 4) if pnls else 0.0,
        "median_pnl": round(median(pnls), 4) if pnls else 0.0,
        "average_pnl_pct": round(mean(pnl_pcts), 6) if pnl_pcts else 0.0,
        "average_mfe_pct": round(mean(mfe_pcts), 6) if mfe_pcts else 0.0,
        "average_mae_pct": round(mean(mae_pcts), 6) if mae_pcts else 0.0,
        "average_missed_profit_pct": round(mean(missed_profit_pcts), 6) if missed_profit_pcts else 0.0,
        "expectancy_r": round(mean(r_multiples), 4) if r_multiples else 0.0,
        "average_runner_r_multiple": round(mean(runner_r_multiples), 4) if runner_r_multiples else 0.0,
        "average_full_trade_r_multiple": round(mean(full_trade_r_multiples), 4) if full_trade_r_multiples else 0.0,
        "average_hold_seconds": round(mean(hold_times), 2) if hold_times else 0.0,
        "median_hold_seconds": round(median(hold_times), 2) if hold_times else 0.0,
        "best_trade": trade_summary(max(round_trips, key=lambda trade: trade.pnl)) if round_trips else None,
        "worst_trade": trade_summary(min(round_trips, key=lambda trade: trade.pnl)) if round_trips else None,
        "by_symbol": summarize_groups(by_symbol),
        "by_exit_reason": summarize_groups(by_reason),
        "by_strategy": summarize_groups(by_strategy),
        "by_entry_market_regime": summarize_groups(by_entry_market_regime),
        "by_day": summarize_groups(by_day),
        "by_day_strategy": summarize_nested_groups(by_day_strategy),
        "by_entry_time_et": summarize_entry_time_hour_et(round_trips),
        "by_day_entry_time_et": summarize_entry_time_hour_et_by_entry_day(round_trips),
        "exit_reason_counts": dict(Counter(trade.reason or "unknown" for trade in round_trips)),
        "unmatched_events": unmatched,
    }


def summarize_positions(positions: list[PositionRoundTrip]) -> dict:
    wins = [position for position in positions if position.pnl > 0]
    losses = [position for position in positions if position.pnl < 0]
    pnls = [position.pnl for position in positions]
    pnl_pcts = [position.pnl_pct for position in positions]
    full_r = [position.full_trade_r_multiple for position in positions if position.full_trade_r_multiple is not None]
    by_strategy = defaultdict(list)
    by_final_reason = defaultdict(list)
    by_entry_market_regime = defaultdict(list)
    for position in positions:
        by_strategy[position.strategy or "unknown"].append(position)
        by_final_reason[position.final_reason or "unknown"].append(position)
        by_entry_market_regime[position.entry_market_regime or "unknown"].append(position)
    return {
        "count": len(positions),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(positions), 4) if positions else 0.0,
        "total_pnl": round(sum(pnls), 4),
        "average_pnl": round(mean(pnls), 4) if pnls else 0.0,
        "median_pnl": round(median(pnls), 4) if pnls else 0.0,
        "average_pnl_pct": round(mean(pnl_pcts), 6) if pnl_pcts else 0.0,
        "expectancy_full_r": round(mean(full_r), 4) if full_r else 0.0,
        "average_legs": round(mean([position.legs for position in positions]), 2) if positions else 0.0,
        "by_strategy": summarize_position_groups(by_strategy),
        "by_final_exit_reason": summarize_position_groups(by_final_reason),
        "by_entry_market_regime": summarize_position_groups(by_entry_market_regime),
        "best_position": position_summary(max(positions, key=lambda position: position.pnl)) if positions else None,
        "worst_position": position_summary(min(positions, key=lambda position: position.pnl)) if positions else None,
    }


def summarize_groups(groups: dict[str, list[RoundTrip]]) -> dict:
    summary = {}
    for name, trades in sorted(groups.items()):
        wins = sum(1 for trade in trades if trade.pnl > 0)
        pnl = sum(trade.pnl for trade in trades)
        summary[name] = {
            "trades": len(trades),
            "wins": wins,
            "win_rate": round(wins / len(trades), 4) if trades else 0.0,
            "total_pnl": round(pnl, 4),
            "average_pnl": round(pnl / len(trades), 4) if trades else 0.0,
            "average_pnl_pct": round(mean([trade.pnl_pct for trade in trades]), 6) if trades else 0.0,
            "average_mfe_pct": round(mean([trade.mfe_pct for trade in trades]), 6) if trades else 0.0,
            "average_hold_seconds": round(mean([trade.hold_seconds for trade in trades]), 2) if trades else 0.0,
            "expectancy_r": round(mean([trade.r_multiple for trade in trades if trade.r_multiple is not None]), 4)
            if any(trade.r_multiple is not None for trade in trades)
            else 0.0,
            "max_drawdown": round(max_drawdown(trades), 4),
        }
    return summary


def summarize_position_groups(groups: dict[str, list[PositionRoundTrip]]) -> dict:
    summary = {}
    for name, positions in sorted(groups.items()):
        wins = sum(1 for position in positions if position.pnl > 0)
        pnl = sum(position.pnl for position in positions)
        full_r = [position.full_trade_r_multiple for position in positions if position.full_trade_r_multiple is not None]
        summary[name] = {
            "positions": len(positions),
            "wins": wins,
            "win_rate": round(wins / len(positions), 4) if positions else 0.0,
            "total_pnl": round(pnl, 4),
            "average_pnl": round(pnl / len(positions), 4) if positions else 0.0,
            "average_pnl_pct": round(mean([position.pnl_pct for position in positions]), 6) if positions else 0.0,
            "expectancy_full_r": round(mean(full_r), 4) if full_r else 0.0,
            "average_legs": round(mean([position.legs for position in positions]), 2) if positions else 0.0,
            "max_drawdown": round(max_position_drawdown(positions), 4),
        }
    return summary


def summarize_nested_groups(groups: dict[str, dict[str, list[RoundTrip]]]) -> dict:
    summary = {}
    for outer_name, nested in sorted(groups.items()):
        summary[outer_name] = summarize_groups(nested)
    return summary


def trade_day(trade: RoundTrip) -> str:
    return datetime.fromtimestamp(trade.sell_timestamp_ms / 1000, tz=TRADING_TZ).date().isoformat()


def event_day(event: TradeEvent) -> str:
    return datetime.fromtimestamp(event.timestamp_ms / 1000, tz=TRADING_TZ).date().isoformat()


def entry_hour_bucket_et(buy_timestamp_ms: int) -> str:
    dt = datetime.fromtimestamp(buy_timestamp_ms / 1000, tz=TRADING_TZ)
    return f"{dt.hour:02d}:00-{dt.hour:02d}:59"


def entry_noon_bucket_et(buy_timestamp_ms: int) -> str:
    dt = datetime.fromtimestamp(buy_timestamp_ms / 1000, tz=TRADING_TZ)
    return "before 12:00" if dt.hour < 12 else "12:00 and after"


NOON_ENTRY_BUCKETS = ("before 12:00", "12:00 and after")


def entry_day_et(trade: RoundTrip) -> str:
    """Calendar date of the buy in America/New_York (for per-day entry-time stats)."""
    return datetime.fromtimestamp(trade.buy_timestamp_ms / 1000, tz=TRADING_TZ).date().isoformat()


def entry_time_bucket_stats(bucket: list[RoundTrip]) -> dict:
    if not bucket:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "total_pnl": 0.0}
    wins = sum(1 for t in bucket if t.pnl > 0)
    losses = sum(1 for t in bucket if t.pnl < 0)
    return {
        "trades": len(bucket),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(bucket), 4),
        "total_pnl": round(sum(t.pnl for t in bucket), 4),
    }


def summarize_entry_time_hour_et(trades: list[RoundTrip]) -> dict[str, dict]:
    by_hour: dict[str, list[RoundTrip]] = defaultdict(list)
    for trade in trades:
        by_hour[entry_hour_bucket_et(trade.buy_timestamp_ms)].append(trade)
    return {hour: entry_time_bucket_stats(hour_trades) for hour, hour_trades in sorted(by_hour.items())}


def summarize_entry_time_hour_et_by_entry_day(trades: list[RoundTrip]) -> dict[str, dict[str, dict]]:
    """Before/after noon buckets grouped by entry calendar day (America/New_York)."""
    by_day: dict[str, list[RoundTrip]] = defaultdict(list)
    for t in trades:
        by_day[entry_day_et(t)].append(t)

    summary = {}
    for day, day_trades in sorted(by_day.items()):
        buckets: dict[str, list[RoundTrip]] = defaultdict(list)
        for trade in day_trades:
            buckets[entry_noon_bucket_et(trade.buy_timestamp_ms)].append(trade)
        summary[day] = {
            name: entry_time_bucket_stats(buckets[name])
            for name in NOON_ENTRY_BUCKETS
            if name in buckets
        }
    return summary


def trade_summary(trade: RoundTrip) -> dict:
    return {
        "symbol": trade.symbol,
        "strategy": trade.strategy,
        "trade_day": trade_day(trade),
        "shares": trade.shares,
        "buy_time": format_timestamp(trade.buy_timestamp_ms),
        "sell_time": format_timestamp(trade.sell_timestamp_ms),
        "buy_price": trade.buy_price,
        "sell_price": trade.sell_price,
        "pnl": round(trade.pnl, 4),
        "pnl_pct": round(trade.pnl_pct, 6),
        "max_price": trade.max_price,
        "min_price": trade.min_price,
        "mfe_pct": round(trade.mfe_pct, 6),
        "mae_pct": round(trade.mae_pct, 6),
        "missed_profit_pct": round(trade.mfe_pct - trade.pnl_pct, 6),
        "reason": trade.reason,
        "hold_seconds": round(trade.hold_seconds, 2),
        "r_multiple": round(trade.r_multiple, 4) if trade.r_multiple is not None else None,
        "exit_stage": trade.exit_stage,
        "runner_r_multiple": round(trade.runner_r_multiple, 4) if trade.runner_r_multiple is not None else None,
        "full_trade_r_multiple": round(trade.full_trade_r_multiple, 4) if trade.full_trade_r_multiple is not None else None,
    }


def position_summary(position: PositionRoundTrip) -> dict:
    return {
        "symbol": position.symbol,
        "strategy": position.strategy,
        "trade_day": datetime.fromtimestamp(position.final_sell_timestamp_ms / 1000, tz=TRADING_TZ).date().isoformat(),
        "shares": position.shares,
        "buy_time": format_timestamp(position.buy_timestamp_ms),
        "final_sell_time": format_timestamp(position.final_sell_timestamp_ms),
        "buy_price": position.buy_price,
        "average_sell_price": round(position.average_sell_price, 4),
        "pnl": round(position.pnl, 4),
        "pnl_pct": round(position.pnl_pct, 6),
        "mfe_pct": round(position.mfe_pct, 6),
        "mae_pct": round(position.mae_pct, 6),
        "final_reason": position.final_reason,
        "exit_reasons": list(position.exit_reasons),
        "exit_stages": list(position.exit_stages),
        "hold_seconds": round(position.hold_seconds, 2),
        "legs": position.legs,
        "full_trade_r_multiple": round(position.full_trade_r_multiple, 4)
        if position.full_trade_r_multiple is not None
        else None,
        "entry_market_regime": position.entry_market_regime,
        "entry_size_multiplier": position.entry_size_multiplier,
    }


def max_drawdown(trades: list[RoundTrip]) -> float:
    peak = 0.0
    cumulative = 0.0
    drawdown = 0.0
    for trade in sorted(trades, key=lambda item: item.sell_timestamp_ms):
        cumulative += trade.pnl
        peak = max(peak, cumulative)
        drawdown = min(drawdown, cumulative - peak)
    return abs(drawdown)


def max_position_drawdown(positions: list[PositionRoundTrip]) -> float:
    peak = 0.0
    cumulative = 0.0
    drawdown = 0.0
    for position in sorted(positions, key=lambda item: item.final_sell_timestamp_ms):
        cumulative += position.pnl
        peak = max(peak, cumulative)
        drawdown = min(drawdown, cumulative - peak)
    return abs(drawdown)


def format_timestamp(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=TRADING_TZ).isoformat(timespec="seconds")


DETAIL_INDENT = "  "
SUMMARY_RULE = "═" * 72


def _format_pnl(value: float) -> str:
    return f"{value:+.2f}"


def _format_r(value: float) -> str:
    return f"{value:+.2f}R"


def _print_section(title: str) -> None:
    print(f"\n{title}")
    print("─" * min(max(len(title), 24), 72))


def _column_widths(headers: list[str], rows: list[list[str]]) -> list[int]:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    return widths


def _print_table(headers: list[str], rows: list[list[str]], *, indent: str = DETAIL_INDENT) -> None:
    if not rows:
        return
    widths = _column_widths(headers, rows)
    header_line = indent + "  ".join(
        f"{header:<{widths[index]}}" if index == 0 else f"{header:>{widths[index]}}"
        for index, header in enumerate(headers)
    )
    print(header_line)
    print(indent + "  ".join("─" * width for width in widths))
    for row in rows:
        print(
            indent
            + "  ".join(
                f"{cell:<{widths[index]}}" if index == 0 else f"{cell:>{widths[index]}}"
                for index, cell in enumerate(row)
            )
        )


def _metric_cell(label: str, value: str, width: int) -> str:
    value = str(value)
    space = width - len(label) - len(value) - 1
    if space < 1:
        space = 1
    return f"{label}{' ' * space}{value}"


def _print_aligned_metric_rows(
    rows: list[list[tuple[str, str]]],
    *,
    indent: str = "",
    column_width: int = 22,
    separator: str = " | ",
) -> None:
    for row in rows:
        if not row:
            continue
        row_width = max(column_width, max(len(label) + len(value) + 2 for label, value in row))
        cells = [_metric_cell(label, value, row_width) for label, value in row]
        print(indent + separator.join(cells))


def _print_by_day_entry_time_et(by_day: dict[str, dict[str, dict]]) -> None:
    _print_section("Entry Time vs 12:00 by Day (America/New_York)")

    trades_w = 5
    win_w = 6
    pnl_w = 7
    label_w = max(len(name) for name in NOON_ENTRY_BUCKETS)
    day_w = max(10, *(len(day) for day in by_day))

    for buckets in by_day.values():
        for name in NOON_ENTRY_BUCKETS:
            item = buckets.get(name)
            if not item or not item["trades"]:
                continue
            trades_w = max(trades_w, len(str(item["trades"])))
            win_w = max(win_w, len(f"{item['win_rate']:.1%}"))
            pnl_w = max(pnl_w, len(_format_pnl(item["total_pnl"])))
            label_w = max(label_w, len(name))

    for day in sorted(by_day):
        buckets = by_day[day]
        blocks: list[str] = []
        for name in NOON_ENTRY_BUCKETS:
            item = buckets.get(name)
            if not item or not item["trades"]:
                continue
            trades = str(item["trades"])
            win = f"{item['win_rate']:.1%}"
            pnl = _format_pnl(item["total_pnl"])
            metrics = f"{trades:>{trades_w}} @ {win:>{win_w}}  {pnl:>{pnl_w}}"
            blocks.append(f"{name:<{label_w}} {metrics}")
        if blocks:
            print(f"{DETAIL_INDENT}{day:<{day_w}}  " + " | ".join(blocks))


def _print_text_details(summary: dict, positions: dict) -> None:
    if summary["by_exit_reason"]:
        _print_section("Exit Reasons")
        rows = [
            [
                reason,
                str(item["trades"]),
                _format_pnl(item["total_pnl"]),
                f"{item['average_pnl_pct']:.2%}",
                f"{item['average_mfe_pct']:.2%}",
                f"{item['average_hold_seconds']:.0f}s",
                f"{item['win_rate']:.1%}",
            ]
            for reason, item in sorted(summary["by_exit_reason"].items(), key=lambda pair: pair[1]["trades"], reverse=True)
        ]
        _print_table(["Reason", "Trades", "P/L", "Avg P/L%", "MFE", "Hold", "Win%"], rows)

    if summary["by_symbol"]:
        _print_section("Symbols")
        rows = [
            [
                symbol,
                str(item["trades"]),
                _format_pnl(item["total_pnl"]),
                f"{item['win_rate']:.1%}",
            ]
            for symbol, item in sorted(summary["by_symbol"].items(), key=lambda pair: pair[1]["total_pnl"], reverse=True)
        ]
        _print_table(["Symbol", "Trades", "P/L", "Win%"], rows)

    if summary["by_strategy"]:
        _print_section("Strategies")
        rows = [
            [
                strategy,
                str(item["trades"]),
                _format_pnl(item["total_pnl"]),
                f"{item['win_rate']:.1%}",
            ]
            for strategy, item in sorted(summary["by_strategy"].items(), key=lambda pair: pair[0])
        ]
        _print_table(["Strategy", "Trades", "P/L", "Win%"], rows)

    if summary.get("by_entry_market_regime"):
        _print_section("Entry Market Regime")
        rows = [
            [
                regime,
                str(item["trades"]),
                _format_pnl(item["total_pnl"]),
                f"{item['win_rate']:.1%}",
            ]
            for regime, item in sort_market_regime_groups(summary["by_entry_market_regime"])
        ]
        _print_table(["Regime", "Trades", "P/L", "Win%"], rows)

    if summary.get("by_entry_time_et"):
        _print_section("Win Rate by Entry Hour (America/New_York)")
        rows = [
            [
                hour,
                str(item["trades"]),
                f"{item['win_rate']:.1%}",
                _format_pnl(item["total_pnl"]),
                str(item["wins"]),
                str(item["losses"]),
            ]
            for hour, item in summary["by_entry_time_et"].items()
        ]
        _print_table(["Hour", "Trades", "Win%", "P/L", "Wins", "Losses"], rows)

    if summary.get("by_day_entry_time_et"):
        _print_by_day_entry_time_et(summary["by_day_entry_time_et"])

    if summary["by_day"]:
        _print_section("Trading Days")
        rows = [
            [
                day,
                str(item["trades"]),
                _format_pnl(item["total_pnl"]),
                _format_r(item["expectancy_r"]),
                _format_pnl(-item["max_drawdown"]),
                f"{item['win_rate']:.1%}",
            ]
            for day, item in sorted(summary["by_day"].items())
        ]
        _print_table(["Day", "Trades", "P/L", "Expect", "Max DD", "Win%"], rows)

    if positions.get("by_strategy"):
        _print_section("Position Strategies")
        rows = [
            [
                strategy,
                str(item["positions"]),
                _format_pnl(item["total_pnl"]),
                f"{item['win_rate']:.1%}",
                f"{item['average_legs']:.2f}",
            ]
            for strategy, item in sorted(positions["by_strategy"].items(), key=lambda pair: pair[0])
        ]
        _print_table(["Strategy", "Positions", "P/L", "Win%", "Avg legs"], rows)

    if positions.get("by_entry_market_regime"):
        _print_section("Position Entry Market Regime")
        rows = [
            [
                regime,
                str(item["positions"]),
                _format_pnl(item["total_pnl"]),
                f"{item['win_rate']:.1%}",
            ]
            for regime, item in sort_market_regime_groups(positions["by_entry_market_regime"])
        ]
        _print_table(["Regime", "Positions", "P/L", "Win%"], rows)

    highlight_rows: list[list[str]] = []
    if summary.get("best_trade"):
        trade = summary["best_trade"]
        highlight_rows.append(["Best trade", trade["symbol"], _format_pnl(trade["pnl"]), trade["reason"]])
    if summary.get("worst_trade"):
        trade = summary["worst_trade"]
        highlight_rows.append(["Worst trade", trade["symbol"], _format_pnl(trade["pnl"]), trade["reason"]])
    if positions.get("best_position"):
        position = positions["best_position"]
        highlight_rows.append(
            ["Best position", position["symbol"], _format_pnl(position["pnl"]), position["final_reason"]]
        )
    if positions.get("worst_position"):
        position = positions["worst_position"]
        highlight_rows.append(
            ["Worst position", position["symbol"], _format_pnl(position["pnl"]), position["final_reason"]]
        )
    if highlight_rows:
        _print_section("Highlights")
        _print_table(["Type", "Symbol", "P/L", "Exit"], highlight_rows)

    if summary["unmatched_events"]:
        _print_section("Diagnostics")
        print(f"{DETAIL_INDENT}Unmatched events: {len(summary['unmatched_events'])}")


def _print_text_summary(summary: dict, positions: dict) -> None:
    print(f"\n{SUMMARY_RULE}")
    print("Trade Journal Summary")
    print("─" * len("Trade Journal Summary"))
    _print_aligned_metric_rows(
        [
            [
                ("Trades", str(summary["trades"])),
                ("Wins", str(summary["wins"])),
                ("Losses", str(summary["losses"])),
                ("Win rate", f"{summary['win_rate']:.1%}"),
            ],
            [
                ("Total P/L", _format_pnl(summary["total_pnl"])),
                ("Avg P/L", _format_pnl(summary["average_pnl"])),
                ("Median P/L", _format_pnl(summary["median_pnl"])),
            ],
            [
                ("Expectancy", _format_r(summary["expectancy_r"])),
                ("Avg runner", _format_r(summary["average_runner_r_multiple"])),
                ("Avg full trade", _format_r(summary["average_full_trade_r_multiple"])),
            ],
            [
                ("Avg P/L%", f"{summary['average_pnl_pct']:.2%}"),
                ("Avg MFE", f"{summary['average_mfe_pct']:.2%}"),
                ("Avg MAE", f"{summary['average_mae_pct']:.2%}"),
                ("Avg missed", f"{summary['average_missed_profit_pct']:.2%}"),
            ],
            [
                ("Avg hold", f"{summary['average_hold_seconds']:.1f}s"),
                ("Median hold", f"{summary['median_hold_seconds']:.1f}s"),
            ],
        ],
        indent=DETAIL_INDENT,
    )

    if positions:
        print(f"\n{DETAIL_INDENT}Positions (partials/runners combined)")
        print(f"{DETAIL_INDENT}────────────────────────────")
        _print_aligned_metric_rows(
            [
                [
                    ("Count", str(positions["count"])),
                    ("Wins", str(positions["wins"])),
                    ("Losses", str(positions["losses"])),
                    ("Win rate", f"{positions['win_rate']:.1%}"),
                ],
                [
                    ("Total P/L", _format_pnl(positions["total_pnl"])),
                    ("Avg P/L", _format_pnl(positions["average_pnl"])),
                    ("Median P/L", _format_pnl(positions["median_pnl"])),
                ],
                [
                    ("Expectancy", _format_r(positions["expectancy_full_r"])),
                    ("Avg legs", f"{positions['average_legs']:.2f}"),
                ],
            ],
            indent=DETAIL_INDENT,
        )


def print_text(summary: dict) -> None:
    positions = summary.get("positions", {})
    _print_text_details(summary, positions)
    _print_text_summary(summary, positions)


def analyze(path: Path, strategy: str | None = None, target_date: str | None = None) -> dict:
    events = load_events(path)
    if target_date:
        events = [event for event in events if event_day(event) == target_date]
    round_trips, unmatched = build_round_trips(events)
    if strategy:
        needle = strategy.strip().lower()
        round_trips = [t for t in round_trips if (t.strategy or "").lower() == needle]
    return summarize(round_trips, unmatched)


def parse_target_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be YYYY-MM-DD") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze confirmed buy/sell events from logs/trade_journal.jsonl.")
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL_FILE, help="Path to trade_journal.jsonl.")
    parser.add_argument(
        "--date",
        type=parse_target_date,
        default=None,
        help="Only include journal events whose timestamp falls on this YYYY-MM-DD trading date in America/New_York.",
    )
    parser.add_argument(
        "--strategy",
        type=str,
        default="",
        help="Only include round-trips for this strategy (e.g. opening_impulse). Matches journal strategy name.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON summary.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = analyze(args.journal, strategy=args.strategy or None, target_date=args.date or None)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print_text(summary)


if __name__ == "__main__":
    main()
