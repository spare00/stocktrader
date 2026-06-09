# stocktrader Instructions

## Market Universe Selector

- `strategy_selectors/select_market_universe.py` builds a **broad tradable boundary pool only** for downstream strategy-specific selectors.
- Market universe selector builds a broad tradable boundary pool only. It should select liquid symbols with constructive daily EMA20/EMA40/EMA60 uptrend structure and exclude rolled-over/declining charts. Strategy-specific setup logic belongs only in dedicated strategy selectors.
- Do **not** put strategy-specific entry/setup logic in the market universe selector. Conditions such as breakout bases, EMA cross entries, stochastic/MACD reversals, gap-and-go entries, SuperTrend, or other per-strategy trade setups belong in their own `strategy_selectors/select_<strategy>.py` files.

### Default job

Return liquid, easily tradable symbols whose daily chart is still in a **constructive uptrend** (established or early recovery):

- average volume, median dollar volume, previous-day volume, price range, and spread (when quotes are checked)
- **Track A (established):** EMA40>EMA60, positive EMA slopes
- **Track B (recovery):** close > EMA20/40, near EMA60 (≥ 98.5%), EMA20 slope > 0, EMA40 turning up, positive 5d/10d trend, improving EMA gaps — stack not required
- EMA stack and price > EMA60 are **ranking bonuses** by default; use `--require-ema-stack` / `--require-price-above-ema60` for hard gates
- reject clearly rolled-over charts only (below EMA20 with flat/negative slope, dual negative EMA slopes, negative 5d/10d, falling away from EMAs)
- **No** strategy-specific setup logic (breakout, gap-and-go, MACD, STOCH, SuperTrend, reversal, intraday timing)

### Modes

- `--mode liquid` (default): constructive uptrend + liquidity boundary
- `--mode limit-up` / `--mode limit-down`: previous-day market-context filters only; same liquidity/tradability requirements; no strategy entry rules
- For limit-down, `require_price_above_ema60` defaults to **False** unless explicitly requested; for normal/uptrend modes it defaults to **True**

### Downstream selectors

If a change needs strategy-specific ranking or setup gates, implement it in a dedicated strategy selector and feed it from the market universe output.
