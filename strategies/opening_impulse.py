from collections import deque
from dataclasses import dataclass
from datetime import datetime, time
import logging
from statistics import median

from candle import SymbolState
from config import Settings
from market_hours import MARKET_TZ
from models import ExitDecision, Signal
from strategies.base import Strategy


LOG = logging.getLogger(__name__)
MARKET_OPEN = time(9, 30)


@dataclass(frozen=True)
class EntryCandidate:
    change_pct: float
    volume_ratio: float
    reason: str
    kind: str


@dataclass(frozen=True)
class OpeningRange:
    open: float
    high: float
    low: float
    midpoint: float
    volume: float
    start_ms: int
    end_ms: int


class OpeningImpulseStrategy(Strategy):
    name = "opening_impulse"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.market_tz = MARKET_TZ
        self._last_reject_log_ms: dict[tuple[str, str], int] = {}

    def evaluate(self, state: SymbolState) -> Signal | None:
        if state.last_event_kind not in {"quote", "bar"}:
            return None

        if not self._within_trading_window(state.last_event_ms):
            return self._reject(state, "window", "outside opening impulse entry window")

        last = self._latest_valid_quote(state)
        if last is None:
            return self._reject(state, "quote", "invalid or missing latest quote")

        quotes = self._recent_quotes(state, self.settings.opening_impulse_window_seconds)
        quote_change_pct = 0.0
        if len(quotes) >= self.settings.opening_impulse_min_quotes and quotes[0].mid > 0:
            quote_change_pct = (quotes[-1].mid - quotes[0].mid) / quotes[0].mid
        volume_ratio = self._volume_ratio(state)
        candidate = self._range_impulse(state, last) or self._bar_impulse(state)

        if candidate is None and quote_change_pct >= self.settings.opening_impulse_change_pct:
            first = quotes[0]
            candidate = EntryCandidate(
                change_pct=quote_change_pct,
                volume_ratio=volume_ratio,
                reason=(
                    f"opening quote impulse {quote_change_pct:.3%} over "
                    f"{(last.timestamp_ms - first.timestamp_ms) / 1000:.0f}s, "
                    f"volume {volume_ratio:.1f}x baseline"
                ),
                kind="quote_impulse",
            )

        if candidate is None:
            quote_detail = (
                f"quotes {len(quotes)} < {self.settings.opening_impulse_min_quotes}"
                if len(quotes) < self.settings.opening_impulse_min_quotes
                else f"quote change {quote_change_pct:.3%} < {self.settings.opening_impulse_change_pct:.3%}"
            )
            return self._reject(state, "change", f"no bar/range signal and {quote_detail}")

        penalty = 0.0
        warnings = []

        if candidate.change_pct > self.settings.opening_impulse_skip_extended_pct:
            penalty += 1.0
            warnings.append(
                f"extended {candidate.change_pct:.3%} > {self.settings.opening_impulse_skip_extended_pct:.3%}"
            )

        spread_bps = last.spread_bps
        if spread_bps > self.settings.opening_impulse_max_spread_bps:
            penalty += 1.0
            warnings.append(f"wide spread {spread_bps:.2f}bps")

        if min(last.bid_size, last.ask_size) < self.settings.opening_impulse_min_quote_size:
            penalty += 0.5
            warnings.append(f"thin quote size {min(last.bid_size, last.ask_size)}")

        if quotes:
            negative_steps = self._negative_steps(quotes)
            if negative_steps > self.settings.opening_impulse_max_negative_steps:
                penalty += 1.0
                warnings.append(f"negative quote steps {negative_steps}")

            recent_high = max(quote.mid for quote in quotes)
            if last.mid < recent_high * (1 - self.settings.opening_impulse_retrace_from_high_pct):
                retrace_pct = (recent_high - last.mid) / recent_high
                penalty += 1.0
                warnings.append(f"quote retrace {retrace_pct:.3%}")

        if candidate.kind == "quote_impulse":
            if candidate.volume_ratio < self.settings.opening_impulse_volume_ratio:
                penalty += 1.0
                warnings.append(f"volume {candidate.volume_ratio:.2f}x")

        reason = candidate.reason
        if warnings:
            reason = f"{reason} | entry_warnings penalty={penalty:.1f}: {', '.join(warnings)}"

        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side="BUY",
            price=last.ask,
            timestamp_ms=last.timestamp_ms,
            change_pct=candidate.change_pct,
            volume_ratio=candidate.volume_ratio,
            spread_bps=spread_bps,
            reason=reason,
        )

    def should_exit(self, state: SymbolState, position) -> ExitDecision | None:
        if state.last_event_kind not in {"quote", "bar"} or position.strategy != self.name:
            return None

        price = state.last_price
        if price is None:
            return None

        event_ms = state.last_event_ms or (state.quote.timestamp_ms if state.quote else position.entry_ms)
        age_seconds = (event_ms - position.entry_ms) / 1000
        if age_seconds < self.exit_activation_delay_seconds(position):
            return None

        pnl_pct = (price - position.entry_price) / position.entry_price if position.entry_price > 0 else 0.0

        if pnl_pct >= 0.01:
            trailing_pct = 0.005 if pnl_pct < 0.02 else 0.003
            trailing_stop = position.max_price * (1 - trailing_pct)
            if price <= trailing_stop:
                return ExitDecision("trailing stop dynamic")

        if pnl_pct > 0 and position.last_high_ts and event_ms - position.last_high_ts > 60_000:
            return ExitDecision("momentum stall")

        bars = list(state.bars)[-max(5, self.settings.opening_impulse_bar_window) :]
        if len(bars) >= 2:
            recent_low = min(bar.low for bar in bars[:-1])
            if pnl_pct <= 0 and price < recent_low:
                return ExitDecision("break structure")

        quotes = self._recent_quotes(state, self.settings.opening_impulse_exit_window_seconds)
        if len(quotes) < self.settings.opening_impulse_exit_min_quotes:
            if pnl_pct <= self.settings.opening_impulse_early_loss_cut_pct:
                return ExitDecision("cut loss early")
            return None

        recent_changes = [quotes[index].mid - quotes[index - 1].mid for index in range(1, len(quotes))]
        negative_steps = sum(1 for change in recent_changes if change < 0)
        if pnl_pct <= 0 and negative_steps > self.settings.opening_impulse_exit_negative_steps:
            return ExitDecision("momentum fade")

        if pnl_pct <= self.settings.opening_impulse_early_loss_cut_pct:
            return ExitDecision("cut loss early")

        return None

    def exit_activation_delay_seconds(self, position) -> int:
        return self.settings.opening_impulse_min_hold_seconds

    def use_fixed_target_exit(self, position) -> bool:
        return False

    def _reject(self, state: SymbolState, code: str, detail: str) -> None:
        timestamp_ms = state.last_event_ms or 0
        key = (state.symbol, code)
        last_log_ms = self._last_reject_log_ms.get(key, -10_000)
        if timestamp_ms - last_log_ms >= 10_000:
            self._last_reject_log_ms[key] = timestamp_ms
            LOG.debug("No opening_impulse entry %s: %s", state.symbol, detail)
        return None

    def _within_trading_window(self, timestamp_ms: int | None) -> bool:
        if timestamp_ms is None:
            return False
        current = datetime.fromtimestamp(timestamp_ms / 1000, tz=self.market_tz)
        if current.hour >= self.settings.opening_impulse_last_entry_hour_et:
            return False
        minutes = current.hour * 60 + current.minute
        market_open = 9 * 60 + 30
        elapsed = minutes - market_open
        return self.settings.opening_impulse_start_minute <= elapsed <= self.settings.opening_impulse_end_minute

    @staticmethod
    def _recent_quotes(state: SymbolState, window_seconds: int) -> list:
        if not state.quotes:
            return []
        latest_ms = state.quotes[-1].timestamp_ms
        threshold = latest_ms - (window_seconds * 1000)
        return [quote for quote in state.quotes if quote.timestamp_ms >= threshold]

    @staticmethod
    def _latest_valid_quote(state: SymbolState):
        quote = state.quote or (state.quotes[-1] if state.quotes else None)
        if quote is None:
            return None
        if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
            return None
        return quote

    @staticmethod
    def _negative_steps(quotes: list) -> int:
        negative_steps = 0
        for index in range(1, len(quotes)):
            if quotes[index].mid < quotes[index - 1].mid:
                negative_steps += 1
        return negative_steps

    def _bar_impulse(self, state: SymbolState) -> EntryCandidate | None:
        if not self.settings.opening_impulse_bar_confirmation:
            return None

        window = max(2, self.settings.opening_impulse_bar_window)
        bars = list(state.bars)[-window:]
        if len(bars) < window:
            return None

        start_price = bars[0].open or bars[0].close
        end_price = bars[-1].close
        if start_price <= 0:
            return None

        change_pct = (end_price - start_price) / start_price
        if change_pct < self.settings.opening_impulse_bar_change_pct:
            return None

        rising_bars = 0
        for index, current in enumerate(bars):
            previous_close = bars[index - 1].close if index > 0 else current.open
            if current.close >= current.open or current.close > previous_close:
                rising_bars += 1
        if rising_bars < self.settings.opening_impulse_bar_min_rising:
            return None

        volume_ratio = self._volume_ratio(state)
        if volume_ratio < self.settings.opening_impulse_bar_volume_ratio:
            return None

        elapsed_seconds = max(60, (bars[-1].end_ms - bars[0].start_ms) / 1000)
        reason = (
            f"opening bar impulse {change_pct:.3%} over {elapsed_seconds:.0f}s, "
            f"{rising_bars}/{len(bars)} rising bars, volume {volume_ratio:.1f}x baseline"
        )
        return EntryCandidate(change_pct=change_pct, volume_ratio=volume_ratio, reason=reason, kind="bar_impulse")

    def _range_impulse(self, state: SymbolState, latest_quote) -> EntryCandidate | None:
        opening_range = self._opening_range(state)
        if opening_range is None:
            return None

        latest_bar = state.bars[-1] if state.bars else None
        if latest_bar is None or latest_bar.end_ms <= opening_range.end_ms:
            return None

        volume_ratio = self._volume_ratio(state)
        if volume_ratio < self.settings.opening_impulse_range_volume_ratio:
            return None

        if not self._bar_momentum(state):
            return None

        latest_mid = latest_quote.mid
        breakout_level = opening_range.high * (1 + self.settings.opening_impulse_range_breakout_buffer_pct)
        if self.settings.opening_impulse_enable_range_breakout and latest_mid >= breakout_level and latest_bar.close >= opening_range.high:
            change_pct = (latest_mid - opening_range.high) / opening_range.high
            reason = (
                f"opening_range_breakout {change_pct:.3%} above {opening_range.high:.2f}, "
                f"volume {volume_ratio:.1f}x baseline"
            )
            return EntryCandidate(
                change_pct=change_pct,
                volume_ratio=volume_ratio,
                reason=reason,
                kind="opening_range_breakout",
            )

        opening_drop_pct = (opening_range.open - opening_range.low) / opening_range.open if opening_range.open else 0.0
        reclaim_level = opening_range.midpoint * (1 + self.settings.opening_impulse_range_reclaim_buffer_pct)
        reclaimed_midpoint = latest_mid >= reclaim_level and latest_bar.close >= opening_range.midpoint
        if (
            self.settings.opening_impulse_enable_range_reversal
            and opening_drop_pct >= self.settings.opening_impulse_range_reversal_min_drop_pct
            and reclaimed_midpoint
        ):
            change_pct = (latest_mid - opening_range.midpoint) / opening_range.midpoint if opening_range.midpoint else 0.0
            reason = (
                f"opening_range_reversal reclaim after {opening_drop_pct:.3%} flush, "
                f"volume {volume_ratio:.1f}x baseline"
            )
            return EntryCandidate(
                change_pct=change_pct,
                volume_ratio=volume_ratio,
                reason=reason,
                kind="opening_range_reversal",
            )

        return None

    def _opening_range(self, state: SymbolState) -> OpeningRange | None:
        range_bars = []
        for bar in state.bars:
            start = datetime.fromtimestamp(bar.start_ms / 1000, tz=self.market_tz)
            end = datetime.fromtimestamp(bar.end_ms / 1000, tz=self.market_tz)
            if start.time() < MARKET_OPEN:
                continue
            minutes_from_open = ((end.hour * 60 + end.minute) - (MARKET_OPEN.hour * 60 + MARKET_OPEN.minute))
            if 0 < minutes_from_open <= self.settings.opening_impulse_range_minutes:
                range_bars.append(bar)

        if not range_bars:
            return None

        high = max(bar.high for bar in range_bars)
        low = min(bar.low for bar in range_bars)
        return OpeningRange(
            open=range_bars[0].open,
            high=high,
            low=low,
            midpoint=(high + low) / 2,
            volume=sum(bar.volume for bar in range_bars),
            start_ms=range_bars[0].start_ms,
            end_ms=range_bars[-1].end_ms,
        )

    def _bar_momentum(self, state: SymbolState) -> bool:
        window = max(2, self.settings.opening_impulse_bar_window)
        bars = list(state.bars)[-window:]
        if len(bars) < window:
            return False
        rising_bars = 0
        for index, current in enumerate(bars):
            previous_close = bars[index - 1].close if index > 0 else current.open
            if current.close >= current.open or current.close > previous_close:
                rising_bars += 1
        return rising_bars >= self.settings.opening_impulse_bar_min_rising

    @staticmethod
    def _volume_ratio(state: SymbolState) -> float:
        if len(state.bars) < 2:
            return 0.0
        latest_volume = state.bars[-1].volume
        baseline = median([bar.volume for bar in list(state.bars)[:-1] if bar.volume > 0] or [1])
        return latest_volume / baseline if baseline else 0.0
