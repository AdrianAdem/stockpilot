"""Exit-model comparison backtest (Teil 2).

Isolation guarantee: entries are generated ONCE (single signal pass with a
fixed, exit-independent 10-day per-symbol cooldown) and every exit model
replays the SAME entry list with a FIXED notional per trade. Trade count is
therefore identical across all runs by construction — only the exit differs.

No live-system changes. No Claude at runtime. Pure technical, backtestable rules.
"""

import asyncio
import statistics
from dataclasses import dataclass

import pandas as pd
import structlog

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(40))

from config.settings import load_config
from config.universe import get_universe
from data.alpaca_client import AlpacaClient
from data.technical import TechnicalAnalysis
from strategy.mean_reversion import MeanReversionStrategy
from strategy.momentum import MomentumStrategy

# --- frozen entry parameters (NOT changed in this task) ---
MIN_SCORE = 0.65
W_MOM, W_MR = 0.55, 0.45
SLIPPAGE = 0.0005
NOTIONAL = 5000.0  # fixed $ per trade -> identical sizing all models
INITIAL_CAPITAL = 100000.0
ENTRY_COOLDOWN = 10  # trading days; exit-independent entry gate
INITIAL_STOP_ATR = 2.0  # entry - 2*ATR, common to all models
LOOKBACK = 200

# --- Model 3 (adaptive) constants — tweak here ---
ADAPTIVE_LOW_FACTOR = 1.5
ADAPTIVE_HIGH_FACTOR = 3.0
# median ATR/price threshold is computed from the entry set at runtime


@dataclass
class Trade:
    symbol: str
    entry_idx: int
    entry_price: float
    atr: float
    rel_vol: float  # ATR/price at entry (for adaptive model)
    exit_idx: int = -1
    exit_price: float = 0.0
    pnl_pct: float = 0.0
    qty: float = 0.0


async def build_entries(symbols, bars):
    """Single deterministic signal pass -> fixed entry list + per-symbol arrays."""
    ta = TechnicalAnalysis()
    mom, mr = MomentumStrategy(W_MOM), MeanReversionStrategy(W_MR)

    all_dates = set()
    for df in bars.values():
        all_dates.update(df.index.strftime("%Y-%m-%d").tolist())
    dates = sorted(all_dates)

    # per-symbol day-indexed arrays for the exit replay
    arr = {}
    for s, df in bars.items():
        d = df.copy()
        d["dstr"] = d.index.strftime("%Y-%m-%d")
        arr[s] = d.set_index("dstr")

    entries = []
    last_entry_idx = {}  # symbol -> last entry day index (cooldown)
    pending = []  # signals from day i, fill at i+1 open

    for i, date in enumerate(dates):
        # fill yesterday's signals at today's open
        for sym in pending:
            a = arr.get(sym)
            if a is None or date not in a.index:
                continue
            if i - last_entry_idx.get(sym, -(10**9)) < ENTRY_COOLDOWN:
                continue
            row = a.loc[date]
            open_px = float(row["open"]) * (1 + SLIPPAGE)
            # ATR at entry (already computed in tech pass below; recompute簡)
            entries.append((sym, i, open_px, date))
            last_entry_idx[sym] = i
        pending = []

        if i < LOOKBACK:
            continue

        # compute tech up to today, generate signals
        tech = {}
        for s, df in bars.items():
            mask = df.index <= pd.Timestamp(date, tz="UTC")
            hist = df[mask].tail(LOOKBACK)
            if len(hist) >= 50:
                tech[s] = ta.calculate_all(hist)

        msig = await mom.generate_signals(list(tech.keys()), tech)
        rsig = await mr.generate_signals(list(tech.keys()), tech)
        by_sym = {}
        for sg in msig:
            by_sym.setdefault(sg.symbol, []).append((W_MOM, sg))
        for sg in rsig:
            by_sym.setdefault(sg.symbol, []).append((W_MR, sg))

        for sym, lst in by_sym.items():
            actions = {sg.action.value for _, sg in lst}
            if "BUY" in actions and "SELL" in actions:
                continue
            if "BUY" not in actions:
                continue
            tw = sum(w for w, _ in lst)
            score = sum(w * sg.score for w, sg in lst) / tw if tw else 0
            if score >= MIN_SCORE:
                pending.append(sym)

    # attach ATR + rel_vol at entry from the arrays
    enriched = []
    for sym, idx, px, date in entries:
        a = arr[sym]
        row = a.loc[date]
        atr = float(row.get("atr_14", 0)) if "atr_14" in a.columns else 0.0
        enriched.append(
            Trade(
                symbol=sym, entry_idx=idx, entry_price=px, atr=atr, rel_vol=(atr / px if px else 0)
            )
        )
    return dates, arr, enriched


