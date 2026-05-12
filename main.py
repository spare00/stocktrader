import asyncio
import argparse
from dataclasses import replace
import hashlib
import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ai_agent import SignalReviewer
from alpaca_stream import AlpacaStreamAuthError, AlpacaStreamConnectionLimitError, build_market_data_stream
from alpaca_client import get_latest_quotes, get_recent_bars
from candle import SymbolState
from config import Settings, load_settings
from execution import build_executor
from market_hours import is_regular_market_time
from modules.news_listener import NewsListener
from modules.symbol_manager import SymbolManager
from models import Bar, Heartbeat, NewsEvent, Quote
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
from strategies.registry import diagnostic_loggers_for, merge_strategy_runtime_snapshots


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
        payload = {
            "market_data_mode": settings.alpaca_market_data_mode,
            "watched_symbols": len(states),
            "active_symbols": active_symbols,
            "open_positions": sorted(executor.open_symbols()),
            "events": dict(self._events),
            "strategies": {},
        }
        if latest_event_ms:
            payload["latest_event_age_seconds"] = round((time.time() * 1000 - latest_event_ms) / 1000, 1)

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
        self._events = {"quotes": 0, "bars": 0, "heartbeats": 0, "news": 0}
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


def warm_dynamic_news_symbol(settings, state: SymbolState, symbol: str, *, bar_limit: int = 5) -> bool:
    """Backfill enough market context for a symbol discovered from news to be tradable quickly."""
    if settings.replay_market_data:
        return False

    warmed = False
    try:
        bars = get_recent_bars(settings, [symbol], limit=bar_limit).get(symbol, [])
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


def runtime_settings_snapshot(settings) -> dict:
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
        "ai_review": settings.ai_review,
        "news_hot_positive_only": settings.news_hot_positive_only,
        "news_hot_min_sentiment_score": settings.news_hot_min_sentiment_score,
        "news_log_events": settings.news_log_events,
        "news_listener_positive_only": settings.news_listener_positive_only,
        "news_listener_min_impact": settings.news_listener_min_impact,
        "news_listener_symbol_cooldown_seconds": settings.news_listener_symbol_cooldown_seconds,
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
            "daily_max_loss": settings.daily_max_loss,
            "daily_max_loss_pct": settings.daily_max_loss_pct,
            "consecutive_loss_pause_count": settings.consecutive_loss_pause_count,
            "consecutive_loss_pause_minutes": settings.consecutive_loss_pause_minutes,
            "consecutive_loss_stop_count": settings.consecutive_loss_stop_count,
            "flatten_before_close_minutes": settings.flatten_before_close_minutes,
        },
        "stream": {
            "heartbeat_seconds": settings.heartbeat_seconds,
            "alpaca_market_data_poll_seconds": settings.alpaca_market_data_poll_seconds,
            "alpaca_fill_timeout_seconds": settings.alpaca_fill_timeout_seconds,
            "alpaca_fill_poll_seconds": settings.alpaca_fill_poll_seconds,
            "max_entry_chase_pct": settings.max_entry_chase_pct,
        },
    }
    snapshot.update(merge_strategy_runtime_snapshots(settings))
    return snapshot


def should_manage_exits_on_heartbeat(settings) -> bool:
    return not settings.replay_market_data


def strategy_log_file(settings) -> Path:
    strategies = [name.strip().lower().replace(" ", "_") for name in settings.strategy_names if name.strip()]
    if not strategies:
        return LOG_FILE
    suffix = "__".join(strategies)
    return LOG_DIR / f"trader_{suffix}.log"


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
    initial_symbols = sorted(set(settings.symbols).union(*(set(symbols) for symbols in strategy_local_symbols.values())))
    if not initial_symbols:
        print(
            "No symbols to trade: set SYMBOLS in `.env`/your profile, or add symbols under "
            "data/<strategy>_plan.json for each active strategy (see strategies registry).",
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

    settings_snapshot = runtime_settings_snapshot(settings)
    settings_snapshot["global_symbols"] = list(settings.symbols)
    settings_snapshot["strategy_symbols"] = strategy_local_symbols
    settings_snapshot["effective_symbols"] = {
        strategy: sorted(set(settings.symbols) | set(local_symbols))
        for strategy, local_symbols in strategy_local_symbols.items()
    }
    stream_settings = replace(settings, symbols=initial_symbols)
    states = {symbol: SymbolState(symbol) for symbol in initial_symbols}
    stream = build_market_data_stream(stream_settings)
    strategies = build_strategies(settings)
    symbol_manager = SymbolManager(states, stream, strategies, global_symbols=settings.symbols)
    for strategy_name, symbols in strategy_local_symbols.items():
        symbol_manager.register_strategy_symbols(strategy_name, symbols)
    strategies_by_name = {strategy.name: strategy for strategy in strategies}
    news_listener = NewsListener(
        symbol_cooldown_seconds=settings.news_listener_symbol_cooldown_seconds,
        min_impact=settings.news_listener_min_impact,
        positive_only=settings.news_listener_positive_only,
    )
    executor = build_executor(stream_settings)
    risk = RiskManager(settings)
    reviewer = SignalReviewer(settings)
    rejection_logs = RejectionLogThrottler()
    heartbeat = HeartbeatReporter()

    logging.info(
        "Monitoring %s with execution mode %s and strategies %s",
        ", ".join(initial_symbols),
        settings.execution_mode,
        ", ".join(settings.strategy_names),
    )
    logging.info("Runtime settings %s", json.dumps(settings_snapshot, sort_keys=True))

    try:
        async for event in stream.events():
            if isinstance(event, Heartbeat):
                heartbeat.record_heartbeat()
                if should_manage_exits_on_heartbeat(settings):
                    manage_all_exits(executor, states, strategies_by_name, event.timestamp_ms, risk)
                heartbeat.emit(settings, states, executor)
                continue
            if isinstance(event, NewsEvent):
                heartbeat.record_news()
                if settings.news_log_events:
                    logging.info("News feed %s", format_news_event_for_log(event))
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
            else:
                continue

            event_ms = state.last_event_ms
            manage_all_exits(executor, states, strategies_by_name, event_ms, risk)

            for strategy in strategies:
                signal = strategy.evaluate(state)
                if not signal:
                    continue

                heartbeat.record_signal(signal.strategy)
                decision = risk.check_entry(signal, executor.open_symbols(), executor.total_pnl(mark_prices(states)))
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
    except KeyboardInterrupt:
        logging.info("Stopped")
