"""Position-sizing comparison backtest.

Frozen across all models: entries (momentum+mean_rev, threshold 0.65, next-open
fill), exit (continuous ATR trailing N=2.5 — the exit-backtest winner), initial
stop (entry-2*ATR), slippage 0.05%. ONLY the position-sizing rule changes.

Capital-realistic: shared capital pool, max 15 concurrent positions, 20% cash
reserve (max 80% invested). Because bigger positions consume more capital and
slots, the trade SET legitimately differs between models — that IS the effect
under test. We report #trades + concentration alongside risk-adjusted return.
"""
import asyncio
import statistics
import copy

import pandas as pd
import structlog

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(40))

from config.settings import load_config
from config.universe import get_universe
from data.alpaca_client import AlpacaClient
from data.technical import TechnicalAnalysis
from strategy.momentum import MomentumStrategy
from strategy.mean_reversion import MeanReversionStrategy

# frozen
MIN_SCORE = 0.65
W_MOM, W_MR = 0.55, 0.45
SLIPPAGE = 0.0005
INITIAL_CAPITAL = 100000.0
LOOKBACK = 200
TRAIL_N = 2.5          # winning exit
INIT_STOP_N = 2.0
MAX_POSITIONS = 15
MAX_INVESTED = 0.80    # keep >=20% cash
MIN_POSITION = 100.0

# sizing constants (tweak here)
BASE_PCT = 0.04        # median-vol stock target weight
FLOOR_PCT = 0.015
CAP_STD = 0.05         # standard cap
CAP_LOWVOL = 0.08      # cap for low-vol names in model 3


def _atr_series(df, length=14):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()


def size_pct(model, rel_vol, median_rv):
    """Return target position weight for a candidate."""
    if model == "flat":
        return CAP_STD
    # vol-based: inverse to volatility -> equal risk per position
    scalar = median_rv / rel_vol if rel_vol > 0 else 1.0
    pct = BASE_PCT * scalar
    if model == "vol":
        return max(FLOOR_PCT, min(pct, CAP_STD))
    if model == "vol_highcap":
        cap = CAP_LOWVOL if rel_vol < median_rv else CAP_STD
        return max(FLOOR_PCT, min(pct, cap))
    return CAP_STD


async def build_candidates(symbols, bars):
    """One deterministic pass -> per-date BUY candidates with ATR/rel_vol/score."""
    ta = TechnicalAnalysis()
    mom, mr = MomentumStrategy(W_MOM), MeanReversionStrategy(W_MR)
    all_dates = set()
    for df in bars.values():
        all_dates.update(df.index.strftime("%Y-%m-%d").tolist())
    dates = sorted(all_dates)

    arr = {}
    for s, df in bars.items():
        d = df.copy(); d["dstr"] = d.index.strftime("%Y-%m-%d")
        arr[s] = d.set_index("dstr")

    cands_by_date = {d: [] for d in dates}
    for i, date in enumerate(dates):
        if i < LOOKBACK:
            continue
        tech = {}
        for s, df in bars.items():
            mask = df.index <= pd.Timestamp(date, tz="UTC")
            hist = df[mask].tail(LOOKBACK)
            if len(hist) >= 50:
                tech[s] = ta.calculate_all(hist)
        msig = await mom.generate_signals(list(tech.keys()), tech)
        rsig = await mr.generate_signals(list(tech.keys()), tech)
        by = {}
        for sg in msig: by.setdefault(sg.symbol, []).append((W_MOM, sg))
        for sg in rsig: by.setdefault(sg.symbol, []).append((W_MR, sg))
        for sym, lst in by.items():
            acts = set(sg.action.value for _, sg in lst)
            if "BUY" in acts and "SELL" in acts: continue
            if "BUY" not in acts: continue
            tw = sum(w for w, _ in lst)
            score = sum(w * sg.score for w, sg in lst) / tw if tw else 0
            if score >= MIN_SCORE:
                td = tech[sym]
                atr = td.get("ATR") or 0
                price = td.get("price") or 0
                if atr > 0 and price > 0:
                    cands_by_date[date].append(
                        {"sym": sym, "score": score, "atr": atr,
                         "rel_vol": atr / price})
    return dates, arr, cands_by_date


