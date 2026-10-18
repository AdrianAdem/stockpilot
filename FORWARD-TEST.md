# Forward test — pass/fail criteria and results

**Criteria fixed on 2026-06-10, before data collection started. Not revised since.**

The point of writing them down first is that a bad week cannot move the goalposts
afterwards.

## Current run
**Run 5: 2026-10-19 → 2026-11-30** (6 weeks).
First run in which the live bot trades the model the backtest actually tested:
completed daily bars, one entry attempt per signal near the open, the plain
strategy score against the 0.65 threshold, and a `fill − 2×ATR` initial stop.

---

## Run 4 results (2026-08-24 → 2026-10-05)

30 trading days, 34 closed trades, equity $99,840 → $96,812.

| # | Criterion | Target | Actual | |
|---|-----------|--------|--------|---|
| 1 | Sharpe ratio | > 0.8 | **−3.92** | ❌ |
| 2 | Max drawdown | < 8% | **−3.99%** | ✅ |
| 3 | Profit factor | > 1.3 | **0.32** | ❌ |
| 4 | Closed trades | ≥ 30 | **34** | ✅ |
| 5 | Uptime | no outage > 24h | two 2-day gaps (17–18 Sep, 28–29 Sep) | ❌ |
| 6 | Stop coverage | 100% at all times | uncovered positions on 17 days, last 14 Sep | ❌ |

**4 criteria failed → per the decision matrix: re-examine the assumptions.**

### Diagnosis

Win rate 32%, average win $103 vs average loss $156. Trades that ended within
five days won **0%** of the time — entries were whipsawed out almost at once.

The decisive check was out-of-sample: the unchanged backtest, run over the
same six weeks, lost **−5.8% at Sharpe −8.2** — worse than live. SPY was flat
(+0.1%). So the loss was not an implementation fault: the strategy has no edge
in a sideways market, while in the preceding trending window (SPY +3.3%) the
same backtest made +2.6% at Sharpe 1.8.

Four live-vs-backtest deviations were found along the way and fixed for run 5
(see changelog). They did not cause run 4's loss — the backtest lost more —
but they meant live was trading a model the backtest never validated.

The same investigation found defects in the backtest harnesses themselves
(docs/BACKTESTS.md, section 5). Re-run cleanly, the strategy's backtested
Sharpe is about **0.3** over 440 days, and the fill-order noise alone spans
−0.5 to 1.0. **Criterion 1 (Sharpe > 0.8) is not met even in backtest.**
Run 5 therefore mainly tests whether live now reproduces the backtest, not
whether the strategy is profitable.

Stop coverage failed on a bug in the protective-stop lifecycle, fixed on
22 Sep; since then no position has been without a stop. Uptime failed because
the bot runs on a laptop that sleeps.

## Run 3 results (2026-06-29 → 2026-08-21)

39 trading days, 36 closed trades, equity $100,285 → $99,971.

| # | Criterion | Target | Actual | |
|---|-----------|--------|--------|---|
| 1 | Sharpe ratio | > 0.8 | **−0.07** | ❌ |
| 2 | Max drawdown | < 8% | **−3.00%** | ✅ |
| 3 | Profit factor | > 1.3 | **0.82** | ❌ |
| 4 | Closed trades | ≥ 30 | **36** | ✅ |
| 5 | Uptime | no unnoticed outage > 24h | active on 33 of 39 days | ⚠️ |
| 6 | Stop coverage | 100% at all times | **5 of 15** (over a weekend) | ❌ |

**3 criteria failed → per the decision matrix: re-examine the assumptions.**

### Diagnosis

The 50% win rate was fine. The asymmetry was not: **average win $122 vs average
loss $150.** For a trend-following system that relationship has to be inverted.

**One symbol accounts for almost the entire shortfall.** WDC was traded four
times for a net −$1005; excluding it the book was **+$514 instead of −$491**.

The cause was not a broken stop. WDC's ATR was 9.2% of its price, putting the
stop 18–23% away, yet it received the same portfolio weight as a name with a
5.5% stop distance — so it risked 3–4× more per trade. That is a sizing flaw,
and it is what run 4 changes.

Criterion 6 failed on a genuine bug (see changelog): a rejected order-replace
was treated as fatal instead of falling back to re-creating the stop, leaving
10 of 15 positions unprotected. Run 3 therefore never tested the exit mechanism
under its intended conditions.

---

## Changelog

