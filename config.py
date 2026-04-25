import os
from dataclasses import dataclass, field


def _csv_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [part.strip().upper() for part in raw.split(",") if part.strip()]


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value is None else float(value)


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None else int(value)


def _strategy_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class Settings:
    alpaca_api_key: str | None = os.getenv("ALPACA_API_KEY")
    alpaca_secret_key: str | None = os.getenv("ALPACA_SECRET_KEY")
    alpaca_paper: bool = os.getenv("ALPACA_PAPER", "true").lower() in {"1", "true", "yes", "on"}
    alpaca_data_feed: str = os.getenv("ALPACA_DATA_FEED", "iex").lower()
    alpaca_stream_url: str | None = os.getenv("ALPACA_STREAM_URL")
    execution_mode: str = os.getenv("EXECUTION_MODE", "local").lower()
    strategy_names: list[str] = field(default_factory=lambda: _strategy_env("STRATEGIES", "spike"))

    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")

    symbols: list[str] = field(default_factory=lambda: _csv_env("SYMBOLS", "AAPL,MSFT,NVDA,TSLA,META"))

    spike_lookback_seconds: int = _int_env("SPIKE_LOOKBACK_SECONDS", 5)
    spike_change_pct: float = _float_env("SPIKE_CHANGE_PCT", 0.0025)
    volume_ratio: float = _float_env("VOLUME_RATIO", 2.0)
    max_spread_bps: float = _float_env("MAX_SPREAD_BPS", 12.0)

    target_profit_pct: float = min(_float_env("TARGET_PROFIT_PCT", 0.01), 0.02)
    stop_loss_pct: float = _float_env("STOP_LOSS_PCT", 0.004)
    max_hold_seconds: int = _int_env("MAX_HOLD_SECONDS", 180)

    starting_cash: float = _float_env("STARTING_CASH", 25_000.0)
    max_position_value: float = _float_env("MAX_POSITION_VALUE", 2_500.0)
    max_open_positions: int = _int_env("MAX_OPEN_POSITIONS", 3)
    trade_cooldown_seconds: int = _int_env("TRADE_COOLDOWN_SECONDS", 30)
    daily_max_loss: float = _float_env("DAILY_MAX_LOSS", 250.0)

    opening_impulse_start_minute: int = _int_env("OPENING_IMPULSE_START_MINUTE", 0)
    opening_impulse_end_minute: int = _int_env("OPENING_IMPULSE_END_MINUTE", 90)
    opening_impulse_window_seconds: int = _int_env("OPENING_IMPULSE_WINDOW_SECONDS", 45)
    opening_impulse_min_quotes: int = _int_env("OPENING_IMPULSE_MIN_QUOTES", 8)
    opening_impulse_change_pct: float = _float_env("OPENING_IMPULSE_CHANGE_PCT", 0.01)
    opening_impulse_skip_extended_pct: float = _float_env("OPENING_IMPULSE_SKIP_EXTENDED_PCT", 0.05)
    opening_impulse_volume_ratio: float = _float_env("OPENING_IMPULSE_VOLUME_RATIO", 2.0)
    opening_impulse_max_spread_bps: float = _float_env("OPENING_IMPULSE_MAX_SPREAD_BPS", 10.0)
    opening_impulse_min_quote_size: int = _int_env("OPENING_IMPULSE_MIN_QUOTE_SIZE", 10)
    opening_impulse_max_negative_steps: int = _int_env("OPENING_IMPULSE_MAX_NEGATIVE_STEPS", 2)
    opening_impulse_exit_window_seconds: int = _int_env("OPENING_IMPULSE_EXIT_WINDOW_SECONDS", 15)
    opening_impulse_exit_min_quotes: int = _int_env("OPENING_IMPULSE_EXIT_MIN_QUOTES", 4)
    opening_impulse_exit_negative_steps: int = _int_env("OPENING_IMPULSE_EXIT_NEGATIVE_STEPS", 2)
    opening_impulse_stall_buffer_pct: float = _float_env("OPENING_IMPULSE_STALL_BUFFER_PCT", 0.0005)

    ai_review: bool = os.getenv("AI_REVIEW", "false").lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    settings = Settings()
    if not settings.symbols:
        raise ValueError("SYMBOLS must include at least one ticker.")
    if not settings.strategy_names:
        raise ValueError("STRATEGIES must include at least one strategy.")
    if not settings.alpaca_api_key or not settings.alpaca_secret_key:
        raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required.")
    if settings.execution_mode not in {"local", "alpaca_paper"}:
        raise ValueError("EXECUTION_MODE must be 'local' or 'alpaca_paper'.")
    if settings.execution_mode == "alpaca_paper" and not settings.alpaca_paper:
        raise ValueError("EXECUTION_MODE=alpaca_paper requires ALPACA_PAPER=true.")

    return settings