def simulate(model, dates, arr, cands_by_date, median_rv):
    cash = INITIAL_CAPITAL
    positions = {}   # sym -> {qty, entry, stop, atr, entry_idx}
    trades = []
    equity_curve = [INITIAL_CAPITAL]
    pending = []     # candidates from day i -> fill day i+1 open

    def bar(sym, d):
        a = arr.get(sym)
        if a is None or d not in a.index: return None
        return a.loc[d]

    for i, date in enumerate(dates):
        # 1. fill pending at today's open
        for cnd in pending:
            sym = cnd["sym"]
            if sym in positions: continue
            if len(positions) >= MAX_POSITIONS: continue
            b = bar(sym, date)
            if b is None: continue
            equity = cash + sum(p["qty"] * (bar(s, date)["close"] if bar(s, date) is not None else p["entry"])
                                for s, p in positions.items())
            invested = sum(p["qty"] * (bar(s, date)["close"] if bar(s, date) is not None else p["entry"])
                           for s, p in positions.items())
            pct = size_pct(model, cnd["rel_vol"], median_rv)
            target = equity * pct
            # cash reserve: don't exceed 80% invested
            room = max(0.0, equity * MAX_INVESTED - invested)
            target = min(target, room, cash)
            if target < MIN_POSITION: continue
            entry = float(b["open"]) * (1 + SLIPPAGE)
            qty = int(target / entry)
            if qty <= 0: continue
            cost = qty * entry
            if cost > cash: continue
            cash -= cost
            positions[sym] = {"qty": qty, "entry": entry,
                              "stop": entry - INIT_STOP_N * cnd["atr"],
                              "atr": cnd["atr"], "entry_idx": i, "pct": pct}
        pending = []

        # 2. intraday stop check + ATR trailing
        for sym in list(positions.keys()):
            p = positions[sym]
            b = bar(sym, date)
            if b is None: continue
            low, close = float(b["low"]), float(b["close"])
            atr_now = float(b["atr_14"]) if not pd.isna(b["atr_14"]) else p["atr"]
            if low <= p["stop"]:
                exit_px = p["stop"] * (1 - SLIPPAGE)
                cash += p["qty"] * exit_px
                trades.append({"sym": sym, "pnl": (exit_px - p["entry"]) * p["qty"],
                               "hold": i - p["entry_idx"], "pct": p["pct"]})
                del positions[sym]
                continue
            if close > p["entry"]:
                p["stop"] = max(p["stop"], close - TRAIL_N * atr_now)

        # 3. new signals today -> fill tomorrow
        for cnd in cands_by_date.get(date, []):
            if cnd["sym"] not in positions:
                pending.append(cnd)

        # 4. mark-to-market
        eqv = cash
        for s, p in positions.items():
            b = bar(s, date)
            eqv += p["qty"] * (float(b["close"]) if b is not None else p["entry"])
        equity_curve.append(eqv)

    # close remaining
    for s, p in positions.items():
        a = arr[s]; last = float(a.iloc[-1]["close"])
        cash += p["qty"] * last
        trades.append({"sym": s, "pnl": (last - p["entry"]) * p["qty"],
                       "hold": len(dates) - p["entry_idx"], "pct": p["pct"]})

    return equity_curve, trades


def metrics(name, curve, trades):
    ret = (curve[-1] - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    rets = [(curve[i] - curve[i-1]) / curve[i-1] for i in range(1, len(curve)) if curve[i-1] > 0]
    sharpe = (statistics.mean(rets) / statistics.pstdev(rets) * (252 ** 0.5)
              if len(rets) > 1 and statistics.pstdev(rets) > 0 else 0)
    peak, dd = curve[0], 0
    for v in curve:
        peak = max(peak, v); dd = min(dd, (v - peak) / peak)
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p <= 0]
    pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float("inf")
    wr = len(wins) / len(pnls) * 100 if pnls else 0
    avg_pct = statistics.mean([t["pct"] for t in trades]) * 100 if trades else 0
    max_pct = max([t["pct"] for t in trades]) * 100 if trades else 0
    return {"name": name, "ret": ret, "sharpe": sharpe, "dd": dd*100, "pf": pf,
            "wr": wr, "n": len(trades), "avg_pct": avg_pct, "max_pct": max_pct}


async def main():
    cfg = load_config(); ac = AlpacaClient(cfg.alpaca)
    u = await get_universe([])
    bars = await ac.get_bars_multi(u[:150], "1Day", limit=420)
    bars = {s: d for s, d in bars.items() if not d.empty and len(d) >= 260}
    for s, d in bars.items():
        d["atr_14"] = _atr_series(d)
    print(f"Universum: {len(bars)} Symbole")
    dates, arr, cands = await build_candidates(list(bars.keys()), bars)
    rvs = [c["rel_vol"] for lst in cands.values() for c in lst if c["rel_vol"] > 0]
    median_rv = statistics.median(rvs)
    print(f"Kandidaten-Signale: {sum(len(v) for v in cands.values())} | Median ATR/Kurs {median_rv:.4f}\n")

    models = [("1: Flat 5%", "flat"),
              ("2: Vol-basiert (Cap 5%)", "vol"),
              ("3: Vol + Low-Vol-Cap 8%", "vol_highcap")]
    results = []
    for label, m in models:
        curve, trades = simulate(m, dates, arr, cands, median_rv)
        results.append(metrics(label, curve, trades))

    print(f"{'Modell':26} {'Return':>8} {'Sharpe':>7} {'MaxDD':>7} {'PF':>6} {'Win':>6} {'#Tr':>5} {'ØGröße':>7} {'MaxGröße':>8}")
    print("-" * 92)
    for r in results:
        pf = f"{r['pf']:.2f}" if r['pf'] != float("inf") else "inf"
        print(f"{r['name']:26} {r['ret']:>7.1f}% {r['sharpe']:>7.2f} {r['dd']:>6.1f}% {pf:>6} {r['wr']:>5.0f}% {r['n']:>5} {r['avg_pct']:>6.1f}% {r['max_pct']:>7.1f}%")

    await ac.close()


if __name__ == "__main__":
    asyncio.run(main())
