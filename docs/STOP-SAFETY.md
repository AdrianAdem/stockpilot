# Protective-order repair — 2026-09-15

The order timeout previously canceled protective GTC sell stops. Reconciliation
ran before that cancellation, leaving positions unprotected until a later scan.

## Changes

- Apply entry timeouts only to standalone buy market/limit orders.
- Preserve protective orders on shutdown; verify entry cancellation separately.
- Confirm submitted/replaced stop status, price and remaining quantity at the broker.
- Preserve the existing stop if a replacement fails; never loosen its broker price.
- Block additional entries when coverage is unconfirmed, including after a failed execution attempt.
- Treat pending/partially filled stops as unresolved until broker state settles.
- Avoid blindly repeating POST/PATCH requests after ambiguous failures.

## Verification

- 43 tests passed, including 17 protective-order and API regression cases.
- Targeted Ruff checks passed; independent code review approved the final delta.
- Paper service restarted on 2026-09-14 at approximately 23:01 UTC.
- Subsequent broker snapshot: 10 positions, 10 full-quantity GTC stops.
  Existing stop submission timestamps preceded the restart. This does not establish
  continuous coverage during future sessions.

## Scope and remaining work

Entry strategies, score threshold, universe and position sizing were not changed.
Stale ATR pagination, fill-ledger reconciliation, contradictory Claude signals,
and backtest time/fill/accounting defects remain separate tasks. Previously
reported backtest metrics should not be treated as validated evidence until those
defects are corrected. The forward-test schedule was not reset by this repair.
