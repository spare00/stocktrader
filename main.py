import asyncio
import argparse
from dataclasses import replace
from datetime import datetime, time as dt_time, timedelta
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ai_agent import SignalReviewer
from alpaca_stream import (
    AlpacaStreamAuthError,
    AlpacaStreamConnectionLimitError,
    AlpacaStreamEndedError,
    AlpacaRestPollingStream,
    build_market_data_stream,
    max_stream_trade_quote_symbols,
    MergedMarketDataStream,
    replay_clock_utc,
    stream_trade_quote_channel_count,
)
from alpaca.data.timeframe import TimeFrame
from alpaca_client import get_bars_between, get_latest_quotes, get_recent_bars, make_clients
from candle import SymbolState
from config import Settings, load_settings
from execution import build_executor, set_source_commit
from market_hours import MARKET_TZ, is_regular_market_time
from market_regime import MarketRegimeMonitor
from modules.dynamic_execution_selector import DynamicExecutionStrengthSelector, load_candidate_symbols
from modules.dynamic_mover_selector import DynamicMoverSelector, Selection as DynamicMoverSelection
from modules.news_listener import NewsListener
from modules.symbol_manager import SymbolManager
from models import Bar, Heartbeat, NewsEvent, Quote, Trade
from opening_plan import (
    default_plan_file_for_strategy,
    default_plan_file_for_settings,
    load_opening_plan,
    parse_plan_symbols,
    plan_overrides,
    selector_command_for_strategy,
    symbols_env_blocks_plan,
)
from risk import RiskManager
from runtime_safety import flatten_on_shutdown, manage_all_exits
from strategies import available_strategy_names, build_strategies
from strategies.registry import diagnostic_loggers_for, merge_strategy_runtime_snapshots, strategies_requiring_trade_ticks


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s | %(message)s"
LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "trader.log"
NOISY_LOGGERS = (
    "alpaca",
    "alpaca.data.live.websocket",
    "websockets",
    "websockets.client",
)
ALPACA_STREAM_LOGGER = "alpaca.data.live.websocket"
POSITIVE_NEWS_TERMS = (
    "beats",
    "beats estimate",
    "beats estimates",
    "beat",
    "beat estimate",
    "beat estimates",
    "raises guidance",
    "raises price target",
    "raised price target",
    "price target raised",
    "maintains outperform",
    "maintains overweight",
    "reiterates outperform",
    "reiterates buy",
    "outperform",
    "overweight",
    "affirms guidance",
    "affirms fy",
    "upgrades",
    "upgrade",
    "surge",
    "jumps",
    "soars",
    "acquisition",
    "contract win",
    "approval",
    "launches",
    "strong earnings",
    "record revenue",
)
NEGATIVE_NEWS_TERMS = (
    "misses",
    "miss",
    "cuts guidance",
    "lowers price target",
    "lowered price target",
    "price target lowered",
    "underperform",
    "underweight",
    "downgrade",
    "downgrades",
    "offering",
    "dilution",
    "investigation",
    "lawsuit",
    "recall",
    "bankruptcy",
    "plunges",
    "falls",
    "weak earnings",
    "contract terminated",
    "terminated",
)


class FriendlyAlpacaStreamErrorFilter(logging.Filter):
    def __init__(self, min_interval_seconds: float = 60.0):
        super().__init__()
        self.min_interval_seconds = min_interval_seconds
        self._last_logged: dict[str, float] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != ALPACA_STREAM_LOGGER or "error during websocket communication" not in record.getMessage():
            return True

        detail = self._error_detail(record)
        key = "dns" if self._is_dns_error(detail) else "websocket"
        now = time.monotonic()
        last_logged = self._last_logged.get(key)
        if last_logged is not None and now - last_logged < self.min_interval_seconds:
            return False
        self._last_logged[key] = now

        if key == "dns":
            record.msg = (
                "Alpaca market-data stream connection problem: cannot resolve Alpaca's data host. "
                "Check internet/DNS/VPN; the bot will not receive live quotes until the stream reconnects. "
                "Detail: %s"
            )
        else:
            record.msg = (
                "Alpaca market-data stream interrupted. The bot may miss live quotes until the stream reconnects. "
                "Detail: %s"
            )
        record.args = (detail,)
        record.exc_info = None
        record.exc_text = None
        return True

    @staticmethod
    def _error_detail(record: logging.LogRecord) -> str:
        if record.exc_info and record.exc_info[1]:
            return str(record.exc_info[1]) or record.exc_info[1].__class__.__name__
        message = record.getMessage().replace("error during websocket communication", "").strip(": ")
        return message or "unknown stream error"

    @staticmethod
    def _is_dns_error(detail: str) -> bool:
        detail_lower = detail.lower()
        return (
            "nodename nor servname provided" in detail_lower
            or "name or service not known" in detail_lower
            or "temporary failure in name resolution" in detail_lower
        )


class FatalAlpacaStreamErrorFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != ALPACA_STREAM_LOGGER or "error during websocket communication" not in record.getMessage():
            return True
        detail = FriendlyAlpacaStreamErrorFilter._error_detail(record)
        if "connection limit exceeded" in detail.lower():
            raise AlpacaStreamConnectionLimitError(
                "Alpaca data stream connection limit exceeded for this API key/feed. "
                "Confirm this runner is using the intended .env key, stop other streams using that same key/feed, "
                "or set ALPACA_MARKET_DATA_MODE=rest for a polling runner."
            )
        return True


class RejectionLogThrottler:
    def __init__(self, min_interval_seconds: float = 30.0):
        self.min_interval_seconds = min_interval_seconds
        self._last_logged: dict[tuple[str, str, str, str], float] = {}

    def should_log(self, symbol: str, side: str, strategy: str, reason: str) -> bool:
        key = (symbol, side, strategy, reason)
        now = time.monotonic()
        last_logged = self._last_logged.get(key)
        if last_logged is not None and now - last_logged < self.min_interval_seconds:
            return False
        self._last_logged[key] = now
        return True


