# AI Stock Trader Monitor

This project is a paper-trading stock monitor built around Alpaca. It uses Alpaca market data for bars and quotes, keeps strategy and risk logic local, and stays in paper mode while the system is being validated.

It does not send live broker orders. Paper-order submission can be enabled explicitly with `EXECUTION_MODE=alpaca_paper`; otherwise it simulates fills locally.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp profiles/paper.env.example profiles/paper.env
```

Set your Alpaca/OpenAI keys in `.env`. Keep shared paper-trading tunables in `profiles/paper.env`. `.env` is for secrets and local machine overrides; the profile files are for shared runtime tuning. Strategy defaults live in code; add strategy-specific environment overrides only when you intentionally want to tune them. **`SYMBOLS` is optional in `profiles/paper.env`:** if set, it creates global symbols shared by every active strategy; selector plan symbols stay strategy-local. Local `.env*` and `profiles/*.env` files are ignored by git; the `*.example` files are committed templates.

For Alpaca paper mode, run:

```bash
scripts/run_paper.sh
```

## Alpaca Smoke Test

Check paper account access, market clock, and minute bars:

```bash
.venv/bin/python scripts/smoke_alpaca.py rest --symbols AAPL,MSFT
```

Check the Alpaca live stream:

```bash
.venv/bin/python scripts/smoke_alpaca.py stream --symbols AAPL,MSFT --seconds 15 --max-messages 10
```

Check the OpenAI review path directly:

```bash
export AI_REVIEW=true
export OPENAI_API_KEY=...
.venv/bin/python scripts/smoke_openai.py
```

Submit one tiny paper order and cancel it right away if you are outside market hours:

```bash
export EXECUTION_MODE=alpaca_paper
.venv/bin/python scripts/smoke_alpaca_order.py --symbol AAPL --qty 1 --cancel-after-submit
```

If you intentionally want to bypass the execution-mode guard for a one-off test, add `--force-submit`.

Execution modes:

- `EXECUTION_MODE=local`: simulate fills only inside the app
- `EXECUTION_MODE=alpaca_paper`: submit market orders to Alpaca paper trading

Market-data modes:

- `ALPACA_MARKET_DATA_MODE=rest`: poll latest quotes and minute bars over REST every `ALPACA_MARKET_DATA_POLL_SECONDS`. This is the default.
- `ALPACA_MARKET_DATA_MODE=stream`: use Alpaca's websocket feed. Runtime features that require real-time trades or news automatically upgrade the effective mode to stream.

Strategies:

- `spike`: short-window price/volume spike detection
- `gap_and_go`: premarket gap continuation that waits for a regular-session breakout above premarket high with volume and spread filters
- `opening_impulse`: market-open impulse capture using opening-range and 1-minute bar structure first, with quote momentum only as fallback and quotes used as execution sanity checks
- `maha7`: 10:00-14:30 ET MA7/MA20 pullback reclaim with stabilized MA trend, RSI 55 reclaim outside the neutral zone, strong higher-high structure, VWAP distance filter, swing-low stop, 50% partial at 0.5R, and final exits at 2R, close below MA7, or RSI below 50 after the minimum hold
- `stoch_macd_reversal`: 1-minute STOCH/MACD confirmation setup: buy when SuperTrend (7,3) is bullish, MACD/CCC is above signal, and STOCH %K is above %D; exit on the mirrored bearish indicator confirmation or risk exits
- `breakout_power`: 1-minute BreakOut Power score cross above 50 with green avg_momentum; bar-based partial and recovery-aware exits

Choose one or many with `STRATEGIES=spike,opening_impulse,gap_and_go,maha7,stoch_macd_reversal,breakout_power`.
When running `main.py`, you can override that directly with `--strategy`; if neither is set, the runner asks for the strategy before starting.

Strategy entry windows are configured independently, in minutes from the
regular-market open at `09:30` New York time. For example, `0` means `09:30`,
`30` means `10:00`, `150` means `12:00`, and negative values are premarket
minutes. Supported window variables:

- `OPENING_IMPULSE_START_MINUTE` / `OPENING_IMPULSE_END_MINUTE`
- `GAP_AND_GO_START_MINUTE` / `GAP_AND_GO_END_MINUTE`
- `MAHA7_START_MINUTE` / `MAHA7_END_MINUTE` (defaults to `30` / `300`, or `10:00` / `14:30` ET)
- `STOCH_MACD_START_MINUTE` / `STOCH_MACD_END_MINUTE`
- `BP_START_MINUTE` / `BP_END_MINUTE`
- `SPIKE_START_MINUTE` / `SPIKE_END_MINUTE` (unset by default, so spike has no strategy-specific window)

MAHA7 also supports `MAHA7_TREND_MIN_BARS` defaulting to `3`,
`MAHA7_MIN_HOLD_SECONDS` defaulting to `120`, and
`MAHA7_MAX_TRADES_PER_SYMBOL_PER_SESSION` defaulting to `3`.

Recommended baseline for a “reliable first 1%” style:

- `STRATEGIES=opening_impulse`
- `TARGET_PROFIT_PCT=0.01`
- `STOP_LOSS_PCT=0.005`
- `MAX_OPEN_POSITIONS=2`
- `TRADE_COOLDOWN_SECONDS=60`
- `REGULAR_MARKET_ONLY=true`
- `FLATTEN_BEFORE_CLOSE_MINUTES=15`
- `HEARTBEAT_SECONDS=5`

When running multiple strategies together, `MAX_POSITION_VALUE`, `MAX_OPEN_POSITIONS`, and
`MAX_HOLD_SECONDS` remain the account-wide defaults, and `TRADE_COOLDOWN_SECONDS` remains
the default symbol cooldown. Optional strategy-specific overrides can be set when one
strategy needs different sizing, book size, hold time, or pacing:

- `STOCH_MACD_MAX_POSITION_VALUE` / `STOCH_MACD_MAX_OPEN_POSITIONS` / `STOCH_MACD_MAX_HOLD_SECONDS` / `STOCH_MACD_TRADE_COOLDOWN_SECONDS`
- `MACD_MAX_POSITION_VALUE` / `MACD_MAX_OPEN_POSITIONS` / `MACD_MAX_HOLD_SECONDS` / `MACD_TRADE_COOLDOWN_SECONDS`
- `STEADY_INTRADAY_MAX_POSITION_VALUE` / `STEADY_INTRADAY_MAX_OPEN_POSITIONS` / `STEADY_INTRADAY_MAX_HOLD_SECONDS` / `STEADY_INTRADAY_TRADE_COOLDOWN_SECONDS`
- `BP_MAX_POSITION_VALUE` / `BP_MAX_OPEN_POSITIONS` / `BP_MAX_HOLD_SECONDS` / `BP_TRADE_COOLDOWN_SECONDS`
- `OPENING_IMPULSE_MAX_POSITION_VALUE` / `OPENING_IMPULSE_MAX_OPEN_POSITIONS` / `OPENING_IMPULSE_MAX_HOLD_SECONDS`
- `GAP_AND_GO_MAX_POSITION_VALUE` / `GAP_AND_GO_MAX_OPEN_POSITIONS` / `GAP_AND_GO_MAX_HOLD_SECONDS`
- `SPIKE_MAX_POSITION_VALUE` / `SPIKE_MAX_OPEN_POSITIONS` / `SPIKE_MAX_HOLD_SECONDS`
- `MAHA7_MAX_POSITION_VALUE` / `MAHA7_MAX_OPEN_POSITIONS` / `MAHA7_MAX_HOLD_SECONDS`

Leave these unset or `0` to use the global defaults.

The global market-regime gate is enabled by default. It watches `MARKET_REGIME_SYMBOLS`
(`SPY,QQQ,IWM` by default) and annotates every accepted trade with the active regime.
Negative conditions are weighted more heavily than positive conditions so weak broad-market
signals harden entries faster than strong signals loosen them. Useful tuning knobs:

- `MARKET_REGIME_ENABLED` and `MARKET_REGIME_BYPASS_STRATEGIES`
- `MARKET_REGIME_RISK_OFF_SCORE`, `MARKET_REGIME_BLOCK_SCORE`, `MARKET_REGIME_RISK_ON_SCORE`
- `MARKET_REGIME_BELOW_VWAP_WEIGHT`, `MARKET_REGIME_VWAP_FALLING_WEIGHT`, `MARKET_REGIME_BELOW_EMA_WEIGHT`
- `MARKET_REGIME_SPY_WEIGHT`, `MARKET_REGIME_QQQ_WEIGHT`, `MARKET_REGIME_IWM_WEIGHT`

The default opening-impulse tuning is intentionally stricter than before: shorter trading window, tighter spreads, stronger volume requirement, and faster momentum-fade exits.

## Run

After the strategy plan exists under `data/`, start the monitor (symbols come from the plan unless you set `SYMBOLS` in the environment):

```bash
scripts/run_paper.sh
# or, with venv activated:
.venv/bin/python main.py
```

To see what `main.py` can run without checking the code:

```bash
.venv/bin/python main.py --list-strategies
```

## Target Selection

Refresh a broad tradable/liquid universe weekly or periodically. This is the global market-selection stage. It intentionally avoids strategy-specific pattern filtering and only keeps stocks that can realistically be traded intraday:

```bash
.venv/bin/python strategy_selectors/select_market_universe.py --top 300
```

Before each market session, you can run the selector for the strategy you plan to trade. These selectors are optional pre-session tools that write strategy-local plan files; `main.py` does not run them automatically. For `opening_impulse`, rank the broad universe with opening-impulse criteria:

```bash
.venv/bin/python strategy_selectors/select_opening_impulse.py --top 12
```

For `maha7`, build the broad universe first, then create a plan from that liquid list:

```bash
.venv/bin/python strategy_selectors/select_maha7.py --top 12
```

Add `--use-ai` if you want the selector to ask OpenAI to refine the final ranked shortlist and write the strategy-specific plan file that `main.py` reads as that strategy's local universe:

```bash
.venv/bin/python strategy_selectors/select_opening_impulse.py --top 12 --use-ai
```

For `gap_and_go`, use its dedicated selector:

```bash
.venv/bin/python strategy_selectors/select_gap_and_go.py --top 5
```

This selector is pre-market only: it ranks symbols using previous-day bars,
premarket bars, and the current premarket quote. It does not depend on the
regular-session open or any post-open breakout behavior.

For `stoch_macd_reversal`, build a daily confirmation-stack watchlist:

```bash
.venv/bin/python strategy_selectors/select_stoch_macd_reversal.py --top 12
```

This selector uses daily OHLCV bars to rank the same confirmation stack used by
the live handler: EMA 5 above SuperTrend (7,3), MACD/CCC above signal, and
STOCH %K above %D. The live strategy still waits for minute confirmation before
entering.

For `breakout_power`, build a daily BreakOut Power alignment watchlist:

```bash
.venv/bin/python strategy_selectors/select_breakout_power.py --top 12
```

This selector uses daily OHLCV bars to rank BP score, green avg_momentum, recent
cross above 50, and MACD/AO/EMA alignment. The live strategy still waits for the
minute-bar BP cross with green momentum before entering.

It also supports an embedded AI refinement pass:

```bash
.venv/bin/python strategy_selectors/select_gap_and_go.py --top 5 --use-ai
```

Run the monitor with keys from `.env` and tunables from `profiles/paper.env`:

```bash
scripts/run_paper.sh
```

Or choose the active strategy directly at runtime:

```bash
.venv/bin/python main.py --strategy opening_impulse
.venv/bin/python main.py --strategy macd_early_impulse stoch_macd_reversal steady_intraday
```

To test a different paper profile without editing the default one:

```bash
TUNING_PROFILE=profiles/paper_aggressive.env scripts/run_paper.sh
```

For a **local Alpaca-compatible mock** (see `profiles/test.env.example`), use either a full path or the short **`PROFILE`** name (`profiles/<PROFILE>.env`):

```bash
PROFILE=test scripts/run_paper.sh
# same as:
TUNING_PROFILE=profiles/test.env scripts/run_paper.sh
```

Keep **`EXECUTION_MODE=alpaca_paper`** in those profiles so order and data clients still use the Alpaca SDK against your mock base URLs (`ALPACA_*_BASE_URL`). Use **`EXECUTION_MODE=local`** only when you want in-app simulated fills; profile switching is separate (`TUNING_PROFILE` / `PROFILE`), not tied to `EXECUTION_MODE`.

Set **`REPLAY_MARKET_DATA=true`** when the mock serves historical bars/quotes. Replay mode keeps the mocked event timestamps as the trading clock, so wall-clock heartbeats do not trigger max-hold, min-hold, cooldown, or shutdown flatten behavior.

**Symbols vs strategy plan:** `data/<strategy>_plan.json` lists tickers from each selector and feeds only that strategy's local universe. If **`SYMBOLS` is set** in `.env` or `profiles/*.env`, those tickers become global symbols visible to every active strategy. The runtime stream is still shared; only the logical strategy universe is separated.

**Configure tickers and default strategies only in files:** Put `SYMBOLS=...` and optional `STRATEGIES=...` in `.env` or `profiles/<name>.env`. Do not rely on shell exports — **`scripts/run_paper.sh` begins with `unset SYMBOLS` and `unset STRATEGIES`** so a stray export from tmux or an old session never reaches `main.py`. Selectors print a `SYMBOLS=...` line for pasting into those files only (JSON field `symbols_env_line`). Running `.venv/bin/python main.py` directly still inherits the shell; prefer `scripts/run_paper.sh` or clear exports first.

**If you run `main.py` without the wrapper:** Run `unset SYMBOLS STRATEGIES` when the watch list or strategy selection looks wrong, or align your shell with the same rule as the wrapper.

If you want `main.py` to trade a selector output, run the selector first, then start the bot with the strategy you want:

```bash
scripts/run_paper.sh --strategy opening_impulse
scripts/run_paper.sh --strategy macd_early_impulse stoch_macd_reversal steady_intraday
```

Runtime logs are written to `logs/trader.log` with rotation. The console shows normal INFO events, while the log file also includes DEBUG diagnostics explaining why `opening_impulse` did not enter, such as low spread quality, insufficient quote move, retrace from local high, or low volume ratio. Confirmed buy/sell events are also appended to `logs/trade_journal.jsonl` so trade history survives log rotation.

The selectors are REST-only pre-session steps. The market selector builds a broad liquid shortlist, and the per-strategy selectors rank that shortlist using strategy-specific criteria. They do not monitor live data and are not run inside `main.py`. At runtime, `SYMBOLS` is the shared global universe and `data/<strategy>_plan.json` is that strategy's local universe. Market data defaults to REST polling; real-time features automatically upgrade the runtime event source to websocket stream mode while REST remains available for warmup/backfill calls.

You can also enable a runtime execution-strength dynamic selector. It reads the top symbols from `data/opening_universe.txt`, subscribes to their bars/quotes/trades without exposing them to strategies, and only adds a symbol to the global universe when it is in the top dollar-volume group and rolling execution strength crosses the threshold:

```bash
DYNAMIC_EXECUTION_SELECTOR_ENABLED=true
DYNAMIC_EXECUTION_SELECTOR_STRENGTH_THRESHOLD=120
DYNAMIC_EXECUTION_SELECTOR_TOP_DOLLAR_VOLUME_COUNT=30
```

REST polling has no trade ticks for true execution-strength calculation, so enabling this selector automatically upgrades the effective runtime market-data mode to `stream`.

Runtime news expansion is separate and opt-in:

```bash
NEWS_DYNAMIC_SYMBOLS_ENABLED=true
```

When enabled, news events can dynamically add hot symbols to the global universe and the effective runtime market-data mode is also upgraded to `stream`. If both runtime news expansion and execution-strength selection are off, the process stays on REST polling unless you explicitly set `ALPACA_MARKET_DATA_MODE=stream`.

The `data/` files act like embedded memory for the workflow. The broad market selector writes `data/opening_universe.txt` by default. The per-strategy selectors read that file by default and write their own strategy plan files, such as `data/opening_impulse_plan.json` and `data/gap_and_go_plan.json`.

`main.py` can start with empty global `SYMBOLS` when active strategies have local plan symbols. If no global symbols and no local plan symbols exist, it stops with "No symbols to trade."

By default it looks at prior completed regular-market opening windows (`09:30-10:00` New York time) rather than whatever bars happen to be most recent. That makes it suitable to run at 08:00 before the market opens:

```bash
.venv/bin/python strategy_selectors/select_opening_impulse.py --days 10 --opening-minutes 30 --top 12
```

The minimum expected opening fluctuation follows the configured profit target automatically: `min_opening_range_pct = TARGET_PROFIT_PCT + min(TARGET_PROFIT_PCT, opening_range_buffer_pct)`. With the default `TARGET_PROFIT_PCT=0.01` and `--opening-range-buffer-pct 0.01`, candidates need about a `2%` median opening-window range. A larger target adds the same cushion instead of doubling without limit. Override it only when you intentionally want a different screen:

```bash
.venv/bin/python strategy_selectors/select_opening_impulse.py --min-opening-range-pct 0.015
```

By default candidates must also show either a short recent daily uptrend or a bottom-reversal pattern. The screen now requires a basic opening follow-through profile too: non-negative median opening-window close movement, at least `0.1` median close/high capture, and at least half of sampled openings closing above the opening price. This keeps the output focused on names that have historically converted opening attention into follow-through instead of only early wick volatility. Override those gates only when you intentionally want to study spike-and-fade behavior:

```bash
.venv/bin/python strategy_selectors/select_opening_impulse.py --min-close-capture-ratio 0 --min-positive-close-day-ratio 0 --min-median-opening-close-bps -100
```

## Test

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Replay

Replay saved bar and quote events through the local paper engine:

```bash
.venv/bin/python scripts/replay_csv.py events.csv --symbols AAPL,MSFT
```

The CSV needs `type`, `symbol`, and `timestamp_ms`. Quote rows use `bid`, `ask`, `bid_size`, and `ask_size`. Bar rows use `open`, `high`, `low`, `close`, `volume`, and optional `vwap`, `start_ms`, `end_ms`.

## Signal Logic

The first strategy watches each symbol for:

- price move over `SPIKE_LOOKBACK_SECONDS`
- volume at least `VOLUME_RATIO` times the recent baseline
- quote spread below `MAX_SPREAD_BPS`

`opening_impulse` prioritizes opening-range breakout/reversal and confirmed 1-minute bar impulses. Fast quote moves are fallback entries only when no bar/range signal is present, and must be sustained for at least 20 seconds with higher-high bar structure. Wide spreads, zero/low volume, invalid quotes, missing higher-high structure, and entries more than 2% above the regular-session open are hard rejects; thin quote size, quote retrace, and negative quote steps remain entry warnings. The bar and range paths are controlled by:

- `OPENING_IMPULSE_BAR_CONFIRMATION=true`
- `OPENING_IMPULSE_END_MINUTE=150`
- `OPENING_IMPULSE_VOLUME_RATIO=1.5`
- `OPENING_IMPULSE_MAX_SPREAD_BPS=15`
- `OPENING_IMPULSE_MIN_QUOTE_MOVE_SECONDS=20`
- `OPENING_IMPULSE_MAX_ENTRY_EXTENSION_PCT=0.02`
- `OPENING_IMPULSE_BAR_WINDOW=3`
- `OPENING_IMPULSE_BAR_MIN_RISING=2`
- `OPENING_IMPULSE_BAR_CHANGE_PCT=0.003`
- `OPENING_IMPULSE_BAR_VOLUME_RATIO=1.5`
- `OPENING_IMPULSE_RANGE_MINUTES=5`
- `OPENING_IMPULSE_ENABLE_RANGE_BREAKOUT=true`
- `OPENING_IMPULSE_ENABLE_RANGE_REVERSAL=true`
- `OPENING_IMPULSE_RANGE_REVERSAL_MIN_DROP_PCT=0.005`
- `OPENING_IMPULSE_RANGE_VOLUME_RATIO=1.2`
- `OPENING_IMPULSE_MIN_HOLD_SECONDS=60`, which delays strategy-managed exits so a 1-minute candle structure can form
- `OPENING_IMPULSE_EXIT_NEGATIVE_STEPS=3`, with quote fade used as a less-sensitive fallback exit after structural checks
- `OPENING_IMPULSE_PULLBACK_PCT=0.005`, used as the normal-volume pullback exit
- `OPENING_IMPULSE_STRONG_VOLUME_RATIO=2.5`
- `OPENING_IMPULSE_STRONG_PULLBACK_PCT=0.01`, letting high-volume winners breathe more before a pullback exit
- `OPENING_IMPULSE_PARTIAL_TAKE_PROFIT_PCT=0.008`
- `OPENING_IMPULSE_PARTIAL_TAKE_PROFIT_FRACTION=0.5`
- `OPENING_IMPULSE_RUNNER_PULLBACK_PCT=0.012`

Accepted paper entries use:

- no fixed take-profit ceiling for `opening_impulse`; profitable exits sell half at +0.8%, then keep the runner for confirmed higher-high break or 1.2% runner pullback
- chase protection via `MAX_ENTRY_CHASE_PCT=0.003`, which skips stale entries if the fresh ask has moved more than 0.3% beyond the signal price
- journal metrics include entry-vs-open percentage, holding duration, max favorable excursion, signal price, slippage, fill latency, R-multiple, runner R-multiple, full-trade R-multiple, and cumulative daily PnL
- stop loss via `STOP_LOSS_PCT`
- max-trade loss protection via `MAX_TRADE_LOSS_R=1.2`
- time exit via `MAX_HOLD_SECONDS`
- max position count, cash/risk sizing, symbol cooldown, consecutive-loss pause/day stop, and daily loss limit
- regular-session gating via `REGULAR_MARKET_ONLY=true`
- end-of-day flattening via `FLATTEN_BEFORE_CLOSE_MINUTES` defaults to 15 minutes before close
- stream heartbeat exits via `HEARTBEAT_SECONDS`
