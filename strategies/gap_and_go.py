from __future__ import annotations

from datetime import datetime, time, timedelta
import logging

from candle import SymbolState
from config import Settings
from market_hours import MARKET_TZ
from models import ExitDecision, Signal
from scripts.select_gap_and_go import (
    inspect_gap_and_go_candidate,
    latest_valid_quote,
    premarket_high_price,
    premarket_volume_ratio,
    previous_regular_bars,
    regular_bars,
    regular_open_price,
    session_date,
)
from strategies.base import Strategy


LOG = logging.getLogger(__name__)
MARKET_OPEN = time(9, 30)
PREMARKET_OPEN = time(4, 0)


class GapAndGoStrategy(Strategy):
    name = "gap_and_go"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_tz = MARKET_TZ
        self._previous_close: dict[str, float] = {}
        self._last_reject_log_ms: dict[tuple[str, str], int] = {}

    def bootstrap_states(self, states: dict[str, SymbolState]) -> None:
        try:
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame

            from alpaca_client import make_clients, to_bar
        except Exception:
            LOG.exception("Gap-and-go bootstrap imports failed")
            return

        symbols = list(states)
        if not symbols:
            return

        now = datetime.now(tz=self.market_tz)
        start_of_day = datetime.combine(now.date(), PREMARKET_OPEN, tzinfo=self.market_tz)
        previous_start = datetime.combine((now - timedelta(days=5)).date(), time.min, tzinfo=self.market_tz)

        try:
            clients = make_clients(self.settings)
            intraday_request = StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=TimeFrame.Minute,
                start=start_of_day,
                end=now,
                feed=clients.feed,
            )
            daily_request = StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=TimeFrame.Day,
                start=previous_start,
                end=now + timedelta(days=1),
                feed=clients.feed,
            )
            intraday_response = clients.historical.get_stock_bars(intraday_request)
            daily_response = clients.historical.get_stock_bars(daily_request)
        except Exception:
            LOG.exception("Gap-and-go bootstrap failed to load historical context")
            return

        seeded_bars = 0
        for symbol, state in states.items():
            if not state.bars:
                for raw_bar in intraday_response.data.get(symbol, []):
                    state.add_bar(to_bar(raw_bar))
                    seeded_bars += 1

            daily_items = daily_response.data.get(symbol, [])
            previous_close = None
            for raw_bar in daily_items:
                bar_date = raw_bar.timestamp.astimezone(self.market_tz).date()
                if bar_date < now.date():
                    previous_close = float(raw_bar.close)
            if previous_close and previous_close > 0:
                self._previous_close[symbol] = previous_close

        if seeded_bars or self._previous_close:
            LOG.info(
                "Gap-and-go bootstrapped %s bars and %s previous closes",
                seeded_bars,
                len(self._previous_close),
            )

    def evaluate(self, state: SymbolState) -> Signal | None:
        if state.last_event_kind not in {"quote", "bar"}:
            return None
        if not self._within_entry_window(state.last_event_ms):
            return None

        prev_close = self._previous_close.get(state.symbol) or self._infer_previous_close(state)
        decision = inspect_gap_and_go_candidate(state, self.settings, previous_close=prev_close)
        if decision.candidate is None:
            return self._reject(state, decision.code, decision.detail)

        last = latest_valid_quote(state)
        assert last is not None
        gap_pct = decision.candidate.gap_pct
        premarket_high = decision.candidate.premarket_high
        premarket_volume = decision.candidate.premarket_volume_ratio
        breakout_level = premarket_high * (1 + self.settings.gap_and_go_breakout_buffer_pct)
        if last.ask <= breakout_level:
            return self._reject(
                state,
                "breakout",
                f"price {last.ask:.2f} <= premarket high breakout {breakout_level:.2f}",
            )

        reason = (
            f"gap_and_go gap {gap_pct:.2%}, premarket volume {premarket_volume:.1f}x, "
            f"breakout above premarket high {premarket_high:.2f}"
        )
        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=last.ask,
            timestamp_ms=last.timestamp_ms,
            change_pct=gap_pct,
            volume_ratio=premarket_volume,
            spread_bps=last.spread_bps,
            reason=reason,
        )

    def should_exit(self, state: SymbolState, position) -> ExitDecision | None:
        if position.strategy != self.name:
            return None

        price = state.last_price
        if price is None:
            return None

        event_ms = state.last_event_ms or (state.quote.timestamp_ms if state.quote else position.entry_ms)
        age_seconds = (event_ms - position.entry_ms) / 1000
        if age_seconds < self.exit_activation_delay_seconds(position):
            return None

        open_price = self._regular_open_price(state)
        session_vwap = self._session_vwap(state)
        if open_price and price < open_price:
            return ExitDecision("lost open")
        if session_vwap and price < session_vwap:
            return ExitDecision("lost vwap")

        recent_bars = self._regular_bars(state)[-max(3, self.settings.gap_and_go_bar_window) :]
        if len(recent_bars) >= 2:
            recent_high = max(bar.high for bar in recent_bars)
            pnl_pct = (price - position.entry_price) / position.entry_price if position.entry_price > 0 else 0.0
            if pnl_pct > 0 and price < recent_high * (1 - self.settings.gap_and_go_trailing_retrace_pct):
                return ExitDecision("trailing stop")

        return None

    def exit_activation_delay_seconds(self, position) -> int:
        return self.settings.gap_and_go_exit_activation_delay_seconds

    def _within_entry_window(self, timestamp_ms: int | None) -> bool:
        if timestamp_ms is None:
            return False
        current = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.market_tz)
        minutes = current.hour * 60 + current.minute
        market_open = (MARKET_OPEN.hour * 60) + MARKET_OPEN.minute
        elapsed = minutes - market_open
        return self.settings.gap_and_go_start_minute <= elapsed <= self.settings.gap_and_go_end_minute

    def _session_vwap(self, state: SymbolState) -> float | None:
        session_bars = regular_bars(state)
        total_volume = sum(bar.volume for bar in session_bars if bar.volume > 0)
        if total_volume <= 0:
            return None
        total_value = sum(bar.vwap * bar.volume for bar in session_bars if bar.volume > 0)
        return total_value / total_volume if total_value > 0 else None

    def _regular_open_price(self, state: SymbolState) -> float | None:
        return regular_open_price(state)

    def _premarket_high(self, state: SymbolState) -> float | None:
        return premarket_high_price(state)

    def _premarket_volume_ratio(self, state: SymbolState) -> float:
        return premarket_volume_ratio(state)

    def _regular_bars(self, state: SymbolState):
        return regular_bars(state)

    def _previous_regular_bars(self, state: SymbolState):
        return previous_regular_bars(state)

    def _session_date(self, state: SymbolState):
        return session_date(state)

    def _infer_previous_close(self, state: SymbolState) -> float | None:
        session_date = self._session_date(state)
        if session_date is None:
            return None
        previous_close = None
        for bar in state.bars:
            current = datetime.fromtimestamp(bar.start_ms / 1000, tz=self.market_tz)
            if current.date() < session_date and current.time() >= MARKET_OPEN:
                previous_close = bar.close
        return previous_close

    def _reject(self, state: SymbolState, code: str, detail: str) -> None:
        timestamp_ms = state.last_event_ms or 0
        key = (state.symbol, code)
        last_log_ms = self._last_reject_log_ms.get(key, -10_000)
        if timestamp_ms - last_log_ms >= 10_000:
            self._last_reject_log_ms[key] = timestamp_ms
            LOG.debug("No gap_and_go entry %s: %s", state.symbol, detail)
        return None
