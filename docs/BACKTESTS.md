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

> **Correction, 2026-10-18.** An audit found two defects in the harnesses that
> sections 1–3 were originally run on: signals lagged one session (the date
> filter compared New-York-stamped bars against UTC midnight), and same-day
> candidates filled in arbitrary API-response order. Both are fixed. Sections
> 1–3 keep their original tables as the record of how each decision was made;
> section 5 re-runs them and states whether each conclusion still holds.

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
losers; 227 trades collapse to 53. *(Re-run, section 5: it fires within the first
weeks and the model never re-enters, leaving 23 trades — any Sharpe it shows
is the Sharpe of not trading.)* Drawdown improves, but only because the
strategy stops working.

*Caveat:* this implementation never re-enters after the stop fires, which is
the harshest possible reading. A re-entry variant would be milder — but the
direction is clear enough not to pursue.

The appeal of "sell at the top" is hindsight: the peak is only identifiable
afterwards. A rule that would have sold at the high would also have sold at
every earlier level that looked identical at the time.

---

## 4. Market-regime gate — rejected

**Question:** forward-test run 4 (24 Aug – 5 Oct) lost 3.0%. Was that the live
implementation or the strategy?

**Out-of-sample check.** The backtest over those six weeks lost more than live
did, in every variant (−5% to −8%), while SPY was flat. The strategy, not the
plumbing, lost money there: long-only momentum is whipsawed in a sideways market.

**Tested:** gate new entries on a textbook regime filter, standard parameters
only. Decision rule fixed before running: adopt only if Sharpe improves both
over the full period *and* in-sample, keeping ≥ 60% of trades. Added once the
fill-order problem (section 5) surfaced — a stricter rule, not a looser one:
the filter must also beat the baseline in ≥ 80% of 30 random same-day fill
orders, each paired with the baseline under the identical order.

| Gate | Sharpe (score order) | In-sample | PF | #Tr | Beats baseline, paired |
|---|---|---|---|---|---|
| None (baseline) | 0.29 | 1.31 | 1.10 | 228 | — |
| SPY > SMA200 | 0.00 | 0.93 | 0.99 | 228 | 7 / 30 |
| SPY > SMA50 | −0.01 | 1.27 | 0.99 | 212 | 9 / 30 |
| Breadth ≥ 50% above SMA50 | 0.55 | 1.19 | 1.25 | 167 | 23 / 30 |

**Decision: no gate.** None passes. Breadth comes closest but loses in-sample
and misses the 80% bar.

*How close this came to shipping:* on the defective harness, SPY > SMA50 looked
like a clear win (Sharpe 0.49 → 0.76, PF 1.20 → 1.35) and was implemented. The
audit and re-run reversed it before it went live.

Harness: [`backtest/regime_compare.py`](../backtest/regime_compare.py)

---

## 5. Method corrections and what they revealed

**One-session signal lag.** Alpaca stamps daily bars at New York midnight
(04:00 or 05:00 UTC). The harnesses selected history with
`index <= Timestamp(date, tz="UTC")`, which excluded day *d*'s own bar — so a
"signal on *d*, fill at *d+1*'s open" was really a signal on *d−1*. No
lookahead, but not the model the live bot runs. Fixed to compare against New
York midnight.

**Gap-down stop fills.** A stop gapped through at the open was filled at the
stop price. It now fills at the open, in every harness. This turned the worst
single trades from roughly −$500 into −$1,100 to −$2,700 for flat sizing.

**Exit harness specifics.** It also took the initial ATR from the fill day's
row — which includes that day's high and low, unknown at the open — and never
checked the stop on the entry day itself. Both fixed.

**Fill order.** With 15 slots and an 80% invested cap, *which* same-day
candidates get filled matters. The order was whatever the data API returned,
which varies between requests. Same model, same data, same 2,400 candidates:

| Fill order | Sharpe |
|---|---|
| API order, request A | 0.24 |
| API order, request B | 0.50 |
| 30 random orders | median 0.50, range **−0.48 to 1.04** |
| By score, highest first (what live does) | 0.29 |

Two conclusions follow. First, every capital-constrained comparison needs a
fixed order; candidates are now sorted by score, as live trades them. Second,
and more important: **the strategy's measured edge is not distinguishable
from the noise of which trades happen to fill.** Taking the highest-scored
setups first does *worse* than the random median, so the score does not rank
trade quality.

**Re-run of the earlier decisions** (score order, lag and gap fills fixed):

| Decision | Original | Re-run | Holds? |
|---|---|---|---|
| Exit: ATR trailing N=2.5 | Sharpe 1.38 vs 0.65 (2-stage) | **0.99** vs 0.67; N=1.5 0.64, N=3.5 0.65, adaptive 0.56 | Yes, clearly — but its top 3 trades are 86% of the profit |
| Sizing: risk 0.25% per trade | Sharpe 0.72 = 0.72, worst −$267 vs −$506 | **0.29 vs 0.13**, worst −$491 vs −$1,119, MaxDD −8.7% vs −11.4% | Yes, more clearly |
| Regime gate | SPY > SMA50 adopted | rejected | **No** |

The exit comparison uses fixed notional per trade, so fill order does not
affect it; its Sharpe values are not comparable to the capital-constrained ones.

### Live-vs-backtest deviations fixed on 2026-10-18

None of these caused run 4's loss — the backtest lost more — but each meant
the live bot traded a model that was never tested:

- Signals used the **still-forming** daily bar. Now completed bars only.
- An unchanged completed-bar signal could **re-buy a name hours after it was
  stopped out**, and enter at any time of day. The backtest fills each signal
  once, at the open. Live now allows one attempt per symbol per session, in the
  first 60 minutes.
- Unconfirmed signals were scored **×0.9**, silently lifting the threshold from
  0.65 to ~0.72; confirmed ones blended in Claude's confidence, so a lukewarm
  "yes" could sink a setup a "no" would have passed. Claude is now a veto only.
- Initial stops were 2×, 2.5× or 3× ATR depending on strategy, or Claude's own
  number. Now `fill − 2×ATR` for every entry, as tested.

---

## Live forward test

Backtests decide *whether* a change ships. They do not establish that the
strategy works — only forward testing on unseen data does that.

Current status and the pre-registered pass/fail criteria live in
[`FORWARD-TEST.md`](../FORWARD-TEST.md). The criteria were fixed before data
collection started and have not been changed since.

The two most recent completed runs failed 3 and 4 of 6 criteria. Both are
documented there, with diagnoses, rather than quietly dropped. Given section 5,
the Sharpe > 0.8 criterion is not met even in backtest.
