# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is an Alpaca-based paper trading system for intraday stock trading strategies. It uses Alpaca market data (REST polling or websocket streams) with local strategy and risk logic. The system runs in paper mode by default; live order submission requires explicit `EXECUTION_MODE=alpaca_paper` configuration.

## Architecture

### Core Components

- **`main.py`**: Main event loop that orchestrates market data ingestion, strategy signal generation, risk checks, and execution
- **`alpaca_stream.py`**: Market data streaming layer; handles both REST polling (`AlpacaRestPollingStream`) and websocket streams (`build_market_data_stream`), with automatic mode selection based on strategy requirements
- **`execution.py`**: Order execution engine with position tracking, partial exits, runner management, and trade journaling to `logs/trade_journal.jsonl`
- **`risk.py`**: `RiskManager` enforces account-wide and per-strategy limits (max positions, daily loss, consecutive losses, symbol cooldowns, max-hold time)
- **`config.py`**: `Settings` dataclass loads environment variables; strategy-specific overrides follow the pattern `<STRATEGY>_<PARAMETER>` (e.g., `LIQUIDITY_SCALPER_MAX_POSITION_VALUE`)
- **`strategies/`**: Each strategy module implements `analyze_signal()` and `should_exit()` methods; registry in `strategies/registry.py`
- **`strategy_selectors/`**: Pre-market ranking scripts that write plan files to `data/` (e.g., `data/opening_impulse_plan.json`)
- **`modules/`**: Runtime dynamic selectors (execution strength, mover promotion, news expansion) and symbol management
- **`market_regime.py`**: Optional broad-market gate that scores SPY/QQQ/IWM conditions and annotates trades

### Data Flow

1. **Pre-market**: Run selector scripts (e.g., `select_opening_impulse.py`) to generate strategy-specific symbol plans
2. **Runtime**: `main.py` loads global `SYMBOLS` (from env) + strategy plan symbols → ingests market data via REST or stream
3. **Signal generation**: Each active strategy's `analyze_signal()` evaluates bars/quotes/trades and emits `Signal` objects
4. **Risk filtering**: `RiskManager.check_entry()` validates signals against account limits, cooldowns, consecutive losses
5. **Execution**: Executor submits orders (local sim or Alpaca paper), tracks positions, manages partials/runners, writes trade journal
6. **Exit management**: Strategies call `should_exit()` for time/technical exits; `runtime_safety.py` handles end-of-day flattening and shutdown

### Market Data Modes

- **REST polling** (default): Periodic calls to `get_latest_quotes()` and `get_recent_bars()` via `ALPACA_MARKET_DATA_POLL_SECONDS` (default 5s)
- **Stream mode** (automatic upgrade): Strategies requiring trade ticks (e.g., `liquidity_scalper`) or runtime features (dynamic selectors, news) automatically open websocket subscriptions
- **Mixed mode**: Stream-dependent symbols use websocket; others stay on REST
- **Replay mode**: Set `REPLAY_MARKET_DATA=true` to use historical timestamps from mock server instead of wall clock

## Common Commands

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp profiles/paper.env.example profiles/paper.env
```

Edit `.env` with Alpaca/OpenAI keys. Edit `profiles/paper.env` for shared trading tunables.

### Testing

```bash
# Run all tests
.venv/bin/python -m unittest discover -s tests -v

# Smoke test Alpaca REST API
.venv/bin/python scripts/smoke_alpaca.py rest --symbols AAPL,MSFT

# Smoke test Alpaca websocket stream
.venv/bin/python scripts/smoke_alpaca.py stream --symbols AAPL,MSFT --seconds 15 --max-messages 10

# Smoke test OpenAI review
export AI_REVIEW=true
.venv/bin/python scripts/smoke_openai.py

# Test paper order submission (cancels immediately if outside market hours)
export EXECUTION_MODE=alpaca_paper
.venv/bin/python scripts/smoke_alpaca_order.py --symbol AAPL --qty 1 --cancel-after-submit
```

### Running Selectors

```bash
# Weekly broad market universe (liquidity + EMA structure)
.venv/bin/python strategy_selectors/select_market_universe.py --top 300

