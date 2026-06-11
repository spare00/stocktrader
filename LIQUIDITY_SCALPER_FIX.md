# Liquidity Scalper: Why It's Not Trading (Critical Issue)

## Problem Summary

The `liquidity_scalper` strategy has **never generated trades** because runtime strategy thresholds are **60-600x stricter** than selector thresholds. Symbols that pass pre-market selection get rejected at runtime.

---

## Root Cause: Threshold Mismatch

### Selector Thresholds (Pre-Market, Soft)
```python
DEFAULT_SELECTOR_MIN_BAR_DOLLAR_VOLUME = 50_000.0       # $50K
DEFAULT_SELECTOR_MIN_SESSION_DOLLAR_VOLUME = 50_000.0   # $50K  
DEFAULT_SELECTOR_MIN_RANGE_PCT = 0.010                  # 1.0%
DEFAULT_SELECTOR_MAX_SPREAD_BPS = 100.0                 # 100 bps (1%)
```

### Runtime Strategy Thresholds (Live Trading, Hard)
```python
liquidity_scalper_min_bar_dollar_volume: 3_000_000.0        # $3M (60x higher!)
liquidity_scalper_min_session_dollar_volume: 30_000_000.0   # $30M (600x higher!)
liquidity_scalper_min_range_pct: 0.015                      # 1.5%
liquidity_scalper_max_spread_bps: 12.0                      # 12 bps
```

### The Gap

| Metric | Selector | Runtime | Gap |
|--------|----------|---------|-----|
| Bar Dollar Volume | $50K | $3M | **60x** |
| Session Dollar Volume | $50K | $30M | **600x** |
| Range % | 1.0% | 1.5% | 1.5x |
| Max Spread | 100 bps | 12 bps | 8.3x tighter |

---

## What Happens

1. **Pre-market**: Selector finds symbols with $50K-$500K bar volume
   - Example: Symbol XYZ with $250K median bar volume, 1.2% range, 15 bps spread
   - **Passes selector** ✅

2. **Runtime (bar structure signal)**: 
   ```python
   bar_dollar_volume = last.close * last.volume  # e.g., $300K
   if bar_dollar_volume < 3_000_000:  # Runtime requires $3M
       return None  # REJECTED ❌
   ```

3. **Runtime (trade tape signal)**:
   ```python
   session_dollar_volume = self._session_dollar_volume(session_bars, state)  # e.g., $8M
   if session_dollar_volume < 30_000_000:  # Runtime requires $30M
       return None  # REJECTED ❌
   ```

4. **Result**: Symbol generates zero signals despite being "selected"

---

## Why This Happened

The runtime thresholds ($3M/$30M) are appropriate for:
- **Institutional trading** (multi-million dollar positions)
- **High-frequency firms** with deep liquidity requirements
- **Large hedge funds** needing to move size without slippage

But they're **completely wrong** for:
- **Retail/paper trading** (typical position sizes $1K-$10K)
- **Scalping strategies** (quick in/out, small size)
- **Alpaca Basic IEX** (limited to ~15 symbols, retail-focused data)

---

## Fix Options

### Option 1: Lower Runtime Thresholds to Match Selector (Recommended for Paper)

**For paper trading / retail accounts:**

```bash
# In profiles/paper.env
LIQUIDITY_SCALPER_MIN_BAR_DOLLAR_VOLUME=250000        # $250K (down from $3M)
LIQUIDITY_SCALPER_MIN_SESSION_DOLLAR_VOLUME=5000000   # $5M (down from $30M)
LIQUIDITY_SCALPER_MIN_RANGE_PCT=0.012                 # 1.2% (down from 1.5%)
LIQUIDITY_SCALPER_MAX_SPREAD_BPS=20                   # 20 bps (up from 12 bps)
```

**Rationale**:
- $250K bar volume = actively traded, not dead
- $5M session volume = enough liquidity for retail scalping
- 1.2% range = enough movement for 0.15%-0.3% scalps
- 20 bps spread = still tight enough for 3 bps net edge requirement

