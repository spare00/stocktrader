from statistics import median

from candle import SymbolState
from config import Settings
from models import Bar, Signal
from strategies.base import Strategy


class SpikeStrategy(Strategy):
    name = "spike"

    def __init__(self, settings: Settings):
        self.settings = settings

    def evaluate(self, state: SymbolState) -> Signal | None:
        if state.last_event_kind != "bar":
            return None

        lookback = self.settings.spike_lookback_seconds
        threshold_ms = state.bars[-1].end_ms - (lookback * 1000)
        last = state.bars[-1]
        prior_index = self._prior_index(list(state.bars), threshold_ms)
        if prior_index is None:
            return None

        bars = list(state.bars)
        prior = bars[prior_index]
        elapsed_seconds = (last.end_ms - prior.end_ms) / 1000
        if elapsed_seconds > lookback:
            return None

        change_pct = (last.close - prior.close) / prior.close
        if abs(change_pct) < self.settings.spike_change_pct:
            return None

        volume_ratio = self._volume_ratio(bars[prior_index:-1], last)
        if volume_ratio < self.settings.volume_ratio:
            return None

        spread_bps = state.quote.spread_bps if state.quote else None
        if spread_bps is not None and spread_bps > self.settings.max_spread_bps:
            return None

        side = "BUY" if change_pct > 0 else "SELL"
        return Signal(
            strategy=self.name,
            symbol=state.symbol,
            side=side,
            price=last.close,
            timestamp_ms=last.end_ms,
            change_pct=change_pct,
            volume_ratio=volume_ratio,
            spread_bps=spread_bps,
            reason=f"{elapsed_seconds:.0f}s move {change_pct:.3%}, volume {volume_ratio:.1f}x baseline",
        )

    @staticmethod
    def _volume_ratio(previous: list[Bar], last: Bar) -> float:
        baseline = median([bar.volume for bar in previous if bar.volume > 0] or [1])
        return last.volume / baseline if baseline else 0.0

    @staticmethod
    def _prior_index(bars: list[Bar], threshold_ms: int) -> int | None:
        for index in range(len(bars) - 2, -1, -1):
            if bars[index].end_ms <= threshold_ms:
                return index
        return None