# Pre-market strategy-specific selectors
.venv/bin/python strategy_selectors/select_opening_impulse.py --top 12
.venv/bin/python strategy_selectors/select_liquidity_scalper.py --top 12
.venv/bin/python strategy_selectors/select_gap_and_go.py --top 5
.venv/bin/python strategy_selectors/select_maha7.py --top 12
.venv/bin/python strategy_selectors/select_stoch_macd_reversal.py --top 12
.venv/bin/python strategy_selectors/select_breakout_power.py --top 12
.venv/bin/python strategy_selectors/select_ema_gap_cross.py --top 12

# With AI refinement
.venv/bin/python strategy_selectors/select_opening_impulse.py --top 12 --use-ai
```

Selectors write JSON plan files to `data/<strategy>_plan.json` containing ranked symbols and metadata.

### Running the Bot

```bash
# Standard paper trading run (loads profiles/paper.env)
scripts/run_paper.sh

# Direct invocation with activated venv
.venv/bin/python main.py

# Override strategy at runtime
.venv/bin/python main.py --strategy opening_impulse
.venv/bin/python main.py --strategy liquidity_scalper,opening_impulse

# List available strategies
.venv/bin/python main.py --list-strategies

# Use alternative profile
PROFILE=test scripts/run_paper.sh
TUNING_PROFILE=profiles/paper_aggressive.env scripts/run_paper.sh
```

**Important**: `scripts/run_paper.sh` unsets `SYMBOLS` and `STRATEGIES` shell variables to prevent stale exports. Configure symbols/strategies in `.env` or `profiles/*.env` files, not shell exports.

### Trade Journal Analysis

```bash
.venv/bin/python scripts/analyze_trade_journal.py
```

Reads `logs/trade_journal.jsonl` and reports PnL, win rate, R-multiples, and per-strategy metrics.

### Replay

```bash
.venv/bin/python scripts/replay_csv.py events.csv --symbols AAPL,MSFT
```

CSV requires: `type`, `symbol`, `timestamp_ms`. Quote rows: `bid`, `ask`, `bid_size`, `ask_size`. Bar rows: `open`, `high`, `low`, `close`, `volume`.

## Configuration

### Environment Hierarchy

1. **`.env`**: Secrets (API keys) and local machine overrides (git-ignored)
2. **`profiles/<name>.env`**: Shared paper-trading tunables (git-ignored; `*.example` files are templates)
3. **Code defaults**: Strategy defaults in `config.py` and strategy modules

Override precedence: command-line args → profile env → `.env` → code defaults

### Key Settings

- `EXECUTION_MODE`: `local` (simulated fills) or `alpaca_paper` (submit to Alpaca paper account)
- `STRATEGIES`: Comma-separated strategy names (or use `--strategy` arg)
- `SYMBOLS`: Global symbols visible to all strategies (optional; strategies can use plan-only symbols)
- `ALPACA_MARKET_DATA_MODE`: Leave unset for normal runs; stream mode auto-upgrades as needed
- `ALPACA_MARKET_DATA_POLL_SECONDS`: REST polling interval (default 5s)
- `REPLAY_MARKET_DATA`: Use historical timestamps from mock server
- `MARKET_REGIME_ENABLED`: Enable SPY/QQQ/IWM broad-market gate
- `MARKET_REGIME_BYPASS_STRATEGIES`: Strategies that skip regime filter

### Strategy-Specific Overrides

Each strategy can override account-wide defaults with `<STRATEGY>_<PARAMETER>`:

```bash
# Account-wide defaults
MAX_POSITION_VALUE=5000
MAX_OPEN_POSITIONS=3
MAX_HOLD_SECONDS=600
TRADE_COOLDOWN_SECONDS=60

# Strategy-specific overrides
LIQUIDITY_SCALPER_MAX_POSITION_VALUE=2000
LIQUIDITY_SCALPER_MAX_OPEN_POSITIONS=5
LIQUIDITY_SCALPER_MAX_HOLD_SECONDS=120
OPENING_IMPULSE_MAX_HOLD_SECONDS=900
```

Leave strategy overrides unset or `0` to use global defaults.

### Entry Windows

Entry windows are configured in minutes from regular-market open (`09:30` ET). Example:

- `0` = 09:30 ET
- `30` = 10:00 ET
- `150` = 12:00 ET
- Negative values = premarket minutes

```bash
OPENING_IMPULSE_START_MINUTE=0
OPENING_IMPULSE_END_MINUTE=150  # 12:00 ET
LIQUIDITY_SCALPER_END_MINUTE=360  # 15:30 ET (flatten before close)
```

## Strategy Selector Architecture

**Two-stage selection**:

1. **Market universe selector** (`select_market_universe.py`): Builds a broad liquid boundary pool based on:
   - Liquidity (avg volume, dollar volume)
   - Tradable price range
   - Constructive daily EMA structure (EMA40 > EMA60 uptrend OR early recovery near EMA60)
   - **No strategy-specific setup logic** (per `AGENTS.md`)

2. **Strategy-specific selectors**: Read market universe, rank by strategy criteria, write `data/<strategy>_plan.json`

Per `AGENTS.md`, the market universe selector must **not** include strategy-specific entry logic (breakout bases, MACD crosses, gap-and-go entries, SuperTrend filters). Those belong in dedicated strategy selectors.

## Adding a New Strategy

1. Create `strategies/<strategy_name>.py` with class implementing:
   - `analyze_signal(self, symbol: str, bars: list[Bar], quote: Quote | None, ...) -> Signal | None`
   - `should_exit(self, position: Position, current_price: float, ...) -> tuple[bool, str]`

2. Register in `strategies/registry.py`:
   ```python
   STRATEGY_REGISTRY["strategy_name"] = YourStrategyClass
   ```

3. Add config fields to `config.py`:
   ```python
   strategy_name_start_minute: int = 0
   strategy_name_end_minute: int = 360
   strategy_name_max_position_value: float | None = None
   ```

4. Create selector `strategy_selectors/select_<strategy_name>.py` that:
   - Reads `data/opening_universe.txt` (or builds own universe)
   - Ranks symbols by strategy-specific criteria
   - Writes `data/<strategy_name>_plan.json`

5. Update `opening_plan.py` mappings:
   ```python
   def default_plan_file_for_strategy(strategy: str) -> str:
       if strategy == "strategy_name":
           return "data/strategy_name_plan.json"
   ```

## Code Conventions

- **Bar/quote/trade timestamps**: Always use `timestamp_ms` (Unix epoch milliseconds)
- **Position tracking**: `execution.py` manages `Position` objects with entry price, stop, target, partials, runners
- **Strategy isolation**: Strategies receive copied bar/quote lists and do not share state
- **Logging levels**: INFO for trade events; DEBUG for rejection reasons (spread, volume, retrace, etc.)
- **Trade journal**: JSONL format in `logs/trade_journal.jsonl` with entry/exit details, slippage, R-multiples
- **Client order IDs**: Use `order_prefixes.py` functions to namespace orders by strategy
- **Stream channel limits**: Alpaca Basic IEX caps trade+quote subscriptions at `ALPACA_STREAM_MAX_TRADE_QUOTE_CHANNELS` (default 30 = ~15 symbols)

## Risk Management Features

- **Account-wide**: max open positions, max position value, daily loss limit, consecutive loss pause/stop
- **Per-strategy**: overrides for max positions, max hold time, trade cooldown, consecutive loss limits
- **Per-symbol**: cooldown after trade, loss-lock after N losses, max trades per session
- **Time-based**: max hold seconds, regular-market-only filter, flatten-before-close window
- **Market regime**: optional SPY/QQQ/IWM scoring with risk-off/block/risk-on thresholds
- **Chase protection**: `MAX_ENTRY_CHASE_PCT` rejects stale signals if fresh ask moved beyond threshold
- **Max trade loss**: `MAX_TRADE_LOSS_R` caps loss at N × initial risk

## Dynamic Features

### Dynamic Execution Selector

Ranks symbols from `data/opening_universe.txt` by real-time execution strength (based on trade ticks). Promotes symbols to global universe when strength crosses threshold and dollar volume qualifies. Auto-upgrades to stream mode.

### Dynamic Mover Promotion

Liquidity scalper feature: monitors `data/dynamic_mover_universe.txt` candidates via bars-only stream, promotes to tradable universe when recent move, RVOL, dollar volume, spread pass thresholds. Does not consume trade/quote channels until promoted.

### News Listener

Optional runtime expansion: subscribes to Alpaca news feed, dynamically adds symbols on positive news events. Auto-upgrades to stream mode.

## Important Constraints

- **Stream mode is expensive**: Trade+quote channels are limited on Alpaca Basic IEX (~15 symbols). Use selectors to constrain universe.
- **One stream connection**: Multiple stream-dependent strategies share one websocket connection.
- **REST for selectors**: All pre-market selectors use REST-only calls (no stream).
- **Paper mode guard**: `scripts/run_paper.sh` refuses to run unless `EXECUTION_MODE=alpaca_paper`.
- **No shell exports**: `run_paper.sh` unsets `SYMBOLS` and `STRATEGIES` to prevent stale exports; configure in files only.

## Liquidity Scalper Threshold Warning

**CRITICAL**: Default `liquidity_scalper` runtime thresholds are **institutional-level** ($3M bar volume, $30M session volume). These will reject all symbols except SPY/QQQ/AAPL level liquidity.

**For paper/retail trading**, override in `profiles/paper.env`:
```bash
LIQUIDITY_SCALPER_MIN_BAR_DOLLAR_VOLUME=250000        # Down from $3M
LIQUIDITY_SCALPER_MIN_SESSION_DOLLAR_VOLUME=5000000   # Down from $30M
LIQUIDITY_SCALPER_MIN_RANGE_PCT=0.012                 # Down from 1.5%
LIQUIDITY_SCALPER_MAX_SPREAD_BPS=20                   # Up from 12 bps
LIQUIDITY_SCALPER_MIN_TAPE_DOLLAR_VOLUME=50000        # Down from $100K
LIQUIDITY_SCALPER_MIN_TRADE_DOLLAR_VOLUME=5000        # Down from $10K
```

Without these overrides, the strategy will generate **zero trades** because selector thresholds ($50K) are 600x lower than runtime thresholds ($30M).

## Log Files

- **`logs/trader.log`**: Rotating log with INFO console output and DEBUG file diagnostics
- **`logs/trade_journal.jsonl`**: JSONL trade history (survives log rotation)
- **`data/<strategy>_plan.json`**: Selector outputs with ranked symbols and metadata
- **`data/opening_universe.txt`**: Broad liquid universe from market selector

## Testing Strategy Changes

1. Add unit tests in `tests/` for signal logic
2. Run smoke tests to validate Alpaca connectivity
3. Use `EXECUTION_MODE=local` for in-app simulated fills during development
4. Run with `EXECUTION_MODE=alpaca_paper` against Alpaca paper account
5. Monitor `logs/trade_journal.jsonl` and analyze with `analyze_trade_journal.py`
6. For historical replay, set `REPLAY_MARKET_DATA=true` and point to mock server

## Market Regime Filter

Enabled by default; watches SPY, QQQ, IWM for broad-market conditions:

- **Risk-off score** (e.g., -20): Hardens entry gates
- **Block score** (e.g., -30): Blocks all new entries
- **Risk-on score** (e.g., +15): Normal or loosened gates

Conditions: price vs VWAP, VWAP slope, price vs EMA20, weighted by symbol importance. Bypass for specific strategies via `MARKET_REGIME_BYPASS_STRATEGIES`.
