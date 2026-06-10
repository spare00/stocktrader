from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass
from typing import Iterable

from models import Bar, Quote, Trade


LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Selection:
    symbol: str
    reason: str
    move_pct: float
    dollar_volume: float
    rvol: float
    spread_bps: float
    timestamp_ms: int
    score: float = 0.0


class DynamicMoverSelector:
    """Promotes strong intraday movers from a constrained candidate universe."""

    def __init__(
        self,
        symbols: Iterable[str],
        *,
        lookback_minutes: int = 5,
        min_move_pct: float = 0.02,
        min_dollar_volume: float = 1_000_000.0,
        min_rvol: float = 3.0,
        max_spread_bps: float = 80.0,
        cooldown_seconds: int = 1800,
        max_dynamic_symbols: int = 10,
        symbol_ttl_minutes: int = 30,
    ):
        self.symbols = {symbol.strip().upper() for symbol in symbols if symbol.strip()}
        self.lookback_ms = max(1, int(lookback_minutes)) * 60_000
        self.min_move_pct = max(0.0, float(min_move_pct))
        self.min_dollar_volume = max(0.0, float(min_dollar_volume))
        self.min_rvol = max(0.0, float(min_rvol))
        self.max_spread_bps = max(0.0, float(max_spread_bps))
        self.cooldown_ms = max(0, int(cooldown_seconds)) * 1000
        self.max_dynamic_symbols = max(0, int(max_dynamic_symbols))
        self.ttl_ms = max(0, int(symbol_ttl_minutes)) * 60_000

        self._bars: dict[str, deque[Bar]] = {}
        self._trades: dict[str, deque[Trade]] = {}
        self._quotes: dict[str, Quote] = {}
        self._last_selected_ms: dict[str, int] = {}
        self._active_until_ms: dict[str, int] = {}
        self._last_rejection_log_ms: dict[tuple[str, str], int] = {}

    @property
    def active_symbols(self) -> set[str]:
        return set(self._active_until_ms)

    def record_bar(self, bar: Bar) -> None:
        symbol = bar.symbol.strip().upper()
        if symbol not in self.symbols:
            return
        bars = self._bars.setdefault(symbol, deque())
        bars.append(bar)
        cutoff = bar.end_ms - max(self.lookback_ms * 12, 60 * 60_000)
        while bars and bars[0].end_ms < cutoff:
            bars.popleft()

    def record_trade(self, trade: Trade) -> None:
        symbol = trade.symbol.strip().upper()
        if symbol not in self.symbols or trade.price <= 0 or trade.size <= 0:
            return
        trades = self._trades.setdefault(symbol, deque())
        trades.append(trade)
        cutoff = trade.timestamp_ms - self.lookback_ms
        while trades and trades[0].timestamp_ms < cutoff:
            trades.popleft()

    def record_quote(self, quote: Quote) -> None:
        symbol = quote.symbol.strip().upper()
        if symbol in self.symbols:
            self._quotes[symbol] = quote

    def record_event(self, event) -> Selection | None:
        symbol = getattr(event, "symbol", "").strip().upper()
        if isinstance(event, Bar):
            self.record_bar(event)
        elif isinstance(event, Trade):
            self.record_trade(event)
        elif isinstance(event, Quote):
            self.record_quote(event)
        else:
            return None
        return self.evaluate(symbol)

    def ranked_candidates(self, timestamp_ms: int, *, allow_missing_spread: bool = False) -> list[Selection]:
        candidates: list[Selection] = []
        for symbol in sorted(self.symbols):
            selection = self.evaluate(
                symbol,
                timestamp_ms=timestamp_ms,
                allow_missing_spread=allow_missing_spread,
                reserve=False,
            )
            if selection is not None:
                candidates.append(selection)
        return sorted(candidates, key=lambda selection: selection.score, reverse=True)

    def evaluate(
        self,
        symbol: str,
        *,
        timestamp_ms: int | None = None,
        allow_missing_spread: bool = False,
        reserve: bool = True,
    ) -> Selection | None:
        normalized = symbol.strip().upper()
        if normalized not in self.symbols:
            return None
        now_ms = timestamp_ms or self._latest_timestamp_ms(normalized)
        if now_ms <= 0:
            return None

        expired = self.expire(now_ms)
        for expired_symbol in expired:
            LOG.info("Dynamic mover TTL expired symbol=%s", expired_symbol)

        if normalized in self._active_until_ms:
            self._log_rejection(normalized, "active", "Dynamic mover skipped: already active symbol=%s", now_ms)
            return None
        if self.max_dynamic_symbols and len(self._active_until_ms) >= self.max_dynamic_symbols:
            self._log_rejection(
                normalized,
                "max_dynamic",
                "Dynamic mover rejected: max dynamic symbols reached symbol=%s active=%d limit=%d",
                now_ms,
                len(self._active_until_ms),
                self.max_dynamic_symbols,
            )
            return None
        last_selected = self._last_selected_ms.get(normalized)
        if last_selected is not None and now_ms - last_selected < self.cooldown_ms:
            self._log_rejection(normalized, "cooldown", "Dynamic mover skipped: cooldown symbol=%s", now_ms)
            return None

        metrics = self._metrics(normalized, now_ms)
        if metrics is None:
            self._log_rejection(normalized, "insufficient_data", "Dynamic mover rejected: insufficient data symbol=%s", now_ms)
            return None
        move_pct, dollar_volume, rvol = metrics
        if move_pct < self.min_move_pct:
            self._log_rejection(
                normalized,
                "move",
                "Dynamic mover rejected: move too small symbol=%s move_pct=%.4f min=%.4f",
                now_ms,
                move_pct,
                self.min_move_pct,
            )
            return None
        if dollar_volume < self.min_dollar_volume:
            self._log_rejection(
                normalized,
                "dollar_volume",
                "Dynamic mover rejected: dollar volume too low symbol=%s dollar_volume=%.0f min=%.0f",
                now_ms,
                dollar_volume,
                self.min_dollar_volume,
            )
            return None
        if rvol < self.min_rvol:
            self._log_rejection(
                normalized,
                "rvol",
                "Dynamic mover rejected: RVOL too low symbol=%s rvol=%.2f min=%.2f",
                now_ms,
                rvol,
                self.min_rvol,
            )
            return None

        spread_bps = self._spread_bps(normalized)
        if not math.isfinite(spread_bps):
            if allow_missing_spread:
                spread_bps = self.max_spread_bps
            else:
                self._log_rejection(
                    normalized,
                    "spread_missing",
                    "Dynamic mover rejected: missing spread symbol=%s",
                    now_ms,
                )
                return None
        if spread_bps > self.max_spread_bps:
            self._log_rejection(
                normalized,
                "spread",
                "Dynamic mover rejected: spread too wide symbol=%s spread_bps=%.1f max=%.1f",
                now_ms,
                spread_bps,
                self.max_spread_bps,
            )
            return None

        score = self._score(move_pct, dollar_volume, rvol, spread_bps)
        selection = Selection(
            symbol=normalized,
            reason=(
                f"{self.lookback_ms // 60000}m move {move_pct * 100:.1f}%, "
                f"rvol {rvol:.1f}, dollar_volume {dollar_volume:.0f}"
            ),
            move_pct=round(move_pct, 6),
            dollar_volume=round(dollar_volume, 2),
            rvol=round(rvol, 4),
            spread_bps=round(spread_bps, 2),
            timestamp_ms=now_ms,
            score=round(score, 4),
        )
        if reserve:
            self.confirm_selection(selection)
        return selection

    def confirm_selection(self, selection: Selection) -> None:
        self._last_selected_ms[selection.symbol] = selection.timestamp_ms
        if self.ttl_ms > 0:
            self._active_until_ms[selection.symbol] = selection.timestamp_ms + self.ttl_ms
        else:
            self._active_until_ms[selection.symbol] = selection.timestamp_ms

    def expire(self, timestamp_ms: int) -> list[str]:
        expired = [
            symbol
            for symbol, active_until_ms in self._active_until_ms.items()
            if active_until_ms <= timestamp_ms
        ]
        for symbol in expired:
            self._active_until_ms.pop(symbol, None)
        return sorted(expired)

    def _metrics(self, symbol: str, now_ms: int) -> tuple[float, float, float] | None:
        bars = self._bars.get(symbol, deque())
        if not bars:
            return None
        cutoff = now_ms - self.lookback_ms
        recent_bars = [bar for bar in bars if bar.end_ms >= cutoff and bar.close > 0]
        if len(recent_bars) < 2:
            return None
        start_price = recent_bars[0].open if recent_bars[0].open > 0 else recent_bars[0].close
        end_price = recent_bars[-1].close
        if start_price <= 0 or end_price <= 0:
            return None
        move_pct = (end_price - start_price) / start_price
        dollar_volume = sum((bar.vwap if bar.vwap > 0 else bar.close) * max(0.0, bar.volume) for bar in recent_bars)
        trade_dollar_volume = sum(trade.price * trade.size for trade in self._trades.get(symbol, deque()))
        dollar_volume = max(dollar_volume, trade_dollar_volume)
        recent_volume = sum(max(0.0, bar.volume) for bar in recent_bars)
        baseline_volume = self._baseline_lookback_volume(symbol, cutoff)
        if baseline_volume <= 0:
            return None
        rvol = recent_volume / baseline_volume
        return move_pct, dollar_volume, rvol

    def _baseline_lookback_volume(self, symbol: str, cutoff_ms: int) -> float:
        older = [bar for bar in self._bars.get(symbol, deque()) if bar.end_ms < cutoff_ms and bar.volume > 0]
        if not older:
            return 0.0
        bucket_count = max(1, round((older[-1].end_ms - older[0].end_ms) / self.lookback_ms))
        return sum(bar.volume for bar in older) / bucket_count

    def _spread_bps(self, symbol: str) -> float:
        quote = self._quotes.get(symbol)
        if quote is None or quote.bid <= 0 or quote.ask <= 0:
            return float("inf")
        return quote.spread_bps

    def _latest_timestamp_ms(self, symbol: str) -> int:
        values = []
        if self._bars.get(symbol):
            values.append(self._bars[symbol][-1].end_ms)
        if self._trades.get(symbol):
            values.append(self._trades[symbol][-1].timestamp_ms)
        quote = self._quotes.get(symbol)
        if quote is not None:
            values.append(quote.timestamp_ms)
        return max(values) if values else 0

    def _score(self, move_pct: float, dollar_volume: float, rvol: float, spread_bps: float) -> float:
        move_score = (move_pct / self.min_move_pct) if self.min_move_pct > 0 else move_pct * 100
        dollar_score = math.log10(max(1.0, dollar_volume / max(1.0, self.min_dollar_volume))) + 1.0
        rvol_score = (rvol / self.min_rvol) if self.min_rvol > 0 else rvol
        spread_penalty = spread_bps / max(1.0, self.max_spread_bps)
        return move_score + dollar_score + rvol_score - spread_penalty

    def _log_rejection(self, symbol: str, reason: str, message: str, now_ms: int, *args) -> None:
        key = (symbol, reason)
        last_ms = self._last_rejection_log_ms.get(key)
        if last_ms is not None and now_ms - last_ms < 60_000:
            return
        self._last_rejection_log_ms[key] = now_ms
        LOG.info(message, symbol, *args)
