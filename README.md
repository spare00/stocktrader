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

Set your Alpaca/OpenAI keys in `.env`. Keep experiment tunables in `profiles/paper.env`; that is the file to adjust while searching for better paper-trading behavior. Local `.env*` and `profiles/*.env` files are ignored by git; the `*.example` files are committed templates.

For Alpaca paper mode, run:

```bash
scripts/run_paper.sh
```

## Alpaca Smoke Test

Check paper account access, market clock, and minute bars:

```bash
venv/bin/python scripts/smoke_alpaca.py rest --symbols AAPL,MSFT
```

Check the Alpaca live stream:

```bash
venv/bin/python scripts/smoke_alpaca.py stream --symbols AAPL,MSFT --seconds 15 --max-messages 10
```

Check the OpenAI review path directly:

```bash
export AI_REVIEW=true
export OPENAI_API_KEY=...
venv/bin/python scripts/smoke_openai.py
```

Submit one tiny paper order and cancel it right away if you are outside market hours:

```bash
export EXECUTION_MODE=alpaca_paper
venv/bin/python scripts/smoke_alpaca_order.py --symbol AAPL --qty 1 --cancel-after-submit
```

If you intentionally want to bypass the execution-mode guard for a one-off test, add `--force-submit`.

Execution modes:

- `EXECUTION_MODE=local`: simulate fills only inside the app
- `EXECUTION_MODE=alpaca_paper`: submit market orders to Alpaca paper trading

Strategies:

- `spike`: short-window price/volume spike detection
- `opening_impulse`: market-open impulse capture using opening-range and 1-minute bar structure first, with quote momentum only as fallback and quotes used as execution sanity checks

Choose one or many with `STRATEGIES=spike,opening_impulse`.

Recommended baseline for a “reliable first 1%” style:

- `STRATEGIES=opening_impulse`
- `TARGET_PROFIT_PCT=0.01`
- `STOP_LOSS_PCT=0.005`
- `MAX_OPEN_POSITIONS=2`
- `TRADE_COOLDOWN_SECONDS=60`
- `REGULAR_MARKET_ONLY=true`
- `FLATTEN_BEFORE_CLOSE_MINUTES=15`
- `HEARTBEAT_SECONDS=5`

The default opening-impulse tuning is intentionally stricter than before: shorter trading window, tighter spreads, stronger volume requirement, and faster momentum-fade exits.

## Run

```bash
export SYMBOLS=AAPL,MSFT,NVDA,TSLA,META
venv/bin/python main.py
```

## Target Selection

Refresh a broad tradable/liquid universe weekly or periodically. This stage intentionally avoids pattern or historical-performance filtering; it only keeps stocks that can realistically be traded intraday:

```bash
venv/bin/python scripts/build_opening_universe.py --limit 300
```

Before each market session, score the broad universe for current state-based intraday patterns and output the top 15 symbols:

```bash
venv/bin/python scripts/score_daily_patterns.py --top 15
```

The daily scorer implements mean reversion, compression breakout, trend continuation, gap-and-go, and opening flush reversal as independent pattern scores. The final score is the best single pattern score, with only a light end-stage spread penalty when the latest quote is wider than `100` bps. It writes `data/daily_pattern_candidates.json` and appends each run to `data/trade_candidates.jsonl`.

The older opening-specific screen remains available when you want to focus only on `opening_impulse`:

```bash
venv/bin/python scripts/screen_opening_impulse.py --top 12
```

Optionally turn that screen into a bounded AI pre-market plan:

```bash
venv/bin/python scripts/ai_opening_plan.py
```

Run the monitor with keys from `.env` and tunables from `profiles/paper.env`:

```bash
scripts/run_paper.sh
```

To test a different paper profile without editing the default one:

```bash
TUNING_PROFILE=profiles/paper_aggressive.env scripts/run_paper.sh
```

If you intentionally want to use the AI-filtered opening plan, pass the normal `main.py` option through:

```bash
scripts/run_paper.sh --use-opening-plan
```

Runtime logs are written to `logs/trader.log` with rotation. The console shows normal INFO events, while the log file also includes DEBUG diagnostics explaining why `opening_impulse` did not enter, such as low spread quality, insufficient quote move, retrace from local high, or low volume ratio. Confirmed buy/sell events are also appended to `logs/trade_journal.jsonl` so trade history survives log rotation.