class HeartbeatReporter:
    def __init__(self, min_interval_seconds: float = 300.0):
        self.min_interval_seconds = min_interval_seconds
        self._last_logged = 0.0
        self._events = {"quotes": 0, "bars": 0, "heartbeats": 0}
        self._signals: dict[str, int] = {}
        self._entries: dict[str, int] = {}
        self._rejections: dict[str, int] = {}
        self._rejection_reasons: dict[str, dict[str, int]] = {}

    def record_quote(self) -> None:
        self._events["quotes"] += 1

    def record_bar(self) -> None:
        self._events["bars"] += 1

    def record_trade(self) -> None:
        self._events["trades"] = self._events.get("trades", 0) + 1

    def record_heartbeat(self) -> None:
        self._events["heartbeats"] += 1

    def record_news(self) -> None:
        self._events["news"] = self._events.get("news", 0) + 1

    def record_signal(self, strategy: str) -> None:
        self._signals[strategy] = self._signals.get(strategy, 0) + 1

    def record_entry(self, strategy: str) -> None:
        self._entries[strategy] = self._entries.get(strategy, 0) + 1

    def record_rejection(self, strategy: str, reason: str) -> None:
        self._rejections[strategy] = self._rejections.get(strategy, 0) + 1
        reasons = self._rejection_reasons.setdefault(strategy, {})
        reasons[reason] = reasons.get(reason, 0) + 1

    def should_log(self) -> bool:
        now = time.monotonic()
        if now - self._last_logged < self.min_interval_seconds:
            return False
        self._last_logged = now
        return True

    def emit(self, settings, states: dict[str, SymbolState], executor) -> None:
        if not self.should_log():
            return

        latest_event_ms = max((state.last_event_ms or 0) for state in states.values()) if states else 0
        active_symbols = sum(1 for state in states.values() if state.last_event_ms is not None)
        effective_mode = effective_market_data_mode(settings)
        payload = {
            "market_data_mode": effective_mode,
            "watched_symbols": len(states),
            "active_symbols": active_symbols,
            "open_positions": sorted(executor.open_symbols()),
            "events": dict(self._events),
            "strategies": {},
        }
        configured_mode = settings.alpaca_market_data_mode
        if configured_mode != effective_mode:
            payload["market_data_mode_config"] = configured_mode
        if latest_event_ms:
            clock_ms = int(replay_clock_utc(settings).timestamp() * 1000)
            payload["latest_event_age_seconds"] = round((clock_ms - latest_event_ms) / 1000, 1)

        for strategy_name in settings.strategy_names:
            reasons = self._rejection_reasons.get(strategy_name, {})
            top_reasons = sorted(reasons.items(), key=lambda item: (-item[1], item[0]))[:3]
            payload["strategies"][strategy_name] = {
                "signals": self._signals.get(strategy_name, 0),
                "entries": self._entries.get(strategy_name, 0),
                "rejections": self._rejections.get(strategy_name, 0),
                "top_rejections": top_reasons,
            }

        logging.info("Heartbeat %s", json.dumps(payload, sort_keys=True))
        self._events = {"quotes": 0, "bars": 0, "heartbeats": 0, "news": 0, "trades": 0}
        self._signals.clear()
        self._entries.clear()
        self._rejections.clear()
        self._rejection_reasons.clear()


def mark_prices(states: dict[str, SymbolState]) -> dict[str, float]:
    return {symbol: state.last_price for symbol, state in states.items() if state.last_price is not None}


def is_high_impact_news(headline: str, summary: str = "") -> bool:
    text = f"{headline} {summary}".lower()
    if not text.strip():
        return False
    # Negative-first guard prevents "contract win" style positives
    # from accepting clearly negative headlines like "contract terminated".
    if any(term in text for term in NEGATIVE_NEWS_TERMS):
        return False
    if any(term in text for term in POSITIVE_NEWS_TERMS):
        return True
    return False


def should_mark_hot_from_news(settings, event: NewsEvent) -> bool:
    return is_high_impact_news(event.headline, event.summary)


def should_expand_symbols_from_news(event: NewsEvent) -> bool:
    return is_regular_market_time(event.timestamp_ms)


def warm_dynamic_news_symbol(settings, state: SymbolState, symbol: str, *, bar_limit: int | None = None) -> bool:
    """Backfill enough market context for a symbol discovered from news to be tradable quickly."""
    if settings.replay_market_data:
        return False

    limit = bar_limit if bar_limit is not None else max(5, settings.indicator_preload_bars)
    warmed = False
    try:
        bars = get_recent_bars(settings, [symbol], limit=limit).get(symbol, [])
    except Exception:
        logging.debug("Could not warm recent bars for news symbol %s", symbol, exc_info=True)
        bars = []
    for bar in bars:
        if all(existing.start_ms != bar.start_ms for existing in state.bars):
            state.add_bar(bar)
            warmed = True

    try:
        quote = get_latest_quotes(settings, [symbol]).get(symbol)
    except Exception:
        logging.debug("Could not warm latest quote for news symbol %s", symbol, exc_info=True)
        quote = None
    if quote is not None:
        state.update_quote(quote)
        warmed = True

    return warmed


def _add_unique_bars(target: dict[str, list[Bar]], bars_map: dict[str, list[Bar]]) -> None:
    for symbol, bars in bars_map.items():
        existing = {bar.start_ms for bar in target.setdefault(symbol, [])}
        for bar in bars:
            if bar.start_ms in existing:
                continue
            target[symbol].append(bar)
            existing.add(bar.start_ms)


def _symbols_needing_bars(bars_map: dict[str, list[Bar]], symbols: list[str], target_count: int) -> list[str]:
    return [symbol for symbol in symbols if len({bar.start_ms for bar in bars_map.get(symbol, [])}) < target_count]


def _session_window(day, *, current_day_end: datetime | None = None) -> tuple[datetime, datetime]:
    start = datetime.combine(day, dt_time(4, 0), tzinfo=MARKET_TZ)
    end = datetime.combine(day, dt_time(16, 0), tzinfo=MARKET_TZ)
    if current_day_end is not None:
        end = min(end, current_day_end)
    return start, end


def _fetch_indicator_backfill_sessions(
    clients,
    symbols: list[str],
    now: datetime,
    target_count: int,
    *,
    max_calendar_days: int = 10,
) -> dict[str, list[Bar]]:
    bars_map: dict[str, list[Bar]] = {symbol: [] for symbol in symbols}
    if target_count <= 0 or not symbols:
        return bars_map
    if max_calendar_days <= 0:
        return bars_map

    for day_offset in range(max_calendar_days):
        needed = _symbols_needing_bars(bars_map, symbols, target_count)
        if not needed:
            break

        day = now.date() - timedelta(days=day_offset)
        start, end = _session_window(day, current_day_end=now if day_offset == 0 else None)
        if end <= start:
            continue

        session_bars = get_bars_between(clients, needed, TimeFrame.Minute, start, end)
        _add_unique_bars(bars_map, session_bars)

    for bars in bars_map.values():
        bars.sort(key=lambda bar: bar.start_ms)
    return bars_map


def _required_preload_bars_for_settings(settings: Settings, limit: int) -> int:
    required = 0
    if "stoch_macd_reversal" in settings.strategy_names:
        required = max(required, settings.stoch_macd_macd_warmup_bars)
    if "macd_early_impulse" in settings.strategy_names:
        required = max(required, settings.macd_macd_warmup_bars)
    if "breakout_power" in settings.strategy_names:
        required = max(required, settings.bp_warmup_bars)
    return min(limit, required) if required > 0 else 0


def _indicator_preload_end_time(settings: Settings, states: dict[str, SymbolState]) -> datetime:
    if settings.replay_market_data:
        latest_event_ms = max((state.last_event_ms or 0) for state in states.values()) if states else 0
        if latest_event_ms > 0:
            return datetime.fromtimestamp(latest_event_ms / 1000, tz=MARKET_TZ)
    return datetime.now(tz=MARKET_TZ)


