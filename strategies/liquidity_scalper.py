from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time
from statistics import median
from typing import Any, ClassVar, Literal

from candle import SymbolState
from config import Settings
from env_vars import EnvSpec, float_env, int_env
from market_hours import MARKET_TZ
from models import Bar, ExitDecision, Quote, Signal, Trade
from opening_memory import OpeningSessionMemory, opening_session_memory
from strategies.base import Strategy


logger = logging.getLogger("strategies.liquidity_scalper")

MARKET_OPEN = time(9, 30)


@dataclass(frozen=True)
class TapeMetrics:
    buy_volume: int
    sell_volume: int
    buy_sell_ratio: float
    dollar_volume: float
    move_pct: float
    trade_count: int
    ask_prints: int
    bid_prints: int
    accel_ratio: float
    dollar_accel_ratio: float
    last_trade_dollar_volume_ratio: float


class LiquidityScalperStrategy(Strategy):
    """Stream-native liquidity scalper: tape impulse entries and tick-level exits."""

    name = "liquidity_scalper"
    requires_plan = False
    requires_trade_ticks = True
    selector_command: ClassVar[str] = ".venv/bin/python strategy_selectors/select_liquidity_scalper.py --top 12"
    env_specs: ClassVar[tuple[EnvSpec, ...]] = (
        ("liquidity_scalper_start_minute", "LIQUIDITY_SCALPER_START_MINUTE", int_env, 0),
        ("liquidity_scalper_end_minute", "LIQUIDITY_SCALPER_END_MINUTE", int_env, 360),
        ("liquidity_scalper_afternoon_start_minute", "LIQUIDITY_SCALPER_AFTERNOON_START_MINUTE", int_env, 0),
        ("liquidity_scalper_afternoon_end_minute", "LIQUIDITY_SCALPER_AFTERNOON_END_MINUTE", int_env, 360),
        ("liquidity_scalper_min_bar_dollar_volume", "LIQUIDITY_SCALPER_MIN_BAR_DOLLAR_VOLUME", float_env, 3_000_000.0),
        ("liquidity_scalper_min_session_dollar_volume", "LIQUIDITY_SCALPER_MIN_SESSION_DOLLAR_VOLUME", float_env, 30_000_000.0),
        ("liquidity_scalper_min_volume_ratio", "LIQUIDITY_SCALPER_MIN_VOLUME_RATIO", float_env, 2.0),
        ("liquidity_scalper_min_range_pct", "LIQUIDITY_SCALPER_MIN_RANGE_PCT", float_env, 0.015),
        ("liquidity_scalper_tape_window_seconds", "LIQUIDITY_SCALPER_TAPE_WINDOW_SECONDS", int_env, 3),
        ("liquidity_scalper_exit_tape_window_seconds", "LIQUIDITY_SCALPER_EXIT_TAPE_WINDOW_SECONDS", int_env, 2),
        ("liquidity_scalper_min_tape_trades", "LIQUIDITY_SCALPER_MIN_TAPE_TRADES", int_env, 3),
        ("liquidity_scalper_min_tape_dollar_volume", "LIQUIDITY_SCALPER_MIN_TAPE_DOLLAR_VOLUME", float_env, 100_000.0),
        ("liquidity_scalper_min_trade_dollar_volume", "LIQUIDITY_SCALPER_MIN_TRADE_DOLLAR_VOLUME", float_env, 10_000.0),
        ("liquidity_scalper_min_buy_sell_ratio", "LIQUIDITY_SCALPER_MIN_BUY_SELL_RATIO", float_env, 1.35),
        ("liquidity_scalper_min_tape_price_move_pct", "LIQUIDITY_SCALPER_MIN_TAPE_PRICE_MOVE_PCT", float_env, 0.0004),
        ("liquidity_scalper_quote_max_lag_ms", "LIQUIDITY_SCALPER_QUOTE_MAX_LAG_MS", int_env, 500),
        ("liquidity_scalper_min_ask_prints", "LIQUIDITY_SCALPER_MIN_ASK_PRINTS", int_env, 2),
        ("liquidity_scalper_tape_accel_min_ratio", "LIQUIDITY_SCALPER_TAPE_ACCEL_MIN_RATIO", float_env, 1.15),
        ("liquidity_scalper_exit_tape_reversal_ratio", "LIQUIDITY_SCALPER_EXIT_TAPE_REVERSAL_RATIO", float_env, 1.35),
        ("liquidity_scalper_flush_lookback_bars", "LIQUIDITY_SCALPER_FLUSH_LOOKBACK_BARS", int_env, 3),
        ("liquidity_scalper_flush_drop_pct", "LIQUIDITY_SCALPER_FLUSH_DROP_PCT", float_env, 0.025),
        ("liquidity_scalper_reclaim_pct", "LIQUIDITY_SCALPER_RECLAIM_PCT", float_env, 0.003),
        ("liquidity_scalper_breakout_lookback_bars", "LIQUIDITY_SCALPER_BREAKOUT_LOOKBACK_BARS", int_env, 5),
        ("liquidity_scalper_breakout_buffer_pct", "LIQUIDITY_SCALPER_BREAKOUT_BUFFER_PCT", float_env, 0.0005),
        ("liquidity_scalper_max_spread_bps", "LIQUIDITY_SCALPER_MAX_SPREAD_BPS", float_env, 12.0),
        ("liquidity_scalper_min_net_edge_bps", "LIQUIDITY_SCALPER_MIN_NET_EDGE_BPS", float_env, 3.0),
        ("liquidity_scalper_min_hold_seconds", "LIQUIDITY_SCALPER_MIN_HOLD_SECONDS", int_env, 1),
        ("liquidity_scalper_micro_profit_pct", "LIQUIDITY_SCALPER_MICRO_PROFIT_PCT", float_env, 0.0015),
        ("liquidity_scalper_quick_profit_pct", "LIQUIDITY_SCALPER_QUICK_PROFIT_PCT", float_env, 0.003),
        ("liquidity_scalper_trailing_pullback_pct", "LIQUIDITY_SCALPER_TRAILING_PULLBACK_PCT", float_env, 0.002),
        ("liquidity_scalper_stall_seconds", "LIQUIDITY_SCALPER_STALL_SECONDS", int_env, 12),
        ("liquidity_scalper_stall_loss_pct", "LIQUIDITY_SCALPER_STALL_LOSS_PCT", float_env, 0.0005),
        ("liquidity_scalper_stop_loss_pct", "LIQUIDITY_SCALPER_STOP_LOSS_PCT", float_env, 0.003),
        (
            "liquidity_scalper_max_trades_per_symbol_per_session",
            "LIQUIDITY_SCALPER_MAX_TRADES_PER_SYMBOL_PER_SESSION",
            int_env,
            6,
        ),
        ("liquidity_scalper_symbol_loss_lock_count", "LIQUIDITY_SCALPER_SYMBOL_LOSS_LOCK_COUNT", int_env, 2),
    )
    diagnostic_loggers: ClassVar[tuple[str, ...]] = ("strategies.liquidity_scalper",)

    @classmethod
    def runtime_settings_section(cls, settings: Any) -> dict[str, Any] | None:
        if cls.name not in settings.strategy_names:
            return None
        return {
            "start_minute": settings.liquidity_scalper_start_minute,
            "end_minute": settings.liquidity_scalper_end_minute,
            "afternoon_start_minute": settings.liquidity_scalper_afternoon_start_minute,
            "afternoon_end_minute": settings.liquidity_scalper_afternoon_end_minute,
            "tape_window_seconds": settings.liquidity_scalper_tape_window_seconds,
            "exit_tape_window_seconds": settings.liquidity_scalper_exit_tape_window_seconds,
            "quote_max_lag_ms": settings.liquidity_scalper_quote_max_lag_ms,
            "min_buy_sell_ratio": settings.liquidity_scalper_min_buy_sell_ratio,
            "min_ask_prints": settings.liquidity_scalper_min_ask_prints,
            "tape_accel_min_ratio": settings.liquidity_scalper_tape_accel_min_ratio,
            "micro_profit_pct": settings.liquidity_scalper_micro_profit_pct,
            "quick_profit_pct": settings.liquidity_scalper_quick_profit_pct,
            "exit_tape_reversal_ratio": settings.liquidity_scalper_exit_tape_reversal_ratio,
            "max_spread_bps": settings.liquidity_scalper_max_spread_bps,
            "min_net_edge_bps": settings.liquidity_scalper_min_net_edge_bps,
            "stall_seconds": settings.liquidity_scalper_stall_seconds,
            "stop_loss_pct": settings.liquidity_scalper_stop_loss_pct,
            "max_trades_per_symbol_per_session": settings.liquidity_scalper_max_trades_per_symbol_per_session,
            "symbol_loss_lock_count": settings.liquidity_scalper_symbol_loss_lock_count,
            "opening_memory_enabled": bool(settings.opening_memory_enabled),
            "opening_memory_lookback_days": settings.opening_memory_lookback_days,
            "opening_memory_min_repeat_days": settings.opening_memory_min_repeat_days,
        }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_tz = MARKET_TZ

    def evaluate(self, state: SymbolState) -> Signal | None:
        if state.last_event_kind not in {"trade", "bar"}:
            return None
        if not self.is_symbol_allowed(state.symbol):
            return None
        if not self._within_entry_window(state.last_event_ms):
            return None
        memory = self._opening_memory(state)
        if self._bearish_memory_blocks_long(memory):
            return None

        if state.last_event_kind == "trade":
            return self._trade_tape_signal(state, memory)

        return self._bar_structure_signal(state, memory)

    def should_exit(self, state: SymbolState, position) -> ExitDecision | None:
        if state.last_event_kind not in {"quote", "bar", "trade"} or position.strategy != self.name:
            return None

        mark = self._mark_price(state, side="long")
        if mark is None or position.entry_price <= 0:
            return None

        event_ms = state.last_event_ms or position.entry_ms
        age_seconds = (event_ms - position.entry_ms) / 1000
        pnl_pct = (mark - position.entry_price) / position.entry_price

        if pnl_pct <= -self.settings.liquidity_scalper_stop_loss_pct:
            return ExitDecision("scalper stop loss")

        if age_seconds < self.settings.liquidity_scalper_min_hold_seconds:
            return None

        if age_seconds >= self.settings.liquidity_scalper_min_hold_seconds:
            reversal = self._exit_tape_reversal(state)
            if reversal is not None:
                return reversal

        if pnl_pct <= -self.settings.liquidity_scalper_stall_loss_pct:
            return ExitDecision("not immediately working")

        if pnl_pct >= self.settings.liquidity_scalper_micro_profit_pct:
            if self._tape_still_supportive(state):
                if pnl_pct >= self.settings.liquidity_scalper_quick_profit_pct:
                    return ExitDecision("quick scalp profit")
            else:
                return ExitDecision("micro scalp profit")

        if position.max_price > position.entry_price:
            pullback_pct = (position.max_price - mark) / position.max_price
            if pullback_pct >= self.settings.liquidity_scalper_trailing_pullback_pct:
                return ExitDecision("scalp pullback")

        if age_seconds >= self.settings.liquidity_scalper_stall_seconds and pnl_pct <= 0:
            return ExitDecision("scalp stall")

        return None

    def exit_activation_delay_seconds(self, position) -> int:
        return 0

    def use_fixed_target_exit(self, position) -> bool:
        return False

    def _trade_tape_signal(self, state: SymbolState, memory: OpeningSessionMemory | None = None) -> Signal | None:
        quote = state.quote
        trade = state.trade
        if trade is None or trade.price <= 0 or trade.size <= 0:
            return None
        if quote is None or quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
            return None

        spread_bps = quote.spread_bps
        if spread_bps > self.settings.liquidity_scalper_max_spread_bps:
            return None
        if not self._has_minimum_net_edge(spread_bps):
            return None

        metrics = self._tape_metrics(state, self.settings.liquidity_scalper_tape_window_seconds)
        if metrics is None:
            return None

        trade_dollar_volume = trade.price * trade.size
        if not self._trade_flow_is_expanding(trade_dollar_volume, metrics):
            return None

        min_buy_sell_ratio = self.settings.liquidity_scalper_min_buy_sell_ratio
        min_tape_move_pct = self.settings.liquidity_scalper_min_tape_price_move_pct
        min_accel_ratio = self.settings.liquidity_scalper_tape_accel_min_ratio
        if self._has_long_repeat_memory(memory):
            relief = self._bounded_relief(self.settings.opening_memory_scalper_threshold_relief)
            min_buy_sell_ratio *= relief
            min_tape_move_pct *= relief
            min_accel_ratio *= relief

        if metrics.buy_sell_ratio < min_buy_sell_ratio:
            return None
        if not self._tape_flow_is_expanding(metrics):
            return None
        if metrics.move_pct < min_tape_move_pct:
            return None
        if metrics.ask_prints < self.settings.liquidity_scalper_min_ask_prints:
            return None
        if metrics.accel_ratio < min_accel_ratio:
            return None

        trigger_quote = self._quote_at_ms(state, trade.timestamp_ms)
        if trigger_quote is None or not self._is_aggressive_buy(trade, trigger_quote):
            return None

        session_bars = self._session_bars(state)
        session_dollar_volume = self._session_dollar_volume(session_bars, state)
        session_flow_ok, session_flow_ratio = self._session_flow_is_expanding(
            session_dollar_volume,
            session_bars,
            tape=metrics,
        )
        if not session_flow_ok:
            return None

        session_range_pct = self._hybrid_range_pct(session_bars, state)
        if session_range_pct < self.settings.liquidity_scalper_min_range_pct:
            return None

        entry_price = quote.ask
        stop_price = entry_price * (1.0 - self.settings.liquidity_scalper_stop_loss_pct)
        reason = (
            f"trade_tape buy_sell={metrics.buy_sell_ratio:.2f} accel={metrics.accel_ratio:.2f} "
            f"dollar_accel={metrics.dollar_accel_ratio:.2f} last_trade_flow={metrics.last_trade_dollar_volume_ratio:.2f}x "
            f"ask_prints={metrics.ask_prints} "
            f"tape_dv ${metrics.dollar_volume:,.0f}/{self.settings.liquidity_scalper_tape_window_seconds}s "
            f"last_trade_dv ${trade_dollar_volume:,.0f} move {metrics.move_pct:.3%}; "
            f"session_dv ${session_dollar_volume:,.0f} flow={session_flow_ratio:.2f}x, "
            f"range {session_range_pct:.2%}"
        )
        if memory is not None and self._has_long_repeat_memory(memory):
            reason = f"{reason}; {memory.summary()}"
        logger.debug("%s entry signal %s", state.symbol, reason)
        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=entry_price,
            timestamp_ms=trade.timestamp_ms,
            change_pct=metrics.move_pct,
            volume_ratio=metrics.buy_sell_ratio,
            spread_bps=spread_bps,
            reason=reason,
            stop_price=stop_price,
        )

    def _bar_structure_signal(self, state: SymbolState, memory: OpeningSessionMemory | None = None) -> Signal | None:
        bars = self._session_bars(state)
        if len(bars) < max(4, self.settings.liquidity_scalper_breakout_lookback_bars + 1):
            return None

        last = bars[-1]
        quote = state.quote
        spread_bps = quote.spread_bps if quote is not None else None
        if spread_bps is not None and spread_bps > self.settings.liquidity_scalper_max_spread_bps:
            return None
        if spread_bps is not None and not self._has_minimum_net_edge(spread_bps):
            return None

        bar_dollar_volume = last.close * last.volume
        bar_flow_ok, bar_flow_ratio = self._bar_flow_is_expanding(bar_dollar_volume, bars)
        if not bar_flow_ok:
            return None
        session_dollar_volume = sum(bar.close * bar.volume for bar in bars)
        session_flow_ok, session_flow_ratio = self._session_flow_is_expanding(session_dollar_volume, bars)
        if not session_flow_ok:
            return None

        session_range_pct = self._range_pct(bars)
        min_range_pct = self.settings.liquidity_scalper_min_range_pct
        min_volume_ratio = self.settings.liquidity_scalper_min_volume_ratio
        if self._has_long_repeat_memory(memory):
            relief = self._bounded_relief(self.settings.opening_memory_scalper_threshold_relief)
            min_range_pct *= relief
            min_volume_ratio *= relief

        if session_range_pct < min_range_pct:
            return None

        volume_ratio = self._volume_ratio(bars)
        if volume_ratio < min_volume_ratio:
            return None

        setup = self._flush_reclaim_setup(bars) or self._liquidity_breakout_setup(bars)
        if setup is None:
            return None

        tape = self._tape_metrics(state, self.settings.liquidity_scalper_tape_window_seconds)
        if tape is None or tape.buy_sell_ratio < 1.0:
            return None

        entry_price = quote.ask if quote is not None and quote.ask > 0 else last.close
        stop_price = entry_price * (1.0 - self.settings.liquidity_scalper_stop_loss_pct)
        reason = (
            f"{setup}; bar_dv ${bar_dollar_volume:,.0f}, session_dv ${session_dollar_volume:,.0f}, "
            f"range {session_range_pct:.2%}, volume {volume_ratio:.1f}x, "
            f"bar_flow={bar_flow_ratio:.2f}x, session_flow={session_flow_ratio:.2f}x, "
            f"tape={tape.buy_sell_ratio:.2f}"
        )
        if memory is not None and self._has_long_repeat_memory(memory):
            reason = f"{reason}; {memory.summary()}"
        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=entry_price,
            timestamp_ms=last.end_ms,
            change_pct=(last.close - bars[-2].close) / bars[-2].close if bars[-2].close > 0 else 0.0,
            volume_ratio=volume_ratio,
            spread_bps=spread_bps,
            reason=reason,
            stop_price=stop_price,
        )

    def _exit_tape_reversal(self, state: SymbolState) -> ExitDecision | None:
        metrics = self._tape_metrics(
            state,
            self.settings.liquidity_scalper_exit_tape_window_seconds,
            require_buy=False,
        )
        if metrics is None:
            return None
        if metrics.sell_volume <= 0:
            return None
        if metrics.buy_volume <= 0:
            return ExitDecision(
                f"tape reversal sell-only "
                f"({self.settings.liquidity_scalper_exit_tape_window_seconds}s)"
            )
        sell_buy = metrics.sell_volume / metrics.buy_volume
        if sell_buy >= self.settings.liquidity_scalper_exit_tape_reversal_ratio:
            return ExitDecision(
                f"tape reversal sell/buy={sell_buy:.2f} "
                f"({self.settings.liquidity_scalper_exit_tape_window_seconds}s)"
            )
        return None

    def _tape_still_supportive(self, state: SymbolState) -> bool:
        metrics = self._tape_metrics(
            state,
            self.settings.liquidity_scalper_exit_tape_window_seconds,
            require_buy=False,
        )
        if metrics is None:
            return False
        return metrics.buy_sell_ratio >= 1.0 and metrics.move_pct >= 0

    def _tape_metrics(
        self,
        state: SymbolState,
        window_seconds: int,
        *,
        require_buy: bool = True,
    ) -> TapeMetrics | None:
        recent = self._recent_trades(state, window_seconds)
        if len(recent) < self.settings.liquidity_scalper_min_tape_trades:
            return None

        buy_volume = 0
        sell_volume = 0
        dollar_volume = 0.0
        ask_prints = 0
        bid_prints = 0
        classified: list[tuple[Trade, Literal["buy", "sell"]]] = []

        for item in recent:
            quote = self._quote_at_ms(state, item.timestamp_ms)
            if quote is None:
                continue
            side = self._classify_trade(item, quote)
            if side == "buy":
                buy_volume += item.size
                if self._is_aggressive_buy(item, quote):
                    ask_prints += 1
            else:
                sell_volume += item.size
                if self._is_aggressive_sell(item, quote):
                    bid_prints += 1
            classified.append((item, side))
            dollar_volume += item.price * item.size

        if len(classified) < self.settings.liquidity_scalper_min_tape_trades:
            return None
        if buy_volume <= 0 and require_buy:
            return None

        first_price = classified[0][0].price
        last_price = classified[-1][0].price
        move_pct = (last_price - first_price) / first_price if first_price > 0 else 0.0
        buy_sell_ratio = buy_volume / max(1, sell_volume)

        midpoint = max(1, len(classified) // 2)
        first_half = classified[:midpoint]
        second_half = classified[midpoint:]
        first_buy = sum(item.size for item, side in first_half if side == "buy")
        first_sell = sum(item.size for item, side in first_half if side == "sell")
        second_buy = sum(item.size for item, side in second_half if side == "buy")
        second_sell = sum(item.size for item, side in second_half if side == "sell")
        first_dollar_volume = sum(item.price * item.size for item, _side in first_half)
        second_dollar_volume = sum(item.price * item.size for item, _side in second_half)
        trade_dollar_volumes = [item.price * item.size for item, _side in classified]
        first_ratio = first_buy / max(1, first_sell)
        second_ratio = second_buy / max(1, second_sell)
        accel_ratio = second_ratio / max(0.01, first_ratio)
        dollar_accel_ratio = second_dollar_volume / max(1.0, first_dollar_volume)
        prior_trade_dollar_volume = median(trade_dollar_volumes[:-1] or [0.0])
        last_trade_dollar_volume_ratio = (
            trade_dollar_volumes[-1] / prior_trade_dollar_volume if prior_trade_dollar_volume > 0 else 0.0
        )

        return TapeMetrics(
            buy_volume=buy_volume,
            sell_volume=sell_volume,
            buy_sell_ratio=buy_sell_ratio,
            dollar_volume=dollar_volume,
            move_pct=move_pct,
            trade_count=len(classified),
            ask_prints=ask_prints,
            bid_prints=bid_prints,
            accel_ratio=accel_ratio,
            dollar_accel_ratio=dollar_accel_ratio,
            last_trade_dollar_volume_ratio=last_trade_dollar_volume_ratio,
        )

    @staticmethod
    def _mark_price(state: SymbolState, *, side: str) -> float | None:
        if state.quote is not None and state.quote.bid > 0 and state.quote.ask > 0:
            return state.quote.bid if side == "long" else state.quote.ask
        if state.trade is not None and state.trade.price > 0:
            return state.trade.price
        return state.last_price

    def _quote_at_ms(self, state: SymbolState, timestamp_ms: int) -> Quote | None:
        max_lag_ms = max(1, self.settings.liquidity_scalper_quote_max_lag_ms)
        best: Quote | None = None
        for quote in reversed(state.quotes):
            if quote.timestamp_ms > timestamp_ms:
                continue
            lag_ms = timestamp_ms - quote.timestamp_ms
            if lag_ms > max_lag_ms:
                break
            best = quote
            break
        if best is not None:
            return best
        if state.quote is not None and state.quote.timestamp_ms <= timestamp_ms:
            lag_ms = timestamp_ms - state.quote.timestamp_ms
            if lag_ms <= max_lag_ms:
                return state.quote
        return None

    @staticmethod
    def _is_aggressive_buy(trade: Trade, quote: Quote) -> bool:
        return trade.price >= quote.ask

    @staticmethod
    def _is_aggressive_sell(trade: Trade, quote: Quote) -> bool:
        return trade.price <= quote.bid

    def _has_minimum_net_edge(self, spread_bps: float) -> bool:
        gross_micro_bps = self.settings.liquidity_scalper_micro_profit_pct * 10_000
        net_edge_bps = gross_micro_bps - max(0.0, spread_bps)
        return net_edge_bps >= self.settings.liquidity_scalper_min_net_edge_bps

    def _flush_reclaim_setup(self, bars: list[Bar]) -> str | None:
        lookback = max(2, self.settings.liquidity_scalper_flush_lookback_bars)
        if len(bars) < lookback + 1:
            return None
        recent = bars[-(lookback + 1) :]
        start_high = max(bar.high for bar in recent[:-1])
        flush_low = min(bar.low for bar in recent)
        if start_high <= 0:
            return None
        drop_pct = (start_high - flush_low) / start_high
        if drop_pct < self.settings.liquidity_scalper_flush_drop_pct:
            return None
        reclaim_pct = (recent[-1].close - flush_low) / flush_low if flush_low > 0 else 0.0
        if reclaim_pct < self.settings.liquidity_scalper_reclaim_pct:
            return None
        if recent[-1].close <= recent[-1].open:
            return None
        return f"flush_reclaim drop {drop_pct:.2%}, reclaim {reclaim_pct:.2%}"

    def _liquidity_breakout_setup(self, bars: list[Bar]) -> str | None:
        lookback = max(2, self.settings.liquidity_scalper_breakout_lookback_bars)
        if len(bars) < lookback + 1:
            return None
        prior = bars[-(lookback + 1) : -1]
        recent_high = max(bar.high for bar in prior)
        breakout_level = recent_high * (1.0 + self.settings.liquidity_scalper_breakout_buffer_pct)
        last = bars[-1]
        if last.close < breakout_level:
            return None
        if last.close <= last.open:
            return None
        return f"liquidity_breakout above {recent_high:.2f}"

    def _session_bars(self, state: SymbolState) -> list[Bar]:
        if not state.last_event_ms:
            return []
        current = datetime.fromtimestamp(state.last_event_ms / 1000, tz=self.market_tz)
        out = []
        for bar in state.bars:
            start = datetime.fromtimestamp(bar.start_ms / 1000, tz=self.market_tz)
            if start.date() == current.date() and start.time() >= MARKET_OPEN:
                out.append(bar)
        return out

    @staticmethod
    def _recent_trades(state: SymbolState, window_seconds: int) -> list[Trade]:
        if not state.trades:
            return []
        latest_ms = state.trades[-1].timestamp_ms
        threshold = latest_ms - max(1, window_seconds) * 1000
        return [trade for trade in state.trades if trade.timestamp_ms >= threshold and trade.price > 0 and trade.size > 0]

    @staticmethod
    def _classify_trade(trade: Trade, quote: Quote) -> Literal["buy", "sell"]:
        if trade.price >= quote.ask:
            return "buy"
        if trade.price <= quote.bid:
            return "sell"
        return "buy" if trade.price >= quote.mid else "sell"

    @staticmethod
    def _session_dollar_volume(bars: list[Bar], state: SymbolState) -> float:
        bar_dv = sum(bar.close * bar.volume for bar in bars)
        if not state.trades or not state.last_event_ms:
            return bar_dv
        current = datetime.fromtimestamp(state.last_event_ms / 1000, tz=MARKET_TZ)
        trade_dv = 0.0
        for trade in state.trades:
            ts = datetime.fromtimestamp(trade.timestamp_ms / 1000, tz=MARKET_TZ)
            if ts.date() == current.date() and ts.time() >= MARKET_OPEN:
                trade_dv += trade.price * trade.size
        return max(bar_dv, trade_dv)

    def _bar_flow_is_expanding(self, bar_dollar_volume: float, bars: list[Bar]) -> tuple[bool, float]:
        threshold = max(0.0, self.settings.liquidity_scalper_min_bar_dollar_volume)
        ratio = self._bar_dollar_volume_ratio(bars)
        relative_floor = threshold * 0.05
        return (
            (threshold <= 0 or bar_dollar_volume >= relative_floor)
            and ratio >= self.settings.liquidity_scalper_min_volume_ratio,
            ratio,
        )

    def _session_flow_is_expanding(
        self,
        session_dollar_volume: float,
        bars: list[Bar],
        *,
        tape: TapeMetrics | None = None,
    ) -> tuple[bool, float]:
        threshold = max(0.0, self.settings.liquidity_scalper_min_session_dollar_volume)
        ratio = self._recent_session_flow_ratio(bars)
        relative_floor = threshold * 0.01
        tape_confirmed = (
            tape is not None
            and tape.dollar_volume >= self.settings.liquidity_scalper_min_tape_dollar_volume * 2.0
            and tape.buy_sell_ratio >= self.settings.liquidity_scalper_min_buy_sell_ratio
        )
        return (
            (threshold <= 0 or session_dollar_volume >= relative_floor)
            and (ratio >= self.settings.liquidity_scalper_min_volume_ratio or tape_confirmed),
            ratio,
        )

    def _trade_flow_is_expanding(self, trade_dollar_volume: float, metrics: TapeMetrics) -> bool:
        threshold = max(0.0, self.settings.liquidity_scalper_min_trade_dollar_volume)
        min_ratio = self.settings.liquidity_scalper_min_volume_ratio
        return (
            threshold <= 0
            or trade_dollar_volume >= threshold * 0.25
            or metrics.last_trade_dollar_volume_ratio >= min_ratio
        )

    def _tape_flow_is_expanding(self, metrics: TapeMetrics) -> bool:
        threshold = max(0.0, self.settings.liquidity_scalper_min_tape_dollar_volume)
        min_ratio = self.settings.liquidity_scalper_min_volume_ratio
        return (
            threshold <= 0
            or metrics.dollar_volume >= threshold * 0.25
            or metrics.dollar_accel_ratio >= min_ratio
        )

    @staticmethod
    def _bar_dollar_volume_ratio(bars: list[Bar]) -> float:
        if len(bars) < 2:
            return 0.0
        current = bars[-1].close * bars[-1].volume
        baseline = median(
            [bar.close * bar.volume for bar in bars[:-1] if bar.close > 0 and bar.volume > 0] or [0.0]
        )
        return current / baseline if baseline > 0 else 0.0

    @staticmethod
    def _recent_session_flow_ratio(bars: list[Bar], lookback: int = 3) -> float:
        if len(bars) < 2:
            return 0.0
        recent = bars[-max(1, min(lookback, len(bars))) :]
        prior = bars[: -len(recent)]
        if not prior:
            prior = bars[:-1]
            recent = bars[-1:]
        recent_dv = [bar.close * bar.volume for bar in recent if bar.close > 0 and bar.volume > 0]
        prior_dv = [bar.close * bar.volume for bar in prior if bar.close > 0 and bar.volume > 0]
        if not recent_dv or not prior_dv:
            return 0.0
        baseline = median(prior_dv)
        if baseline <= 0:
            return 0.0
        return max(median(recent_dv) / baseline, recent_dv[-1] / baseline)

    @staticmethod
    def _hybrid_range_pct(bars: list[Bar], state: SymbolState) -> float:
        prices = []
        for bar in bars:
            prices.extend([bar.high, bar.low])
        prices.extend(trade.price for trade in state.trades if trade.price > 0)
        last_price = state.trade.price if state.trade is not None else state.last_price
        if not prices or not last_price or last_price <= 0:
            return 0.0
        return (max(prices) - min(prices)) / last_price

    def _within_entry_window(self, timestamp_ms: int | None) -> bool:
        if timestamp_ms is None:
            return False
        current = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.market_tz)
        minutes = current.hour * 60 + current.minute
        market_open = MARKET_OPEN.hour * 60 + MARKET_OPEN.minute
        elapsed = minutes - market_open
        morning = self.settings.liquidity_scalper_start_minute <= elapsed <= self.settings.liquidity_scalper_end_minute
        afternoon = (
            self.settings.liquidity_scalper_afternoon_start_minute
            <= elapsed
            <= self.settings.liquidity_scalper_afternoon_end_minute
        )
        return morning or afternoon

    def _opening_memory(self, state: SymbolState) -> OpeningSessionMemory | None:
        if not self.settings.opening_memory_enabled:
            return None
        return opening_session_memory(
            state,
            lookback_days=self.settings.opening_memory_lookback_days,
            opening_minutes=self.settings.opening_memory_window_minutes,
            min_impulse_pct=self.settings.opening_memory_min_impulse_pct,
            fade_pct=self.settings.opening_memory_fade_pct,
            max_close_loss_pct=self.settings.opening_memory_max_close_loss_pct,
        )

    def _has_long_repeat_memory(self, memory: OpeningSessionMemory | None) -> bool:
        if memory is None:
            return False
        return memory.has_long_repeat(self.settings.opening_memory_min_repeat_days)

    def _bearish_memory_blocks_long(self, memory: OpeningSessionMemory | None) -> bool:
        if memory is None or not self.settings.opening_memory_bearish_long_block:
            return False
        min_repeat_days = self.settings.opening_memory_min_repeat_days
        return memory.has_short_repeat(min_repeat_days) and memory.short_score() > memory.long_score()

    @staticmethod
    def _bounded_relief(value: float) -> float:
        return min(1.0, max(0.1, value))

    @staticmethod
    def _volume_ratio(bars: list[Bar]) -> float:
        if len(bars) < 2:
            return 0.0
        baseline = median([bar.volume for bar in bars[:-1] if bar.volume > 0] or [0.0])
        return bars[-1].volume / baseline if baseline > 0 else 0.0

    @staticmethod
    def _range_pct(bars: list[Bar]) -> float:
        if not bars:
            return 0.0
        high = max(bar.high for bar in bars)
        low = min(bar.low for bar in bars)
        return (high - low) / bars[-1].close if bars[-1].close > 0 else 0.0
