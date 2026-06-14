from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum
import json
import logging
from pathlib import Path
from typing import Any, ClassVar

from candle import SymbolState
from config import Settings
from env_vars import EnvSpec, bool_env, float_env, int_env, str_env
from market_hours import MARKET_TZ
from models import ExitDecision, Signal
from strategies.base import Strategy
from strategy_selectors.select_gap_and_go import latest_valid_quote

LOG = logging.getLogger(__name__)
MARKET_OPEN = time(9, 30)


class RecoveryState(Enum):
    WATCHING = "watching"
    SCALING_IN = "scaling_in"
    RECOVERY_CONFIRMED = "recovery_confirmed"
    PROFIT_HOLD = "profit_hold"
    SCALING_OUT = "scaling_out"
    EXITED = "exited"


@dataclass
class SymbolRecoveryState:
    symbol: str
    state: RecoveryState = RecoveryState.WATCHING
    reference_price: float = 0.0
    current_tranche: int = 0
    total_shares: int = 0
    average_cost: float = 0.0
    planned_risk_budget: float = 0.0
    unrealized_loss: float = 0.0
    recovery_confirmed_ms: int = 0
    partial1_taken: bool = False
    partial2_taken: bool = False
    partial3_taken: bool = False
    highest_price_after_entry: float = 0.0
    last_add_ms: int = 0
    entry_bar_index: int = 0


@dataclass(frozen=True)
class SymbolRecoveryProfile:
    symbol: str
    avg_daily_dollar_volume: float = 0.0
    expected_10m_dollar_volume: float = 0.0
    relative_volume: float | None = None
    decline_threshold_pct: float = 0.0
    atr_pct: float | None = None


