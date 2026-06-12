# Recovery Scale Strategy

## Summary

`recovery_scale` is a scale-in / scale-out recovery strategy for liquid stocks whose
larger trend is constructive. It intentionally starts with a small position during a
controlled decline, increases the position as the drawdown deepens, then holds through
the recovery and scales out as profit, overextension, or trend weakness appears.

The strategy is not a blind averaging-down system. It only averages down inside a
predefined risk budget, in symbols with enough liquidity, and when the global market
regime allows recovery trades. If the decline stops looking like a normal pullback and
starts looking like structural failure, the strategy must stop adding and move toward
defense.

## Core Idea

High-liquidity stocks with constructive higher-timeframe structure often recover from
intraday or short-term selloffs, especially when broad market conditions are supportive.
The strategy tries to lower the average cost during that selloff so the later recovery
can become profitable earlier than a confirmation-only entry.

The core operating rule:

> Start small while the stock is falling, add more only as planned drawdown levels are
> reached, stop adding if the structure breaks, then hold and scale out after recovery.

## Required Inputs

- Minute bars for the traded symbol.
- Current quote/spread for execution sanity checks.
- Daily or higher-timeframe context for broad trend quality.
- Global Market Regime state.
- Existing global risk state, including soft, hard, and kill limits.
- Indicator values used by the current charting stack:
  - EMA 5 / 20 / 40 / 60
  - VWAP
  - SuperTrend
  - MACD / histogram
  - AO
  - EMA Gap
  - BreakoutPower
  - RSI
  - ATR or equivalent volatility estimate

## Candidate Universe

Use the existing market universe as a broad tradable boundary pool. Do not put this
strategy's setup logic into the market universe selector.

Preferred candidates:

- High liquidity and tight spreads.
- Strong dollar volume and reliable fills.
- Larger trend is upward or constructively recovering.
- Daily EMA structure is healthy enough to support mean reversion.
- Not in obvious long-term decline.
- Not subject to known event/news risk that invalidates technical recovery behavior.

Avoid:

- Low-liquidity names.
- Wide-spread names.
- Stocks in persistent daily downtrends.
- Stocks with collapsing volume.
- Gap-down names caused by severe news, earnings shock, halt risk, or fundamental break.
- Symbols whose intraday decline is moving with broad market risk-off pressure.

## Global Market Regime Integration

`recovery_scale` must use the existing Global Market Regime gate. Market condition
changes should adjust both entry permission and scale-in aggressiveness.

Suggested behavior:

| Regime | New Entries | Averaging Down | Position Size | Exit Bias |
| --- | --- | --- | --- | --- |
| Risk-on / Bullish | Allowed | Full ladder allowed | 100% planned budget | Let winners breathe |
| Neutral | Allowed with filters | Reduced ladder | 60-70% planned budget | Take partials sooner |
| Weak / Block-adjacent | Very selective | 1-2 early tranches only | 25-40% planned budget | Fast defense |
| Risk-off / Block | Blocked | No adds | 0% new budget | Reduce or flatten weak positions |

If the regime worsens while already in a position:

- Stop opening new `recovery_scale` positions.
- Stop later averaging-down tranches first.
- Preserve only positions that already show recovery confirmation.
- Force defensive exits if the account reaches hard or kill risk limits.

## Risk Budget Model

This strategy expects temporary unrealized losses. It therefore needs a strategy-local
drawdown bucket that coexists with global risk controls.

Recommended layers:

- Global soft limit: stop new entries, reduce planned ladder size, and pause late adds.
- Global hard limit: stop all averaging down and reduce weak positions.
- Global kill switch: account protection overrides strategy state.
- Strategy daily loss budget: caps total realized + unrealized damage from
  `recovery_scale`.
- Per-symbol planned-loss budget: caps worst-case damage before the first order is sent.

Important rule:

> Averaging down is allowed only while the position remains inside its preplanned
> drawdown envelope and global risk state has not hardened.

## Strategy States

### 1. `WATCHING`

The symbol is in the candidate pool, but no position exists.

Look for:

- Larger trend constructive.
- Market regime supportive enough.
- Intraday selloff beginning or already underway.
- Spread and liquidity acceptable.

No order is required in this state.

