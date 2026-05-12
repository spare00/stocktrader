"""TradingView-style continuous indicator bar chains (no session-date reset)."""

from __future__ import annotations

from datetime import datetime, time

from candle import SymbolState
from config import Settings
from market_hours import MARKET_TZ
from models import Bar

PREMARKET_OPEN = time(4, 0)
MARKET_CLOSE = time(16, 0)


def continuous_indicator_bars(state: SymbolState, settings: Settings) -> list[Bar]:
    """Return chronological bars for EMA/MACD/STOCH without isolating the current session date.

    When ``indicator_include_afterhours`` is False, keep 04:00–16:00 ET bars (premarket + regular).
    When True, include all timestamps (extended / overnight if present in ``state.bars``).
    """
    out: list[Bar] = []
    for bar in state.bars:
        current = datetime.fromtimestamp(bar.start_ms / 1000, tz=MARKET_TZ)
        ts_time = current.time()
        if settings.indicator_include_afterhours:
            out.append(bar)
        elif PREMARKET_OPEN <= ts_time < MARKET_CLOSE:
            out.append(bar)
    return out
