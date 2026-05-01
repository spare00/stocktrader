import os
from dataclasses import dataclass, field
from typing import Any, Callable


def _csv_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [part.strip().upper() for part in raw.split(",") if part.strip()]


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value is None else float(value)


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None else int(value)


def _optional_int_env(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    return default if value is None or value == "" else int(value)


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _strategy_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class Settings:
    alpaca_api_key: str | None = None
    alpaca_secret_key: str | None = None
    alpaca_paper: bool = True
    alpaca_data_feed: str = "iex"
    alpaca_stream_url: str | None = None
    alpaca_market_data_mode: str = "stream"
    alpaca_market_data_poll_seconds: float = 5.0
    execution_mode: str = "local"
    strategy_names: list[str] = field(default_factory=lambda: ["opening_impulse"])

    openai_api_key: str | None = None
    openai_model: str = "gpt-5.4-mini"

    symbols: list[str] = field(default_factory=lambda: ["AAPL", "MSFT", "NVDA", "TSLA", "META"])

    gap_and_go_start_minute: int = 0
    gap_and_go_end_minute: int = 30
    gap_and_go_min_gap_pct: float = 0.02
    gap_and_go_premarket_volume_ratio: float = 2.0
    gap_and_go_max_spread_bps: float = 10.0
    gap_and_go_min_price: float = 5.0
    gap_and_go_breakout_buffer_pct: float = 0.0
    gap_and_go_exit_activation_delay_seconds: int = 15
    gap_and_go_trailing_retrace_pct: float = 0.008
    gap_and_go_bar_window: int = 5

    spike_lookback_seconds: int = 5
    spike_change_pct: float = 0.0025
    spike_start_minute: int | None = None
    spike_end_minute: int | None = None
    volume_ratio: float = 2.0
    max_spread_bps: float = 12.0

    target_profit_pct: float = 0.01
    stop_loss_pct: float = 0.005
    max_hold_seconds: int = 120

    starting_cash: float = 25_000.0
    max_position_value: float = 2_500.0
    max_open_positions: int = 2
    trade_cooldown_seconds: int = 60
    failed_entry_cooldown_seconds: int = 30
    daily_max_loss: float = 250.0
    regular_market_only: bool = True
    flatten_before_close_minutes: int = 15
    heartbeat_seconds: int = 5
    alpaca_fill_timeout_seconds: float = 15.0
    alpaca_fill_poll_seconds: float = 0.25

    opening_impulse_start_minute: int = 0
    opening_impulse_end_minute: int = 150
    opening_impulse_last_entry_hour_et: int = 12
    opening_impulse_window_seconds: int = 30
    opening_impulse_min_quotes: int = 10
    opening_impulse_change_pct: float = 0.009
    opening_impulse_skip_extended_pct: float = 0.03
    opening_impulse_volume_ratio: float = 1.5
    opening_impulse_min_quote_move_seconds: int = 20
    opening_impulse_max_entry_extension_pct: float = 0.02
    opening_impulse_bar_confirmation: bool = True
    opening_impulse_bar_window: int = 3
    opening_impulse_bar_min_rising: int = 2
    opening_impulse_bar_change_pct: float = 0.003
    opening_impulse_bar_volume_ratio: float = 1.5
    opening_impulse_range_minutes: int = 5
    opening_impulse_enable_range_breakout: bool = True
    opening_impulse_enable_range_reversal: bool = True
    opening_impulse_range_breakout_buffer_pct: float = 0.0005
    opening_impulse_range_reversal_min_drop_pct: float = 0.005
    opening_impulse_range_reclaim_buffer_pct: float = 0.0
    opening_impulse_range_volume_ratio: float = 1.2
    opening_impulse_max_spread_bps: float = 15.0
    opening_impulse_min_quote_size: int = 25
    opening_impulse_max_negative_steps: int = 1
    opening_impulse_exit_window_seconds: int = 10
    opening_impulse_exit_min_quotes: int = 4
    opening_impulse_exit_negative_steps: int = 4
    opening_impulse_min_hold_seconds: int = 15
    opening_impulse_winner_min_pnl_pct: float = 0.003
    opening_impulse_early_loss_cut_pct: float = 0.0
    opening_impulse_stall_buffer_pct: float = 0.001
    opening_impulse_retrace_from_high_pct: float = 0.008
    opening_impulse_volume_collapse_ratio: float = 0.5
    opening_impulse_price_stall_seconds: int = 60

    maha7_pullback_reclaim_start_minute: int = 30
    maha7_pullback_reclaim_end_minute: int = 300
    maha7_pullback_reclaim_rsi_period: int = 14
    maha7_pullback_reclaim_rsi_above_min_bars: int = 2
    maha7_pullback_reclaim_flat_slope_pct: float = 0.0002
    maha7_pullback_reclaim_consolidation_candles: int = 10
    maha7_pullback_reclaim_vwap_min_distance_pct: float = 0.002
    maha7_pullback_reclaim_pullback_ma7_distance_pct: float = 0.003
    maha7_pullback_reclaim_volume_min_ratio: float = 0.8
    maha7_pullback_reclaim_reentry_cooldown_seconds: int = 600
    maha7_pullback_reclaim_min_minutes_after_opening_impulse: int = 5
    maha7_pullback_reclaim_partial_r: float = 0.5
    maha7_pullback_reclaim_target_r: float = 2.0

    ai_review: bool = False


EnvReader = Callable[[str, Any], Any]
EnvSpec = tuple[str, str, EnvReader, Any]


def _str_env(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)


def _lower_env(name: str, default: str) -> str:
    return os.getenv(name, default).lower()


COMMON_ENV: tuple[EnvSpec, ...] = (
    ("alpaca_api_key", "ALPACA_API_KEY", _str_env, None),
    ("alpaca_secret_key", "ALPACA_SECRET_KEY", _str_env, None),
    ("alpaca_paper", "ALPACA_PAPER", _bool_env, True),
    ("alpaca_data_feed", "ALPACA_DATA_FEED", _lower_env, "iex"),
    ("alpaca_stream_url", "ALPACA_STREAM_URL", _str_env, None),
    ("alpaca_market_data_mode", "ALPACA_MARKET_DATA_MODE", _lower_env, "stream"),
    ("alpaca_market_data_poll_seconds", "ALPACA_MARKET_DATA_POLL_SECONDS", _float_env, 5.0),
    ("execution_mode", "EXECUTION_MODE", _lower_env, "local"),
    ("openai_api_key", "OPENAI_API_KEY", _str_env, None),
    ("openai_model", "OPENAI_MODEL", _str_env, "gpt-5.4-mini"),
    ("symbols", "SYMBOLS", _csv_env, "AAPL,MSFT,NVDA,TSLA,META"),
    ("target_profit_pct", "TARGET_PROFIT_PCT", _float_env, 0.01),
    ("stop_loss_pct", "STOP_LOSS_PCT", _float_env, 0.005),
    ("max_hold_seconds", "MAX_HOLD_SECONDS", _int_env, 120),
    ("starting_cash", "STARTING_CASH", _float_env, 25_000.0),
    ("max_position_value", "MAX_POSITION_VALUE", _float_env, 2_500.0),
    ("max_open_positions", "MAX_OPEN_POSITIONS", _int_env, 2),
    ("trade_cooldown_seconds", "TRADE_COOLDOWN_SECONDS", _int_env, 60),
    ("failed_entry_cooldown_seconds", "FAILED_ENTRY_COOLDOWN_SECONDS", _int_env, 30),
    ("daily_max_loss", "DAILY_MAX_LOSS", _float_env, 250.0),
    ("regular_market_only", "REGULAR_MARKET_ONLY", _bool_env, True),
    ("flatten_before_close_minutes", "FLATTEN_BEFORE_CLOSE_MINUTES", _int_env, 15),
    ("heartbeat_seconds", "HEARTBEAT_SECONDS", _int_env, 5),
    ("alpaca_fill_timeout_seconds", "ALPACA_FILL_TIMEOUT_SECONDS", _float_env, 15.0),
    ("alpaca_fill_poll_seconds", "ALPACA_FILL_POLL_SECONDS", _float_env, 0.25),
    ("ai_review", "AI_REVIEW", _bool_env, False),
)

STRATEGY_ENV: dict[str, tuple[EnvSpec, ...]] = {
    "spike": (
        ("spike_lookback_seconds", "SPIKE_LOOKBACK_SECONDS", _int_env, 5),
        ("spike_change_pct", "SPIKE_CHANGE_PCT", _float_env, 0.0025),
        ("spike_start_minute", "SPIKE_START_MINUTE", _optional_int_env, None),
        ("spike_end_minute", "SPIKE_END_MINUTE", _optional_int_env, None),
        ("volume_ratio", "VOLUME_RATIO", _float_env, 2.0),
        ("max_spread_bps", "MAX_SPREAD_BPS", _float_env, 12.0),
    ),
    "gap_and_go": (
        ("gap_and_go_start_minute", "GAP_AND_GO_START_MINUTE", _int_env, 0),
        ("gap_and_go_end_minute", "GAP_AND_GO_END_MINUTE", _int_env, 30),
        ("gap_and_go_min_gap_pct", "GAP_AND_GO_MIN_GAP_PCT", _float_env, 0.02),
        ("gap_and_go_premarket_volume_ratio", "GAP_AND_GO_PREMARKET_VOLUME_RATIO", _float_env, 2.0),
        ("gap_and_go_max_spread_bps", "GAP_AND_GO_MAX_SPREAD_BPS", _float_env, 10.0),
        ("gap_and_go_min_price", "GAP_AND_GO_MIN_PRICE", _float_env, 5.0),
        ("gap_and_go_breakout_buffer_pct", "GAP_AND_GO_BREAKOUT_BUFFER_PCT", _float_env, 0.0),
        ("gap_and_go_exit_activation_delay_seconds", "GAP_AND_GO_EXIT_ACTIVATION_DELAY_SECONDS", _int_env, 15),
        ("gap_and_go_trailing_retrace_pct", "GAP_AND_GO_TRAILING_RETRACE_PCT", _float_env, 0.008),
        ("gap_and_go_bar_window", "GAP_AND_GO_BAR_WINDOW", _int_env, 5),
    ),
    "opening_impulse": (
        ("opening_impulse_start_minute", "OPENING_IMPULSE_START_MINUTE", _int_env, 0),
        ("opening_impulse_end_minute", "OPENING_IMPULSE_END_MINUTE", _int_env, 150),
        ("opening_impulse_last_entry_hour_et", "OPENING_IMPULSE_LAST_ENTRY_HOUR_ET", _int_env, 12),
        ("opening_impulse_window_seconds", "OPENING_IMPULSE_WINDOW_SECONDS", _int_env, 30),
        ("opening_impulse_min_quotes", "OPENING_IMPULSE_MIN_QUOTES", _int_env, 10),
        ("opening_impulse_change_pct", "OPENING_IMPULSE_CHANGE_PCT", _float_env, 0.009),
        ("opening_impulse_skip_extended_pct", "OPENING_IMPULSE_SKIP_EXTENDED_PCT", _float_env, 0.03),
        ("opening_impulse_volume_ratio", "OPENING_IMPULSE_VOLUME_RATIO", _float_env, 1.5),
        ("opening_impulse_min_quote_move_seconds", "OPENING_IMPULSE_MIN_QUOTE_MOVE_SECONDS", _int_env, 20),
        ("opening_impulse_max_entry_extension_pct", "OPENING_IMPULSE_MAX_ENTRY_EXTENSION_PCT", _float_env, 0.02),
        ("opening_impulse_bar_confirmation", "OPENING_IMPULSE_BAR_CONFIRMATION", _bool_env, True),
        ("opening_impulse_bar_window", "OPENING_IMPULSE_BAR_WINDOW", _int_env, 3),
        ("opening_impulse_bar_min_rising", "OPENING_IMPULSE_BAR_MIN_RISING", _int_env, 2),
        ("opening_impulse_bar_change_pct", "OPENING_IMPULSE_BAR_CHANGE_PCT", _float_env, 0.003),
        ("opening_impulse_bar_volume_ratio", "OPENING_IMPULSE_BAR_VOLUME_RATIO", _float_env, 1.5),
        ("opening_impulse_range_minutes", "OPENING_IMPULSE_RANGE_MINUTES", _int_env, 5),
        ("opening_impulse_enable_range_breakout", "OPENING_IMPULSE_ENABLE_RANGE_BREAKOUT", _bool_env, True),
        ("opening_impulse_enable_range_reversal", "OPENING_IMPULSE_ENABLE_RANGE_REVERSAL", _bool_env, True),
        ("opening_impulse_range_breakout_buffer_pct", "OPENING_IMPULSE_RANGE_BREAKOUT_BUFFER_PCT", _float_env, 0.0005),
        ("opening_impulse_range_reversal_min_drop_pct", "OPENING_IMPULSE_RANGE_REVERSAL_MIN_DROP_PCT", _float_env, 0.005),
        ("opening_impulse_range_reclaim_buffer_pct", "OPENING_IMPULSE_RANGE_RECLAIM_BUFFER_PCT", _float_env, 0.0),
        ("opening_impulse_range_volume_ratio", "OPENING_IMPULSE_RANGE_VOLUME_RATIO", _float_env, 1.2),
        ("opening_impulse_max_spread_bps", "OPENING_IMPULSE_MAX_SPREAD_BPS", _float_env, 15.0),
        ("opening_impulse_min_quote_size", "OPENING_IMPULSE_MIN_QUOTE_SIZE", _int_env, 25),
        ("opening_impulse_max_negative_steps", "OPENING_IMPULSE_MAX_NEGATIVE_STEPS", _int_env, 1),
        ("opening_impulse_exit_window_seconds", "OPENING_IMPULSE_EXIT_WINDOW_SECONDS", _int_env, 10),
        ("opening_impulse_exit_min_quotes", "OPENING_IMPULSE_EXIT_MIN_QUOTES", _int_env, 4),
        ("opening_impulse_exit_negative_steps", "OPENING_IMPULSE_EXIT_NEGATIVE_STEPS", _int_env, 4),
        ("opening_impulse_min_hold_seconds", "OPENING_IMPULSE_MIN_HOLD_SECONDS", _int_env, 15),
        ("opening_impulse_winner_min_pnl_pct", "OPENING_IMPULSE_WINNER_MIN_PNL_PCT", _float_env, 0.003),
        ("opening_impulse_early_loss_cut_pct", "OPENING_IMPULSE_EARLY_LOSS_CUT_PCT", _float_env, 0.0),
        ("opening_impulse_stall_buffer_pct", "OPENING_IMPULSE_STALL_BUFFER_PCT", _float_env, 0.001),
        ("opening_impulse_retrace_from_high_pct", "OPENING_IMPULSE_RETRACE_FROM_HIGH_PCT", _float_env, 0.008),
        ("opening_impulse_volume_collapse_ratio", "OPENING_IMPULSE_VOLUME_COLLAPSE_RATIO", _float_env, 0.5),
        ("opening_impulse_price_stall_seconds", "OPENING_IMPULSE_PRICE_STALL_SECONDS", _int_env, 60),
    ),
    "maha7_pullback_reclaim": (
        ("maha7_pullback_reclaim_start_minute", "MAHA7_PULLBACK_RECLAIM_START_MINUTE", _int_env, 30),
        ("maha7_pullback_reclaim_end_minute", "MAHA7_PULLBACK_RECLAIM_END_MINUTE", _int_env, 300),
        ("maha7_pullback_reclaim_rsi_period", "MAHA7_PULLBACK_RECLAIM_RSI_PERIOD", _int_env, 14),
        ("maha7_pullback_reclaim_rsi_above_min_bars", "MAHA7_PULLBACK_RECLAIM_RSI_ABOVE_MIN_BARS", _int_env, 2),
        ("maha7_pullback_reclaim_flat_slope_pct", "MAHA7_PULLBACK_RECLAIM_FLAT_SLOPE_PCT", _float_env, 0.0002),
        ("maha7_pullback_reclaim_consolidation_candles", "MAHA7_PULLBACK_RECLAIM_CONSOLIDATION_CANDLES", _int_env, 10),
        ("maha7_pullback_reclaim_vwap_min_distance_pct", "MAHA7_PULLBACK_RECLAIM_VWAP_MIN_DISTANCE_PCT", _float_env, 0.002),
        ("maha7_pullback_reclaim_pullback_ma7_distance_pct", "MAHA7_PULLBACK_RECLAIM_PULLBACK_MA7_DISTANCE_PCT", _float_env, 0.003),
        ("maha7_pullback_reclaim_volume_min_ratio", "MAHA7_PULLBACK_RECLAIM_VOLUME_MIN_RATIO", _float_env, 0.8),
        ("maha7_pullback_reclaim_reentry_cooldown_seconds", "MAHA7_PULLBACK_RECLAIM_REENTRY_COOLDOWN_SECONDS", _int_env, 600),
        ("maha7_pullback_reclaim_min_minutes_after_opening_impulse", "MAHA7_PULLBACK_RECLAIM_MIN_MINUTES_AFTER_OPENING_IMPULSE", _int_env, 5),
        ("maha7_pullback_reclaim_partial_r", "MAHA7_PULLBACK_RECLAIM_PARTIAL_R", _float_env, 0.5),
        ("maha7_pullback_reclaim_target_r", "MAHA7_PULLBACK_RECLAIM_TARGET_R", _float_env, 2.0),
    ),
}


def _read_env(specs: tuple[EnvSpec, ...]) -> dict[str, Any]:
    return {field_name: reader(env_name, default) for field_name, env_name, reader, default in specs}


def load_settings(strategy_names: list[str] | None = None, validate: bool = True) -> Settings:
    active_strategy_names = _strategy_env("STRATEGIES", "opening_impulse") if strategy_names is None else strategy_names
    values = _read_env(COMMON_ENV)
    values["strategy_names"] = active_strategy_names
    values["target_profit_pct"] = min(values["target_profit_pct"], 0.02)

    for strategy_name in active_strategy_names:
        values.update(_read_env(STRATEGY_ENV.get(strategy_name, ())))

    settings = Settings(**values)
    if not validate:
        return settings

    if not settings.symbols:
        raise ValueError("SYMBOLS must include at least one ticker.")
    if strategy_names is None and not settings.strategy_names:
        raise ValueError("STRATEGIES must include at least one strategy.")
    if not settings.alpaca_api_key or not settings.alpaca_secret_key:
        raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required.")
    if settings.execution_mode not in {"local", "alpaca_paper"}:
        raise ValueError("EXECUTION_MODE must be 'local' or 'alpaca_paper'.")
    if settings.execution_mode == "alpaca_paper" and not settings.alpaca_paper:
        raise ValueError("EXECUTION_MODE=alpaca_paper requires ALPACA_PAPER=true.")
    if settings.alpaca_market_data_mode not in {"stream", "rest"}:
        raise ValueError("ALPACA_MARKET_DATA_MODE must be 'stream' or 'rest'.")
    if settings.alpaca_market_data_poll_seconds <= 0:
        raise ValueError("ALPACA_MARKET_DATA_POLL_SECONDS must be greater than 0.")

    return settings