### 2. `SCALING_IN`

Initial small entry has started, and the strategy is lowering average cost as planned
drawdown levels are reached.

Expected behavior:

- Unrealized loss is normal.
- Add only at predefined price or ATR intervals.
- Add size increases with deeper planned drawdown.
- Do not add if broad market regime worsens past allowed threshold.
- Do not add after structural failure.

### 3. `RECOVERY_CONFIRMED`

The position has stopped being an averaging-down trade and has become a recovery trade.

Typical confirmation signals:

- Price recovers EMA20.
- SuperTrend flips bullish or remains supportive.
- RSI recovers above 50.
- MACD histogram improves or turns positive.
- AO improves or turns positive.
- EMA Gap recovers toward or above zero.
- BreakoutPower rises above 50.

After this state starts:

- Stop pure averaging down.
- Allow only controlled pullback adds if still under max budget.
- Move risk management from drawdown allowance to recovery protection.

### 4. `PROFIT_HOLD`

The trade is profitable or near profit, and the strategy is trying to capture the
recovery trend.

Hold while:

- Price remains above EMA20 or relevant support.
- SuperTrend remains bullish.
- Higher lows hold.
- Momentum does not collapse.

### 5. `SCALING_OUT`

The strategy is reducing exposure into profit, resistance, overextension, or weakening
structure.

Partial exits should begin before the entire recovery fails.

### 6. `EXITED`

No position remains. Re-entry requires a fresh setup and should respect cooldowns.

## Scale-In Ladder

The exact ladder should be configurable. A conservative default should start small and
increase add size as drawdown deepens.

Example percent-drop ladder:

| Tranche | Trigger From Reference | Planned Size |
| --- | ---: | ---: |
| 1 | -1.0% | 10% |
| 2 | -2.0% | 15% |
| 3 | -3.5% | 20% |
| 4 | -5.0% | 25% |
| 5 | -7.0% | 30% |

Example ATR ladder:

| Tranche | Trigger From Reference | Planned Size |
| --- | ---: | ---: |
| 1 | -0.5 ATR | 10% |
| 2 | -1.0 ATR | 15% |
| 3 | -1.6 ATR | 20% |
| 4 | -2.3 ATR | 25% |
| 5 | -3.1 ATR | 30% |

Implementation should prefer volatility-aware spacing. Percent-drop and ATR triggers
can both be supported, with the stricter trigger used for higher-volatility symbols.

## Scale-In Eligibility

Allow the next add only when all are true:

- Symbol still satisfies liquidity and spread limits.
- Global Market Regime allows the tranche number being considered.
- Position remains within the planned per-symbol risk budget.
- Daily strategy loss budget is not exhausted.
- Current decline is still plausibly a pullback, not structural failure.
- No hard global risk state is active.
- The add will not exceed max position value or max exposure limits.

Structural failure examples:

- Price keeps making new lows without meaningful bounces.
- EMA60 is falling and price is far below it.
- SuperTrend remains bearish while momentum continues deteriorating.
- MACD and AO are both expanding to the downside.
- RSI cannot recover from weak levels.
- BreakoutPower cannot hold above 50 during attempted recovery.
- Broad market regime moves into risk-off/block.

## Recovery Confirmation

Recovery confirmation should require a cluster of signals, not one indicator alone.

Recommended minimum:

- Price above EMA20.
- SuperTrend bullish or no longer pressing from above.
- RSI above 50.
- At least two of:
  - MACD histogram improving or positive.
  - AO improving or positive.
  - EMA Gap near zero or positive.
  - BreakoutPower above 50.
  - Price reclaiming VWAP.

Stronger confirmation:

- EMA5 above EMA20.
- Price above VWAP.
- BreakoutPower above 70.
- Higher low after the recovery push.
- Pullback holds EMA5/EMA20 or VWAP.

## Pullback Adds After Recovery

After `RECOVERY_CONFIRMED`, do not continue blind averaging down. Adds should become
trend-support adds.

Allow a pullback add only when:

- Price pulls back toward EMA5, EMA20, or VWAP.
- The prior recovery low holds.
- SuperTrend remains bullish.
- MACD/AO do not fully roll over.
- Global Market Regime remains supportive.
- The position remains below max planned size.

