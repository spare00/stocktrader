from __future__ import annotations

import re
from dataclasses import dataclass
from statistics import mean

from candle import SymbolState
from config import Settings
from models import Signal


MARKET_REGIME_RE = re.compile(r"\bmarket_regime\s+[a-z_]+\b")


@dataclass(frozen=True)
class MarketRegime:
    name: str
    score: float
    max_score: float
    allow_new_entries: bool
    position_size_multiplier: float
    reason: str


class MarketRegimeMonitor:
    """Small, auditable market-regime gate built from broad ETF intraday bars."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._last_name: str | None = None

    def evaluate(self, states: dict[str, SymbolState]) -> MarketRegime:
        if not self.settings.market_regime_enabled:
            return MarketRegime("disabled", 0, 0, True, 1.0, "market regime disabled")

        symbols = [symbol.upper() for symbol in self.settings.market_regime_symbols if symbol]
        if not symbols:
            return MarketRegime("neutral", 0, 0, True, 1.0, "no market regime symbols configured")

        score = 0.0
        max_score = 0.0
        details: list[str] = []
        for symbol in symbols:
            state = states.get(symbol)
            bars = list(state.bars) if state is not None else []
            if len(bars) < self.settings.market_regime_min_bars:
                details.append(f"{symbol}:insufficient")
                continue

            current = bars[-1]
            session_vwap = self._session_vwap(bars)
            prev_vwap = self._session_vwap(bars[:-5]) if len(bars) > 5 else None
            ema20 = self._ema([bar.close for bar in bars], 20)
            symbol_score = 0.0
            symbol_weight = self._symbol_weight(symbol)

            if session_vwap is not None:
                symbol_score += self._condition_score(
                    current.close >= session_vwap,
                    self.settings.market_regime_below_vwap_weight,
                )
            if session_vwap is not None and prev_vwap is not None:
                symbol_score += self._condition_score(
                    session_vwap > prev_vwap,
                    self.settings.market_regime_vwap_falling_weight,
                )
            if ema20 is not None:
                symbol_score += self._condition_score(
                    current.close >= ema20,
                    self.settings.market_regime_below_ema_weight,
                )

            weighted_score = symbol_score * symbol_weight
            score += weighted_score
            max_score += self.settings.market_regime_positive_weight * 3 * symbol_weight
            details.append(f"{symbol}:{self._fmt_score(weighted_score)}")

        if max_score <= 0:
            return MarketRegime("neutral", 0, 0, True, 1.0, "market regime warmup")

        if score <= self.settings.market_regime_block_score:
            name = "panic"
            allow = False
            size_mult = 0.0
        elif score <= self.settings.market_regime_risk_off_score:
            name = "risk_off"
            allow = True
            size_mult = self.settings.market_regime_risk_off_size_multiplier
        elif score >= self.settings.market_regime_risk_on_score:
            name = "risk_on"
            allow = True
            size_mult = self.settings.market_regime_risk_on_size_multiplier
        else:
            name = "neutral"
            allow = True
            size_mult = 1.0

        reason = f"{name} score={self._fmt_score(score)}/{self._fmt_score(max_score)} {' '.join(details)}"
        return MarketRegime(name, score, max_score, allow, max(0.0, size_mult), reason)

    def apply_to_signal(self, signal: Signal, regime: MarketRegime) -> tuple[Signal | None, str | None]:
        if self.strategy_bypasses(signal.strategy):
            return self._signal_with_market_regime(signal, self.regime_for_strategy(regime, signal.strategy)), None
        if not self.settings.market_regime_enabled or regime.name in {"disabled", "bypassed", "neutral"}:
            return self._signal_with_market_regime(signal, regime), None
        if not regime.allow_new_entries:
            return None, f"market regime {regime.reason}"
        if regime.position_size_multiplier == 1.0:
            return self._signal_with_market_regime(signal, regime), None
        return self._signal_with_market_regime(signal, regime, regime.position_size_multiplier), None

    def _signal_with_market_regime(
        self,
        signal: Signal,
        regime: MarketRegime,
        size_multiplier: float = 1.0,
    ) -> Signal:
        if MARKET_REGIME_RE.search(signal.reason):
            reason = signal.reason
        else:
            suffix = f" | market_regime {regime.reason}"
            if size_multiplier != 1.0:
                suffix += f" size_mult={size_multiplier:.2f}"
            reason = f"{signal.reason}{suffix}"
        adjusted = Signal(
            strategy=signal.strategy,
            symbol=signal.symbol,
            side=signal.side,
            price=signal.price,
            timestamp_ms=signal.timestamp_ms,
            change_pct=signal.change_pct,
            volume_ratio=signal.volume_ratio,
            spread_bps=signal.spread_bps,
            reason=reason,
            stop_price=signal.stop_price,
            session_open_price=signal.session_open_price,
            entry_open_pct=signal.entry_open_pct,
            position_size_multiplier=signal.position_size_multiplier * size_multiplier,
            runner_mode=signal.runner_mode,
            allow_add_to_position=signal.allow_add_to_position,
        )
        return adjusted

    def regime_for_strategy(self, regime: MarketRegime, strategy_name: str) -> MarketRegime:
        if not self.strategy_bypasses(strategy_name):
            return regime
        return MarketRegime(
            "bypassed",
            regime.score,
            regime.max_score,
            True,
            1.0,
            f"bypassed for {strategy_name}; actual {regime.reason}",
        )

    def strategy_bypasses(self, strategy_name: str) -> bool:
        normalized = strategy_name.strip().lower()
        return normalized in {
            str(name).strip().lower()
            for name in self.settings.market_regime_bypass_strategies
            if str(name).strip()
        }

    def _condition_score(self, positive: bool, negative_weight: float) -> float:
        if positive:
            return max(0.0, self.settings.market_regime_positive_weight)
        return -max(0.0, negative_weight)

    def _symbol_weight(self, symbol: str) -> float:
        weights = {
            "SPY": self.settings.market_regime_spy_weight,
            "QQQ": self.settings.market_regime_qqq_weight,
            "IWM": self.settings.market_regime_iwm_weight,
        }
        return max(0.0, weights.get(symbol.upper(), self.settings.market_regime_default_symbol_weight))

    @staticmethod
    def _fmt_score(value: float) -> str:
        return f"{value:.2f}".rstrip("0").rstrip(".")

    def should_log_change(self, regime: MarketRegime) -> bool:
        if not self.settings.market_regime_log_changes:
            return False
        changed = regime.name != self._last_name
        self._last_name = regime.name
        return changed

    @staticmethod
    def _session_vwap(bars) -> float | None:
        total_volume = sum(bar.volume for bar in bars if bar.volume > 0)
        if total_volume <= 0:
            return None
        total_value = sum(bar.vwap * bar.volume for bar in bars if bar.volume > 0)
        return total_value / total_volume if total_value > 0 else None

    @staticmethod
    def _ema(values: list[float], period: int) -> float | None:
        if len(values) < period or period <= 0:
            return None
        alpha = 2 / (period + 1)
        ema = mean(values[:period])
        for value in values[period:]:
            ema = (value * alpha) + (ema * (1 - alpha))
        return ema
