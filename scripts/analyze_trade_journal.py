import argparse
import json
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


DEFAULT_JOURNAL_FILE = Path("logs/trade_journal.jsonl")
TRADING_TZ = ZoneInfo("America/New_York")


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
                    reason=event.reason.split(" | ")[0],
                    hold_seconds=(event.timestamp_ms - buy.timestamp_ms) / 1000,
                    r_multiple=event.r_multiple,
                    exit_stage=event.exit_stage,
                    runner_r_multiple=event.runner_r_multiple,
                    full_trade_r_multiple=event.full_trade_r_multiple,
                    cumulative_daily_pnl=event.cumulative_daily_pnl,
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


def pct_change(price: float, entry_price: float) -> float:
    return (price - entry_price) / entry_price if entry_price > 0 else 0.0


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
    for trade in round_trips:
        by_symbol[trade.symbol].append(trade)
        by_reason[trade.reason or "unknown"].append(trade)
        by_strategy[trade.strategy or "unknown"].append(trade)
        day = trade_day(trade)
        by_day[day].append(trade)
        by_day_strategy[day][trade.strategy or "unknown"].append(trade)

    return {
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
        "by_day": summarize_groups(by_day),
        "by_day_strategy": summarize_nested_groups(by_day_strategy),
        "by_entry_time_et": summarize_entry_time_noon_et(round_trips),
        "by_day_entry_time_et": summarize_entry_time_noon_et_by_entry_day(round_trips),
        "exit_reason_counts": dict(Counter(trade.reason or "unknown" for trade in round_trips)),
        "unmatched_events": unmatched,
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


def summarize_nested_groups(groups: dict[str, dict[str, list[RoundTrip]]]) -> dict:
    summary = {}
    for outer_name, nested in sorted(groups.items()):
        summary[outer_name] = summarize_groups(nested)
    return summary


def trade_day(trade: RoundTrip) -> str:
    return datetime.fromtimestamp(trade.sell_timestamp_ms / 1000, tz=TRADING_TZ).date().isoformat()


def entry_before_noon_et(buy_timestamp_ms: int) -> bool:
    """True when the entry (buy) instant is strictly before 12:00:00 in America/New_York."""
    dt = datetime.fromtimestamp(buy_timestamp_ms / 1000, tz=TRADING_TZ)
    noon = dt.replace(hour=12, minute=0, second=0, microsecond=0)
    return dt < noon


def entry_day_et(trade: RoundTrip) -> str:
    """Calendar date of the buy in America/New_York (for per-day entry-time stats)."""
    return datetime.fromtimestamp(trade.buy_timestamp_ms / 1000, tz=TRADING_TZ).date().isoformat()


def noon_bucket_stats(bucket: list[RoundTrip]) -> dict:
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


def summarize_entry_time_noon_et(trades: list[RoundTrip]) -> dict[str, dict]:
    before = [t for t in trades if entry_before_noon_et(t.buy_timestamp_ms)]
    after = [t for t in trades if not entry_before_noon_et(t.buy_timestamp_ms)]
    return {
        "before_12_00_et": noon_bucket_stats(before),
        "from_12_00_et": noon_bucket_stats(after),
    }


def summarize_entry_time_noon_et_by_entry_day(trades: list[RoundTrip]) -> dict[str, dict[str, dict]]:
    """Same noon buckets as by_entry_time_et, grouped by entry calendar day (America/New_York)."""
    by_day: dict[str, list[RoundTrip]] = defaultdict(list)
    for t in trades:
        by_day[entry_day_et(t)].append(t)
    return {day: summarize_entry_time_noon_et(day_trades) for day, day_trades in sorted(by_day.items())}


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


def max_drawdown(trades: list[RoundTrip]) -> float:
    peak = 0.0
    cumulative = 0.0
    drawdown = 0.0
    for trade in sorted(trades, key=lambda item: item.sell_timestamp_ms):
        cumulative += trade.pnl
        peak = max(peak, cumulative)
        drawdown = min(drawdown, cumulative - peak)
    return abs(drawdown)


def format_timestamp(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=TRADING_TZ).isoformat(timespec="seconds")


def print_text(summary: dict) -> None:
    print("Trade Journal Summary")
    print(f"Trades: {summary['trades']} | Wins: {summary['wins']} | Losses: {summary['losses']} | Win rate: {summary['win_rate']:.1%}")
    print(f"Total P/L: {summary['total_pnl']:.2f} | Avg P/L: {summary['average_pnl']:.2f} | Median P/L: {summary['median_pnl']:.2f}")
    print(
        f"Expectancy: {summary['expectancy_r']:.2f}R | "
        f"Avg runner: {summary['average_runner_r_multiple']:.2f}R | "
        f"Avg full trade: {summary['average_full_trade_r_multiple']:.2f}R"
    )
    print(
        "Avg P/L%: "
        f"{summary['average_pnl_pct']:.2%} | Avg MFE: {summary['average_mfe_pct']:.2%} | "
        f"Avg MAE: {summary['average_mae_pct']:.2%} | Avg missed: {summary['average_missed_profit_pct']:.2%}"
    )
    print(f"Avg hold: {summary['average_hold_seconds']:.1f}s | Median hold: {summary['median_hold_seconds']:.1f}s")

    if summary["by_exit_reason"]:
        print("\nExit Reasons")
        for reason, item in sorted(summary["by_exit_reason"].items(), key=lambda pair: pair[1]["trades"], reverse=True):
            print(
                f"- {reason}: {item['trades']} trades, P/L {item['total_pnl']:.2f}, "
                f"avg P/L% {item['average_pnl_pct']:.2%}, avg MFE {item['average_mfe_pct']:.2%}, "
                f"avg hold {item['average_hold_seconds']:.1f}s, win rate {item['win_rate']:.1%}"
            )

    if summary["by_symbol"]:
        print("\nSymbols")
        for symbol, item in sorted(summary["by_symbol"].items(), key=lambda pair: pair[1]["total_pnl"], reverse=True):
            print(f"- {symbol}: {item['trades']} trades, P/L {item['total_pnl']:.2f}, win rate {item['win_rate']:.1%}")

    if summary["by_strategy"]:
        print("\nStrategies")
        for strategy, item in sorted(summary["by_strategy"].items(), key=lambda pair: pair[1]["total_pnl"], reverse=True):
            print(f"- {strategy}: {item['trades']} trades, P/L {item['total_pnl']:.2f}, win rate {item['win_rate']:.1%}")

    if summary.get("by_entry_time_et"):
        b = summary["by_entry_time_et"]["before_12_00_et"]
        a = summary["by_entry_time_et"]["from_12_00_et"]
        print("\nWin rate by entry time (America/New_York vs 12:00)")
        print(
            f"- Before 12:00: {b['trades']} trades, win rate {b['win_rate']:.1%}, "
            f"P/L {b['total_pnl']:.2f} (wins {b['wins']}, losses {b['losses']})"
        )
        print(
            f"- From 12:00:   {a['trades']} trades, win rate {a['win_rate']:.1%}, "
            f"P/L {a['total_pnl']:.2f} (wins {a['wins']}, losses {a['losses']})"
        )

    if summary.get("by_day_entry_time_et"):
        print("\nPer day — entry time vs 12:00 (America/New_York, entry date)")
        for day, buckets in summary["by_day_entry_time_et"].items():
            b = buckets["before_12_00_et"]
            a = buckets["from_12_00_et"]
            print(
                f"- {day}: before {b['trades']} @ {b['win_rate']:.1%} P/L {b['total_pnl']:.2f} | "
                f"from 12:00 {a['trades']} @ {a['win_rate']:.1%} P/L {a['total_pnl']:.2f}"
            )

    if summary["by_day"]:
        print("\nTrading Days")
        for day, item in sorted(summary["by_day"].items()):
            print(
                f"- {day}: {item['trades']} trades, P/L {item['total_pnl']:.2f}, "
                f"expectancy {item['expectancy_r']:.2f}R, max DD {item['max_drawdown']:.2f}, "
                f"win rate {item['win_rate']:.1%}"
            )

    if summary["best_trade"]:
        print(f"\nBest: {summary['best_trade']['symbol']} P/L {summary['best_trade']['pnl']:.2f} via {summary['best_trade']['reason']}")
    if summary["worst_trade"]:
        print(f"Worst: {summary['worst_trade']['symbol']} P/L {summary['worst_trade']['pnl']:.2f} via {summary['worst_trade']['reason']}")

    if summary["unmatched_events"]:
        print(f"\nUnmatched events: {len(summary['unmatched_events'])}")


def analyze(path: Path, strategy: str | None = None) -> dict:
    round_trips, unmatched = build_round_trips(load_events(path))
    if strategy:
        needle = strategy.strip().lower()
        round_trips = [t for t in round_trips if (t.strategy or "").lower() == needle]
    return summarize(round_trips, unmatched)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze confirmed buy/sell events from logs/trade_journal.jsonl.")
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL_FILE, help="Path to trade_journal.jsonl.")
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
    summary = analyze(args.journal, strategy=args.strategy or None)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print_text(summary)


if __name__ == "__main__":
    main()