### Option 2: Raise Selector Thresholds to Match Runtime (Institutional)

**For live/institutional accounts:**

```bash
# In strategy_selectors/select_liquidity_scalper.py
DEFAULT_SELECTOR_MIN_BAR_DOLLAR_VOLUME = 2_500_000.0
DEFAULT_SELECTOR_MIN_SESSION_DOLLAR_VOLUME = 25_000_000.0
DEFAULT_SELECTOR_MIN_RANGE_PCT = 0.015
DEFAULT_SELECTOR_MAX_SPREAD_BPS = 15.0
```

**Rationale**:
- Only select symbols that will actually pass runtime
- But very few symbols will qualify (SPY, QQQ, AAPL, TSLA, etc.)
- Defeats the purpose of a scalper (needs many opportunities)

### Option 3: Create Separate Profiles (Best Long-Term Solution)

**profiles/paper.env** (retail/testing):
```bash
LIQUIDITY_SCALPER_MIN_BAR_DOLLAR_VOLUME=250000
LIQUIDITY_SCALPER_MIN_SESSION_DOLLAR_VOLUME=5000000
LIQUIDITY_SCALPER_MIN_RANGE_PCT=0.012
LIQUIDITY_SCALPER_MAX_SPREAD_BPS=20
```

**profiles/live.env** (institutional, if ever used):
```bash
LIQUIDITY_SCALPER_MIN_BAR_DOLLAR_VOLUME=3000000
LIQUIDITY_SCALPER_MIN_SESSION_DOLLAR_VOLUME=30000000
LIQUIDITY_SCALPER_MIN_RANGE_PCT=0.015
LIQUIDITY_SCALPER_MAX_SPREAD_BPS=12
```

---

## Comparison: What Symbols Qualify?

### Current Runtime Thresholds ($3M/$30M)

Typical qualifying symbols:
- **SPY** (S&P 500 ETF): ~$50B session volume ✅
- **QQQ** (Nasdaq ETF): ~$15B session volume ✅
- **AAPL**: ~$8B session volume ✅
- **TSLA**: ~$12B session volume ✅
- **NVDA**: ~$20B session volume ✅

That's it. Maybe 5-10 symbols total on any given day.

### Proposed Paper Thresholds ($250K/$5M)

Typical qualifying symbols:
- All of the above ✅
- **Mid-cap growth stocks** (PLTR, HOOD, COIN, etc.)
- **Active small caps** (momentum names)
- **Sector ETFs** (XLF, XLK, XLE)
- **Volatility products** (UVXY, VXX)

Probably 50-100 symbols on an active day = **actual scalping opportunities**.

---

## Evidence From Code

### Strategy Rejects at Bar Structure Entry (Line 261-264)
```python
bar_dollar_volume = last.close * last.volume
if bar_dollar_volume < self.settings.liquidity_scalper_min_bar_dollar_volume:
    return None  # $3M threshold = instant rejection for most symbols
```

### Strategy Rejects at Trade Tape Entry (Line 216-218)
```python
session_dollar_volume = self._session_dollar_volume(session_bars, state)
if session_dollar_volume < self.settings.liquidity_scalper_min_session_dollar_volume:
    return None  # $30M threshold = instant rejection for most symbols
```

### Selector Comments Acknowledge This (Line 41)
```python
# Selector thresholds are softer than live strategy gates; runtime still uses LIQUIDITY_SCALPER_* env.
```

The comment **knows** there's a gap but doesn't mention it's **600x**.

---

## Testing The Fix

### Step 1: Check What Selector Returns

```bash
.venv/bin/python strategy_selectors/select_liquidity_scalper.py --top 12
```

Look at the output's `median_session_dollar_volume` and `p75_bar_dollar_volume` for selected symbols.

### Step 2: Compare to Runtime Requirements

