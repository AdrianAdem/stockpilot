"""Risk-based position sizing comparison.

Motivated by live forward-test results: flat percentage sizing gives every
position the same share of the portfolio regardless of how far its stop sits.
A high-ATR name (WDC: ATR 9.2% of price -> stop 18-23% away) therefore risks
3-4x more per trade than a quiet one, and a single such symbol accounted for
-$1005 of the -$491 net result.

Frozen across every model: entries (momentum + mean reversion, threshold 0.65,
next-open fill), exit (continuous ATR trailing N=2.5), initial stop
(entry - 2*ATR), slippage 0.05%, max 15 concurrent positions, 80% invested cap.
ONLY the sizing rule, the post-stop cooldown and the portfolio stop vary.
"""

import asyncio
import statistics

import pandas as pd
import structlog

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(40))

from backtest.sizing_compare import (  # noqa: E402
    INIT_STOP_N,
    INITIAL_CAPITAL,
    MAX_INVESTED,
    MAX_POSITIONS,
    MIN_POSITION,
    SLIPPAGE,
    TRAIL_N,
    _atr_series,
    build_candidates,
)
from config.settings import load_config  # noqa: E402
from config.universe import get_universe  # noqa: E402
from data.alpaca_client import AlpacaClient  # noqa: E402

FLAT_PCT = 0.05  # current live behaviour
HARD_CAP_PCT = 0.05  # never exceed this share of equity, whatever the maths says
FLOOR_PCT = 0.005


def target_value(model, equity, entry, stop):
    """Dollar amount to allocate to one candidate."""
    if model["type"] == "flat":
        return equity * FLAT_PCT

    # risk-based: size so that (entry - stop) * qty == equity * risk_budget
    risk_per_share = max(entry - stop, 1e-9)
    risk_frac = risk_per_share / entry  # stop distance as % of price
    pct = model["risk"] / risk_frac
    return equity * max(FLOOR_PCT, min(pct, HARD_CAP_PCT))


def simulate(model, dates, arr, cands_by_date):
    cash = INITIAL_CAPITAL
    positions = {}
    trades = []
    equity_curve = [INITIAL_CAPITAL]
    pending = []
    cooldown_until = {}  # symbol -> day index it may be re-entered
    peak_equity = INITIAL_CAPITAL
    halted = False

    def bar(sym, d):
        a = arr.get(sym)
        if a is None or d not in a.index:
            return None
        return a.loc[d]

    def mark(date):
        v = cash
        for s, p in positions.items():
            b = bar(s, date)
            v += p["qty"] * (float(b["close"]) if b is not None else p["entry"])
        return v

    for i, date in enumerate(dates):
        equity_now = mark(date)

        # optional portfolio-level trailing stop: flatten everything on a
        # drawdown from the equity high-water mark
        if model.get("port_stop") and not halted:
            peak_equity = max(peak_equity, equity_now)
            if equity_now < peak_equity * (1 - model["port_stop"]):
                for s, p in list(positions.items()):
                    b = bar(s, date)
                    px = float(b["close"]) if b is not None else p["entry"]
                    exit_px = px * (1 - SLIPPAGE)
                    cash += p["qty"] * exit_px
                    trades.append(
                        {
                            "sym": s,
                            "pnl": (exit_px - p["entry"]) * p["qty"],
                            "hold": i - p["entry_idx"],
                            "reason": "portfolio_stop",
                        }
                    )
                    del positions[s]
                halted = True
                pending = []

        # 1. fill pending at today's open
        if not halted:
            for cnd in pending:
                sym = cnd["sym"]
                if sym in positions or len(positions) >= MAX_POSITIONS:
                    continue
                if i < cooldown_until.get(sym, -1):
                    continue
                b = bar(sym, date)
                if b is None:
                    continue
                equity = mark(date)
                invested = equity - cash
                entry = float(b["open"]) * (1 + SLIPPAGE)
                stop = entry - INIT_STOP_N * cnd["atr"]
                target = target_value(model, equity, entry, stop)
                room = max(0.0, equity * MAX_INVESTED - invested)
                target = min(target, room, cash)
                if target < MIN_POSITION:
                    continue
                qty = int(target / entry)
                if qty <= 0 or qty * entry > cash:
                    continue
                cash -= qty * entry
                positions[sym] = {
                    "qty": qty,
                    "entry": entry,
                    "stop": stop,
                    "atr": cnd["atr"],
                    "entry_idx": i,
                    "pct": (qty * entry) / equity,
                }
        pending = []

        # 2. intraday stop check + ATR trailing
        for sym in list(positions.keys()):
            p = positions[sym]
            b = bar(sym, date)
            if b is None:
                continue
            low, close = float(b["low"]), float(b["close"])
            atr_now = float(b["atr_14"]) if not pd.isna(b["atr_14"]) else p["atr"]
            if low <= p["stop"]:
                exit_px = p["stop"] * (1 - SLIPPAGE)
                cash += p["qty"] * exit_px
                trades.append(
                    {
                        "sym": sym,
                        "pnl": (exit_px - p["entry"]) * p["qty"],
                        "hold": i - p["entry_idx"],
                        "pct": p["pct"],
                        "reason": "stop",
                    }
                )
                del positions[sym]
                if model.get("cooldown"):
                    cooldown_until[sym] = i + model["cooldown"]
                continue
            if close > p["entry"]:
                p["stop"] = max(p["stop"], close - TRAIL_N * atr_now)

        # 3. new signals today -> fill tomorrow
        if not halted:
            for cnd in cands_by_date.get(date, []):
                if cnd["sym"] not in positions:
                    pending.append(cnd)

        equity_curve.append(mark(date))

    # close survivors at the last available close
    for s, p in positions.items():
        last = float(arr[s].iloc[-1]["close"])
        cash += p["qty"] * last
        trades.append(
            {
                "sym": s,
                "pnl": (last - p["entry"]) * p["qty"],
                "hold": len(dates) - p["entry_idx"],
                "pct": p["pct"],
                "reason": "eod",
            }
        )

    return equity_curve, trades


