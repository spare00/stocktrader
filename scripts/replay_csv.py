import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from candle import SymbolState
from config import Settings
from execution import LocalPaperExecutor, PositionTracker
from models import Bar, Quote
from risk import RiskManager
from strategies import build_strategies


def parse_float(row: dict, name: str, default: float | None = None) -> float:
    value = row.get(name)
    if value in {None, ""}:
        if default is None:
            raise ValueError(f"Missing required field: {name}")
        return default
    return float(value)


def parse_int(row: dict, name: str, default: int | None = None) -> int:
    value = row.get(name)
    if value in {None, ""}:
        if default is None:
            raise ValueError(f"Missing required field: {name}")
        return default
    return int(float(value))


def optional_int(row: dict, name: str) -> int | None:
    value = row.get(name)
    return None if value in {None, ""} else int(float(value))


def parse_event(row: dict) -> Bar | Quote:
    kind = (row.get("type") or row.get("kind") or "").strip().lower()
    symbol = (row.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("Missing required field: symbol")

    if kind == "quote":
        timestamp_ms = parse_int(row, "timestamp_ms")
        return Quote(
            symbol=symbol,
            bid=parse_float(row, "bid"),
            ask=parse_float(row, "ask"),
            bid_size=parse_int(row, "bid_size", 0),
            ask_size=parse_int(row, "ask_size", 0),
            timestamp_ms=timestamp_ms,
        )

    if kind == "bar":
        end_ms = optional_int(row, "end_ms")
        if end_ms is None:
            end_ms = parse_int(row, "timestamp_ms")
        close = parse_float(row, "close")
        return Bar(
            symbol=symbol,
            open=parse_float(row, "open", close),
            high=parse_float(row, "high", close),
            low=parse_float(row, "low", close),
            close=close,
            volume=parse_float(row, "volume", 0.0),
            vwap=parse_float(row, "vwap", close),
            start_ms=parse_int(row, "start_ms", end_ms - 1000),
            end_ms=end_ms,
        )

    raise ValueError(f"Unsupported event type: {kind}")


def event_timestamp(event: Bar | Quote) -> int:
    return event.timestamp_ms if isinstance(event, Quote) else event.end_ms


def load_events(path: Path) -> list[Bar | Quote]:
    with path.open(newline="") as handle:
        return sorted((parse_event(row) for row in csv.DictReader(handle)), key=event_timestamp)


def replay(events: list[Bar | Quote], settings: Settings) -> dict:
    states = {symbol: SymbolState(symbol) for symbol in settings.symbols}
    strategies = build_strategies(settings)
    strategies_by_name = {strategy.name: strategy for strategy in strategies}
    executor = LocalPaperExecutor(PositionTracker(settings))
    risk = RiskManager(settings)
    signals = 0
    rejections = 0

    for event in events:
        state = states.get(event.symbol)
        if state is None:
            continue

        if isinstance(event, Quote):
            state.update_quote(event)
        else:
            state.add_bar(event)

        event_ms = state.last_event_ms
        for exit_state in states.values():
            executor.manage_exit(exit_state, strategies_by_name, event_ms)

        for strategy in strategies:
            signal = strategy.evaluate(state)
            if not signal:
                continue

            signals += 1
            decision = risk.check_entry(signal, executor.open_symbols(), mark_to_market_pnl(executor, states))
            if not decision.allowed:
                rejections += 1
                continue

            fill = executor.buy(signal)
            if fill:
                risk.record_trade(signal.symbol, signal.timestamp_ms, signal.strategy)
                break

    return {
        "cash": round(executor.tracker.cash, 2),
        "fills": [fill.__dict__ for fill in executor.tracker.fills],
        "open_positions": {symbol: position.__dict__ for symbol, position in executor.tracker.positions.items()},
        "realized_pnl": round(executor.realized_pnl, 2),
        "rejections": rejections,
        "signals": signals,
    }


def mark_to_market_pnl(executor: LocalPaperExecutor, states: dict[str, SymbolState]) -> float:
    mark_prices = {symbol: state.last_price for symbol, state in states.items() if state.last_price is not None}
    return executor.total_pnl(mark_prices)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay bar/quote CSV events through the local paper strategy engine.")
    parser.add_argument("events", type=Path, help="CSV with type,symbol,timestamp_ms plus quote or bar fields.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols to include. Defaults to Settings/SYMBOLS.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    symbols = [part.strip().upper() for part in args.symbols.split(",") if part.strip()]
    settings_kwargs = {
        "alpaca_api_key": "replay",
        "alpaca_secret_key": "replay",
        "execution_mode": "local",
    }
    if symbols:
        settings_kwargs["symbols"] = symbols

    summary = replay(load_events(args.events), Settings(**settings_kwargs))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