class RecoveryScaleStrategy(Strategy):
    """Scale-in/scale-out recovery strategy for liquid stocks in constructive trends.

    Entry: Starts with small position during controlled decline in high-liquidity stocks
    with constructive higher-timeframe structure. Adds at predefined ATR or % intervals.

    Scale-in ladder: Increases position size as drawdown deepens (default 5 tranches).
    Stop adding on structural failure or regime worsening.

    Recovery confirmation: Price above EMA20, SuperTrend bullish, RSI > 50, plus 2 of:
    MACD/AO improving, EMA Gap positive, BreakoutPower > 50, price above VWAP.

    Scale-out: Partial exits at +1.5-2.5%, +4-5%, overextension (RSI 70/BP 80).
    Final exit on SuperTrend bearish, EMA20 loss, or higher low break.

    Global Market Regime integration: Full ladder in risk-on, reduced in neutral,
    minimal in weak, blocked in risk-off.
    """

    name = "recovery_scale"
    env_specs: ClassVar[tuple[EnvSpec, ...]] = (
        ("recovery_scale_start_minute", "RECOVERY_SCALE_START_MINUTE", int_env, 0),
        ("recovery_scale_end_minute", "RECOVERY_SCALE_END_MINUTE", int_env, 360),
        ("recovery_scale_max_tranches", "RECOVERY_SCALE_MAX_TRANCHES", int_env, 5),
        ("recovery_scale_initial_tranche_pct", "RECOVERY_SCALE_INITIAL_TRANCHE_PCT", float_env, 0.10),
        ("recovery_scale_ladder_mode", "RECOVERY_SCALE_LADDER_MODE", str_env, "atr"),
        ("recovery_scale_min_liquidity_dollar_volume", "RECOVERY_SCALE_MIN_LIQUIDITY_DOLLAR_VOLUME", float_env, 0.0),
        ("recovery_scale_min_relative_volume", "RECOVERY_SCALE_MIN_RELATIVE_VOLUME", float_env, 0.35),
        ("recovery_scale_max_order_10m_volume_pct", "RECOVERY_SCALE_MAX_ORDER_10M_VOLUME_PCT", float_env, 0.015),
        ("recovery_scale_decline_atr_multiple", "RECOVERY_SCALE_DECLINE_ATR_MULTIPLE", float_env, 0.35),
        ("recovery_scale_decline_floor_pct", "RECOVERY_SCALE_DECLINE_FLOOR_PCT", float_env, 0.0025),
        ("recovery_scale_decline_cap_pct", "RECOVERY_SCALE_DECLINE_CAP_PCT", float_env, 0.0125),
        ("recovery_scale_max_spread_bps", "RECOVERY_SCALE_MAX_SPREAD_BPS", float_env, 12.0),
        ("recovery_scale_min_price", "RECOVERY_SCALE_MIN_PRICE", float_env, 10.0),
        ("recovery_scale_max_trades_per_symbol_per_session", "RECOVERY_SCALE_MAX_TRADES_PER_SYMBOL_PER_SESSION", int_env, 1),
        ("recovery_scale_symbol_loss_lock_count", "RECOVERY_SCALE_SYMBOL_LOSS_LOCK_COUNT", int_env, 1),
        ("recovery_scale_daily_strategy_loss_budget", "RECOVERY_SCALE_DAILY_STRATEGY_LOSS_BUDGET", float_env, 200.0),
        ("recovery_scale_per_symbol_risk_budget_pct", "RECOVERY_SCALE_PER_SYMBOL_RISK_BUDGET_PCT", float_env, 0.015),
        ("recovery_scale_risk_on_budget_multiplier", "RECOVERY_SCALE_RISK_ON_BUDGET_MULTIPLIER", float_env, 1.0),
        ("recovery_scale_neutral_budget_multiplier", "RECOVERY_SCALE_NEUTRAL_BUDGET_MULTIPLIER", float_env, 0.65),
        ("recovery_scale_weak_budget_multiplier", "RECOVERY_SCALE_WEAK_BUDGET_MULTIPLIER", float_env, 0.35),
        ("recovery_scale_block_new_entries", "RECOVERY_SCALE_BLOCK_NEW_ENTRIES", bool_env, True),
        ("recovery_scale_recovery_rsi_min", "RECOVERY_SCALE_RECOVERY_RSI_MIN", float_env, 50.0),
        ("recovery_scale_recovery_breakout_power_min", "RECOVERY_SCALE_RECOVERY_BREAKOUT_POWER_MIN", float_env, 50.0),
        ("recovery_scale_partial1_profit_pct", "RECOVERY_SCALE_PARTIAL1_PROFIT_PCT", float_env, 0.015),
        ("recovery_scale_partial1_size", "RECOVERY_SCALE_PARTIAL1_SIZE", float_env, 0.25),
        ("recovery_scale_partial2_profit_pct", "RECOVERY_SCALE_PARTIAL2_PROFIT_PCT", float_env, 0.04),
        ("recovery_scale_partial2_size", "RECOVERY_SCALE_PARTIAL2_SIZE", float_env, 0.30),
        ("recovery_scale_partial3_rsi_threshold", "RECOVERY_SCALE_PARTIAL3_RSI_THRESHOLD", float_env, 70.0),
        ("recovery_scale_partial3_bp_threshold", "RECOVERY_SCALE_PARTIAL3_BP_THRESHOLD", float_env, 80.0),
        ("recovery_scale_partial3_size", "RECOVERY_SCALE_PARTIAL3_SIZE", float_env, 0.25),
        ("recovery_scale_exit_ema20_loss", "RECOVERY_SCALE_EXIT_EMA20_LOSS", bool_env, True),
        ("recovery_scale_exit_supertrend_bearish", "RECOVERY_SCALE_EXIT_SUPERTREND_BEARISH", bool_env, True),
        ("recovery_scale_exit_higher_low_break", "RECOVERY_SCALE_EXIT_HIGHER_LOW_BREAK", bool_env, True),
        ("recovery_scale_trailing_stop_atr_multiplier", "RECOVERY_SCALE_TRAILING_STOP_ATR_MULTIPLIER", float_env, 1.5),
        ("recovery_scale_require_daily_uptrend", "RECOVERY_SCALE_REQUIRE_DAILY_UPTREND", bool_env, True),
        ("recovery_scale_require_ema40_above_ema60", "RECOVERY_SCALE_REQUIRE_EMA40_ABOVE_EMA60", bool_env, True),
        ("recovery_scale_max_decline_bars", "RECOVERY_SCALE_MAX_DECLINE_BARS", int_env, 10),
        ("recovery_scale_min_bounce_pct", "RECOVERY_SCALE_MIN_BOUNCE_PCT", float_env, 0.003),
    )
    diagnostic_loggers: ClassVar[tuple[str, ...]] = ("strategies.recovery_scale",)
    selector_command: ClassVar[str] = ".venv/bin/python strategy_selectors/select_recovery_scale.py --top 8"
    requires_plan: ClassVar[bool] = True

    @classmethod
    def runtime_settings_section(cls, settings: Any) -> dict[str, Any] | None:
        if cls.name not in settings.strategy_names:
            return None
        return {
            "start_minute": settings.recovery_scale_start_minute,
            "end_minute": settings.recovery_scale_end_minute,
            "max_tranches": settings.recovery_scale_max_tranches,
            "ladder_mode": settings.recovery_scale_ladder_mode,
            "min_liquidity": settings.recovery_scale_min_liquidity_dollar_volume,
            "min_relative_volume": settings.recovery_scale_min_relative_volume,
            "max_order_10m_volume_pct": settings.recovery_scale_max_order_10m_volume_pct,
            "decline_atr_multiple": settings.recovery_scale_decline_atr_multiple,
            "max_spread_bps": settings.recovery_scale_max_spread_bps,
            "daily_strategy_loss_budget": settings.recovery_scale_daily_strategy_loss_budget,
            "per_symbol_risk_budget_pct": settings.recovery_scale_per_symbol_risk_budget_pct,
        }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_tz = MARKET_TZ
        self._symbol_states: dict[str, SymbolRecoveryState] = {}
        self._symbol_profiles = self._load_symbol_profiles()
        self._last_reject_log_ms: dict[tuple[str, str], int] = {}
        self._strategy_daily_loss: float = 0.0

    def bootstrap_states(self, states: dict[str, SymbolState]) -> None:
        try:
            from alpaca.data.timeframe import TimeFrame
            from alpaca_client import get_bars_between, make_clients
        except Exception:
            LOG.exception("Recovery scale bootstrap imports failed")
            return

        symbols = list(states)
        if not symbols:
            return

        now = datetime.now(tz=self.market_tz)
        start = datetime.combine((now - timedelta(days=120)).date(), time.min, tzinfo=self.market_tz)

        try:
            clients = make_clients(self.settings)
            daily_bars = get_bars_between(clients, symbols, TimeFrame.Day, start, now + timedelta(days=1))
        except Exception:
            LOG.exception("Recovery scale bootstrap failed to load daily bars")
            return

        seeded = 0
        for symbol, state in states.items():
            daily_items = daily_bars.get(symbol, [])
            if daily_items:
                state.daily_bars = daily_items
                seeded += 1

        if seeded:
            LOG.info("Recovery scale bootstrapped %s symbols with daily bars", seeded)

    def evaluate(self, state: SymbolState) -> Signal | None:
        if not self.is_symbol_allowed(state.symbol):
            return None
        if state.last_event_kind not in {"quote", "bar"}:
            return None
        if not self._within_entry_window(state.last_event_ms):
            return None

        # Get or create symbol state
        symbol_state = self._symbol_states.get(state.symbol)
        if symbol_state is None:
            symbol_state = SymbolRecoveryState(symbol=state.symbol)
            self._symbol_states[state.symbol] = symbol_state

        # Handle scale-in adds vs new entries
        if symbol_state.state == RecoveryState.SCALING_IN:
            return self._evaluate_scale_in_add(state, symbol_state)
        elif symbol_state.state != RecoveryState.WATCHING:
            return None

        # New entry logic
        # Check market regime
        regime_ok, regime_reason = self._check_market_regime()
        if not regime_ok:
            return self._reject(state, "regime", regime_reason)

        quote = latest_valid_quote(state)
        if quote is None or quote.ask <= 0:
            return self._reject(state, "quote", "invalid quote")

        if quote.spread_bps > self.settings.recovery_scale_max_spread_bps:
            return self._reject(state, "spread", f"spread {quote.spread_bps:.1f} > max {self.settings.recovery_scale_max_spread_bps:.1f}")

        if quote.ask < self.settings.recovery_scale_min_price:
            return self._reject(state, "price", f"price {quote.ask:.2f} < min {self.settings.recovery_scale_min_price:.2f}")

        # Check daily trend structure
        if not self._check_daily_trend(state):
            return self._reject(state, "daily_trend", "daily trend not constructive")

        # Check if intraday decline is beginning or underway
        if not self._check_intraday_decline(state):
            return self._reject(state, "decline", "no controlled intraday decline detected")

        # Check position value budget
        position_value_budget = self._calculate_risk_budget(quote.ask)
        if position_value_budget <= 0:
            return self._reject(state, "risk_budget", "no position value budget available")

        # Get ladder configuration
        tranche_sizes = list(self.settings.recovery_scale_tranche_sizes)
        if not tranche_sizes or len(tranche_sizes) < self.settings.recovery_scale_max_tranches:
            # Fallback to defaults
            tranche_sizes = [0.10, 0.15, 0.20, 0.25, 0.30]

        # Calculate initial tranche size
        initial_size_pct = tranche_sizes[0] if tranche_sizes else 0.10
        initial_value = position_value_budget * initial_size_pct
        shares = int(initial_value / quote.ask)
        if shares <= 0:
            return self._reject(state, "shares", "insufficient capital for initial tranche")

        liquidity_ok, liquidity_reason = self._check_liquidity(state, planned_order_value=initial_value)
        if not liquidity_ok:
            return self._reject(state, "liquidity", liquidity_reason)

        # Initialize symbol state for scaling in
        symbol_state.state = RecoveryState.SCALING_IN
        symbol_state.reference_price = quote.ask
        symbol_state.current_tranche = 1
        symbol_state.planned_risk_budget = position_value_budget
        symbol_state.last_add_ms = state.last_event_ms
        symbol_state.entry_bar_index = len(state.bars)

        # Calculate stop price (use ATR-based trailing stop)
        atr = self._get_atr(state)
        stop_price = quote.ask - (atr * self.settings.recovery_scale_trailing_stop_atr_multiplier) if atr > 0 else quote.ask * 0.97

        LOG.info(
            "Recovery scale initial entry: %s at $%.2f, tranche 1/%d, budget $%.0f, atr=%.2f",
            state.symbol,
            quote.ask,
            self.settings.recovery_scale_max_tranches,
            position_value_budget,
            atr,
        )

        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=quote.ask,
            timestamp_ms=state.last_event_ms,
            change_pct=0.0,
            volume_ratio=0.0,
            spread_bps=quote.spread_bps,
            reason=f"recovery_scale_tranche_1_decline_entry",
            stop_price=stop_price,
            position_size_multiplier=initial_size_pct,
        )

    def _evaluate_scale_in_add(self, state: SymbolState, symbol_state: SymbolRecoveryState) -> Signal | None:
        """Evaluate whether to add to existing position (tranches 2-5)."""
        quote = latest_valid_quote(state)
        if quote is None or quote.ask <= 0:
            return None

        current_price = quote.ask

        # Don't add if already at max tranches
        if symbol_state.current_tranche >= self.settings.recovery_scale_max_tranches:
            return None

        # Check if enough time has passed since last add (prevent rapid adds)
        if state.last_event_ms - symbol_state.last_add_ms < 10_000:  # 10 second minimum
            return None

        # Get ladder triggers
        ladder_mode = self.settings.recovery_scale_ladder_mode
        if ladder_mode == "atr":
            triggers = list(self.settings.recovery_scale_atr_ladder)
            atr = self._get_atr(state)
            if atr <= 0:
                return None
            trigger_prices = [symbol_state.reference_price - (atr * mult) for mult in triggers]
        else:  # percent mode
            triggers = list(self.settings.recovery_scale_pct_ladder)
            trigger_prices = [symbol_state.reference_price * (1 - pct) for pct in triggers]

        # Check if current price hit next tranche trigger
        next_tranche_idx = symbol_state.current_tranche  # 0-indexed for lists
        if next_tranche_idx >= len(trigger_prices):
            return None

        next_trigger_price = trigger_prices[next_tranche_idx]
        if current_price > next_trigger_price:
            return None  # Not triggered yet

        # Check for structural failure before adding
        if self._is_structural_failure(state, symbol_state):
            LOG.info("Skipping tranche %d add for %s: structural failure", next_tranche_idx + 1, state.symbol)
            return None

        # Check market regime still allows adds
        regime_ok, _ = self._check_market_regime()
        if not regime_ok:
            LOG.info("Skipping tranche %d add for %s: regime hardened", next_tranche_idx + 1, state.symbol)
            return None

        # Calculate add size
        tranche_sizes = list(self.settings.recovery_scale_tranche_sizes)
        if not tranche_sizes or next_tranche_idx >= len(tranche_sizes):
            tranche_sizes = [0.10, 0.15, 0.20, 0.25, 0.30]

        add_size_pct = tranche_sizes[next_tranche_idx] if next_tranche_idx < len(tranche_sizes) else 0.20
        add_value = symbol_state.planned_risk_budget * add_size_pct
        shares = int(add_value / current_price)

        if shares <= 0:
            return None

        # Update state
        symbol_state.current_tranche += 1
        symbol_state.last_add_ms = state.last_event_ms

        # Update stop
        atr = self._get_atr(state)
        stop_price = current_price - (atr * self.settings.recovery_scale_trailing_stop_atr_multiplier) if atr > 0 else current_price * 0.97

        LOG.info(
            "Recovery scale add: %s tranche %d/%d at $%.2f (trigger $%.2f), size %.1f%%",
            state.symbol,
            symbol_state.current_tranche,
            self.settings.recovery_scale_max_tranches,
            current_price,
            next_trigger_price,
            add_size_pct * 100,
        )

        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=current_price,
            timestamp_ms=state.last_event_ms,
            change_pct=0.0,
            volume_ratio=0.0,
            spread_bps=quote.spread_bps,
            reason=f"recovery_scale_tranche_{symbol_state.current_tranche}_add",
            stop_price=stop_price,
            position_size_multiplier=add_size_pct,
            allow_add_to_position=True,
        )

    def should_exit(self, state: SymbolState, position: Any) -> ExitDecision | None:
        """Evaluate exit logic based on current state and position."""
        symbol_state = self._symbol_states.get(state.symbol)
        if symbol_state is None or symbol_state.state == RecoveryState.EXITED:
            return None

        quote = latest_valid_quote(state)
        if quote is None or quote.bid <= 0:
            return None

        current_price = quote.bid
        pnl_pct = (current_price - position.entry_price) / position.entry_price

        # Update tracking
        if current_price > symbol_state.highest_price_after_entry:
            symbol_state.highest_price_after_entry = current_price

        # State-specific exit logic
        if symbol_state.state == RecoveryState.SCALING_IN:
            return self._exit_scaling_in(state, position, symbol_state, current_price, pnl_pct)
        elif symbol_state.state == RecoveryState.RECOVERY_CONFIRMED:
            return self._exit_recovery_confirmed(state, position, symbol_state, current_price, pnl_pct)
        elif symbol_state.state in (RecoveryState.PROFIT_HOLD, RecoveryState.SCALING_OUT):
            return self._exit_profit_hold(state, position, symbol_state, current_price, pnl_pct)

        return None

    def on_entry_fill(self, fill: Any) -> None:
        """Track fills for averaging down."""
        symbol_state = self._symbol_states.get(fill.symbol)
        if symbol_state is None:
            return

        symbol_state.total_shares += fill.shares
        if symbol_state.total_shares > 0:
            symbol_state.average_cost = (
                symbol_state.average_cost * (symbol_state.total_shares - fill.shares) + fill.price * fill.shares
            ) / symbol_state.total_shares

    def on_exit_fill(self, fill: Any) -> None:
        """Track realized loss budget and reset symbol state after full exits."""
        if fill.pnl < 0:
            self._strategy_daily_loss += abs(fill.pnl)
        if getattr(fill, "exit_stage", "") != "partial":
            symbol_state = self._symbol_states.get(fill.symbol)
            if symbol_state is not None:
                symbol_state.state = RecoveryState.EXITED

    def _within_entry_window(self, timestamp_ms: int) -> bool:
        dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.market_tz)
        market_open = datetime.combine(dt.date(), MARKET_OPEN, tzinfo=self.market_tz)
        minutes_since_open = (dt - market_open).total_seconds() / 60
        return self.settings.recovery_scale_start_minute <= minutes_since_open <= self.settings.recovery_scale_end_minute

    def _check_market_regime(self) -> tuple[bool, str]:
        """Check if market regime allows new entries."""
        if self._market_regime is None:
            return True, "no regime filter"

        # Block in risk-off
        if self.settings.recovery_scale_block_new_entries and self._market_regime.score <= self.settings.market_regime_block_score:
            return False, f"blocked: regime {self._market_regime.name} score {self._market_regime.score:.1f}"

        return True, f"regime {self._market_regime.name} ok"

    def _load_symbol_profiles(self) -> dict[str, SymbolRecoveryProfile]:
        path = Path("data/recovery_scale_plan.json")
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            LOG.debug("Could not load recovery scale plan profiles from %s", path, exc_info=True)
            return {}

        profiles: dict[str, SymbolRecoveryProfile] = {}
        for row in payload.get("ranked") or payload.get("details") or []:
            symbol = str(row.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            profiles[symbol] = SymbolRecoveryProfile(
                symbol=symbol,
                avg_daily_dollar_volume=float(row.get("avg_daily_dollar_volume") or 0.0),
                expected_10m_dollar_volume=float(row.get("expected_10m_dollar_volume") or 0.0),
                relative_volume=float(row["relative_volume"]) if row.get("relative_volume") is not None else None,
                decline_threshold_pct=float(row.get("decline_threshold_pct") or 0.0),
                atr_pct=float(row["atr_pct"]) if row.get("atr_pct") is not None else None,
            )
        return profiles

    def _profile_for(self, symbol: str) -> SymbolRecoveryProfile | None:
        return self._symbol_profiles.get(symbol.strip().upper())

    def _check_liquidity(self, state: SymbolState, planned_order_value: float = 0.0) -> tuple[bool, str]:
        """Check symbol-relative liquidity and order impact."""
        bars = list(state.bars)
        if len(bars) < 10:
            return False, "need at least 10 bars for relative liquidity"

        recent_bars = bars[-10:]
        recent_10m_dollar_volume = sum(bar.close * bar.volume for bar in recent_bars)
        if recent_10m_dollar_volume <= 0:
            return False, "no recent dollar volume"

        absolute_floor = self.settings.recovery_scale_min_liquidity_dollar_volume
        if absolute_floor > 0 and recent_10m_dollar_volume < absolute_floor:
            return False, f"recent 10m dollar volume {recent_10m_dollar_volume:.0f} < floor {absolute_floor:.0f}"

        expected_10m = self._expected_10m_dollar_volume(state)
        if expected_10m > 0:
            relative_volume = recent_10m_dollar_volume / expected_10m
            if relative_volume < self.settings.recovery_scale_min_relative_volume:
                return (
                    False,
                    f"relative volume {relative_volume:.2f} < min {self.settings.recovery_scale_min_relative_volume:.2f}",
                )

        if planned_order_value > 0:
            max_order_value = recent_10m_dollar_volume * self.settings.recovery_scale_max_order_10m_volume_pct
            if max_order_value > 0 and planned_order_value > max_order_value:
                return False, f"order ${planned_order_value:.0f} > {self.settings.recovery_scale_max_order_10m_volume_pct:.2%} of recent 10m volume"

        return True, "ok"

    def _expected_10m_dollar_volume(self, state: SymbolState) -> float:
        profile = self._profile_for(state.symbol)
        if profile and profile.expected_10m_dollar_volume > 0:
            return profile.expected_10m_dollar_volume

        daily_bars = list(getattr(state, "daily_bars", []) or [])
        if daily_bars:
            sample = daily_bars[-20:]
            avg_daily_dollar_volume = sum(bar.close * bar.volume for bar in sample) / len(sample)
            return avg_daily_dollar_volume * (10 / 390)
        return 0.0

    def _check_daily_trend(self, state: SymbolState) -> bool:
        """Check if daily trend structure is constructive."""
        if not self.settings.recovery_scale_require_daily_uptrend:
            return True

        daily_bars = getattr(state, "daily_bars", [])
        if len(daily_bars) < 60:
            return True  # Not enough data, allow

        last_daily = daily_bars[-1]
        ema40 = self._calculate_ema(daily_bars, 40)
        ema60 = self._calculate_ema(daily_bars, 60)

        if ema40 <= 0 or ema60 <= 0:
            return True

        # Require EMA40 > EMA60 (uptrend) or price recovering toward EMA60
        if self.settings.recovery_scale_require_ema40_above_ema60:
            if ema40 < ema60:
                # Allow early recovery: price within 5% of EMA60
                distance_from_ema60 = abs(last_daily.close - ema60) / ema60
                return distance_from_ema60 < 0.05

        return True

    def _check_intraday_decline(self, state: SymbolState) -> bool:
        """Check if there's a controlled intraday decline (not structural failure)."""
        bars = list(state.bars)
        if len(bars) < 10:
            return False

        recent_bars = bars[-self.settings.recovery_scale_max_decline_bars:]
        if not recent_bars:
            return False

        # Check for decline with some bounces (not relentless selling)
        closes = [bar.close for bar in recent_bars]
        lows = [bar.low for bar in recent_bars]

        # Must have some decline from recent high
        recent_high = max(bar.high for bar in recent_bars)
        current_price = closes[-1]
        decline_pct = (recent_high - current_price) / recent_high

        decline_threshold_pct = self._decline_threshold_pct(state)
        if decline_pct < decline_threshold_pct:
            return False

        # Check for at least one bounce (prevents catching falling knife)
        has_bounce = False
        for i in range(1, len(closes)):
            if closes[i] > closes[i-1]:
                bounce_pct = (closes[i] - lows[i-1]) / lows[i-1]
                if bounce_pct >= self.settings.recovery_scale_min_bounce_pct:
                    has_bounce = True
                    break

        return has_bounce or decline_pct < decline_threshold_pct * 3.0  # Allow mild declines without bounce

    def _decline_threshold_pct(self, state: SymbolState) -> float:
        profile = self._profile_for(state.symbol)
        if profile and profile.decline_threshold_pct > 0:
            return profile.decline_threshold_pct

        quote = latest_valid_quote(state)
        price = quote.mid if quote is not None and quote.mid > 0 else (list(state.bars)[-1].close if state.bars else 0.0)
        atr = self._get_atr(state)
        atr_pct = atr / price if atr > 0 and price > 0 else 0.01
        raw = atr_pct * self.settings.recovery_scale_decline_atr_multiple
        return max(self.settings.recovery_scale_decline_floor_pct, min(self.settings.recovery_scale_decline_cap_pct, raw))

    def _calculate_risk_budget(self, price: float) -> float:
        """Calculate position value budget based on regime and strategy limits.

        Returns the maximum total position value (not risk/loss), which will be
        scaled across multiple tranches.
        """
        # Base position value from strategy-specific or global max
        base_value = self.settings.recovery_scale_max_position_value
        if base_value <= 0:
            base_value = self.settings.max_position_value

        # Check strategy daily loss budget
        if self._strategy_daily_loss >= self.settings.recovery_scale_daily_strategy_loss_budget:
            return 0.0

        # Adjust for market regime
        if self._market_regime is not None:
            if self._market_regime.score >= self.settings.market_regime_risk_on_score:
                multiplier = self.settings.recovery_scale_risk_on_budget_multiplier
            elif self._market_regime.score <= self.settings.market_regime_risk_off_score:
                multiplier = self.settings.recovery_scale_weak_budget_multiplier
            else:
                multiplier = self.settings.recovery_scale_neutral_budget_multiplier

            base_value *= multiplier

        return base_value

    def _get_atr(self, state: SymbolState, period: int = 14) -> float:
        """Calculate ATR for volatility-based stops."""
        bars = list(state.bars)
        if len(bars) < period + 1:
            return 0.0

        recent_bars = bars[-(period + 1):]
        true_ranges = []
        for i in range(1, len(recent_bars)):
            prev_close = recent_bars[i-1].close
            current = recent_bars[i]
            tr = max(
                current.high - current.low,
                abs(current.high - prev_close),
                abs(current.low - prev_close)
            )
            true_ranges.append(tr)

        return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0

    def _calculate_ema(self, bars: list, period: int) -> float:
        """Calculate EMA for given period."""
        if len(bars) < period:
            return 0.0

        closes = [bar.close for bar in bars[-period:]]
        multiplier = 2 / (period + 1)
        ema = closes[0]
        for close in closes[1:]:
            ema = (close - ema) * multiplier + ema
        return ema

    def _exit_scaling_in(self, state: SymbolState, position: Any, symbol_state: SymbolRecoveryState,
                         current_price: float, pnl_pct: float) -> ExitDecision | None:
        """Exit logic during scaling-in phase."""
        # Check for structural failure
        if self._is_structural_failure(state, symbol_state):
            symbol_state.state = RecoveryState.EXITED
            return ExitDecision(reason="structural_failure", shares=None)

        # Check for recovery confirmation
        if self._check_recovery_confirmation(state, symbol_state, current_price):
            symbol_state.state = RecoveryState.RECOVERY_CONFIRMED
            symbol_state.recovery_confirmed_ms = state.last_event_ms
            LOG.info("Recovery confirmed for %s at $%.2f", state.symbol, current_price)

        return None

    def _exit_recovery_confirmed(self, state: SymbolState, position: Any, symbol_state: SymbolRecoveryState,
                                  current_price: float, pnl_pct: float) -> ExitDecision | None:
        """Exit logic after recovery confirmed."""
        # Transition to PROFIT_HOLD if profitable
        if pnl_pct > 0:
            symbol_state.state = RecoveryState.PROFIT_HOLD
            LOG.info("Entering profit hold for %s at $%.2f (%.2f%%)", state.symbol, current_price, pnl_pct * 100)

        # Check for loss of recovery
        if not self._maintains_recovery_structure(state):
            return ExitDecision(reason="recovery_structure_lost", shares=None)

        return None

    def _exit_profit_hold(self, state: SymbolState, position: Any, symbol_state: SymbolRecoveryState,
                          current_price: float, pnl_pct: float) -> ExitDecision | None:
        """Exit logic during profit hold and scale-out."""
        # Scale-out logic
        avg_cost = symbol_state.average_cost if symbol_state.average_cost > 0 else position.entry_price
        profit_from_avg = (current_price - avg_cost) / avg_cost

        # Partial 1: First profit target
        if not symbol_state.partial1_taken and profit_from_avg >= self.settings.recovery_scale_partial1_profit_pct:
            symbol_state.partial1_taken = True
            symbol_state.state = RecoveryState.SCALING_OUT
            shares = int(position.shares * self.settings.recovery_scale_partial1_size)
            LOG.info("Taking partial1 on %s at $%.2f (%.2f%% from avg)", state.symbol, current_price, profit_from_avg * 100)
            return ExitDecision(reason=f"partial1_profit_{profit_from_avg:.3f}", shares=shares, mark_partial=True)

        # Partial 2: Second profit target
        if not symbol_state.partial2_taken and profit_from_avg >= self.settings.recovery_scale_partial2_profit_pct:
            symbol_state.partial2_taken = True
            shares = int(position.shares * self.settings.recovery_scale_partial2_size)
            LOG.info("Taking partial2 on %s at $%.2f (%.2f%% from avg)", state.symbol, current_price, profit_from_avg * 100)
            return ExitDecision(reason=f"partial2_profit_{profit_from_avg:.3f}", shares=shares, mark_partial=True)

        # Partial 3: Overextension
        if not symbol_state.partial3_taken:
            rsi = self._get_rsi(state)
            bp = self._get_breakout_power(state)
            if rsi >= self.settings.recovery_scale_partial3_rsi_threshold or bp >= self.settings.recovery_scale_partial3_bp_threshold:
                symbol_state.partial3_taken = True
                shares = int(position.shares * self.settings.recovery_scale_partial3_size)
                LOG.info("Taking partial3 on %s at $%.2f (overextension: RSI=%.1f, BP=%.1f)",
                        state.symbol, current_price, rsi, bp)
                return ExitDecision(reason=f"partial3_overextension_rsi{rsi:.0f}_bp{bp:.0f}", shares=shares, mark_partial=True)

        # Final exit signals
        exit_reason = self._check_final_exit_signals(state, symbol_state, current_price, pnl_pct)
        if exit_reason:
            symbol_state.state = RecoveryState.EXITED
            return ExitDecision(reason=exit_reason, shares=None)

        return None

    def _is_structural_failure(self, state: SymbolState, symbol_state: SymbolRecoveryState) -> bool:
        """Check if decline has become structural failure rather than pullback."""
        bars = list(state.bars)
        if len(bars) < 10:
            return False

        # Continuous new lows without meaningful bounces
        recent_bars = bars[-10:]
        lows = [bar.low for bar in recent_bars]
        continuous_decline = all(lows[i] <= lows[i-1] * 1.002 for i in range(1, len(lows)))

        if continuous_decline:
            LOG.debug("Structural failure detected: continuous decline in %s", state.symbol)
            return True

        # EMA structure collapse
        if len(bars) >= 60:
            ema20 = self._calculate_ema(bars, 20)
            ema60 = self._calculate_ema(bars, 60)
            current_price = bars[-1].close

            if ema20 > 0 and ema60 > 0:
                if current_price < ema60 * 0.95 and ema20 < ema60:  # Far below EMA60 and declining
                    LOG.debug("Structural failure: price %.2f far below EMA60 %.2f", current_price, ema60)
                    return True

        return False

    def _check_recovery_confirmation(self, state: SymbolState, symbol_state: SymbolRecoveryState,
                                     current_price: float) -> bool:
        """Check cluster of recovery signals."""
        bars = list(state.bars)
        if len(bars) < 40:
            return False

        ema20 = self._calculate_ema(bars, 20)
        rsi = self._get_rsi(state)
        bp = self._get_breakout_power(state)

        # Core requirements
        if current_price <= ema20:
            return False
        if rsi < self.settings.recovery_scale_recovery_rsi_min:
            return False

        # Count supporting signals
        supporting_signals = 0

        # BreakoutPower
        if bp >= self.settings.recovery_scale_recovery_breakout_power_min:
            supporting_signals += 1

        # Price above VWAP
        if bars and bars[-1].vwap > 0 and current_price > bars[-1].vwap:
            supporting_signals += 1

        # MACD improving (simplified: just check histogram positive)
        macd_hist = self._get_macd_histogram(state)
        if macd_hist > 0:
            supporting_signals += 1

        # EMA Gap improving (EMA5 > EMA20)
        ema5 = self._calculate_ema(bars, 5)
        if ema5 > ema20:
            supporting_signals += 1

        return supporting_signals >= 2

    def _maintains_recovery_structure(self, state: SymbolState) -> bool:
        """Check if recovery structure is maintained."""
        bars = list(state.bars)
        if len(bars) < 20:
            return True

        ema20 = self._calculate_ema(bars, 20)
        current_price = bars[-1].close

        # Losing EMA20
        if self.settings.recovery_scale_exit_ema20_loss and current_price < ema20 * 0.998:
            return False

        # SuperTrend bearish
        if self.settings.recovery_scale_exit_supertrend_bearish:
            st_result = self._get_supertrend(state)
            if st_result is not None:
                st_value, st_bullish = st_result
                if not st_bullish:
                    return False

        return True

    def _check_final_exit_signals(self, state: SymbolState, symbol_state: SymbolRecoveryState,
                                   current_price: float, pnl_pct: float) -> str | None:
        """Check for final exit signals."""
        # SuperTrend bearish
        if self.settings.recovery_scale_exit_supertrend_bearish:
            st_result = self._get_supertrend(state)
            if st_result is not None:
                st_value, st_bullish = st_result
                if not st_bullish:
                    return f"supertrend_bearish_{st_value:.2f}"

        # EMA20 loss
        if self.settings.recovery_scale_exit_ema20_loss:
            ema20 = self._calculate_ema(list(state.bars), 20)
            if ema20 > 0 and current_price < ema20 * 0.995:
                return "ema20_loss"

        # Higher low break
        if self.settings.recovery_scale_exit_higher_low_break:
            if self._higher_low_broken(state, symbol_state):
                return "higher_low_break"

        # Trailing stop
        if symbol_state.highest_price_after_entry > 0:
            atr = self._get_atr(state)
            if atr > 0:
                trailing_stop = symbol_state.highest_price_after_entry - (atr * self.settings.recovery_scale_trailing_stop_atr_multiplier)
                if current_price < trailing_stop:
                    return f"trailing_stop_{trailing_stop:.2f}"

        return None

    def _higher_low_broken(self, state: SymbolState, symbol_state: SymbolRecoveryState) -> bool:
        """Check if recent higher low structure is broken."""
        bars = list(state.bars)
        if len(bars) < 10:
            return False

        # Find recent swing lows
        recent_bars = bars[-10:]
        lows = [bar.low for bar in recent_bars]

        if len(lows) < 3:
            return False

        # Check if latest low broke below previous swing low
        return lows[-1] < min(lows[-5:-1])

    def _get_rsi(self, state: SymbolState, period: int = 14) -> float:
        """Calculate RSI."""
        bars = list(state.bars)
        if len(bars) < period + 1:
            return 50.0

        closes = [bar.close for bar in bars[-(period + 1):]]
        gains = []
        losses = []

        for i in range(1, len(closes)):
            change = closes[i] - closes[i-1]
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))

        avg_gain = sum(gains) / len(gains) if gains else 0
        avg_loss = sum(losses) / len(losses) if losses else 0

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def _get_breakout_power(self, state: SymbolState) -> float:
        """Get BreakoutPower indicator value."""
        indicators = getattr(state, "indicators", None)
        if indicators is None:
            return 50.0
        return indicators.get("breakout_power", 50.0)

    def _get_macd_histogram(self, state: SymbolState) -> float:
        """Get MACD histogram value."""
        indicators = getattr(state, "indicators", None)
        if indicators is None:
            return 0.0
        return indicators.get("macd_histogram", 0.0)

    def _get_supertrend(self, state: SymbolState, period: int = 10, multiplier: float = 2.0) -> tuple[float, bool] | None:
        """Calculate SuperTrend using proper implementation from stoch_macd_reversal."""
        from strategies.stoch_macd_reversal import StochMACDReversalStrategy

        bars = list(state.bars)
        if len(bars) < period + 1:
            return None

        return StochMACDReversalStrategy._compute_supertrend(bars, period, multiplier)

    def _reject(self, state: SymbolState, reason_key: str, detail: str) -> None:
        """Throttled rejection logging."""
        key = (state.symbol, reason_key)
        last_log_ms = self._last_reject_log_ms.get(key, 0)
        if state.last_event_ms - last_log_ms >= 30_000:
            LOG.debug("Recovery scale reject %s: %s - %s", state.symbol, reason_key, detail)
            self._last_reject_log_ms[key] = state.last_event_ms
        return None
