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

Execution modes:

- `EXECUTION_MODE=local`: simulate fills only inside the app
- `EXECUTION_MODE=alpaca_paper`: submit market orders to Alpaca paper trading

## Run

```bash
export SYMBOLS=AAPL,MSFT,NVDA,TSLA,META
venv/bin/python main.py
```

## Test

```bash
venv/bin/python -m unittest discover -s tests -v
```

## Signal Logic

The first strategy watches each symbol for:

- price move over `SPIKE_LOOKBACK_SECONDS`
- volume at least `VOLUME_RATIO` times the recent baseline
- quote spread below `MAX_SPREAD_BPS`

Accepted paper entries use:

- target profit capped at 2% by `TARGET_PROFIT_PCT`
- stop loss via `STOP_LOSS_PCT`
- time exit via `MAX_HOLD_SECONDS`
- max position count, cash sizing, symbol cooldown, and daily loss limit
