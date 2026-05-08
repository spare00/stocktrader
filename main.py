import asyncio
import argparse
from dataclasses import replace
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
from alpaca_stream import AlpacaStreamAuthError, AlpacaStreamConnectionLimitError, build_market_data_stream
from candle import SymbolState
from config import load_settings
from execution import build_executor
from modules.news_listener import NewsListener
from modules.symbol_manager import SymbolManager
from models import Bar, Heartbeat, NewsEvent, Quote
from opening_plan import (
    apply_opening_plan,
    default_plan_file_for_strategy,
    default_plan_file_for_settings,
    load_opening_plan,
    parse_plan_symbols,
    selector_command_for_strategy,
    symbols_env_blocks_plan,
)
from risk import RiskManager
from runtime_safety import flatten_on_shutdown, manage_all_exits
from strategies import available_strategy_names, build_strategies
from strategies.registry import diagnostic_loggers_for, merge_strategy_runtime_snapshots, strategies_requiring_plan


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
    "beat",
    "raises guidance",
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
OPENING_UNIVERSE_FILE = Path("data/opening_universe.txt")
MACD_DEFAULT_UNIVERSE = (
    "AAPL",
    "AMD",
    "AMZN",
    "META",
    "MSFT",
    "NVDA",
    "QQQ",
    "TSLA",
)
NEGATIVE_NEWS_TERMS = (
    "misses",
    "miss",
    "cuts guidance",
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
        choices=available_strategy_names(),
        help="Run exactly one strategy for this session. Overrides STRATEGIES for main.py.",
    )
    parser.add_argument(
        "-l",
        "--list-strategies",
        action="store_true",
        help="List available strategies and exit.",
    )
    parser.add_argument("--opening-plan", type=Path, default=None, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def resolve_strategy_plan_path(settings, explicit_path: Path | None, strategy_name: str | None = None) -> Path:
    if explicit_path:
        return explicit_path
    if strategy_name:
        return default_plan_file_for_strategy(strategy_name)
    return default_plan_file_for_settings(settings)


def load_opening_universe_symbols(path: Path = OPENING_UNIVERSE_FILE) -> list[str]:
    if not path.exists():
        return []
    symbols: list[str] = []
    text = path.read_text(encoding="utf-8")
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0]
        for raw_symbol in line.replace(",", " ").split():
            symbol = raw_symbol.strip().upper()
            if symbol:
                symbols.append(symbol)
    return list(dict.fromkeys(symbols))


def expand_symbols_for_macd(settings):
    if "macd_early_impulse" not in settings.strategy_names:
        return settings
    opening_universe = load_opening_universe_symbols()
    macd_universe = opening_universe or list(MACD_DEFAULT_UNIVERSE)
    merged = list(dict.fromkeys([symbol.strip().upper() for symbol in (settings.symbols + macd_universe) if symbol.strip()]))
    if merged == settings.symbols:
        return settings
    logging.info(
        "MACD watchlist expanded to %s symbols using %s",
        len(merged),
        OPENING_UNIVERSE_FILE if opening_universe else "built-in default universe",
    )
    return replace(settings, symbols=merged)


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


def run_strategy_selector(strategy_name: str) -> tuple[bool, str]:
    command = selector_command_for_strategy(strategy_name)
    try:
        subprocess.run(command, shell=True, check=True)
    except subprocess.CalledProcessError as exc:
        return False, f"selector failed with exit code {exc.returncode}"
    return True, "selector completed"


def ensure_strategy_plan_ready(
    path: Path,
    strategy_name: str,
    *,
    max_wait_seconds: int,
    retry_seconds: int,
    min_symbols: int,
) -> list[str]:
    """
    Ensure the strategy plan exists and has selected symbols.

    Automatically runs the strategy selector and retries until the plan validates
    or the max wait window is reached.
    """
    start = time.monotonic()
    attempt = 0
    last_error: Exception | None = None
    wait_step = max(1, retry_seconds)
    max_wait = max(0, max_wait_seconds)
    required_symbols = max(1, int(min_symbols))

    while True:
        attempt += 1
        ok, detail = run_strategy_selector(strategy_name)
        if ok:
            try:
                symbols = validate_strategy_plan(path, strategy_name, min_symbols=required_symbols)
                if attempt > 1:
                    logging.info(
                        "Strategy plan %s ready for %s after %s attempts (symbols=%s, minimum=%s)",
                        path,
                        strategy_name,
                        attempt,
                        len(symbols),
                        required_symbols,
                    )
                return symbols
            except (FileNotFoundError, ValueError) as exc:
                last_error = exc
                logging.info(
                    "Selector run %s for %s did not produce a ready plan yet: %s",
                    attempt,
                    strategy_name,
                    exc,
                )
        else:
            last_error = ValueError(detail)
            logging.warning("Selector run %s for %s failed: %s", attempt, strategy_name, detail)

        elapsed = int(time.monotonic() - start)
        if elapsed >= max_wait:
            break
        sleep_for = min(wait_step, max_wait - elapsed)
        logging.info(
            "Waiting %ss before retrying selector for %s (%ss/%ss elapsed)",
            sleep_for,
            strategy_name,
            elapsed,
            max_wait,
        )
        time.sleep(sleep_for)

    if last_error is not None:
        raise last_error
    raise ValueError(f"Strategy plan for {strategy_name} is not ready.")