def metrics(name, curve, trades):
    ret = (curve[-1] - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    rets = [
        (curve[i] - curve[i - 1]) / curve[i - 1] for i in range(1, len(curve)) if curve[i - 1] > 0
    ]
    sharpe = (
        statistics.mean(rets) / statistics.pstdev(rets) * (252**0.5)
        if len(rets) > 1 and statistics.pstdev(rets) > 0
        else 0
    )
    peak, dd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        dd = min(dd, (v - peak) / peak)
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float("inf")
    worst = min(pnls) if pnls else 0
    return {
        "name": name,
        "ret": ret,
        "sharpe": sharpe,
        "dd": dd * 100,
        "pf": pf,
        "wr": len(wins) / len(pnls) * 100 if pnls else 0,
        "n": len(trades),
        "avg_win": statistics.mean(wins) if wins else 0,
        "avg_loss": statistics.mean(losses) if losses else 0,
        "worst": worst,
    }


async def main():
    cfg = load_config()
    ac = AlpacaClient(cfg.alpaca)
    u = await get_universe([])
    bars = await ac.get_bars_multi(u[:150], "1Day", limit=420)
    bars = {s: d for s, d in bars.items() if not d.empty and len(d) >= 260}
    for _s, d in bars.items():
        d["atr_14"] = _atr_series(d)
    print(f"Universum: {len(bars)} Symbole")

    dates, arr, cands = await build_candidates(list(bars.keys()), bars)
    print(f"Kandidaten: {sum(len(v) for v in cands.values())}\n")

    models = [
        ("A  Flat 5% (aktuell)", {"type": "flat"}),
        ("B1 Risk 0.25%/Trade", {"type": "risk", "risk": 0.0025}),
        ("B2 Risk 0.50%/Trade", {"type": "risk", "risk": 0.005}),
        ("B3 Risk 0.75%/Trade", {"type": "risk", "risk": 0.0075}),
        ("C  Risk 0.5% + Cooldown 10d", {"type": "risk", "risk": 0.005, "cooldown": 10}),
        ("D  Risk 0.5% + Portfolio-Stop 2%", {"type": "risk", "risk": 0.005, "port_stop": 0.02}),
    ]

    results = []
    for label, m in models:
        curve, trades = simulate(m, dates, arr, cands)
        results.append(metrics(label, curve, trades))

    hdr = f"{'Modell':32} {'Return':>8} {'Sharpe':>7} {'MaxDD':>7} {'PF':>6} {'Win':>5} {'#Tr':>5} {'ØGew':>7} {'ØVerl':>7} {'schlecht.':>9}"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        pf = f"{r['pf']:.2f}" if r["pf"] != float("inf") else "inf"
        print(
            f"{r['name']:32} {r['ret']:>7.1f}% {r['sharpe']:>7.2f} {r['dd']:>6.1f}% {pf:>6} "
            f"{r['wr']:>4.0f}% {r['n']:>5} {r['avg_win']:>7.0f} {r['avg_loss']:>7.0f} {r['worst']:>9.0f}"
        )

    await ac.close()


if __name__ == "__main__":
    asyncio.run(main())