Do not add strength if RSI is already overextended or the price is chasing far above
support.

## Scale-Out Plan

Partial exits should recover capital early while keeping a runner for strong recoveries.

Example exits:

| Exit | Trigger | Size |
| --- | --- | ---: |
| Partial 1 | Average cost +1.5% to +2.5%, or first meaningful resistance | 20-30% |
| Partial 2 | Average cost +4% to +5%, prior high, or overextension | 25-35% |
| Partial 3 | RSI near/above 70, BreakoutPower 80 then rolling over, or MACD histogram fading | 20-30% |
| Final | SuperTrend bearish, EMA20 lost, higher low broken, or trailing stop hit | Remainder |

Weak recoveries should take profits faster. Strong recoveries in bullish regimes can
hold the runner longer.

## Exit And Failure Rules

Exit defense should depend on the state.

During `SCALING_IN`:

- Do not exit only because the position is temporarily red.
- Exit or stop adding when the planned drawdown envelope is violated.
- Exit if structural failure appears.
- Exit if global hard/kill limits require it.

During `RECOVERY_CONFIRMED`:

- Reduce if price loses EMA20 soon after confirmation.
- Reduce if recovery fails to hold the prior higher low.
- Stop adding after momentum rolls over.

During `PROFIT_HOLD` and `SCALING_OUT`:

- Protect breakeven after meaningful partial profit.
- Use SuperTrend, EMA20, higher-low failure, or ATR trailing stop for the runner.
- Fully exit when two or more major weakness signals align.

Major weakness signals:

- SuperTrend turns bearish.
- Price closes below EMA20.
- MACD crosses down or histogram expands negative.
- RSI falls below 50 after recovery.
- Higher low breaks.
- Global Market Regime hardens.

## Global Risk Interaction

`recovery_scale` must not fight the account-level risk system. Instead, global risk state
should alter strategy behavior.

Suggested interaction:

- Normal: full strategy behavior allowed by market regime.
- Soft limit: no new entries; shrink remaining ladder; pause late tranches.
- Hard limit: no new entries; no averaging down; reduce weak or unconfirmed positions.
- Kill switch: flatten or enter account-defined emergency handling regardless of state.

The strategy should expose enough reason text to explain when an add was skipped due to
global risk rather than symbol-specific failure.

## Implementation Checklist

- Add `recovery_scale` strategy class and register it in the strategy registry.
- Add strategy-specific settings with safe defaults.
- Add per-symbol state tracking for tranche count, average cost, reference price,
  planned budget, recovery confirmation, partial exits, and stop levels.
- Integrate existing Global Market Regime state before entry and before every add.
- Add explicit `SCALING_IN`, `RECOVERY_CONFIRMED`, `PROFIT_HOLD`, `SCALING_OUT`, and
  `EXITED` state transitions.
- Ensure max position value, max open positions, daily loss, and global hard/kill limits
  are checked before every order.
- Add clear decision reasons for skipped entries, skipped adds, partial exits, and
  failure exits.
- Add tests for:
  - Bullish regime full ladder.
  - Weak regime reduced ladder.
  - Risk-off blocking.
  - Global soft limit pausing adds.
  - Global hard limit stopping adds.
  - Recovery confirmation transition.
  - Pullback add after confirmation.
  - Partial exits and final exit.

## Initial Tuning Defaults

These are starting points for paper trading, not final optimized values.

- Max tranches: 5.
- Initial tranche: 10% of planned position.
- Max pre-confirmation exposure in weak regime: 25-40%.
- Max position value: strategy-specific override preferred.
- Min RSI for recovery confirmation: 50.
- BreakoutPower recovery threshold: 50.
- Strong BreakoutPower threshold: 70.
- Overextension BreakoutPower threshold: 80.
- First partial profit: +1.5% to +2.5% from average cost.
- Second partial profit: +4% to +5% from average cost.
- Stop adding after global soft limit unless regime and risk budget explicitly allow it.
- Stop all averaging down after global hard limit.

## Design Principle

The strategy makes money by being early in the recovery, but survives by being strict
about where averaging down is allowed.

The most important rule:

> Lower the average cost only while the trade still looks like a high-liquidity,
> constructive-trend pullback inside a supportive market regime.
