"""Market-regime filter comparison.

Motivated by forward-test run 4 (2026-08-24 -> 2026-10-05): live -3.0%, and
the backtest over the same window lost even more (-5.8%) while SPY was flat.
The strategy itself, not the live implementation, lost money in that regime.

Frozen across every variant: entries (momentum + mean reversion, threshold
0.65, next-open fill), exit (ATR trailing N=2.5), initial stop (entry - 2*ATR),
risk-based sizing 0.25% per trade, slippage, position and invested caps.
ONLY a gate on the day's new entries varies. Regime state for day d uses data
up to d's close; those entries fill at d+1's open, so there is no lookahead.

Decision rule, fixed before running: adopt a filter only if Sharpe improves
both over the full period and in-sample (before run 3), keeping at least 60%
of the baseline trades. The run-4 window is reported but cannot decide,
because it is the window that prompted the test.
"""

import asyncio
import statistics

import pandas as pd
import structlog

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(40))

from backtest.risk_sizing_compare import metrics, simulate  # noqa: E402
from backtest.sizing_compare import _atr_series, build_candidates  # noqa: E402
from config.settings import load_config  # noqa: E402
from config.universe import get_universe  # noqa: E402
from data.alpaca_client import AlpacaClient  # noqa: E402

MODEL = {"type": "risk", "risk": 0.0025}
WINDOWS = [
    ("in-sample", "2025-06-01", "2026-06-28"),
    ("run 3", "2026-06-29", "2026-08-21"),
    ("run 4", "2026-08-24", "2026-10-05"),
]


def by_date(series: pd.Series) -> dict[str, bool]:
    """Index a boolean series by YYYY-MM-DD, matching the candidate keys."""
    s = series.copy()
    s.index = s.index.strftime("%Y-%m-%d")
    return s[~s.index.duplicated(keep="last")].to_dict()


def window_stats(dates, curve, start, end):
    idx = [i for i, d in enumerate(dates) if start <= d <= end]
    v = [curve[i + 1] for i in idx]  # curve[0] is the starting capital
    r = [v[i] / v[i - 1] - 1 for i in range(1, len(v))]
    sd = statistics.pstdev(r) if len(r) > 1 else 0
    sharpe = statistics.mean(r) / sd * 252**0.5 if sd else 0
    return (v[-1] / v[0] - 1) * 100, sharpe


async def main():
    ac = AlpacaClient(load_config().alpaca)
    universe = await get_universe([])
    bars = await ac.get_bars_multi([*universe[:150], "SPY"], "1Day", limit=420)
    spy = bars.pop("SPY")
    bars = {s: d for s, d in bars.items() if not d.empty and len(d) >= 260}
    for d in bars.values():
        d["atr_14"] = _atr_series(d)
    dates, arr, cands = await build_candidates(list(bars.keys()), bars)

    close = spy["close"]
    above = pd.DataFrame({s: d["close"] > d["close"].rolling(50).mean() for s, d in bars.items()})
    gates = {
        "SPY > SMA200": by_date(close > close.rolling(200).mean()),
        "SPY > SMA50": by_date(close > close.rolling(50).mean()),
        "breadth >= 50% above SMA50": by_date(above.mean(axis=1) >= 0.5),
    }
    variants = [("no filter", cands)] + [
        (name, {d: c for d, c in cands.items() if gate.get(d, False)})
        for name, gate in gates.items()
    ]

    header = f"{'variant':28} {'ret':>7} {'sharpe':>7} {'maxDD':>7} {'PF':>5} {'#tr':>4}"
    print(header + "".join(f" | {w[0]:>16}" for w in WINDOWS))
    for name, c in variants:
        curve, trades = simulate(MODEL, dates, arr, c)
        m = metrics(name, curve, trades)
        row = (
            f"{name:28} {m['ret']:6.1f}% {m['sharpe']:7.2f} {m['dd']:6.1f}% "
            f"{m['pf']:5.2f} {m['n']:4}"
        )
        for _, a, b in WINDOWS:
            ret, sh = window_stats(dates, curve, a, b)
            row += f" | {ret:+6.2f}% S{sh:+5.2f}"
        print(row)

    await ac.close()


if __name__ == "__main__":
    asyncio.run(main())
