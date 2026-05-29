import os
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
    alpaca_trading_base_url: str | None = None
    alpaca_data_base_url: str | None = None
    alpaca_market_data_mode: str = "rest"
    alpaca_market_data_poll_seconds: float = 5.0
    replay_use_mock_clock: bool = False
    replay_closed_bars_only: bool = False
    replay_clock_timeout_seconds: float = 0.5
    execution_mode: str = "local"
    strategy_names: list[str] = field(default_factory=lambda: ["opening_impulse"])

    openai_api_key: str | None = None
    openai_model: str = "gpt-5.4-mini"

    symbols: list[str] = field(default_factory=list)

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
    gap_and_go_max_trades_per_symbol_per_session: int = 2
    gap_and_go_symbol_loss_lock_count: int = 2
    gap_and_go_consecutive_loss_pause_count: int | None = None
    gap_and_go_consecutive_loss_pause_minutes: int | None = None
    gap_and_go_consecutive_loss_stop_count: int | None = None

    spike_lookback_seconds: int = 5
    spike_change_pct: float = 0.0025
    spike_start_minute: int | None = None
    spike_end_minute: int | None = None
    spike_consecutive_loss_pause_count: int | None = None
    spike_consecutive_loss_pause_minutes: int | None = None
    spike_consecutive_loss_stop_count: int | None = None
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
    max_open_positions: int = 8
    trade_cooldown_seconds: int = 60
    opening_impulse_max_open_positions: int = 0
    opening_impulse_max_position_value: float = 0.0
    opening_impulse_max_hold_seconds: int = 0
    opening_impulse_consecutive_loss_pause_count: int | None = None
    opening_impulse_consecutive_loss_pause_minutes: int | None = None
    opening_impulse_consecutive_loss_stop_count: int | None = None
    gap_and_go_max_open_positions: int = 0
    gap_and_go_max_position_value: float = 0.0
    gap_and_go_max_hold_seconds: int = 0
    spike_max_open_positions: int = 0
    spike_max_position_value: float = 0.0
    spike_max_hold_seconds: int = 0
    stoch_macd_max_open_positions: int = 0
    stoch_macd_max_position_value: float = 0.0
    stoch_macd_max_hold_seconds: int = 0
    stoch_macd_trade_cooldown_seconds: int = 0
    stoch_macd_consecutive_loss_pause_count: int | None = None
    stoch_macd_consecutive_loss_pause_minutes: int | None = None
    stoch_macd_consecutive_loss_stop_count: int | None = None
    macd_max_open_positions: int = 0
    macd_max_position_value: float = 0.0
    macd_max_hold_seconds: int = 0
    macd_trade_cooldown_seconds: int = 0
    macd_burst_max_entries: int = 0
    macd_burst_window_seconds: int = 0
    macd_consecutive_loss_pause_count: int | None = None
    macd_consecutive_loss_pause_minutes: int | None = None
    macd_consecutive_loss_stop_count: int | None = None
    steady_intraday_max_open_positions: int = 0
    steady_intraday_max_position_value: float = 0.0
    steady_intraday_max_hold_seconds: int = 0
    steady_intraday_trade_cooldown_seconds: int = 0
    steady_intraday_consecutive_loss_pause_count: int | None = None
    steady_intraday_consecutive_loss_pause_minutes: int | None = None
    steady_intraday_consecutive_loss_stop_count: int | None = None
    maha7_max_open_positions: int = 0
    maha7_max_position_value: float = 0.0
    maha7_max_hold_seconds: int = 0
    maha7_consecutive_loss_pause_count: int | None = None
    maha7_consecutive_loss_pause_minutes: int | None = None
    maha7_consecutive_loss_stop_count: int | None = None
    failed_entry_cooldown_seconds: int = 30
    daily_max_loss: float = 250.0
    daily_max_loss_pct: float = 0.02
    consecutive_loss_pause_count: int | None = None
    consecutive_loss_pause_minutes: int = 30
    consecutive_loss_stop_count: int | None = None
    regular_market_only: bool = True
    flatten_before_close_minutes: int = 15
    heartbeat_seconds: int = 5
    alpaca_fill_timeout_seconds: float = 5.0
    alpaca_fill_poll_seconds: float = 0.25
    max_entry_chase_pct: float = 0.003
    replay_market_data: bool = False

    market_regime_enabled: bool = True
    market_regime_symbols: list[str] = field(default_factory=lambda: ["SPY", "QQQ", "IWM"])
    market_regime_min_bars: int = 20
    market_regime_risk_off_score: float = -3.0
    market_regime_block_score: float = -7.0
    market_regime_risk_on_score: float = 5.0
    market_regime_risk_off_size_multiplier: float = 0.5
    market_regime_risk_on_size_multiplier: float = 1.0
    market_regime_log_changes: bool = True
    market_regime_bypass_strategies: list[str] = field(default_factory=list)
    market_regime_positive_weight: float = 1.0
    market_regime_below_vwap_weight: float = 2.0
    market_regime_vwap_falling_weight: float = 1.0
    market_regime_below_ema_weight: float = 2.0
    market_regime_spy_weight: float = 1.25
    market_regime_qqq_weight: float = 1.5
    market_regime_iwm_weight: float = 1.0
    market_regime_default_symbol_weight: float = 1.0

    indicator_preload_bars: int = 1000
    indicator_max_bars_per_symbol: int = 3000
    indicator_include_afterhours: bool = False

    opening_impulse_start_minute: int = 0
    opening_impulse_end_minute: int = 150
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
    opening_impulse_news_hot_minutes: int = 10
    opening_impulse_news_change_pct: float = 0.003
    opening_impulse_news_min_volume_ratio: float = 1.3
    opening_impulse_news_tight_pullback_pct: float = 0.003
    opening_impulse_news_max_hold_seconds: int = 90
    opening_impulse_news_max_move_since_event_pct: float = 0.02
    opening_impulse_max_trades_per_symbol_per_session: int = 2
    opening_impulse_symbol_loss_lock_count: int = 2
    opening_impulse_failed_continuation_no_high_seconds: int = 120
    opening_impulse_failed_continuation_max_mfe_pct: float = 0.004
    opening_impulse_reentry_reclaim_lookback_bars: int = 5
    opening_impulse_reentry_min_volume_ratio: float = 1.3

    maha7_start_minute: int = 30
    maha7_end_minute: int = 210
    maha7_rsi_period: int = 14
    maha7_rsi_above_min_bars: int = 2
    maha7_flat_slope_pct: float = 0.0002
    maha7_consolidation_candles: int = 10
    maha7_vwap_min_distance_pct: float = 0.002
    maha7_pullback_ma7_distance_pct: float = 0.003
    maha7_volume_min_ratio: float = 1.25
    maha7_reentry_cooldown_seconds: int = 1200
    maha7_min_minutes_after_opening_impulse: int = 5
    maha7_trend_min_bars: int = 3
    maha7_min_hold_seconds: int = 120
    maha7_max_trades_per_symbol_per_session: int = 2
    maha7_symbol_loss_lock_count: int = 1
    maha7_early_loss_cut_seconds: int = 120
    maha7_early_loss_cut_pct: float = 0.002
    maha7_partial_r: float = 0.5
    maha7_partial_size: float = 0.5
    maha7_target_r: float = 2.0
    maha7_move_stop_to_entry_after_partial: bool = True
    maha7_hard_target_r_exit: bool = True
    maha7_trend_quality_enabled: bool = True
    maha7_min_30m_range_pct: float = 0.01
    maha7_chop_max_ma_spacing_pct: float = 0.002
    maha7_chop_max_range_pct: float = 0.007
    maha7_require_higher_low: bool = True
    maha7_allow_continuation: bool = True
    maha7_continuation_pullback_min_pct: float = 0.003
    maha7_continuation_pullback_max_pct: float = 0.012
    maha7_reclaim_buffer_pct: float = 0.0005
    maha7_allow_early_trend_entry: bool = True
    maha7_early_trend_max_bars_since_cross: int = 15
    maha7_runner_confirm_break_bars: int = 2
    maha7_runner_peak_pullback_pct: float = 0.012
    maha7_swing_lookback: int = 5
    maha7_stop_anchor_buffer_pct: float = 0.001
    maha7_min_r_pct: float = 0.003
    maha7_max_r_pct: float = 0.012
    maha7_continuation_volume_ratio: float = 1.35
    maha7_max_chase_pct: float = 0.01
    maha7_recent_high_lookback: int = 20
    maha7_momentum_green_bars: int = 2
    maha7_disable_ma7_exit: bool = False

    steady_intraday_start_minute: int = 15
    steady_intraday_end_minute: int = 330
    steady_intraday_min_bars: int = 55
    steady_intraday_orb_minutes: int = 15
    steady_intraday_ema_fast: int = 9
    steady_intraday_ema_mid: int = 20
    steady_intraday_ema_slow: int = 50
    steady_intraday_atr_period: int = 14
    steady_intraday_min_atr_pct: float = 0.0018
    steady_intraday_max_atr_pct: float = 0.015
    steady_intraday_min_range_pct: float = 0.006
    steady_intraday_min_volume_ratio: float = 1.15
    steady_intraday_breakout_volume_ratio: float = 1.35
    steady_intraday_max_spread_bps: float = 12.0
    steady_intraday_min_price: float = 5.0
    steady_intraday_vwap_buffer_pct: float = 0.0005
    steady_intraday_max_vwap_extension_pct: float = 0.025
    steady_intraday_max_ema_extension_pct: float = 0.012
    steady_intraday_stop_atr_multiple: float = 1.1
    steady_intraday_stop_buffer_pct: float = 0.0008
    steady_intraday_min_r_pct: float = 0.0025
    steady_intraday_max_r_pct: float = 0.012
    steady_intraday_partial_r: float = 1.0
    steady_intraday_partial_size: float = 0.5
    steady_intraday_target_r: float = 2.0
    steady_intraday_runner_pullback_pct: float = 0.009
    steady_intraday_min_hold_seconds: int = 30
    steady_intraday_breakdown_bars: int = 2
    steady_intraday_breakdown_min_hold_seconds: int = 900
    steady_intraday_lost_vwap_min_hold_seconds: int = 900
    steady_intraday_stall_minutes: int = 25
    steady_intraday_stall_min_r: float = 0.35
    steady_intraday_position_size_multiplier: float = 0.8
    steady_intraday_max_trades_per_symbol_per_session: int = 2
    steady_intraday_symbol_loss_lock_count: int = 1
    steady_intraday_allow_orb_breakout: bool = True
    steady_intraday_allow_pullback_reclaim: bool = True

    macd_start_minute: int = 0
    macd_end_minute: int = 360
    macd_hist_threshold: float = 0.001
    macd_volume_ratio: float = 1.35
    macd_target_profit_pct: float = 0.012
    macd_stop_loss_pct: float = 0.0035
    macd_trailing_stop_pct: float = 0.0045
    macd_trailing_activation_pct: float = 0.003
    macd_chop_range_pct: float = 0.0035
    macd_max_spread_bps: float = 15.0
    macd_atr_period: int = 14
    macd_min_atr_pct: float = 0.0015
    macd_max_atr_pct: float = 0.0300
    macd_range_lookback_bars: int = 20
    macd_min_range_pct: float = 0.0040
    macd_structure_lookback_bars: int = 6
    macd_stop_buffer_pct: float = 0.0008
    macd_min_r_pct: float = 0.0020
    macd_max_r_pct: float = 0.0150
    macd_partial_r: float = 1.0
    macd_partial_size: float = 0.5
    macd_target_r: float = 2.0
    macd_runner_pullback_pct: float = 0.0090
    macd_early_window_minutes: int = 60
    macd_early_min_volume_ratio: float = 1.50
    macd_early_volume_average_fallback_enabled: bool = False
    macd_early_volume_lookback_bars: int = 3
    macd_early_min_avg_volume_ratio: float = 1.40
    macd_early_min_latest_volume_ratio: float = 0.70
    macd_early_max_spread_bps: float = 12.0
    macd_early_min_hist_norm: float = 0.0012
    macd_early_max_vwap_extension_pct: float = 0.015
    macd_volume_impulse_runner_volume_ratio: float = 3.0
    macd_volume_impulse_runner_hist_norm: float = 0.0015
    macd_volume_impulse_max_spread_bps: float = 30.0
    macd_volume_impulse_size_multiplier: float = 0.5
    macd_skip_midday: bool = False
    macd_min_hold_seconds: int = 60
    macd_hist_rise_bars: int = 2
    macd_require_positive_hist: bool = True
    macd_momentum_exit_min_profit_pct: float = 0.0015
    macd_early_loss_cut_seconds: int = 75
    macd_early_loss_cut_pct: float = 0.0022
    macd_early_impulse_max_trades_per_symbol_per_session: int = 2
    macd_early_impulse_symbol_loss_lock_count: int = 2
    macd_macd_warmup_bars: int = 120
    macd_risk_off_hist_multiplier: float = 1.35
    macd_risk_off_volume_add: float = 0.25
    macd_risk_off_chop_range_multiplier: float = 1.15
    macd_risk_off_min_range_multiplier: float = 1.15
    macd_risk_off_max_extension_multiplier: float = 0.80
    macd_risk_off_max_vwap_extension_multiplier: float = 0.80
    macd_risk_off_max_r_multiplier: float = 0.85
    macd_risk_on_hist_multiplier: float = 0.90
    macd_risk_on_volume_add: float = -0.10
    macd_risk_on_chop_range_multiplier: float = 0.90
    macd_risk_on_min_range_multiplier: float = 0.90
    macd_risk_on_max_extension_multiplier: float = 1.10
    macd_risk_on_max_vwap_extension_multiplier: float = 1.10
    macd_risk_on_max_r_multiplier: float = 1.05
    macd_neutral_hist_multiplier: float = 1.10
    macd_neutral_volume_add: float = 0.08
    macd_neutral_chop_range_multiplier: float = 1.10
    macd_neutral_min_range_multiplier: float = 1.10
    macd_neutral_max_extension_multiplier: float = 0.90
    macd_neutral_max_vwap_extension_multiplier: float = 0.90
    macd_neutral_max_r_multiplier: float = 0.92

    stoch_macd_start_minute: int = 0
    stoch_macd_end_minute: int = 360
    stoch_macd_min_bars: int = 0
    stoch_macd_ema_period: int = 5
    stoch_macd_supertrend_enabled: bool = True
    stoch_macd_require_ema_above_supertrend: bool = False
    stoch_macd_supertrend_buffer_pct: float = 0.0005
    stoch_macd_supertrend_period: int = 7
    stoch_macd_supertrend_multiplier: float = 3.0
    stoch_macd_min_volume_ratio: float = 0.80
    stoch_macd_max_spread_bps: float = 15.0
    stoch_macd_vwap_enabled: bool = True
    stoch_macd_vwap_buffer_pct: float = 0.0005
    stoch_macd_require_vwap_rising: bool = True
    stoch_macd_min_hist_norm: float = 0.00005
    stoch_macd_hist_rise_bars: int = 2
    stoch_macd_macd_rise_bars: int = 2
    stoch_macd_stoch_cross_lookback_bars: int = 3
    stoch_macd_max_k: float = 88.0
    stoch_macd_allow_overbought_expansion: bool = False
    stoch_macd_overbought_min_hist_rise_norm: float = 0.00005
    stoch_macd_ao_filter_enabled: bool = False
    stoch_macd_ao_fast_period: int = 5
    stoch_macd_ao_slow_period: int = 34
    stoch_macd_ao_rise_bars: int = 2
    stoch_macd_ao_min_value: float = 0.0
    stoch_macd_atr_period: int = 14
    stoch_macd_min_atr_pct: float = 0.0015
    stoch_macd_max_atr_pct: float = 0.0300
    stoch_macd_range_lookback_bars: int = 20
    stoch_macd_min_range_pct: float = 0.0040
    stoch_macd_partial_r: float = 1.0
    stoch_macd_partial_size: float = 0.5
    stoch_macd_runner_pullback_pct: float = 0.006
    stoch_macd_structure_lookback_bars: int = 6
    stoch_macd_min_r_pct: float = 0.0025
    stoch_macd_max_r_pct: float = 0.0120
    stoch_macd_early_window_minutes: int = 60
    stoch_macd_early_min_volume_ratio: float = 1.20
    stoch_macd_early_volume_lookback_bars: int = 3
    stoch_macd_early_min_avg_volume_ratio: float = 0.90
    stoch_macd_early_max_spread_bps: float = 12.0
    stoch_macd_early_min_hist_norm: float = 0.00010
    stoch_macd_early_vwap_buffer_pct: float = 0.0010
    stoch_macd_stop_loss_pct: float = 0.0045
    stoch_macd_target_profit_pct: float = 0.012
    stoch_macd_trailing_activation_pct: float = 0.004
    stoch_macd_trailing_stop_pct: float = 0.005
    stoch_macd_min_hold_seconds: int = 30
    stoch_macd_stop_grace_seconds: int = 20
    stoch_macd_stop_confirmations: int = 2
    stoch_macd_stop_max_spread_bps: float = 30.0
    stoch_macd_catastrophic_stop_loss_pct: float = 0.01
    stoch_macd_max_trades_per_symbol_per_session: int = 2
    stoch_macd_symbol_loss_lock_count: int = 1
    stoch_macd_macd_warmup_bars: int = 35
    stoch_macd_risk_off_stoch_cross_lookback_bars: int = 3
    stoch_macd_risk_off_hist_multiplier: float = 1.5
    stoch_macd_risk_off_volume_add: float = 0.3
    stoch_macd_risk_off_vwap_buffer_multiplier: float = 1.5
    stoch_macd_risk_off_max_r_multiplier: float = 0.8
    stoch_macd_neutral_hist_multiplier: float = 1.15
    stoch_macd_neutral_volume_add: float = 0.10
    stoch_macd_neutral_vwap_buffer_multiplier: float = 1.20
    stoch_macd_neutral_max_r_multiplier: float = 0.90
    stoch_macd_reentry_fresh_enabled: bool = True
    stoch_macd_reentry_fresh_lookback_bars: int = 20
    stoch_macd_reentry_high_buffer_pct: float = 0.0005
    stoch_macd_reentry_hist_rise_multiplier: float = 1.25
    stoch_macd_reentry_volume_add: float = 0.25
    stoch_macd_reentry_max_k: float = 84.0
    stoch_macd_reentry_max_support_extension_pct: float = 0.0045
    stoch_macd_respect_consecutive_loss_limits: bool = False

    bp_start_minute: int = 0
    bp_end_minute: int = 360
    bp_warmup_bars: int = 40
    bp_green_threshold: float = 65.0
    bp_trend_line: float = 50.0
    bp_hold_floor: float = 45.0
    bp_decline_grace_bars: int = 2
    bp_partial_size: float = 0.5
    bp_momentum_ema_period: int = 4
    bp_max_spread_bps: float = 15.0
    bp_stop_lookback_bars: int = 6
    bp_stop_buffer_pct: float = 0.001
    bp_stop_loss_pct: float = 0.005
    bp_min_hold_seconds: int = 0
    bp_max_trades_per_symbol_per_session: int = 3
    bp_symbol_loss_lock_count: int = 1
    bp_respect_consecutive_loss_limits: bool = False

    ai_review: bool = False
    news_hot_positive_only: bool = True
    news_hot_min_sentiment_score: float = 0.5
    news_log_events: bool = False
    news_dynamic_symbols_enabled: bool = False
    news_listener_positive_only: bool = True
    news_listener_min_impact: float = 0.5
    news_listener_symbol_cooldown_seconds: int = 120
    dynamic_execution_selector_enabled: bool = False
    dynamic_execution_selector_universe_file: str = "data/opening_universe.txt"
    dynamic_execution_selector_candidate_limit: int = 100
    dynamic_execution_selector_top_dollar_volume_count: int = 30
    dynamic_execution_selector_strength_threshold: float = 120.0
    dynamic_execution_selector_lookback_seconds: int = 60
    dynamic_execution_selector_min_dollar_volume: float = 500_000.0
    dynamic_execution_selector_cooldown_seconds: int = 600