def preload_indicator_bars_for_states(settings: Settings, states: dict[str, SymbolState]) -> dict[str, int]:
    """Append recent minute bars into each symbol state for continuous indicator warmup."""
    counts: dict[str, int] = {symbol: 0 for symbol in states}
    replay_data_base_url = bool((settings.alpaca_data_base_url or "").strip())
    if settings.replay_market_data and not replay_data_base_url:
        return counts
    if settings.indicator_preload_bars <= 0 or not states:
        return counts
    if not settings.alpaca_api_key or not settings.alpaca_secret_key:
        logging.info("Indicator preload skipped: missing Alpaca credentials")
        return counts

    limit = min(settings.indicator_preload_bars, settings.indicator_max_bars_per_symbol)
    if limit <= 0:
        return counts

    symbols = list(states.keys())
    bars_map: dict[str, list[Bar]] = {symbol: [] for symbol in symbols}
    try:
        clients = make_clients(settings)
        now = _indicator_preload_end_time(settings, states)
        max_calendar_days = 1 if settings.replay_market_data and replay_data_base_url else 10
        bars_map = _fetch_indicator_backfill_sessions(
            clients,
            symbols,
            now,
            limit,
            max_calendar_days=max_calendar_days,
        )
    except Exception:
        logging.exception("Indicator preload: explicit session backfill failed")

    try:
        recent_map = get_recent_bars(settings, symbols, limit=limit)
    except Exception:
        logging.exception("Indicator preload: get_recent_bars failed")
        recent_map = {symbol: [] for symbol in symbols}
    for symbol in symbols:
        seen = {bar.start_ms for bar in bars_map.get(symbol, [])}
        for bar in recent_map.get(symbol, []):
            if bar.start_ms in seen:
                continue
            bars_map.setdefault(symbol, []).append(bar)
            seen.add(bar.start_ms)

    for symbol in symbols:
        state = states[symbol]
        original_event_kind = state.last_event_kind
        original_event_ms = state.last_event_ms
        seen = {bar.start_ms for bar in state.bars}
        for bar in bars_map.get(symbol, []):
            if bar.start_ms in seen:
                continue
            state.add_bar(bar)
            seen.add(bar.start_ms)
            counts[symbol] += 1
        if counts[symbol]:
            ordered_bars = sorted(state.bars, key=lambda item: item.start_ms)
            state.bars.clear()
            state.bars.extend(ordered_bars)
            if original_event_ms is not None:
                state.last_event_kind = original_event_kind
                state.last_event_ms = original_event_ms
        if counts[symbol]:
            logging.info("Loaded %s historical bars for %s", counts[symbol], symbol)
        loaded_unique_bars = len({bar.start_ms for bar in state.bars})
        required_warmup = _required_preload_bars_for_settings(settings, limit)
        if required_warmup > 0 and loaded_unique_bars < required_warmup:
            logging.warning(
                "Indicator preload for %s returned only %s bars; active strategies need %s real warmup bars",
                symbol,
                loaded_unique_bars,
                required_warmup,
            )

    total = sum(counts.values())
    if total:
        nonzero = [counts[s] for s in symbols if counts[s]]
        mx = max(nonzero) if nonzero else 0
        logging.info(
            "Indicator preload completed: symbols=%s total_bars=%s max_bars_single_symbol=%s",
            len(symbols),
            total,
            mx,
        )
    else:
        logging.info("Indicator preload completed: no bars returned for symbols=%s", len(symbols))
    return counts


def format_news_event_for_log(event: NewsEvent, *, max_headline_chars: int = 180) -> str:
    symbols = ", ".join(event.symbols) if event.symbols else "-"
    headline = (event.headline or "").strip().replace("\n", " ")
    if len(headline) > max_headline_chars:
        headline = f"{headline[:max_headline_chars - 1]}…"
    source = (event.source or "").strip() or "unknown"
    return f"symbols=[{symbols}] source={source} headline={headline!r}"


def credential_fingerprint(value: str | None) -> str | None:
    if not value:
        return None
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    suffix = value[-4:] if len(value) >= 4 else value
    return f"{digest}:{suffix}"


