from statistics import median

from config import Settings
from candle import SymbolState
from models import Bar, Signal


class SpikeStrategy:
    def __init__(self, settings: Settings):
        self.settings = settings

    def evaluate(self, state: SymbolState) -> Signal | None:
        lookback = self.settings.spike_lookback_seconds
        if len(state.bars) <= lookback:
            return None

        last = state.bars[-1]
        prior = state.bars[-lookback - 1]
        change_pct = (last.close - prior.close) / prior.close
        if abs(change_pct) < self.settings.spike_change_pct:
            return None

        volume_ratio = self._volume_ratio(list(state.bars)[-lookback - 1 : -1], last)
        if volume_ratio < self.settings.volume_ratio:
            return None

        spread_bps = state.quote.spread_bps if state.quote else None
        if spread_bps is not None and spread_bps > self.settings.max_spread_bps:
            return None

        side = "BUY" if change_pct > 0 else "SELL"
        return Signal(
            symbol=state.symbol,
            side=side,
            price=last.close,
            timestamp_ms=last.end_ms,
            change_pct=change_pct,
            volume_ratio=volume_ratio,
            spread_bps=spread_bps,
            reason=(
                f"{lookback}s move {change_pct:.3%}, "
                f"volume {volume_ratio:.1f}x baseline"
            ),
        )

    @staticmethod
    def _volume_ratio(previous: list[Bar], last: Bar) -> float:
        baseline = median([bar.volume for bar in previous if bar.volume > 0] or [1])
        return last.volume / baseline if baseline else 0.0
