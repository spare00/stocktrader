# AI Stock Trader Monitor

This project is a paper-trading market monitor for short-horizon stock signals. It uses Massive's real-time stock WebSocket feed for per-second bars and quotes, detects sudden price/volume spikes across a configurable watchlist, and simulates entries/exits with risk limits.

It does not send live broker orders yet. That is intentional: seconds-to-minutes trading needs paper validation, latency checks, slippage assumptions, and broker-specific order handling before real money is connected.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `MASSIVE_API_KEY` in your shell or `.env` loader. Optional OpenAI reviews require `OPENAI_API_KEY` and `AI_REVIEW=true`.

## Run

```bash
export MASSIVE_API_KEY=...
export SYMBOLS=AAPL,MSFT,NVDA,TSLA,META
python3 main.py
```

## Massive Smoke Test

Check REST snapshots first:

```bash
python3 scripts/smoke_massive.py rest --symbols AAPL,MSFT
```

Add `--snapshot` or `--top-movers` if your Massive plan includes those snapshot endpoints.

Check the live WebSocket feed:

```bash
python3 scripts/smoke_massive.py ws --symbols AAPL,MSFT --seconds 15 --max-messages 10
```

## Test

```bash
python3 -m unittest discover -s tests -v
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

## Data Sources

Relevant Massive docs:

- REST docs index: https://massive.com/docs/rest/llms.txt
- WebSocket docs index: https://massive.com/docs/websocket/llms.txt
- Per-second stock aggregates: https://massive.com/docs/websocket/stocks/aggregates-per-second.md
- Stock quotes: https://massive.com/docs/websocket/stocks/quotes.md
- Stock top movers: https://massive.com/docs/rest/stocks/snapshots/top-market-movers.md
