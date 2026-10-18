"""Market regime gate for new long entries.

The strategy is long-only trend following. Backtested over 440 days, taking
new entries only while SPY closes above its 50-day SMA raised Sharpe from
0.49 to 0.76 and profit factor from 1.20 to 1.35 (in-sample 1.12 -> 1.70),
at 195 instead of 225 trades. See docs/BACKTESTS.md.

The gate only blocks *new* entries; open positions keep their stops and
exits regardless of regime.
"""

import pandas as pd

REGIME_SYMBOL = "SPY"


def market_uptrend(bars: pd.DataFrame | None, sma_length: int = 50) -> bool | None:
    """True if the last completed close is above its SMA, None if undecidable.

    Callers must pass completed daily bars only: a partial session would let
    an intraday dip or spike flip the regime several times a day.
    """
    if bars is None or bars.empty or "close" not in bars:
        return None
    close = bars["close"].dropna()
    if len(close) < sma_length:
        return None
    sma = close.rolling(sma_length).mean().iloc[-1]
    return bool(close.iloc[-1] > sma)