def resolve_source_revision(
    repo_root: Path | None = None,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, str | bool | None]:
    root = repo_root or Path(__file__).resolve().parent
    environment = env if env is not None else os.environ

    def git(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    commit_full = git("rev-parse", "HEAD")
    if commit_full:
        return {
            "commit": git("rev-parse", "--short", "HEAD") or commit_full,
            "commit_full": commit_full,
            "dirty": bool(git("status", "--porcelain")),
        }

    override = (environment.get("SOURCE_COMMIT") or environment.get("GIT_COMMIT") or "").strip()
    if override:
        return {
            "commit": override[:12],
            "commit_full": override,
            "dirty": None,
        }
    return {"commit": None, "commit_full": None, "dirty": None}


def runtime_settings_snapshot(settings, strategy_symbol_counts: dict[str, int] | None = None) -> dict:
    snapshot = {
        "execution_mode": settings.execution_mode,
        "strategies": settings.strategy_names,
        "symbols": settings.symbols,
        "alpaca_paper": settings.alpaca_paper,
        "alpaca_data_feed": settings.alpaca_data_feed,
        "alpaca_api_key_fingerprint": credential_fingerprint(settings.alpaca_api_key),
        "alpaca_market_data_mode": settings.alpaca_market_data_mode,
        "regular_market_only": settings.regular_market_only,
        "replay_market_data": settings.replay_market_data,
        "indicator_preload_bars": settings.indicator_preload_bars,
        "indicator_max_bars_per_symbol": settings.indicator_max_bars_per_symbol,
        "indicator_include_afterhours": settings.indicator_include_afterhours,
        "ai_review": settings.ai_review,
        "news_hot_positive_only": settings.news_hot_positive_only,
        "news_hot_min_sentiment_score": settings.news_hot_min_sentiment_score,
        "news_log_events": settings.news_log_events,
        "news_dynamic_symbols_enabled": settings.news_dynamic_symbols_enabled,
        "news_listener_positive_only": settings.news_listener_positive_only,
        "news_listener_min_impact": settings.news_listener_min_impact,
        "news_listener_symbol_cooldown_seconds": settings.news_listener_symbol_cooldown_seconds,
        "dynamic_execution_selector": {
            "enabled": settings.dynamic_execution_selector_enabled,
            "universe_file": settings.dynamic_execution_selector_universe_file,
            "candidate_limit": settings.dynamic_execution_selector_candidate_limit,
            "top_dollar_volume_count": settings.dynamic_execution_selector_top_dollar_volume_count,
            "strength_threshold": settings.dynamic_execution_selector_strength_threshold,
            "lookback_seconds": settings.dynamic_execution_selector_lookback_seconds,
            "min_dollar_volume": settings.dynamic_execution_selector_min_dollar_volume,
            "cooldown_seconds": settings.dynamic_execution_selector_cooldown_seconds,
        },
        "dynamic_mover": {
            "enabled": settings.dynamic_mover_enabled,
            "runtime_enabled": dynamic_mover_runtime_enabled(settings),
            "universe_file": settings.dynamic_mover_universe_file,
            "lookback_minutes": settings.dynamic_mover_lookback_minutes,
            "min_move_pct": settings.dynamic_mover_min_move_pct,
            "min_dollar_volume": settings.dynamic_mover_min_dollar_volume,
            "min_rvol": settings.dynamic_mover_min_rvol,
            "max_spread_bps": settings.dynamic_mover_max_spread_bps,
            "cooldown_seconds": settings.dynamic_mover_cooldown_seconds,
            "max_dynamic_symbols": settings.dynamic_mover_max_dynamic_symbols,
            "symbol_ttl_minutes": settings.dynamic_mover_symbol_ttl_minutes,
        },
        "openai_model": settings.openai_model if settings.ai_review else None,
        "risk": {
            "target_profit_pct": settings.target_profit_pct,
            "stop_loss_pct": settings.stop_loss_pct,
            "max_hold_seconds": settings.max_hold_seconds,
            "max_position_value": settings.max_position_value,
            "position_sizing_mode": settings.position_sizing_mode,
            "risk_per_trade_pct": settings.risk_per_trade_pct,
            "max_trade_loss_r": settings.max_trade_loss_r,
            "max_open_positions": settings.max_open_positions,
            "trade_cooldown_seconds": settings.trade_cooldown_seconds,
            "strategy_overrides": {
                "opening_impulse": {
                    "max_open_positions": settings.opening_impulse_max_open_positions,
                    "max_position_value": settings.opening_impulse_max_position_value,
                    "max_hold_seconds": settings.opening_impulse_max_hold_seconds,
                },
                "gap_and_go": {
                    "max_open_positions": settings.gap_and_go_max_open_positions,
                    "max_position_value": settings.gap_and_go_max_position_value,
                    "max_hold_seconds": settings.gap_and_go_max_hold_seconds,
                },
                "spike": {
                    "max_open_positions": settings.spike_max_open_positions,
                    "max_position_value": settings.spike_max_position_value,
                    "max_hold_seconds": settings.spike_max_hold_seconds,
                },
                "liquidity_scalper": {
                    "max_open_positions": settings.liquidity_scalper_max_open_positions,
                    "max_position_value": settings.liquidity_scalper_max_position_value,
                    "max_hold_seconds": settings.liquidity_scalper_max_hold_seconds,
                    "trade_cooldown_seconds": settings.liquidity_scalper_trade_cooldown_seconds,
                },
                "stoch_macd_reversal": {
                    "max_open_positions": settings.stoch_macd_max_open_positions,
                    "max_position_value": settings.stoch_macd_max_position_value,
                    "max_hold_seconds": settings.stoch_macd_max_hold_seconds,
                    "trade_cooldown_seconds": settings.stoch_macd_trade_cooldown_seconds,
                },
                "macd_early_impulse": {
                    "max_open_positions": settings.macd_max_open_positions,
                    "max_position_value": settings.macd_max_position_value,
                    "max_hold_seconds": settings.macd_max_hold_seconds,
                    "trade_cooldown_seconds": settings.macd_trade_cooldown_seconds,
                },
                "steady_intraday": {
                    "max_open_positions": settings.steady_intraday_max_open_positions,
                    "max_position_value": settings.steady_intraday_max_position_value,
                    "max_hold_seconds": settings.steady_intraday_max_hold_seconds,
                    "trade_cooldown_seconds": settings.steady_intraday_trade_cooldown_seconds,
                },
                "maha7": {
                    "max_open_positions": settings.maha7_max_open_positions,
                    "max_position_value": settings.maha7_max_position_value,
                    "max_hold_seconds": settings.maha7_max_hold_seconds,
                },
                "breakout_power": {
                    "max_open_positions": settings.bp_max_open_positions,
                    "max_position_value": settings.bp_max_position_value,
                    "max_hold_seconds": settings.bp_max_hold_seconds,
                    "trade_cooldown_seconds": settings.bp_trade_cooldown_seconds,
                },
                "ema_gap_cross": {
                    "max_hold_seconds": settings.egc_max_hold_seconds,
                },
            },
            "daily_max_loss": settings.daily_max_loss,
            "daily_max_loss_pct": settings.daily_max_loss_pct,
            "consecutive_loss_pause_count": settings.consecutive_loss_pause_count,
            "consecutive_loss_pause_minutes": settings.consecutive_loss_pause_minutes,
            "consecutive_loss_stop_count": settings.consecutive_loss_stop_count,
            "consecutive_loss_effective_limits": {
                strategy: RiskManager(
                    settings,
                    strategy_symbol_counts=strategy_symbol_counts or {},
                ).consecutive_loss_limits_snapshot(strategy)
                for strategy in settings.strategy_names
            },
            "flatten_before_close_minutes": settings.flatten_before_close_minutes,
        },
        "stream": {
            "heartbeat_seconds": settings.heartbeat_seconds,
            "alpaca_market_data_poll_seconds": settings.alpaca_market_data_poll_seconds,
            "replay_use_mock_clock": settings.replay_use_mock_clock,
            "replay_closed_bars_only": settings.replay_closed_bars_only,
            "replay_clock_timeout_seconds": settings.replay_clock_timeout_seconds,
            "alpaca_fill_timeout_seconds": settings.alpaca_fill_timeout_seconds,
            "alpaca_fill_poll_seconds": settings.alpaca_fill_poll_seconds,
            "max_entry_chase_pct": settings.max_entry_chase_pct,
        },
    }
    snapshot.update(merge_strategy_runtime_snapshots(settings))
    snapshot["source_revision"] = resolve_source_revision()
    return snapshot


def realtime_stream_reasons(settings: Settings) -> list[str]:
    reasons = []
    if settings.market_data_requires_trade_ticks:
        reasons.append("active strategy requires trade ticks")
    if settings.dynamic_execution_selector_enabled:
        reasons.append("dynamic execution selector requires trade ticks")
    if dynamic_mover_runtime_enabled(settings):
        reasons.append("dynamic mover selector requires stream bars")
    if settings.news_dynamic_symbols_enabled:
        reasons.append("dynamic news symbols require news stream")
    if settings.news_log_events:
        reasons.append("news logging requires news stream")
    return reasons


def dynamic_mover_runtime_enabled(settings: Settings) -> bool:
    return bool(
        settings.dynamic_mover_enabled
        and any(strategy.strip().lower() == "liquidity_scalper" for strategy in settings.strategy_names)
    )


def effective_market_data_mode(settings: Settings) -> str:
    if settings.alpaca_market_data_mode == "stream":
        return "stream"
    return "stream" if realtime_stream_reasons(settings) else settings.alpaca_market_data_mode


def should_manage_exits_on_heartbeat(settings) -> bool:
    return not settings.replay_market_data


def dynamic_trade_quote_symbol_limit(settings: Settings, *, stream_trades: bool) -> int:
    if settings.alpaca_stream_max_trade_quote_channels <= 0:
        return 0
    return max_stream_trade_quote_symbols(settings.alpaca_stream_max_trade_quote_channels, stream_trades=stream_trades)


def symbols_requiring_trade_quote_stream(
    settings: Settings,
    strategy_local_symbols: dict[str, list[str]],
    *,
    dynamic_execution_symbols: list[str] | None = None,
) -> set[str]:
    stream_required_strategies = set(strategies_requiring_trade_ticks(settings.strategy_names))
    symbols: set[str] = set(dynamic_execution_symbols or [])
    if stream_required_strategies:
        symbols.update(settings.symbols)
    for strategy_name in stream_required_strategies:
        symbols.update(strategy_local_symbols.get(strategy_name, []))
    return {symbol.strip().upper() for symbol in symbols if symbol.strip()}


def can_promote_dynamic_symbol(settings: Settings, active_symbols: set[str], symbol: str, *, stream_trades: bool) -> bool:
    limit = dynamic_trade_quote_symbol_limit(settings, stream_trades=stream_trades)
    if limit <= 0:
        return True
    normalized = symbol.strip().upper()
    return normalized in active_symbols or len(active_symbols) < limit


def strategy_log_file(settings) -> Path:
    return LOG_FILE


def setup_logging(log_file: Path | None = None, strategy_names: list[str] | None = None) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    console.addFilter(FatalAlpacaStreamErrorFilter())
    console.addFilter(FriendlyAlpacaStreamErrorFilter())
    target_log_file = log_file or LOG_FILE
    file_handler = RotatingFileHandler(target_log_file, maxBytes=5_000_000, backupCount=10)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(FatalAlpacaStreamErrorFilter())
    file_handler.addFilter(FriendlyAlpacaStreamErrorFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(file_handler)

    # When unset (e.g. tests), match previous hard-coded diagnostic set (spike excluded).
    diagnostic_names = (
        strategy_names
        if strategy_names is not None
        else ["opening_impulse", "gap_and_go", "maha7"]
    )
    for logger_name in diagnostic_loggers_for(diagnostic_names):
        logging.getLogger(logger_name).setLevel(logging.DEBUG)
    for logger_name in NOISY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.INFO)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the paper-trading monitor.")
    parser.add_argument(
        "-s",
        "--strategy",
        nargs="+",
        metavar="STRATEGY",
        help=(
            "Run one or more strategies for this session. Accepts repeated values "
            "or comma-separated names, and overrides STRATEGIES for main.py."
        ),
    )
    parser.add_argument(
        "-l",
        "--list-strategies",
        action="store_true",
        help="List available strategies and exit.",
    )
    parser.add_argument("--opening-plan", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.strategy:
        try:
            args.strategy = normalize_strategy_names(args.strategy)
        except ValueError as exc:
            parser.error(str(exc))
    return args


def normalize_strategy_names(raw_values: list[str] | str) -> list[str]:
    if isinstance(raw_values, str):
        raw_parts = [raw_values]
    else:
        raw_parts = list(raw_values)
    names = [
        part.strip().lower()
        for raw in raw_parts
        for part in str(raw).replace(",", " ").split()
        if part.strip()
    ]
    available = set(available_strategy_names())
    unknown = [name for name in names if name not in available]
    if unknown:
        raise ValueError(
            "Unknown strategy: "
            + ", ".join(unknown)
            + ". Available strategies: "
            + ", ".join(available_strategy_names())
        )
    return list(dict.fromkeys(names))


def configured_strategy_names() -> list[str]:
    raw = os.getenv("STRATEGIES")
    if raw is None:
        return []
    return normalize_strategy_names(raw)


def prompt_for_strategy_names() -> list[str]:
    available = available_strategy_names()
    if not sys.stdin.isatty():
        raise RuntimeError(
            "No strategy was provided. Run with -s, for example: "
            f"scripts/run_paper.sh -s {available[0]}, or set STRATEGIES in .env or a profile."
        )
    while True:
        print("Select strategy or strategies.", file=sys.stderr)
        print("Available: " + ", ".join(available), file=sys.stderr)
        response = input("Strategy name(s): ").strip()
        if not response:
            print("Please enter at least one strategy name.", file=sys.stderr)
            continue
        try:
            return normalize_strategy_names(response)
        except ValueError as exc:
            print(exc, file=sys.stderr)


def resolve_strategy_plan_path(settings, explicit_path: Path | None, strategy_name: str | None = None) -> Path:
    if explicit_path:
        return explicit_path
    if strategy_name:
        return default_plan_file_for_strategy(strategy_name)
    return default_plan_file_for_settings(settings)


def load_strategy_local_symbols(settings: Settings, plan_paths: dict[str, Path] | None = None) -> dict[str, list[str]]:
    """Load selector/plan symbols into strategy-local universes."""
    symbols_by_strategy: dict[str, list[str]] = {}
    plan_paths = plan_paths or {}
    for raw_name in settings.strategy_names:
        strategy_name = raw_name.strip().lower()
        if not strategy_name:
            continue
        path = plan_paths.get(strategy_name) or default_plan_file_for_strategy(strategy_name)
        if not path.exists():
            symbols_by_strategy[strategy_name] = []
            continue
        plan = load_opening_plan(path)
        symbols_by_strategy[strategy_name] = parse_plan_symbols(plan)
    return symbols_by_strategy


def apply_strategy_plan_settings(settings: Settings, strategy_name: str, explicit_path: Path | None = None) -> Settings:
    """Apply bounded plan settings without moving plan symbols into the global universe."""
    path = resolve_strategy_plan_path(settings, explicit_path, strategy_name=strategy_name)
    if not path.exists():
        return settings
    plan = load_opening_plan(path)
    overrides = plan_overrides(settings, plan)
    overrides.pop("symbols", None)
    if not overrides:
        return settings
    return replace(settings, **overrides)


def _strategy_name_from(strategy_name_or_settings) -> str:
    if isinstance(strategy_name_or_settings, str):
        name = strategy_name_or_settings.strip().lower()
        return name or "opening_impulse"
    strategy_names = getattr(strategy_name_or_settings, "strategy_names", None) or []
    if strategy_names:
        return str(strategy_names[0]).strip().lower() or "opening_impulse"
    return "opening_impulse"


def validate_strategy_plan(path: Path, strategy_name_or_settings, *, min_symbols: int = 1) -> list[str]:
    strategy_name = _strategy_name_from(strategy_name_or_settings)
    if not path.exists():
        command = selector_command_for_strategy(strategy_name)
        raise FileNotFoundError(
            f"Missing strategy plan file: {path}. Run the selector first, for example: {command}"
        )

    plan = load_opening_plan(path)
    symbols = parse_plan_symbols(plan)
    if not symbols:
        command = selector_command_for_strategy(strategy_name)
        raise ValueError(
            f"Strategy plan file has no selected symbols: {path}. Regenerate it first, for example: {command}"
        )
    required = max(1, int(min_symbols))
    if len(symbols) < required:
        command = selector_command_for_strategy(strategy_name)
        raise ValueError(
            f"Strategy plan has {len(symbols)} symbols but requires at least {required}: {path}. "
            f"Regenerate it first, for example: {command}"
        )
    return symbols


def strategy_plan_guide(path: Path, strategy_name_or_settings, error: Exception) -> str:
    strategy_name = _strategy_name_from(strategy_name_or_settings)
    selector_command = selector_command_for_strategy(strategy_name)
    run_command = f"scripts/run_paper.sh -s {strategy_name}"
    return "\n".join(
        [
            f"Strategy plan is not ready for `{strategy_name}`.",
            f"Problem: {error}",
            "",
            "Create or refresh the strategy plan first:",
            f"  {selector_command}",
            "",
            "Then start paper trading again:",
            f"  {run_command}",
        ]
    )


async def main(args: argparse.Namespace | None = None) -> None:
    args = args or parse_args()
    if args.list_strategies:
        print("\n".join(available_strategy_names()))
        return
    requested_strategies = args.strategy if args.strategy else configured_strategy_names()
    if not requested_strategies:
        requested_strategies = prompt_for_strategy_names()
    settings = load_settings(strategy_names=requested_strategies)
    loaded_plan_paths: dict[str, Path] = {}
    if args.opening_plan and len(settings.strategy_names) != 1:
        print(
            "--opening-plan can only target a single active strategy; use per-strategy data/<strategy>_plan.json files.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    for raw_strategy in settings.strategy_names:
        strategy_name = raw_strategy.strip().lower()
        if not strategy_name:
            continue
        plan_path = resolve_strategy_plan_path(
            settings,
            args.opening_plan if len(settings.strategy_names) == 1 else None,
            strategy_name=strategy_name,
        )
        if not plan_path.exists():
            continue
        loaded_plan_paths[strategy_name] = plan_path
        settings = apply_strategy_plan_settings(settings, strategy_name, plan_path)
    strategy_local_symbols = load_strategy_local_symbols(settings, loaded_plan_paths)
    tradable_symbols = set(settings.symbols).union(*(set(symbols) for symbols in strategy_local_symbols.values()))
    regime_symbols = set(settings.market_regime_symbols) if settings.market_regime_enabled else set()
    dynamic_execution_symbols: list[str] = []
    dynamic_execution_selector: DynamicExecutionStrengthSelector | None = None
    if settings.dynamic_execution_selector_enabled:
        dynamic_execution_symbols = load_candidate_symbols(
            settings.dynamic_execution_selector_universe_file,
            settings.dynamic_execution_selector_candidate_limit,
        )
        if dynamic_execution_symbols:
            dynamic_execution_selector = DynamicExecutionStrengthSelector(
                dynamic_execution_symbols,
                strength_threshold=settings.dynamic_execution_selector_strength_threshold,
                lookback_seconds=settings.dynamic_execution_selector_lookback_seconds,
                top_dollar_volume_count=settings.dynamic_execution_selector_top_dollar_volume_count,
                min_dollar_volume=settings.dynamic_execution_selector_min_dollar_volume,
                cooldown_seconds=settings.dynamic_execution_selector_cooldown_seconds,
            )
    dynamic_mover_symbols: list[str] = []
    dynamic_mover_selector: DynamicMoverSelector | None = None
    dynamic_mover_enabled = dynamic_mover_runtime_enabled(settings)
    if dynamic_mover_enabled:
        dynamic_mover_symbols = load_candidate_symbols(settings.dynamic_mover_universe_file, 0)
        if dynamic_mover_symbols:
            dynamic_mover_selector = DynamicMoverSelector(
                dynamic_mover_symbols,
                lookback_minutes=settings.dynamic_mover_lookback_minutes,
                min_move_pct=settings.dynamic_mover_min_move_pct,
                min_dollar_volume=settings.dynamic_mover_min_dollar_volume,
                min_rvol=settings.dynamic_mover_min_rvol,
                max_spread_bps=settings.dynamic_mover_max_spread_bps,
                cooldown_seconds=settings.dynamic_mover_cooldown_seconds,
                max_dynamic_symbols=settings.dynamic_mover_max_dynamic_symbols,
                symbol_ttl_minutes=settings.dynamic_mover_symbol_ttl_minutes,
            )
    initial_symbols = sorted(tradable_symbols.union(regime_symbols))
    stream_trade_quote_symbols = symbols_requiring_trade_quote_stream(
        settings,
        strategy_local_symbols,
        dynamic_execution_symbols=dynamic_execution_symbols,
    )
    stream_symbols = sorted(stream_trade_quote_symbols.union(dynamic_mover_symbols))
    stream_bars_only = frozenset(
        set(dynamic_mover_symbols) - stream_trade_quote_symbols
    )
    rest_poll_symbols = sorted(set(initial_symbols) - set(stream_symbols))
    stream_trades_enabled = bool(
        settings.dynamic_execution_selector_enabled or dynamic_mover_enabled or settings.market_data_requires_trade_ticks
    )
    if (
        not tradable_symbols
        and not dynamic_execution_symbols
        and not dynamic_mover_symbols
        and not settings.news_dynamic_symbols_enabled
    ):
        print(
            "No symbols to trade: set SYMBOLS in `.env`/your profile, or add symbols under "
            "data/<strategy>_plan.json for each active strategy (see strategies registry), or enable "
            "DYNAMIC_EXECUTION_SELECTOR_ENABLED/DYNAMIC_MOVER_ENABLED with populated universe files, or enable NEWS_DYNAMIC_SYMBOLS_ENABLED.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    log_file = strategy_log_file(settings)
    setup_logging(log_file, settings.strategy_names)
    for strategy_name, plan_path in loaded_plan_paths.items():
        logging.info("Loaded %s plan from %s", strategy_name, plan_path)
    if loaded_plan_paths and symbols_env_blocks_plan():
        logging.info(
            "Global watchlist: SYMBOLS from environment is shared by all strategies (%s)",
            os.getenv("SYMBOLS", "").strip(),
        )

    effective_symbol_counts = {
        strategy: len(set(settings.symbols) | set(local_symbols))
        for strategy, local_symbols in strategy_local_symbols.items()
    }
    requested_market_data_mode = settings.alpaca_market_data_mode
    selected_market_data_mode = effective_market_data_mode(settings)
    stream_trade_quote_channels = stream_trade_quote_channel_count(
        stream_symbols,
        bars_only_symbols=stream_bars_only,
        stream_trades=stream_trades_enabled,
    )
    stream_channel_limit = settings.alpaca_stream_max_trade_quote_channels
    if selected_market_data_mode == "stream" and stream_channel_limit > 0:
        if stream_trade_quote_channels > stream_channel_limit:
            max_symbols = max_stream_trade_quote_symbols(stream_channel_limit, stream_trades=stream_trades_enabled)
            per_symbol = "quote+trade" if stream_trades_enabled else "quote"
            print(
                "Alpaca Basic IEX stream allows "
                f"{stream_channel_limit} trade/quote channels ({max_symbols} symbols with {per_symbol} each). "
                f"This run needs {stream_trade_quote_channels} channels for "
                f"{len(stream_symbols) - len(stream_bars_only)} trade/quote stream symbols "
                f"({len(stream_symbols)} stream total, {len(stream_bars_only)} bars-only stream, "
                f"{len(rest_poll_symbols)} REST-polled). "
                "Reduce SYMBOLS or the plan size for stream-dependent strategies, or set "
                "ALPACA_STREAM_MAX_TRADE_QUOTE_CHANNELS=0 on Algo Trader Plus.",
                file=sys.stderr,
            )
            raise SystemExit(2)
    settings_snapshot = runtime_settings_snapshot(settings, effective_symbol_counts)
    settings_snapshot["alpaca_market_data_effective_mode"] = selected_market_data_mode
    settings_snapshot["alpaca_market_data_stream_reasons"] = realtime_stream_reasons(settings)
    settings_snapshot["global_symbols"] = list(settings.symbols)
    settings_snapshot["strategy_symbols"] = strategy_local_symbols
    settings_snapshot["dynamic_execution_symbols"] = dynamic_execution_symbols
    settings_snapshot["dynamic_mover_symbols"] = dynamic_mover_symbols
    settings_snapshot["alpaca_stream_subscriptions"] = {
        "stream_symbols": stream_symbols,
        "bars_only_symbols": sorted(stream_bars_only),
        "rest_poll_symbols": rest_poll_symbols,
        "trade_quote_channels": stream_trade_quote_channels,
        "trade_quote_channel_limit": stream_channel_limit,
        "stream_trades_enabled": stream_trades_enabled,
    }
    settings_snapshot["market_regime"] = {
        "enabled": settings.market_regime_enabled,
        "symbols": list(settings.market_regime_symbols),
        "min_bars": settings.market_regime_min_bars,
        "risk_off_score": settings.market_regime_risk_off_score,
        "block_score": settings.market_regime_block_score,
        "risk_on_score": settings.market_regime_risk_on_score,
        "risk_off_size_multiplier": settings.market_regime_risk_off_size_multiplier,
        "risk_on_size_multiplier": settings.market_regime_risk_on_size_multiplier,
        "bypass_strategies": list(settings.market_regime_bypass_strategies),
        "weights": {
            "positive": settings.market_regime_positive_weight,
            "below_vwap": settings.market_regime_below_vwap_weight,
            "vwap_falling": settings.market_regime_vwap_falling_weight,
            "below_ema": settings.market_regime_below_ema_weight,
            "SPY": settings.market_regime_spy_weight,
            "QQQ": settings.market_regime_qqq_weight,
            "IWM": settings.market_regime_iwm_weight,
            "default": settings.market_regime_default_symbol_weight,
        },
    }
    settings_snapshot["effective_symbols"] = {
        strategy: sorted(set(settings.symbols) | set(local_symbols))
        for strategy, local_symbols in strategy_local_symbols.items()
    }
    runtime_event_symbols = stream_symbols if selected_market_data_mode == "stream" else initial_symbols
    stream_settings = replace(settings, symbols=runtime_event_symbols, alpaca_market_data_mode=selected_market_data_mode)
    states = {
        symbol: SymbolState(symbol, indicator_max_bars=stream_settings.indicator_max_bars_per_symbol)
        for symbol in initial_symbols
    }

    await asyncio.to_thread(preload_indicator_bars_for_states, stream_settings, states)

    stream = build_market_data_stream(stream_settings, bars_only_symbols=stream_bars_only)
    if selected_market_data_mode == "stream" and rest_poll_symbols:
        rest_settings = replace(settings, symbols=rest_poll_symbols, alpaca_market_data_mode="rest")
        stream = MergedMarketDataStream(stream, AlpacaRestPollingStream(rest_settings))
    strategies = build_strategies(settings)
    symbol_manager = SymbolManager(
        states, stream, strategies, global_symbols=settings.symbols, settings=stream_settings
    )
    for strategy_name, symbols in strategy_local_symbols.items():
        symbol_manager.register_strategy_symbols(strategy_name, symbols)
    strategies_by_name = {strategy.name: strategy for strategy in strategies}
    news_listener = NewsListener(
        symbol_cooldown_seconds=settings.news_listener_symbol_cooldown_seconds,
        min_impact=settings.news_listener_min_impact,
        positive_only=settings.news_listener_positive_only,
    )
    set_source_commit((settings_snapshot.get("source_revision") or {}).get("commit"))
    executor = build_executor(stream_settings)
    risk = RiskManager(settings, strategy_symbol_counts=effective_symbol_counts)
    reviewer = SignalReviewer(settings)
    market_regime = MarketRegimeMonitor(settings)
    rejection_logs = RejectionLogThrottler()
    heartbeat = HeartbeatReporter()
    dynamic_promoted_symbols: set[str] = set()

    logging.info(
        "Monitoring %s with execution mode %s and strategies %s",
        ", ".join(initial_symbols),
        settings.execution_mode,
        ", ".join(settings.strategy_names),
    )
    if selected_market_data_mode != requested_market_data_mode:
        logging.info(
            "Market data mode upgraded %s -> %s: %s",
            requested_market_data_mode,
            selected_market_data_mode,
            "; ".join(realtime_stream_reasons(settings)),
        )
    if selected_market_data_mode == "stream":
        logging.info(
            "Alpaca stream subscriptions: %d symbols, %d bars-only symbols, %d trade/quote channels (limit %d)",
            len(stream_symbols),
            len(stream_bars_only),
            stream_trade_quote_channels,
            stream_channel_limit,
        )
        if rest_poll_symbols:
            logging.info("REST polling %d non-stream symbols alongside websocket stream", len(rest_poll_symbols))
    logging.info("Runtime settings %s", json.dumps(settings_snapshot, sort_keys=True))

    try:

        def _bootstrap_all_strategies() -> None:
            for strategy in strategies:
                try:
                    strategy.bootstrap_states(states)
                except Exception:
                    logging.exception("bootstrap_states failed for %s", getattr(strategy, "name", "?"))

        async def _promote_dynamic_mover(selection: DynamicMoverSelection) -> bool:
            active_symbols = symbol_manager.all_symbols()
            if selection.symbol in active_symbols:
                logging.info("Dynamic mover skipped: already tradable symbol=%s", selection.symbol)
                return False
            if not can_promote_dynamic_symbol(
                settings,
                active_symbols,
                selection.symbol,
                stream_trades=stream_trades_enabled,
            ):
                limit = dynamic_trade_quote_symbol_limit(settings, stream_trades=stream_trades_enabled)
                logging.info(
                    "Dynamic mover rejected: channel limit reached symbol=%s active=%d limit=%d",
                    selection.symbol,
                    len(active_symbols),
                    limit,
                )
                return False

            dynamic_mover_selector.confirm_selection(selection)
            added = symbol_manager.add_symbol(selection.symbol)
            state = states.get(selection.symbol)
            if added:
                dynamic_promoted_symbols.add(selection.symbol)
                logging.info('Added dynamic mover %s reason="%s"', selection.symbol, selection.reason)
            if added and state is not None:
                warmed = await asyncio.to_thread(
                    warm_dynamic_news_symbol,
                    settings,
                    state,
                    selection.symbol,
                )
                if warmed:
                    logging.info("Warmed dynamic mover %s from recent market data", selection.symbol)
            return added

        await asyncio.to_thread(_bootstrap_all_strategies)
        async for event in stream.events():
            if isinstance(event, Heartbeat):
                heartbeat.record_heartbeat()
                if dynamic_mover_selector is not None:
                    expired = dynamic_mover_selector.expire(event.timestamp_ms)
                    for symbol in expired:
                        logging.info("Dynamic mover TTL expired symbol=%s", symbol)
                        if symbol in dynamic_promoted_symbols:
                            logging.info(
                                "Dynamic mover retained after TTL symbol=%s; channel remains allocated",
                                symbol,
                            )
                            dynamic_promoted_symbols.discard(symbol)
                if should_manage_exits_on_heartbeat(settings):
                    manage_all_exits(executor, states, strategies_by_name, event.timestamp_ms, risk)
                heartbeat.emit(settings, states, executor)
                continue
            if isinstance(event, NewsEvent):
                heartbeat.record_news()
                if settings.news_log_events:
                    logging.info("News feed %s", format_news_event_for_log(event))
                if not settings.news_dynamic_symbols_enabled:
                    continue
                if not should_expand_symbols_from_news(event):
                    logging.debug("Ignoring news for dynamic symbols outside regular market hours")
                    continue
                for classified in news_listener.process(event):
                    added = symbol_manager.add_symbol(classified.symbol)
                    state = states.get(classified.symbol)
                    if added:
                        logging.info("Added symbol %s from news stream", classified.symbol)
                    if state is None:
                        continue
                    if added:
                        warmed = await asyncio.to_thread(
                            warm_dynamic_news_symbol,
                            settings,
                            state,
                            classified.symbol,
                        )
                        if warmed:
                            logging.info("Warmed symbol %s from recent market data", classified.symbol)
                    state.mark_news(
                        classified.timestamp_ms,
                        price=state.last_price,
                        sentiment=classified.sentiment,
                        impact=classified.impact,
                    )
                continue

            if dynamic_execution_selector is not None:
                selection = None
                if isinstance(event, Quote):
                    dynamic_execution_selector.record_quote(event)
                elif isinstance(event, Bar):
                    dynamic_execution_selector.record_bar(event)
                elif isinstance(event, Trade):
                    if is_regular_market_time(event.timestamp_ms):
                        selection = dynamic_execution_selector.record_trade(event)
                if selection is not None:
                    added = symbol_manager.add_symbol(selection.symbol)
                    state = states.get(selection.symbol)
                    if added:
                        logging.info(
                            "Added symbol %s from dynamic execution selector strength=%.2f dollar_volume=%.0f rank=%d buy_volume=%d sell_volume=%d",
                            selection.symbol,
                            selection.execution_strength,
                            selection.dollar_volume,
                            selection.dollar_volume_rank,
                            selection.buy_volume,
                            selection.sell_volume,
                        )
                    if added and state is not None:
                        warmed = await asyncio.to_thread(
                            warm_dynamic_news_symbol,
                            settings,
                            state,
                            selection.symbol,
                        )
                        if warmed:
                            logging.info("Warmed symbol %s from recent market data", selection.symbol)
            if dynamic_mover_selector is not None:
                if isinstance(event, Quote):
                    dynamic_mover_selector.record_quote(event)
                elif isinstance(event, Trade):
                    dynamic_mover_selector.record_trade(event)
                elif isinstance(event, Bar):
                    dynamic_mover_selector.record_bar(event)
                    if is_regular_market_time(event.end_ms):
                        candidates = dynamic_mover_selector.ranked_candidates(
                            event.end_ms,
                            allow_missing_spread=True,
                        )
                        for candidate in candidates:
                            try:
                                quotes = await asyncio.to_thread(get_latest_quotes, settings, [candidate.symbol])
                            except Exception:
                                logging.debug("Could not fetch quote for dynamic mover %s", candidate.symbol, exc_info=True)
                                quotes = {}
                            quote = quotes.get(candidate.symbol)
                            if quote is not None:
                                dynamic_mover_selector.record_quote(quote)
                            selection = dynamic_mover_selector.evaluate(
                                candidate.symbol,
                                timestamp_ms=event.end_ms,
                                reserve=False,
                            )
                            if selection is None:
                                continue
                            await _promote_dynamic_mover(selection)
            state = states.get(event.symbol)
            if state is None:
                logging.debug("Ignoring market data for unsubscribed symbol %s", event.symbol)
                continue

            if isinstance(event, Quote):
                heartbeat.record_quote()
                state.update_quote(event)
            elif isinstance(event, Bar):
                heartbeat.record_bar()
                state.add_bar(event)
            elif isinstance(event, Trade):
                heartbeat.record_trade()
                state.update_trade(event)
            else:
                continue

            event_ms = state.last_event_ms
            manage_all_exits(executor, states, strategies_by_name, event_ms, risk)
            regime = market_regime.evaluate(states)
            if market_regime.should_log_change(regime):
                logging.info("Market regime %s", regime.reason)

            for strategy in strategies:
                strategy.set_market_regime(market_regime.regime_for_strategy(regime, strategy.name))
                signal = strategy.evaluate(state)
                if not signal:
                    continue

                heartbeat.record_signal(signal.strategy)
                adjusted_signal, regime_reject = market_regime.apply_to_signal(signal, regime)
                if regime_reject:
                    heartbeat.record_rejection(signal.strategy, regime_reject)
                    if rejection_logs.should_log(signal.symbol, signal.side, signal.strategy, regime_reject):
                        logging.info(
                            "Signal rejected %s %s from %s: %s",
                            signal.symbol,
                            signal.side,
                            signal.strategy,
                            regime_reject,
                        )
                    continue
                signal = adjusted_signal or signal
                decision = risk.check_entry(
                    signal,
                    executor.open_symbols(),
                    executor.total_pnl(mark_prices(states)),
                    executor.open_strategy_counts(),
                )
                if not decision.allowed:
                    heartbeat.record_rejection(signal.strategy, decision.reason)
                    if rejection_logs.should_log(signal.symbol, signal.side, signal.strategy, decision.reason):
                        logging.info(
                            "Signal rejected %s %s from %s: %s",
                            signal.symbol,
                            signal.side,
                            signal.strategy,
                            decision.reason,
                        )
                    continue

                note = await asyncio.to_thread(reviewer.review, signal)
                if note:
                    logging.info("AI review %s %s: %s", signal.strategy, signal.symbol, note)

                fill = executor.buy(signal, state)
                if fill:
                    heartbeat.record_entry(signal.strategy)
                    risk.record_trade(signal.symbol, signal.timestamp_ms, signal.strategy)
                    strategy_obj = strategies_by_name.get(signal.strategy)
                    if strategy_obj is not None:
                        try:
                            strategy_obj.on_entry_fill(fill)
                        except Exception:
                            logging.exception("Strategy entry-fill hook failed for %s", signal.strategy)
                    break
                if executor.consume_failed_entry(signal.symbol):
                    risk.record_failed_entry(signal.symbol, signal.timestamp_ms)
    finally:
        flatten_on_shutdown(settings, executor, states, strategies_by_name)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AlpacaStreamAuthError as exc:
        logging.error("Alpaca stream authentication failed: %s", exc)
    except AlpacaStreamConnectionLimitError as exc:
        logging.error("%s", exc)
    except AlpacaStreamEndedError as exc:
        logging.error("Alpaca stream stopped unexpectedly: %s", exc)
    except KeyboardInterrupt:
        logging.info("Stopped")
