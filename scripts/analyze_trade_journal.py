import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_JOURNAL_FILE = Path("logs/trade_journal.jsonl")


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
    reason: str
    hold_seconds: float


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
    round_trips = []
    unmatched = []

    for event in events:
        if event.event == "buy":
            open_lots[event.symbol].append(event)
            continue

        if event.event != "sell":
            unmatched.append({"event": event.event, "symbol": event.symbol, "reason": "unsupported event"})
            continue

        remaining_shares = event.shares
        while remaining_shares > 0 and open_lots[event.symbol]:
            buy = open_lots[event.symbol][0]
            matched_shares = min(remaining_shares, buy.shares)
            allocated_pnl = event.pnl * (matched_shares / event.shares) if event.shares else event.pnl
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
                    reason=event.reason.split(" | ")[0],
                    hold_seconds=(event.timestamp_ms - buy.timestamp_ms) / 1000,
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
                )

        if remaining_shares > 0:
            unmatched.append({"event": "sell", "symbol": event.symbol, "shares": remaining_shares, "reason": "sell without matching buy"})

    for symbol, buys in open_lots.items():
        for buy in buys:
            unmatched.append({"event": "buy", "symbol": symbol, "shares": buy.shares, "reason": "open position"})

    return round_trips, unmatched


def summarize(round_trips: list[RoundTrip], unmatched: list[dict]) -> dict:
    wins = [trade for trade in round_trips if trade.pnl > 0]
    losses = [trade for trade in round_trips if trade.pnl < 0]
    flat = [trade for trade in round_trips if trade.pnl == 0]
    hold_times = [trade.hold_seconds for trade in round_trips]
    pnls = [trade.pnl for trade in round_trips]

    by_symbol = defaultdict(list)
    by_reason = defaultdict(list)
    by_strategy = defaultdict(list)
    for trade in round_trips:
        by_symbol[trade.symbol].append(trade)
        by_reason[trade.reason or "unknown"].append(trade)
        by_strategy[trade.strategy or "unknown"].append(trade)

    return {
        "trades": len(round_trips),
        "wins": len(wins),
        "losses": len(losses),
        "flat": len(flat),
        "win_rate": round(len(wins) / len(round_trips), 4) if round_trips else 0.0,
        "total_pnl": round(sum(pnls), 4),
        "average_pnl": round(mean(pnls), 4) if pnls else 0.0,
        "median_pnl": round(median(pnls), 4) if pnls else 0.0,
        "average_hold_seconds": round(mean(hold_times), 2) if hold_times else 0.0,
        "median_hold_seconds": round(median(hold_times), 2) if hold_times else 0.0,
        "best_trade": trade_summary(max(round_trips, key=lambda trade: trade.pnl)) if round_trips else None,
        "worst_trade": trade_summary(min(round_trips, key=lambda trade: trade.pnl)) if round_trips else None,
        "by_symbol": summarize_groups(by_symbol),
        "by_exit_reason": summarize_groups(by_reason),
        "by_strategy": summarize_groups(by_strategy),
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
            "average_hold_seconds": round(mean([trade.hold_seconds for trade in trades]), 2) if trades else 0.0,
        }
    return summary


def trade_summary(trade: RoundTrip) -> dict:
    return {
        "symbol": trade.symbol,
        "strategy": trade.strategy,
        "shares": trade.shares,
        "buy_time": format_timestamp(trade.buy_timestamp_ms),
        "sell_time": format_timestamp(trade.sell_timestamp_ms),
        "buy_price": trade.buy_price,
        "sell_price": trade.sell_price,
        "pnl": round(trade.pnl, 4),
        "reason": trade.reason,
        "hold_seconds": round(trade.hold_seconds, 2),
    }


def format_timestamp(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000).isoformat(timespec="seconds")


def print_text(summary: dict) -> None:
    print("Trade Journal Summary")
    print(f"Trades: {summary['trades']} | Wins: {summary['wins']} | Losses: {summary['losses']} | Win rate: {summary['win_rate']:.1%}")
    print(f"Total P/L: {summary['total_pnl']:.2f} | Avg P/L: {summary['average_pnl']:.2f} | Median P/L: {summary['median_pnl']:.2f}")
    print(f"Avg hold: {summary['average_hold_seconds']:.1f}s | Median hold: {summary['median_hold_seconds']:.1f}s")

    if summary["by_exit_reason"]:
        print("\nExit Reasons")
        for reason, item in sorted(summary["by_exit_reason"].items(), key=lambda pair: pair[1]["trades"], reverse=True):
            print(f"- {reason}: {item['trades']} trades, P/L {item['total_pnl']:.2f}, win rate {item['win_rate']:.1%}")

    if summary["by_symbol"]:
        print("\nSymbols")
        for symbol, item in sorted(summary["by_symbol"].items(), key=lambda pair: pair[1]["total_pnl"], reverse=True):
            print(f"- {symbol}: {item['trades']} trades, P/L {item['total_pnl']:.2f}, win rate {item['win_rate']:.1%}")

    if summary["best_trade"]:
        print(f"\nBest: {summary['best_trade']['symbol']} P/L {summary['best_trade']['pnl']:.2f} via {summary['best_trade']['reason']}")
    if summary["worst_trade"]:
        print(f"Worst: {summary['worst_trade']['symbol']} P/L {summary['worst_trade']['pnl']:.2f} via {summary['worst_trade']['reason']}")

    if summary["unmatched_events"]:
        print(f"\nUnmatched events: {len(summary['unmatched_events'])}")


def analyze(path: Path) -> dict:
    round_trips, unmatched = build_round_trips(load_events(path))
    return summarize(round_trips, unmatched)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze confirmed buy/sell events from logs/trade_journal.jsonl.")
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL_FILE, help="Path to trade_journal.jsonl.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON summary.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = analyze(args.journal)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print_text(summary)


if __name__ == "__main__":
    main()