```bash
# In profiles/paper.env, check:
grep LIQUIDITY_SCALPER profiles/paper.env
```

If the selector's median values are **much lower** than runtime requirements, you'll get zero trades.

### Step 3: Lower Runtime Thresholds

Add to `profiles/paper.env`:
```bash
LIQUIDITY_SCALPER_MIN_BAR_DOLLAR_VOLUME=250000
LIQUIDITY_SCALPER_MIN_SESSION_DOLLAR_VOLUME=5000000
LIQUIDITY_SCALPER_MIN_RANGE_PCT=0.012
LIQUIDITY_SCALPER_MAX_SPREAD_BPS=20
```

### Step 4: Rerun Selector and Bot

```bash
.venv/bin/python strategy_selectors/select_liquidity_scalper.py --top 12
scripts/run_paper.sh --strategy liquidity_scalper
```

You should now see signals in the logs.

---

## Recommended Fix (Immediate Action)

### For Paper Trading

Add to `profiles/paper.env`:
```bash
# Liquidity scalper: lower thresholds for paper/retail trading
LIQUIDITY_SCALPER_MIN_BAR_DOLLAR_VOLUME=250000
LIQUIDITY_SCALPER_MIN_SESSION_DOLLAR_VOLUME=5000000
LIQUIDITY_SCALPER_MIN_RANGE_PCT=0.012
LIQUIDITY_SCALPER_MAX_SPREAD_BPS=20
LIQUIDITY_SCALPER_MIN_TAPE_DOLLAR_VOLUME=50000
LIQUIDITY_SCALPER_MIN_TRADE_DOLLAR_VOLUME=5000
```

### Update CLAUDE.md

Document this threshold gap in the CLAUDE.md file:

```markdown
## Liquidity Scalper Thresholds

**IMPORTANT**: Default runtime thresholds are institutional-level ($3M bar volume, $30M session volume). 
For paper/retail trading, override in profiles/paper.env:

- LIQUIDITY_SCALPER_MIN_BAR_DOLLAR_VOLUME=250000 (down from $3M)
- LIQUIDITY_SCALPER_MIN_SESSION_DOLLAR_VOLUME=5000000 (down from $30M)
- LIQUIDITY_SCALPER_MAX_SPREAD_BPS=20 (up from 12 bps)
```

---

## Impact Analysis

### Before Fix (Current State)
- ❌ Selector selects 12 symbols
- ❌ Runtime rejects all 12 (too low liquidity)
- ❌ Zero trades generated
- ❌ Strategy appears "broken" but it's just mis-configured

### After Fix (Lowered Thresholds)
- ✅ Selector selects 12 symbols
- ✅ Runtime accepts 6-10 of them (adequate liquidity for retail)
- ✅ Generates tape impulse signals during active periods
- ✅ Strategy actually trades

---

## Why Default Thresholds Are So High

Likely reasons:
1. **Copied from institutional example** - someone using this for real money with large size
2. **Conservative starting point** - "better safe than sorry"
3. **Alpaca SIP data assumed** - unlimited symbols, institutional liquidity
4. **Never tested on Alpaca Basic IEX** - which has ~15 symbol limit and retail focus

The $3M/$30M thresholds make sense if you're:
- Trading $100K+ positions
- Using Alpaca SIP (unlimited symbols)
- Only want the most liquid names (SPY, QQQ, AAPL)

But they make **zero sense** for:
- Paper trading with $5K positions
- Alpaca Basic IEX with 15-symbol limit
- Testing a scalping strategy

---

## Summary

**Problem**: Runtime thresholds 60-600x stricter than selector thresholds  
**Cause**: Institutional-level liquidity requirements ($3M/$30M)  
**Impact**: Zero trades despite strategy being correctly implemented  
**Fix**: Lower runtime thresholds to $250K/$5M for paper/retail  
**Priority**: **CRITICAL** - strategy is unusable without this fix

The strategy code itself is **excellent** (see review). The configuration is just tuned for institutions, not retail.