- **2026-10-18 — live signal path aligned with the backtest. Test restarted.**
  (a) Signals use completed daily bars instead of the still-forming bar.
  (b) One entry attempt per symbol per session, within 60 minutes of the open
  (`ENTRY_WINDOW_MINUTES`). Previously an unchanged signal could re-buy a name
  hours after its stop fired.
  (c) Claude is a veto only. The ×0.9 haircut on unconfirmed signals (which
  raised the effective threshold to ~0.72) and the confidence blend are gone.
  (d) Initial stop `fill − 2×ATR` for every strategy; mean reversion, whale and
  Claude previously set their own distances.
  (e) Claude decisions are cached per symbol per day, and calls pause for six
  hours after a billing error (2,333 failed calls during run 4).
  A market-regime gate (SPY > SMA50) was implemented, then **removed before
  going live**: the harness re-run showed it failing its own decision rule.
- **2026-09-22 — protective-stop lifecycle fixed** (pending orders, broker fill
  accounting, completed daily bars for ATR). Not a restart; mid-run fix.
- **2026-08-23 — stop-coverage bug fixed, risk-based sizing added. Test restarted.**
  (a) Alpaca rejects a PATCH replace when the order is no longer replaceable
  (422/403); the failure was treated as fatal, so stops were neither updated nor
  re-created. Now falls back to cancel-and-recreate, and reconciliation also runs
  outside market hours.
  (b) Position size now follows stop distance (`RISK_PER_TRADE=0.0025`) instead of
  a flat percentage. Backtest over 419 days with identical entries and exits:
  worst single trade −$506 → −$267, max drawdown −5.5% → −4.6%, Sharpe unchanged.
- **2026-06-29 — exit logic replaced:** fixed two-stage trailing → continuous ATR
  trailing at N=2.5 (backtest Sharpe 1.38 vs 0.65). Test restarted.
- **2026-06-17 — liquidity filter fixed:** the volume threshold was applied to IEX
  feed volume (~2–3% of consolidated), admitting only ~29 mega-caps instead of ~200
  names. Test restarted.

## Criteria (unchanged since 2026-06-10)

| # | Criterion | Threshold | Rationale |
|---|-----------|-----------|-----------|
| 1 | Sharpe ratio | > 0.8 | Backtest showed 1.3 — forward may degrade but must stay clearly positive risk-adjusted |
| 2 | Max drawdown | < 8% | Backtest −4.6%; ~2× tolerance, beyond that the risk model has failed |
| 3 | Profit factor | > 1.3 | 1.3 = genuinely profitable after slippage |
| 4 | Closed trades | ≥ 30 | Below that there is no statistical statement to make |
| 5 | Uptime | no unnoticed outage > 24h | Heartbeat and Telegram alerts must surface downtime |
| 6 | Stop coverage | 100% at all times | No position without a live stop |

## Honest expectation

- The corrected backtest makes about +2.7% over 440 days at Sharpe 0.3 —
  roughly **+0.4% over six weeks**, inside the noise. A flat or slightly
  negative run 5 is the expected outcome, not a malfunction.
- The useful question for run 5: does live P&L now track the backtest over the
  same weeks? Compare both at the end. A large gap means a remaining
  implementation difference; a small gap with a poor result means the
  strategy needs new ideas, not more tuning.
- Entries happen only in the first hour after the open. On days the machine is
  asleep at the open, the bot will not buy — exits and stops are unaffected.

## Abort conditions (stop and analyse immediately)

- Total drawdown > 10%
- Positions without stop coverage on 3 consecutive days
- Claude API offline > 48h without an alert

## Tested and rejected

- **Portfolio-level stop** ("liquidate everything on an X% drawdown from the high"):
  −2.5% return, Sharpe −0.49, 53 trades instead of 227. Cuts the winners along with
  the losers.
- **Larger risk budgets** (0.5%, 0.75% per trade): indistinguishable from flat
  sizing, because the 5% per-position cap binds first.

## Decision on 2026-11-30

- **All 6 criteria met** → discuss the next phase
- **1–2 failed** → root-cause analysis; do not blindly re-tune parameters; run another 6 weeks
- **3+ failed** → re-examine the strategy's assumptions

## Weekly check (Mondays)

```bash
.venv/bin/python -c "
import asyncio
from storage.db import Database
async def t():
    db=Database(); await db.connect()
    cur=await db._db.execute('''SELECT COUNT(*), SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),
        ROUND(SUM(pnl),2) FROM trades WHERE closed_at > '2026-10-19' AND pnl != 0''')
    n,w,pnl=await cur.fetchone()
    print(f'closed: {n} | wins: {w} | realised: \${pnl}')
    await db.close()
asyncio.run(t())"
```
