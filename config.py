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


@dataclass(frozen=True)
class Settings:
    massive_api_key: str | None = os.getenv("MASSIVE_API_KEY")
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")

    symbols: list[str] = field(default_factory=lambda: _csv_env("SYMBOLS", "AAPL,MSFT,NVDA,TSLA,META"))
    websocket_url: str = os.getenv("MASSIVE_WS_URL", "wss://socket.massive.com/stocks")

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

    ai_review: bool = os.getenv("AI_REVIEW", "false").lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    settings = Settings()
    if not settings.massive_api_key:
        raise ValueError("MASSIVE_API_KEY is required for live market data.")
    if not settings.symbols:
        raise ValueError("SYMBOLS must include at least one ticker.")
    return settings
