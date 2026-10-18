# Forward test — pass/fail criteria and results

**Criteria fixed on 2026-06-10, before data collection started. Not revised since.**

The point of writing them down first is that a bad week cannot move the goalposts
afterwards.

## Current run
**Run 5: 2026-10-19 → 2026-11-30** (6 weeks).
First run in which the live signal path matches the backtested one (completed
daily bars, undiscounted threshold, stop anchored to the fill) and new entries
are gated by market regime.

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

Three live-vs-backtest deviations were found along the way and fixed for run 5
(see changelog). They did not cause run 4's loss — the backtest lost more —
but they meant live was trading a model the backtest never validated.

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

- **2026-10-18 — live signal path aligned with the backtest, regime gate added.
  Test restarted.**
  (a) Signals are computed on completed daily bars; previously they used the
  still-forming bar, so intraday noise could trigger entries.
  (b) The 0.9 haircut on signals without Claude confirmation is removed. It
  silently raised the live entry threshold from the backtested 0.65 to ~0.72.
  (c) The initial stop keeps its 2×ATR distance but is measured from the fill.
  (d) New entries only while SPY's last close is above its 50-day SMA. Backtest
  over 440 days: Sharpe 0.49 → 0.76, profit factor 1.20 → 1.35, 225 → 195
  trades. It does **not** rescue the run-4 window (−6.4% → −6.1%): SPY sat above
  its SMA50 on 26 of those 30 days. Sideways chop above the averages remains an
  unsolved weakness, documented rather than tuned away.
  (e) Claude decisions are cached per symbol per day, and calls pause for six
  hours after a billing error (2,333 failed calls during run 4).
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

- The regime-gated backtest returns 7.5% over 440 days ≈ **+1.0% over six weeks**.
  Live will likely be lower.
- **If the market chops sideways above its averages again, run 5 will lose** —
  the gate does not cover that case. A losing run in a flat market says the
  known weakness is still there, not that the fixes failed. Compare against SPY.
- With the gate active, weeks without a single new entry are expected
  behaviour, not an outage. Telegram reports every regime switch.
- A win rate near 40% is normal for trend following, provided the average win
  exceeds the average loss.

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