The screener is a REST-only pre-session step. It ranks liquid companies by prior opening-window movement, opening-window dollar volume, spread, quote size, daily trend/reversal context, and opening follow-through quality, then prints an `export SYMBOLS=...` line. It does not monitor live data and is not used inside `main.py`, so order handling stays focused on the fixed `SYMBOLS` list.

The `data/` files act like embedded memory for the workflow. The broad weekly universe builder writes `data/opening_universe.txt` by default. Both the daily pattern scorer and the opening-specific legacy screen read that file by default. The daily pattern scorer writes `data/daily_pattern_candidates.json` and appends run history to `data/trade_candidates.jsonl`; the opening-specific legacy flow still writes `data/opening_screen.json` and `data/opening_plan.json`.

By default it looks at prior completed regular-market opening windows (`09:30-10:00` New York time) rather than whatever bars happen to be most recent. That makes it suitable to run at 08:00 before the market opens:

```bash
venv/bin/python scripts/screen_opening_impulse.py --days 10 --opening-minutes 30 --top 12
```

The minimum expected opening fluctuation follows the configured profit target automatically: `min_opening_range_pct = TARGET_PROFIT_PCT + min(TARGET_PROFIT_PCT, opening_range_buffer_pct)`. With the default `TARGET_PROFIT_PCT=0.01` and `--opening-range-buffer-pct 0.01`, candidates need about a `2%` median opening-window range. A larger target adds the same cushion instead of doubling without limit. Override it only when you intentionally want a different screen:

```bash
venv/bin/python scripts/screen_opening_impulse.py --min-opening-range-pct 0.015
```

By default candidates must also show either a short recent daily uptrend or a bottom-reversal pattern. The screen now requires a basic opening follow-through profile too: non-negative median opening-window close movement, at least `0.1` median close/high capture, and at least half of sampled openings closing above the opening price. This keeps the output focused on names that have historically converted opening attention into follow-through instead of only early wick volatility. Override those gates only when you intentionally want to study spike-and-fade behavior:

```bash
venv/bin/python scripts/screen_opening_impulse.py --min-close-capture-ratio 0 --min-positive-close-day-ratio 0 --min-median-opening-close-bps -100
```

## Test

```bash
venv/bin/python -m unittest discover -s tests -v
```

## Replay

Replay saved bar and quote events through the local paper engine:

```bash
venv/bin/python scripts/replay_csv.py events.csv --symbols AAPL,MSFT
```

The CSV needs `type`, `symbol`, and `timestamp_ms`. Quote rows use `bid`, `ask`, `bid_size`, and `ask_size`. Bar rows use `open`, `high`, `low`, `close`, `volume`, and optional `vwap`, `start_ms`, `end_ms`.

## Signal Logic

The first strategy watches each symbol for:

- price move over `SPIKE_LOOKBACK_SECONDS`
- volume at least `VOLUME_RATIO` times the recent baseline
- quote spread below `MAX_SPREAD_BPS`

`opening_impulse` prioritizes opening-range breakout/reversal and confirmed 1-minute bar impulses. Fast quote moves are fallback entries only when no bar/range signal is present. Wide spread, thin quote size, quote retrace, and negative quote steps are recorded as entry warnings rather than hard filters; invalid quotes remain hard rejects. The bar and range paths are controlled by:

- `OPENING_IMPULSE_BAR_CONFIRMATION=true`
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
- `OPENING_IMPULSE_RETRACE_FROM_HIGH_PCT=0.008`, leaving room for normal opening volatility before a retrace exit

Accepted paper entries use:

- target profit typically set to 1% by `TARGET_PROFIT_PCT`
- stop loss via `STOP_LOSS_PCT`
- time exit via `MAX_HOLD_SECONDS`
- max position count, cash sizing, symbol cooldown, and daily loss limit
- regular-session gating via `REGULAR_MARKET_ONLY=true`
- end-of-day flattening via `FLATTEN_BEFORE_CLOSE_MINUTES` defaults to 15 minutes before close
- stream heartbeat exits via `HEARTBEAT_SECONDS`
