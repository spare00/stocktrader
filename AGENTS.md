# stocktrader Instructions

## Market Universe Selector

- `strategy_selectors/select_market_universe.py` builds a **broad tradable boundary pool only** for downstream strategy-specific selectors.
- It should select liquid symbols with constructive daily EMA20/EMA40/EMA60 structure and exclude rolled-over/declining charts. Strategy-specific setup logic belongs only in dedicated strategy selectors.
- Do **not** put strategy-specific entry/setup logic in the market universe selector. Conditions such as breakout bases, EMA cross entries, stochastic/MACD reversals, gap-and-go entries, SuperTrend, or other per-strategy trade setups belong in their own `strategy_selectors/select_<strategy>.py` files.

### Default job

Return liquid, easily tradable symbols whose daily chart is still in a **constructive uptrend** (established or early recovery):

- average volume, median dollar volume, previous-day volume, price range, and spread (when quotes are checked)
- **Track A (established):** EMA40 > EMA60, positive EMA20/EMA40 slopes, non-negative EMA60 slope
- **Track B (recovery):** close > EMA20/40, near EMA60 (≥ 98.5%), EMA20 slope > 0, EMA40 turning up, positive 5d/10d trend, improving EMA gaps — stack not required
- EMA stack and price > EMA60 are **ranking bonuses** by default; use `--require-ema-stack` / `--require-price-above-ema60` for hard gates
- reject clearly rolled-over charts only (below EMA20 with flat/negative slope, dual negative EMA slopes, negative 5d/10d, falling away from EMAs)
- **No** strategy-specific setup logic (breakout, gap-and-go, MACD, STOCH, SuperTrend, reversal, intraday timing)

### Modes

- `--mode liquid` (default): constructive uptrend + liquidity boundary
- `--mode limit-up` / `--mode limit-down`: previous-day market-context filters only; same liquidity/tradability requirements; no strategy entry rules
- `price > EMA60` is **not** a default hard gate because the recovery track allows near-EMA60 names; use `--require-price-above-ema60` only when intentionally narrowing the boundary pool.

### Downstream selectors

If a change needs strategy-specific ranking or setup gates, implement it in a dedicated strategy selector and feed it from the market universe output.

## Strategy Selector Plugin Contract

Treat each strategy like a plugin. `main.py` does **not** run selectors automatically; it only reads the plan files they write.

### Required for every strategy selector

1. **Script:** `strategy_selectors/select_<strategy>.py`
2. **Output path (default):** `data/<strategy>_plan.json` via `default_plan_file_for_strategy()` / `write_strategy_plan()`
3. **Plan JSON must include:**
   - `strategy` — registry name (e.g. `"recovery_scale"`)
   - `symbols` — string list of selected tickers (**required**; this is what `main.py` loads)
4. **Strategy class hooks** in `strategies/<strategy>.py`:
   - `name`
   - `selector_command` (hint when plan file is missing)
   - register in `strategies/registry.py` → `_STRATEGY_CLASSES`

Use `strategy_selectors/plan.py` → `build_strategy_plan()` so new selectors stay consistent.

### Recommended plan envelope

Keep strategy-specific fields extensible, but prefer this shared shape:

```json
{
  "strategy": "example",
  "selection_stage": "pre_market",
  "symbols": ["AAPL", "NVDA"],
  "ranked": [{ "symbol": "AAPL", "score": 82.1 }],
  "rejected": [],
  "settings": {},
  "risk_note": "Human-readable summary."
}
```

- `ranked` — per-symbol selector metadata (scores, flags, setup context)
- `settings` — optional bounded overrides for `main.py` (`plan_overrides()`)
- Extra top-level keys (`generated_at`, filter counts, etc.) are fine

### What `main.py` reads

- **Symbols:** `parse_plan_symbols(plan)` uses `symbols`, or legacy `selected_symbols`
- **Stdout JSON** (`selected_symbols`, `symbols_env_line`) is for humans/scripts only — not loaded at runtime
- **Not a strategy plan:** `select_market_universe.py` → `data/opening_universe.txt` (boundary pool for downstream selectors)

### Checklist for a new strategy

1. Add strategy class + `env_specs` in `strategies/<name>.py`
2. Register in `strategies/registry.py`
3. Add config defaults in `config.py` when needed
4. Create `strategy_selectors/select_<name>.py` that writes `data/<name>_plan.json` with `symbols` + `ranked`
5. Set `selector_command` on the strategy class
6. Use `selector_argument_parser()` from `strategy_selectors/cli.py` for `-h` defaults
7. Do **not** put strategy entry/setup logic in `select_market_universe.py`
