# Backtest log

Every parameter that reaches the live system is decided by a controlled
backtest, not by intuition. This file records those experiments — including the
ones whose answer was "the change is not worth making".

**Shared setup unless stated otherwise:** 150 S&P symbols, 419 trading days,
$100k starting capital, 0.05% slippage, no commission (Alpaca), signal on day
*i* fills at day *i+1*'s open (no lookahead), stops checked against the
intraday low, max 15 concurrent positions, 80% invested cap.

Each experiment freezes everything except the variable under test. Where the
trade count is identical across rows, the entry set was provably unchanged.

---

## 1. Exit model

**Question:** the original exit was a two-stage trailing stop (+5% → move stop
to break-even, +10% → move to +5%). Does a continuous ATR trailing stop do
better, and at which multiple?

Entries, initial stop and sizing identical across all rows; **1002 entries in
every run** by construction.

| Exit model | Return | Sharpe | MaxDD | PF | Win | Ø hold |
|---|---|---|---|---|---|---|
| Fixed 2-stage (baseline) | 120.8% | 0.65 | **−83.7%** | 1.93 | 29.5% | 25.1d |
| ATR trailing N=1.5 | 27.6% | 0.96 | −6.1% | 1.30 | 39.2% | 8.1d |
| **ATR trailing N=2.5** | 75.5% | **1.38** | −15.5% | 1.61 | 35.9% | 14.4d |
| ATR trailing N=3.5 | 81.2% | 1.01 | −32.7% | 1.55 | 32.8% | 19.9d |
| Adaptive ATR (1.5 low-vol / 3.0 high-vol) | 61.1% | 1.19 | −16.0% | 1.51 | 36.9% | 13.1d |

> Returns here are on a fixed-notional basis (position size not capped by
> available capital), so the absolute numbers are inflated. They are valid for
> *comparing* exits, not as a return expectation.

**What the individual trades showed:** on AMCR, which ran +460%, the tight
N=1.5 stop was shaken out after 5 days for +0.1% while N=2.5 captured the whole
move. The adaptive model inherited that failure because it applies the tight
factor to exactly the low-volatility names that trend quietly.

**Decision: continuous ATR trailing, N=2.5.** Best Sharpe, and it replaced a
baseline whose −83.7% drawdown no real account survives. The adaptive model was
rejected under a pre-registered tiebreaker (a Sharpe gap below 0.1 goes to the
simpler model) — here it lost outright anyway.

Harness: [`backtest/exit_compare.py`](../backtest/exit_compare.py)

---

## 2. Position sizing

**Question:** flat percentage sizing gives every position the same share of
equity regardless of stop distance. A name whose ATR is 9% of its price has a
stop ~20% away and therefore risks several times more per trade than a quiet
name. Does sizing by stop distance help?

This was prompted by live data: one high-ATR symbol lost $1005 while the entire
book was down $491.

| Sizing model | Return | Sharpe | MaxDD | PF | #Tr | Ø win | Ø loss | worst trade |
|---|---|---|---|---|---|---|---|---|
| Flat 5% (baseline) | 8.6% | 0.72 | −5.5% | 1.34 | 227 | $392 | −$178 | **−$506** |
| **Risk 0.25% / trade** | 7.4% | 0.72 | **−4.6%** | 1.34 | 227 | $338 | −$154 | **−$267** |
| Risk 0.50% / trade | 8.6% | 0.73 | −5.4% | 1.35 | 227 | $390 | −$177 | −$506 |
| Risk 0.75% / trade | 8.6% | 0.72 | −5.5% | 1.34 | 227 | $392 | −$178 | −$506 |
| Risk 0.5% + 10d cooldown after stop-out | 9.6% | 0.76 | −5.2% | 1.37 | 228 | $404 | −$181 | −$536 |

**Decision: risk 0.25% per trade.** Note what this does *not* claim: Sharpe is
unchanged (0.72), and the return is 1.2 points lower. The reason to ship it is
the right-hand column — the worst single trade halves and max drawdown falls.
For a system intended to eventually trade real money, that trade is worth
making. Larger risk budgets (0.5%, 0.75%) are indistinguishable from flat
sizing because the 5% per-position cap binds before the risk budget does.

The cooldown variant had the best Sharpe, but by 0.04 — inside the noise band,
so the pre-registered tiebreaker keeps the simpler model.

Harness: [`backtest/risk_sizing_compare.py`](../backtest/risk_sizing_compare.py)

---

## 3. Portfolio-level stop — rejected

**Question:** the equity curve peaked around +3% before giving it back. Would a
portfolio-wide stop ("liquidate everything on a 2% drawdown from the high-water
mark") lock in those gains?

| Model | Return | Sharpe | MaxDD | PF | #Tr |
|---|---|---|---|---|---|
| Risk 0.5% (reference) | 8.6% | 0.73 | −5.4% | 1.35 | 227 |
| **+ portfolio stop 2%** | **−2.5%** | **−0.49** | −3.6% | 0.55 | **53** |

**Decision: rejected.** The stop fires early and takes the winners out with the
losers; 227 trades collapse to 53. Drawdown improves, but only because the
strategy stops working.

*Caveat:* this implementation never re-enters after the stop fires, which is
the harshest possible reading. A re-entry variant would be milder — but the
direction is clear enough not to pursue.

The appeal of "sell at the top" is hindsight: the peak is only identifiable
afterwards. A rule that would have sold at the high would also have sold at
every earlier level that looked identical at the time.

---

## Live forward test

Backtests decide *whether* a change ships. They do not establish that the
strategy works — only forward testing on unseen data does that.

Current status and the pre-registered pass/fail criteria live in
[`FORWARD-TEST.md`](../FORWARD-TEST.md). The criteria were fixed before data
collection started and have not been changed since.

The most recent completed run failed 3 of 6 criteria (Sharpe −0.07, profit
factor 0.82, and a stop-coverage bug), which is documented there rather than
quietly dropped.
