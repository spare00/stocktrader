import logging
import time

from candle import SymbolState
from market_hours import should_flatten_before_close


def manage_all_exits(executor, states: dict[str, SymbolState], strategies_by_name: dict, event_ms: int | None, risk=None) -> None:
    for exit_state in states.values():
        fill = executor.manage_exit(exit_state, strategies_by_name, event_ms)
        if fill and risk is not None:
            risk.record_exit(fill.pnl, fill.timestamp_ms)


def flatten_on_shutdown(settings, executor, states: dict[str, SymbolState], strategies_by_name: dict, now_ms: int | None = None) -> None:
    timestamp_ms = now_ms or int(time.time() * 1000)
    if not should_flatten_before_close(timestamp_ms, settings.flatten_before_close_minutes):
        return

    logging.info("Shutdown during close flatten window; attempting final flatten")
    manage_all_exits(executor, states, strategies_by_name, timestamp_ms)
