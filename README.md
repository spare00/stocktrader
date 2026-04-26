# AI Stock Trader Monitor

This project is a paper-trading stock monitor built around Alpaca. It uses Alpaca market data for bars and quotes, keeps strategy and risk logic local, and stays in paper mode while the system is being validated.

It does not send live broker orders. Paper-order submission can be enabled explicitly with `EXECUTION_MODE=alpaca_paper`; otherwise it simulates fills locally.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set your Alpaca paper credentials in your shell or `.env` loader. Optional OpenAI reviews require `OPENAI_API_KEY` and `AI_REVIEW=true`.

For Alpaca paper mode, export:

```bash
export ALPACA_API_KEY=...
export ALPACA_SECRET_KEY=...
export ALPACA_PAPER=true
export ALPACA_DATA_FEED=iex
export EXECUTION_MODE=local
export STRATEGIES=opening_impulse
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
- `opening_impulse`: market-open, quote-velocity, spread, and volume-spike based opening impulse capture

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

Refresh a broad liquid/moving universe weekly or periodically:

```bash
venv/bin/python scripts/build_opening_universe.py --limit 150
```

Before each market session, screen that universe for `opening_impulse` candidates:

```bash
venv/bin/python scripts/screen_opening_impulse.py --top 12
```

Optionally turn that screen into a bounded AI pre-market plan:

```bash
venv/bin/python scripts/ai_opening_plan.py
```

Run the monitor with the plan when you want the AI-filtered symbols and conservative setting adjustments:

```bash
venv/bin/python main.py --use-opening-plan
```

The screener is a REST-only pre-session step. It ranks liquid companies by prior opening-window movement, opening-window dollar volume, spread, quote size, daily trend/reversal context, and opening follow-through quality, then prints an `export SYMBOLS=...` line. It does not monitor live data and is not used inside `main.py`, so order handling stays focused on the fixed `SYMBOLS` list.

The `data/` files act like embedded memory for the workflow. The universe builder writes `data/opening_universe.txt` by default. When that file exists, the daily screener reads it by default; if it has not been generated yet, the screener falls back to its built-in starter universe. The daily screener writes `data/opening_screen.json` by default, and the AI plan step writes `data/opening_plan.json` by default.

By default it looks at prior completed regular-market opening windows (`09:30-10:00` New York time) rather than whatever bars happen to be most recent. That makes it suitable to run at 08:00 before the market opens:

```bash
venv/bin/python scripts/screen_opening_impulse.py --days 10 --opening-minutes 30 --top 12
```

The minimum expected opening fluctuation follows the configured profit target automatically: `min_opening_range_pct = TARGET_PROFIT_PCT + min(TARGET_PROFIT_PCT, opening_range_buffer_pct)`. With the default `TARGET_PROFIT_PCT=0.01` and `--opening-range-buffer-pct 0.01`, candidates need about a `2%` median opening-window range. A larger target adds the same cushion instead of doubling without limit. Override it only when you intentionally want a different screen:

```bash
venv/bin/python scripts/screen_opening_impulse.py --min-opening-range-pct 0.015
```

By default candidates must also show either a short recent daily uptrend or a bottom-reversal pattern. Symbols that often spike early and fade back receive a lower score through `fade_bps` and `close_capture_ratio`.

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

Accepted paper entries use:

- target profit typically set to 1% by `TARGET_PROFIT_PCT`
- stop loss via `STOP_LOSS_PCT`
- time exit via `MAX_HOLD_SECONDS`
- max position count, cash sizing, symbol cooldown, and daily loss limit
- regular-session gating via `REGULAR_MARKET_ONLY=true`
- end-of-day flattening via `FLATTEN_BEFORE_CLOSE_MINUTES` defaults to 15 minutes before close
- stream heartbeat exits via `HEARTBEAT_SECONDS`