def _atr_series(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(
        axis=1
    )
    return tr.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()


def replay(trade: Trade, dates, arr, exit_model, median_rv):
    """Replay one trade from entry to exit under the given model. Returns
    (exit_idx, exit_price). Stops checked intraday via the day's LOW."""
    a = arr[trade.symbol]
    {d: k for k, d in enumerate(dates)}
    entry = trade.entry_price
    stop = entry - INITIAL_STOP_ATR * trade.atr  # common initial stop
    # adaptive factor
    if exit_model[0] == "adaptive":
        n = ADAPTIVE_LOW_FACTOR if trade.rel_vol < median_rv else ADAPTIVE_HIGH_FACTOR
    elif exit_model[0] == "atr":
        n = exit_model[1]
    else:
        n = None

    for k in range(trade.entry_idx + 1, len(dates)):
        date = dates[k]
        if date not in a.index:
            continue
        row = a.loc[date]
        low, _high, close = float(row["low"]), float(row["high"]), float(row["close"])
        atr_now = float(row["atr_14"]) if not pd.isna(row["atr_14"]) else trade.atr

        # 1. intraday stop hit?
        if low <= stop:
            return k, stop * (1 - SLIPPAGE)

        # 2. raise stop per model (only upward)
        gain = (close - entry) / entry
        if exit_model[0] == "fixed2":
            if gain >= 0.10:
                stop = max(stop, entry * 1.05)
            elif gain >= 0.05:
                stop = max(stop, entry)
        else:  # atr / adaptive: continuous, only once profitable
            if close > entry:
                stop = max(stop, close - n * atr_now)

    # still open at end -> close at last available close
    last = a.iloc[-1]
    return len(dates) - 1, float(last["close"]) * (1 - SLIPPAGE)


def metrics(trades, dates, name):
    # daily equity curve: fixed notional per trade, mark-to-market
    [INITIAL_CAPITAL] * len(dates)
    # build per-day contribution
    [0.0] * len(dates)
    pnls = []
    holds = []
    for t in trades:
        qty = NOTIONAL / t.entry_price
        pnl = (t.exit_price - t.entry_price) * qty
        pnls.append(pnl)
        holds.append(t.exit_idx - t.entry_idx)
    # equity curve via cumulative realized at exit day
    curve = [INITIAL_CAPITAL]
    running = INITIAL_CAPITAL
    exits_by_day = {}
    for t, pnl in zip(trades, pnls, strict=False):
        exits_by_day.setdefault(t.exit_idx, 0.0)
        exits_by_day[t.exit_idx] += pnl
    for k in range(len(dates)):
        running += exits_by_day.get(k, 0.0)
        curve.append(running)

    total_ret = (curve[-1] - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    rets = [
        (curve[i] - curve[i - 1]) / curve[i - 1] for i in range(1, len(curve)) if curve[i - 1] > 0
    ]
    if len(rets) > 1 and statistics.pstdev(rets) > 0:
        sharpe = statistics.mean(rets) / statistics.pstdev(rets) * (252**0.5)
    else:
        sharpe = 0.0
    peak, maxdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        maxdd = min(maxdd, (v - peak) / peak)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    wr = len(wins) / len(pnls) * 100 if pnls else 0
    avg_hold = statistics.mean(holds) if holds else 0
    return {
        "name": name,
        "ret": total_ret,
        "sharpe": sharpe,
        "maxdd": maxdd * 100,
        "pf": pf,
        "wr": wr,
        "n": len(trades),
        "hold": avg_hold,
        "pnls": pnls,
        "total_pnl": sum(pnls),
    }


async def main():
    cfg = load_config()
    ac = AlpacaClient(cfg.alpaca)
    u = await get_universe([])
    syms = u[:150]
    bars = await ac.get_bars_multi(syms, "1Day", limit=420)
    bars = {s: d for s, d in bars.items() if not d.empty and len(d) >= 260}
    # attach ATR series to each frame
    for _s, d in bars.items():
        d["atr_14"] = _atr_series(d)
    print(f"Universum: {len(bars)} Symbole, {max(len(d) for d in bars.values())} Tage")

    dates, arr, entries = await build_entries(list(bars.keys()), bars)
    print(f"Entries (fix, alle Modelle identisch): {len(entries)}")

    median_rv = statistics.median([t.rel_vol for t in entries if t.rel_vol > 0])
    print(f"Median ATR/Kurs (adaptive Schwelle): {median_rv:.4f}\n")

    models = [
        ("Modell 1: Fixed 2-Stufen", ("fixed2",)),
        ("Modell 2: ATR N=1.5", ("atr", 1.5)),
        ("Modell 2: ATR N=2.5", ("atr", 2.5)),
        ("Modell 2: ATR N=3.5", ("atr", 3.5)),
        ("Modell 3: Adaptiv 1.5/3.0", ("adaptive",)),
    ]

    # pick 3 example symbols that actually have a big winner under baseline
    results = []
    per_model_trades = {}
    for label, model in models:
        import copy

        trades = [copy.copy(t) for t in entries]
        for t in trades:
            ei, ep = replay(t, dates, arr, model, median_rv)
            t.exit_idx, t.exit_price = ei, ep
            t.pnl_pct = (ep - t.entry_price) / t.entry_price * 100
        results.append(metrics(trades, dates, label))
        per_model_trades[label] = trades

    print(
        f"{'Modell':28} {'Return':>8} {'Sharpe':>7} {'MaxDD':>7} {'PF':>6} {'WinRate':>8} {'#Trades':>8} {'Ø-Hold':>7}"
    )
    print("-" * 90)
    base_n = results[0]["n"]
    for r in results:
        flag = "  <-- #TRADES WEICHT AB!" if r["n"] != base_n else ""
        pf = f"{r['pf']:.2f}" if r["pf"] != float("inf") else "inf"
        print(
            f"{r['name']:28} {r['ret']:>7.1f}% {r['sharpe']:>7.2f} {r['maxdd']:>6.1f}% {pf:>6} {r['wr']:>7.1f}% {r['n']:>8} {r['hold']:>6.1f}d{flag}"
        )

    print(
        f"\n#Trades identisch in allen Läufen: {all(r['n'] == base_n for r in results)} ({base_n})"
    )

    # Example trades: pick the 2 biggest baseline winners + 1 loser, show all models
    base_trades = per_model_trades[models[0][0]]
    base_sorted = sorted(range(len(base_trades)), key=lambda j: base_trades[j].pnl_pct)
    pick = [base_sorted[-1], base_sorted[-2], base_sorted[0]]  # 2 best, 1 worst
    print("\n=== Beispiel-Trades: gleicher Entry, Exit je Modell ===")
    for j in pick:
        sym = base_trades[j].symbol
        ep = base_trades[j].entry_price
        print(f"\n  {sym} (Entry ${ep:.2f}):")
        for label, _ in models:
            t = per_model_trades[label][j]
            held = t.exit_idx - t.entry_idx
            print(f"    {label:28} Exit ${t.exit_price:.2f} ({t.pnl_pct:+.1f}%, {held}d)")

    # robustness: top-3 trades share of total pnl for best model
    print("\n=== Robustheit: Anteil Top-3-Trades am Gesamt-PnL ===")
    for r in results:
        srt = sorted(r["pnls"], reverse=True)
        top3 = sum(srt[:3])
        share = top3 / r["total_pnl"] * 100 if r["total_pnl"] else 0
        print(f"  {r['name']:28} Top-3 = ${top3:>8.0f} von ${r['total_pnl']:>8.0f} ({share:.0f}%)")

    await ac.close()


if __name__ == "__main__":
    asyncio.run(main())
