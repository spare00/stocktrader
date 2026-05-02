from dataclasses import dataclass, field
from typing import Any

from env_vars import (
    EnvReader,
    EnvSpec,
    bool_env as _bool_env,
    csv_env as _csv_env,
    float_env as _float_env,
    int_env as _int_env,
    lower_env as _lower_env,
    optional_int_env as _optional_int_env,
    read_env as _read_env,
    strategy_names_csv as _strategy_env,
    str_env as _str_env,
)


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
    position_sizing_mode: str = "fixed_value"
    risk_per_trade_pct: float = 0.005
    max_trade_loss_r: float = 1.2
    max_open_positions: int = 2
    trade_cooldown_seconds: int = 60
    failed_entry_cooldown_seconds: int = 30
    daily_max_loss: float = 250.0
    daily_max_loss_pct: float = 0.02
    consecutive_loss_pause_count: int = 3
    consecutive_loss_pause_minutes: int = 30
    consecutive_loss_stop_count: int = 5
    regular_market_only: bool = True
    flatten_before_close_minutes: int = 15
    heartbeat_seconds: int = 5
    alpaca_fill_timeout_seconds: float = 5.0
    alpaca_fill_poll_seconds: float = 0.25
    max_entry_chase_pct: float = 0.003

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
    opening_impulse_pullback_pct: float = 0.005
    opening_impulse_strong_volume_ratio: float = 2.5
    opening_impulse_strong_pullback_pct: float = 0.01
    opening_impulse_partial_take_profit_pct: float = 0.008
    opening_impulse_partial_take_profit_fraction: float = 0.5
    opening_impulse_runner_pullback_pct: float = 0.012
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
    maha7_pullback_reclaim_volume_min_ratio: float = 1.1
    maha7_pullback_reclaim_reentry_cooldown_seconds: int = 600
    maha7_pullback_reclaim_min_minutes_after_opening_impulse: int = 5
    maha7_pullback_reclaim_trend_min_bars: int = 3
    maha7_pullback_reclaim_min_hold_seconds: int = 120
    maha7_pullback_reclaim_max_trades_per_symbol_per_session: int = 3
    maha7_pullback_reclaim_partial_r: float = 0.5
    maha7_pullback_reclaim_target_r: float = 2.0
    maha7_pullback_reclaim_hard_target_r_exit: bool = False
    maha7_pullback_reclaim_trend_quality_enabled: bool = True
    maha7_pullback_reclaim_min_30m_range_pct: float = 0.01
    maha7_pullback_reclaim_chop_max_ma_spacing_pct: float = 0.002
    maha7_pullback_reclaim_chop_max_range_pct: float = 0.007
    maha7_pullback_reclaim_require_higher_low: bool = True
    maha7_pullback_reclaim_allow_continuation: bool = True
    maha7_pullback_reclaim_continuation_pullback_min_pct: float = 0.003
    maha7_pullback_reclaim_continuation_pullback_max_pct: float = 0.012
    maha7_pullback_reclaim_reclaim_buffer_pct: float = 0.0005
    maha7_pullback_reclaim_allow_early_trend_entry: bool = True
    maha7_pullback_reclaim_early_trend_max_bars_since_cross: int = 15
    maha7_pullback_reclaim_stall_exit_bars: int = 8
    maha7_pullback_reclaim_stall_min_progress_r: float = 0.2
    maha7_pullback_reclaim_immediate_failed_ma7_exit: bool = True
    maha7_pullback_reclaim_runner_peak_pullback_pct: float = 0.012
    maha7_pullback_reclaim_runner_confirm_break_bars: int = 2

    ai_review: bool = False


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
    ("position_sizing_mode", "POSITION_SIZING_MODE", _lower_env, "fixed_value"),
    ("risk_per_trade_pct", "RISK_PER_TRADE_PCT", _float_env, 0.005),
    ("max_trade_loss_r", "MAX_TRADE_LOSS_R", _float_env, 1.2),
    ("max_open_positions", "MAX_OPEN_POSITIONS", _int_env, 2),
    ("trade_cooldown_seconds", "TRADE_COOLDOWN_SECONDS", _int_env, 60),
    ("failed_entry_cooldown_seconds", "FAILED_ENTRY_COOLDOWN_SECONDS", _int_env, 30),
    ("daily_max_loss", "DAILY_MAX_LOSS", _float_env, 250.0),
    ("daily_max_loss_pct", "DAILY_MAX_LOSS_PCT", _float_env, 0.02),
    ("consecutive_loss_pause_count", "CONSECUTIVE_LOSS_PAUSE_COUNT", _int_env, 3),
    ("consecutive_loss_pause_minutes", "CONSECUTIVE_LOSS_PAUSE_MINUTES", _int_env, 30),
    ("consecutive_loss_stop_count", "CONSECUTIVE_LOSS_STOP_COUNT", _int_env, 5),
    ("regular_market_only", "REGULAR_MARKET_ONLY", _bool_env, True),
    ("flatten_before_close_minutes", "FLATTEN_BEFORE_CLOSE_MINUTES", _int_env, 15),
    ("heartbeat_seconds", "HEARTBEAT_SECONDS", _int_env, 5),
    ("alpaca_fill_timeout_seconds", "ALPACA_FILL_TIMEOUT_SECONDS", _float_env, 5.0),
    ("alpaca_fill_poll_seconds", "ALPACA_FILL_POLL_SECONDS", _float_env, 0.25),
    ("max_entry_chase_pct", "MAX_ENTRY_CHASE_PCT", _float_env, 0.003),
    ("ai_review", "AI_REVIEW", _bool_env, False),
)


def load_settings(strategy_names: list[str] | None = None, validate: bool = True) -> Settings:
    active_strategy_names = _strategy_env("STRATEGIES", "opening_impulse") if strategy_names is None else strategy_names
    values = _read_env(COMMON_ENV)
    values["strategy_names"] = active_strategy_names
    values["target_profit_pct"] = min(values["target_profit_pct"], 0.02)

    from strategies.registry import strategy_environment_specs

    strategy_env = strategy_environment_specs()
    for strategy_name in active_strategy_names:
        values.update(_read_env(strategy_env.get(strategy_name, ())))

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