COMMON_ENV: tuple[EnvSpec, ...] = (
    ("alpaca_api_key", "ALPACA_API_KEY", _str_env, None),
    ("alpaca_secret_key", "ALPACA_SECRET_KEY", _str_env, None),
    ("alpaca_paper", "ALPACA_PAPER", _bool_env, True),
    ("alpaca_data_feed", "ALPACA_DATA_FEED", _lower_env, "iex"),
    ("alpaca_stream_url", "ALPACA_STREAM_URL", _str_env, None),
    ("alpaca_trading_base_url", "ALPACA_TRADING_BASE_URL", _str_env, None),
    ("alpaca_data_base_url", "ALPACA_DATA_BASE_URL", _str_env, None),
    ("alpaca_market_data_mode", "ALPACA_MARKET_DATA_MODE", _lower_env, "rest"),
    ("alpaca_market_data_poll_seconds", "ALPACA_MARKET_DATA_POLL_SECONDS", _float_env, 1.0),
    ("replay_use_mock_clock", "REPLAY_USE_MOCK_CLOCK", _bool_env, False),
    ("replay_closed_bars_only", "REPLAY_CLOSED_BARS_ONLY", _bool_env, False),
    ("replay_clock_timeout_seconds", "REPLAY_CLOCK_TIMEOUT_SECONDS", _float_env, 0.5),
    ("execution_mode", "EXECUTION_MODE", _lower_env, "local"),
    ("openai_api_key", "OPENAI_API_KEY", _str_env, None),
    ("openai_model", "OPENAI_MODEL", _str_env, "gpt-5.4-mini"),
    ("symbols", "SYMBOLS", _csv_env, ""),
    ("target_profit_pct", "TARGET_PROFIT_PCT", _float_env, 0.01),
    ("stop_loss_pct", "STOP_LOSS_PCT", _float_env, 0.005),
    ("max_hold_seconds", "MAX_HOLD_SECONDS", _int_env, 120),
    ("starting_cash", "STARTING_CASH", _float_env, 25_000.0),
    ("max_position_value", "MAX_POSITION_VALUE", _float_env, 2_500.0),
    ("position_sizing_mode", "POSITION_SIZING_MODE", _lower_env, "fixed_value"),
    ("risk_per_trade_pct", "RISK_PER_TRADE_PCT", _float_env, 0.005),
    ("max_trade_loss_r", "MAX_TRADE_LOSS_R", _float_env, 1.2),
    ("max_open_positions", "MAX_OPEN_POSITIONS", _int_env, 8),
    ("trade_cooldown_seconds", "TRADE_COOLDOWN_SECONDS", _int_env, 60),
    ("opening_impulse_max_open_positions", "OPENING_IMPULSE_MAX_OPEN_POSITIONS", _int_env, 0),
    ("opening_impulse_max_position_value", "OPENING_IMPULSE_MAX_POSITION_VALUE", _float_env, 0.0),
    ("opening_impulse_max_hold_seconds", "OPENING_IMPULSE_MAX_HOLD_SECONDS", _int_env, 0),
    (
        "opening_impulse_consecutive_loss_pause_count",
        "OPENING_IMPULSE_CONSECUTIVE_LOSS_PAUSE_COUNT",
        _optional_int_env,
        None,
    ),
    (
        "opening_impulse_consecutive_loss_pause_minutes",
        "OPENING_IMPULSE_CONSECUTIVE_LOSS_PAUSE_MINUTES",
        _optional_int_env,
        None,
    ),
    (
        "opening_impulse_consecutive_loss_stop_count",
        "OPENING_IMPULSE_CONSECUTIVE_LOSS_STOP_COUNT",
        _optional_int_env,
        None,
    ),
    ("gap_and_go_consecutive_loss_pause_count", "GAP_AND_GO_CONSECUTIVE_LOSS_PAUSE_COUNT", _optional_int_env, None),
    (
        "gap_and_go_consecutive_loss_pause_minutes",
        "GAP_AND_GO_CONSECUTIVE_LOSS_PAUSE_MINUTES",
        _optional_int_env,
        None,
    ),
    ("gap_and_go_consecutive_loss_stop_count", "GAP_AND_GO_CONSECUTIVE_LOSS_STOP_COUNT", _optional_int_env, None),
    ("gap_and_go_max_open_positions", "GAP_AND_GO_MAX_OPEN_POSITIONS", _int_env, 0),
    ("gap_and_go_max_position_value", "GAP_AND_GO_MAX_POSITION_VALUE", _float_env, 0.0),
    ("gap_and_go_max_hold_seconds", "GAP_AND_GO_MAX_HOLD_SECONDS", _int_env, 0),
    ("spike_consecutive_loss_pause_count", "SPIKE_CONSECUTIVE_LOSS_PAUSE_COUNT", _optional_int_env, None),
    ("spike_consecutive_loss_pause_minutes", "SPIKE_CONSECUTIVE_LOSS_PAUSE_MINUTES", _optional_int_env, None),
    ("spike_consecutive_loss_stop_count", "SPIKE_CONSECUTIVE_LOSS_STOP_COUNT", _optional_int_env, None),
    ("spike_max_open_positions", "SPIKE_MAX_OPEN_POSITIONS", _int_env, 0),
    ("spike_max_position_value", "SPIKE_MAX_POSITION_VALUE", _float_env, 0.0),
    ("spike_max_hold_seconds", "SPIKE_MAX_HOLD_SECONDS", _int_env, 0),
    ("stoch_macd_max_open_positions", "STOCH_MACD_MAX_OPEN_POSITIONS", _int_env, 0),
    ("stoch_macd_max_position_value", "STOCH_MACD_MAX_POSITION_VALUE", _float_env, 0.0),
    ("stoch_macd_max_hold_seconds", "STOCH_MACD_MAX_HOLD_SECONDS", _int_env, 0),
    ("stoch_macd_trade_cooldown_seconds", "STOCH_MACD_TRADE_COOLDOWN_SECONDS", _int_env, 0),
    ("stoch_macd_consecutive_loss_pause_count", "STOCH_MACD_CONSECUTIVE_LOSS_PAUSE_COUNT", _optional_int_env, None),
    (
        "stoch_macd_consecutive_loss_pause_minutes",
        "STOCH_MACD_CONSECUTIVE_LOSS_PAUSE_MINUTES",
        _optional_int_env,
        None,
    ),
    ("stoch_macd_consecutive_loss_stop_count", "STOCH_MACD_CONSECUTIVE_LOSS_STOP_COUNT", _optional_int_env, None),
    ("macd_max_open_positions", "MACD_MAX_OPEN_POSITIONS", _int_env, 0),
    ("macd_max_position_value", "MACD_MAX_POSITION_VALUE", _float_env, 0.0),
    ("macd_max_hold_seconds", "MACD_MAX_HOLD_SECONDS", _int_env, 0),
    ("macd_trade_cooldown_seconds", "MACD_TRADE_COOLDOWN_SECONDS", _int_env, 0),
    ("macd_burst_max_entries", "MACD_BURST_MAX_ENTRIES", _int_env, 0),
    ("macd_burst_window_seconds", "MACD_BURST_WINDOW_SECONDS", _int_env, 0),
    ("macd_consecutive_loss_pause_count", "MACD_CONSECUTIVE_LOSS_PAUSE_COUNT", _optional_int_env, None),
    ("macd_consecutive_loss_pause_minutes", "MACD_CONSECUTIVE_LOSS_PAUSE_MINUTES", _optional_int_env, None),
    ("macd_consecutive_loss_stop_count", "MACD_CONSECUTIVE_LOSS_STOP_COUNT", _optional_int_env, None),
    ("steady_intraday_max_open_positions", "STEADY_INTRADAY_MAX_OPEN_POSITIONS", _int_env, 0),
    ("steady_intraday_max_position_value", "STEADY_INTRADAY_MAX_POSITION_VALUE", _float_env, 0.0),
    ("steady_intraday_max_hold_seconds", "STEADY_INTRADAY_MAX_HOLD_SECONDS", _int_env, 0),
    ("steady_intraday_trade_cooldown_seconds", "STEADY_INTRADAY_TRADE_COOLDOWN_SECONDS", _int_env, 0),
    (
        "steady_intraday_consecutive_loss_pause_count",
        "STEADY_INTRADAY_CONSECUTIVE_LOSS_PAUSE_COUNT",
        _optional_int_env,
        None,
    ),
    (
        "steady_intraday_consecutive_loss_pause_minutes",
        "STEADY_INTRADAY_CONSECUTIVE_LOSS_PAUSE_MINUTES",
        _optional_int_env,
        None,
    ),
    (
        "steady_intraday_consecutive_loss_stop_count",
        "STEADY_INTRADAY_CONSECUTIVE_LOSS_STOP_COUNT",
        _optional_int_env,
        None,
    ),
    ("maha7_consecutive_loss_pause_count", "MAHA7_CONSECUTIVE_LOSS_PAUSE_COUNT", _optional_int_env, None),
    ("maha7_consecutive_loss_pause_minutes", "MAHA7_CONSECUTIVE_LOSS_PAUSE_MINUTES", _optional_int_env, None),
    ("maha7_consecutive_loss_stop_count", "MAHA7_CONSECUTIVE_LOSS_STOP_COUNT", _optional_int_env, None),
    ("maha7_max_open_positions", "MAHA7_MAX_OPEN_POSITIONS", _int_env, 0),
    ("maha7_max_position_value", "MAHA7_MAX_POSITION_VALUE", _float_env, 0.0),
    ("maha7_max_hold_seconds", "MAHA7_MAX_HOLD_SECONDS", _int_env, 0),
    ("failed_entry_cooldown_seconds", "FAILED_ENTRY_COOLDOWN_SECONDS", _int_env, 30),
    ("daily_max_loss", "DAILY_MAX_LOSS", _float_env, 250.0),
    ("daily_max_loss_pct", "DAILY_MAX_LOSS_PCT", _float_env, 0.02),
    ("consecutive_loss_pause_count", "CONSECUTIVE_LOSS_PAUSE_COUNT", _optional_int_env, None),
    ("consecutive_loss_pause_minutes", "CONSECUTIVE_LOSS_PAUSE_MINUTES", _int_env, 30),
    ("consecutive_loss_stop_count", "CONSECUTIVE_LOSS_STOP_COUNT", _optional_int_env, None),
    ("regular_market_only", "REGULAR_MARKET_ONLY", _bool_env, True),
    ("flatten_before_close_minutes", "FLATTEN_BEFORE_CLOSE_MINUTES", _int_env, 15),
    ("heartbeat_seconds", "HEARTBEAT_SECONDS", _int_env, 5),
    ("alpaca_fill_timeout_seconds", "ALPACA_FILL_TIMEOUT_SECONDS", _float_env, 5.0),
    ("alpaca_fill_poll_seconds", "ALPACA_FILL_POLL_SECONDS", _float_env, 0.25),
    ("max_entry_chase_pct", "MAX_ENTRY_CHASE_PCT", _float_env, 0.003),
    ("replay_market_data", "REPLAY_MARKET_DATA", _bool_env, False),
    ("market_regime_enabled", "MARKET_REGIME_ENABLED", _bool_env, True),
    ("market_regime_symbols", "MARKET_REGIME_SYMBOLS", _csv_env, "SPY,QQQ,IWM"),
    ("market_regime_min_bars", "MARKET_REGIME_MIN_BARS", _int_env, 20),
    ("market_regime_risk_off_score", "MARKET_REGIME_RISK_OFF_SCORE", _float_env, -3.0),
    ("market_regime_block_score", "MARKET_REGIME_BLOCK_SCORE", _float_env, -7.0),
    ("market_regime_risk_on_score", "MARKET_REGIME_RISK_ON_SCORE", _float_env, 5.0),
    ("market_regime_risk_off_size_multiplier", "MARKET_REGIME_RISK_OFF_SIZE_MULTIPLIER", _float_env, 0.5),
    ("market_regime_risk_on_size_multiplier", "MARKET_REGIME_RISK_ON_SIZE_MULTIPLIER", _float_env, 1.0),
    ("market_regime_log_changes", "MARKET_REGIME_LOG_CHANGES", _bool_env, True),
    ("market_regime_bypass_strategies", "MARKET_REGIME_BYPASS_STRATEGIES", _csv_env, ""),
    ("market_regime_positive_weight", "MARKET_REGIME_POSITIVE_WEIGHT", _float_env, 1.0),
    ("market_regime_below_vwap_weight", "MARKET_REGIME_BELOW_VWAP_WEIGHT", _float_env, 2.0),
    ("market_regime_vwap_falling_weight", "MARKET_REGIME_VWAP_FALLING_WEIGHT", _float_env, 1.0),
    ("market_regime_below_ema_weight", "MARKET_REGIME_BELOW_EMA_WEIGHT", _float_env, 2.0),
    ("market_regime_spy_weight", "MARKET_REGIME_SPY_WEIGHT", _float_env, 1.25),
    ("market_regime_qqq_weight", "MARKET_REGIME_QQQ_WEIGHT", _float_env, 1.5),
    ("market_regime_iwm_weight", "MARKET_REGIME_IWM_WEIGHT", _float_env, 1.0),
    ("market_regime_default_symbol_weight", "MARKET_REGIME_DEFAULT_SYMBOL_WEIGHT", _float_env, 1.0),
    ("indicator_preload_bars", "INDICATOR_PRELOAD_BARS", _int_env, 1000),
    ("indicator_max_bars_per_symbol", "INDICATOR_MAX_BARS_PER_SYMBOL", _int_env, 3000),
    ("indicator_include_afterhours", "INDICATOR_INCLUDE_AFTERHOURS", _bool_env, False),
    ("ai_review", "AI_REVIEW", _bool_env, False),
    ("news_hot_positive_only", "NEWS_HOT_POSITIVE_ONLY", _bool_env, True),
    ("news_hot_min_sentiment_score", "NEWS_HOT_MIN_SENTIMENT_SCORE", _float_env, 0.5),
    ("news_log_events", "NEWS_LOG_EVENTS", _bool_env, False),
    ("news_dynamic_symbols_enabled", "NEWS_DYNAMIC_SYMBOLS_ENABLED", _bool_env, False),
    ("news_listener_positive_only", "NEWS_LISTENER_POSITIVE_ONLY", _bool_env, True),
    ("news_listener_min_impact", "NEWS_LISTENER_MIN_IMPACT", _float_env, 0.5),
    ("news_listener_symbol_cooldown_seconds", "NEWS_LISTENER_SYMBOL_COOLDOWN_SECONDS", _int_env, 120),
    ("dynamic_execution_selector_enabled", "DYNAMIC_EXECUTION_SELECTOR_ENABLED", _bool_env, False),
    (
        "dynamic_execution_selector_universe_file",
        "DYNAMIC_EXECUTION_SELECTOR_UNIVERSE_FILE",
        _str_env,
        "data/opening_universe.txt",
    ),
    (
        "dynamic_execution_selector_candidate_limit",
        "DYNAMIC_EXECUTION_SELECTOR_CANDIDATE_LIMIT",
        _int_env,
        100,
    ),
    (
        "dynamic_execution_selector_top_dollar_volume_count",
        "DYNAMIC_EXECUTION_SELECTOR_TOP_DOLLAR_VOLUME_COUNT",
        _int_env,
        30,
    ),
    (
        "dynamic_execution_selector_strength_threshold",
        "DYNAMIC_EXECUTION_SELECTOR_STRENGTH_THRESHOLD",
        _float_env,
        120.0,
    ),
    (
        "dynamic_execution_selector_lookback_seconds",
        "DYNAMIC_EXECUTION_SELECTOR_LOOKBACK_SECONDS",
        _int_env,
        60,
    ),
    (
        "dynamic_execution_selector_min_dollar_volume",
        "DYNAMIC_EXECUTION_SELECTOR_MIN_DOLLAR_VOLUME",
        _float_env,
        500_000.0,
    ),
    (
        "dynamic_execution_selector_cooldown_seconds",
        "DYNAMIC_EXECUTION_SELECTOR_COOLDOWN_SECONDS",
        _int_env,
        600,
    ),
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

    if "maha7" in active_strategy_names:
        legacy_ma7_bars = os.getenv("MAHA7_MA7_BREAKDOWN_BARS")
        new_ma7_bars = os.getenv("MAHA7_RUNNER_CONFIRM_BREAK_BARS")
        if legacy_ma7_bars is not None and new_ma7_bars is None:
            values["maha7_runner_confirm_break_bars"] = int(legacy_ma7_bars)

    settings = Settings(**values)
    if not validate:
        return settings

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
    if settings.replay_clock_timeout_seconds <= 0:
        raise ValueError("REPLAY_CLOCK_TIMEOUT_SECONDS must be greater than 0.")

    return settings