async def main(args: argparse.Namespace | None = None) -> None:
    args = args or parse_args()
    if args.list_strategies:
        print("\n".join(available_strategy_names()))
        return
    requested_strategies = [args.strategy] if args.strategy else None
    settings = load_settings(strategy_names=requested_strategies)
    plan_required_for = strategies_requiring_plan(settings.strategy_names)
    opening_plan_path: Path | None = None
    if plan_required_for:
        plan_strategy = plan_required_for[0]
        opening_plan_path = resolve_strategy_plan_path(settings, args.opening_plan, strategy_name=plan_strategy)
        auto_selector = str(os.getenv("AUTO_RUN_SELECTOR", "1")).strip().lower() not in {"0", "false", "no", "off"}
        auto_selector_max_wait_seconds = int(os.getenv("AUTO_SELECTOR_MAX_WAIT_SECONDS", "1800"))
        auto_selector_retry_seconds = int(os.getenv("AUTO_SELECTOR_RETRY_SECONDS", "60"))
        auto_selector_min_symbols = int(os.getenv("AUTO_SELECTOR_MIN_SYMBOLS", "20"))
        try:
            validate_strategy_plan(opening_plan_path, plan_strategy, min_symbols=auto_selector_min_symbols)
        except (FileNotFoundError, ValueError) as exc:
            if not auto_selector:
                print(strategy_plan_guide(opening_plan_path, plan_strategy, exc), file=sys.stderr)
                raise SystemExit(2) from None
            logging.info(
                "Auto selector enabled for %s. Ensuring plan %s is ready "
                "(max wait %ss, retry %ss, minimum symbols %s).",
                plan_strategy,
                opening_plan_path,
                auto_selector_max_wait_seconds,
                auto_selector_retry_seconds,
                auto_selector_min_symbols,
            )
            try:
                ensure_strategy_plan_ready(
                    opening_plan_path,
                    plan_strategy,
                    max_wait_seconds=auto_selector_max_wait_seconds,
                    retry_seconds=auto_selector_retry_seconds,
                    min_symbols=auto_selector_min_symbols,
                )
            except (FileNotFoundError, ValueError) as retry_exc:
                print(strategy_plan_guide(opening_plan_path, plan_strategy, retry_exc), file=sys.stderr)
                raise SystemExit(2) from None
        settings = apply_opening_plan(settings, opening_plan_path)
    settings = expand_symbols_for_macd(settings)
    log_file = strategy_log_file(settings)
    setup_logging(log_file, settings.strategy_names)
    if opening_plan_path is not None:
        logging.info("Loaded opening plan from %s", opening_plan_path)
    if opening_plan_path is not None and symbols_env_blocks_plan():
        logging.info(
            "Watchlist: SYMBOLS from environment overrides strategy plan (%s)",
            os.getenv("SYMBOLS", "").strip(),
        )

    settings_snapshot = runtime_settings_snapshot(settings)
    states = {symbol: SymbolState(symbol) for symbol in settings.symbols}
    stream = build_market_data_stream(settings)
    strategies = build_strategies(settings)
    for strategy in strategies:
        strategy.bootstrap_states(states)
    symbol_manager = SymbolManager(states, stream, strategies)
    strategies_by_name = {strategy.name: strategy for strategy in strategies}
    news_listener = NewsListener(
        symbol_cooldown_seconds=settings.news_listener_symbol_cooldown_seconds,
        min_impact=settings.news_listener_min_impact,
        positive_only=settings.news_listener_positive_only,
    )
    executor = build_executor(settings)
    risk = RiskManager(settings)
    reviewer = SignalReviewer(settings)
    rejection_logs = RejectionLogThrottler()
    heartbeat = HeartbeatReporter()

    logging.info(
        "Monitoring %s with execution mode %s and strategies %s",
        ", ".join(settings.symbols),
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
                for classified in news_listener.process(event):
                    added = symbol_manager.add_symbol(classified.symbol)
                    if added:
                        logging.info("Added symbol %s from news stream", classified.symbol)
                    state = states.get(classified.symbol)
                    if state is None:
                        continue
                    state.mark_news(
                        classified.timestamp_ms,
                        price=state.last_price,
                        sentiment=classified.sentiment,
                        impact=classified.impact,
                    )
                continue

            state = states.get(event.symbol)
            if state is None:
                if symbol_manager.add_symbol(event.symbol):
                    logging.info("Added symbol %s from market data stream", event.symbol)
                    state = states.get(event.symbol)
                if state is None:
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
