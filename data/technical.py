import numpy as np
import pandas as pd
import structlog

logger = structlog.get_logger()


def _rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # Wilder's smoothing (RMA) == EWM with alpha = 1/length
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _ema(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(span=length, adjust=False).mean()


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()


class TechnicalAnalysis:
    """Indicator calculations on OHLCV bars. No external TA library —
    pandas/numpy only (pandas-ta is unmaintained and breaks on numpy>=2)."""

    def calculate_all(self, df: pd.DataFrame) -> dict:
        if df.empty or len(df) < 50:
            return {}

        close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
        result: dict = {}

        # RSI (14)
        rsi = _rsi(close, 14)
        result["RSI"] = round(float(rsi.iloc[-1]), 2) if not np.isnan(rsi.iloc[-1]) else None

        # MACD (12, 26, 9)
        macd_line = _ema(close, 12) - _ema(close, 26)
        signal_line = _ema(macd_line, 9)
        hist = macd_line - signal_line
        result["MACD"] = round(float(macd_line.iloc[-1]), 4)
        result["MACD_signal"] = round(float(signal_line.iloc[-1]), 4)
        result["MACD_histogram"] = round(float(hist.iloc[-1]), 4)

        prev_m, prev_s = float(macd_line.iloc[-2]), float(signal_line.iloc[-2])
        cur_m, cur_s = float(macd_line.iloc[-1]), float(signal_line.iloc[-1])
        if prev_m <= prev_s and cur_m > cur_s:
            result["MACD_crossover"] = "bullish"
        elif prev_m >= prev_s and cur_m < cur_s:
            result["MACD_crossover"] = "bearish"
        else:
            result["MACD_crossover"] = "none"

        result["MACD_bullish_recent"] = False
        for i in range(max(len(macd_line) - 3, 1), len(macd_line)):
            if (macd_line.iloc[i - 1] <= signal_line.iloc[i - 1]
                    and macd_line.iloc[i] > signal_line.iloc[i]):
                result["MACD_bullish_recent"] = True
                break

        # Bollinger Bands (20, 2)
        mid = close.rolling(20).mean()
        std = close.rolling(20).std()
        bb_upper = mid + 2 * std
        bb_lower = mid - 2 * std
        result["BB_upper"] = round(float(bb_upper.iloc[-1]), 2)
        result["BB_middle"] = round(float(mid.iloc[-1]), 2)
        result["BB_lower"] = round(float(bb_lower.iloc[-1]), 2)

        current_price = float(close.iloc[-1])
        result["price"] = round(current_price, 2)
        if current_price <= bb_lower.iloc[-1]:
            result["BB_position"] = "below_lower"
        elif current_price >= bb_upper.iloc[-1]:
            result["BB_position"] = "above_upper"
        else:
            result["BB_position"] = "inside"

        # SMA 50 / 200
        sma50 = close.rolling(50).mean()
        sma200 = close.rolling(200).mean() if len(df) >= 200 else None
        result["SMA_50"] = round(float(sma50.iloc[-1]), 2) if not np.isnan(sma50.iloc[-1]) else None
        result["SMA_200"] = (round(float(sma200.iloc[-1]), 2)
                             if sma200 is not None and not np.isnan(sma200.iloc[-1]) else None)
        result["above_SMA50"] = current_price > sma50.iloc[-1] if result["SMA_50"] else None
        result["above_SMA200"] = (current_price > sma200.iloc[-1]
                                  if result["SMA_200"] else None)

        # Golden / Death cross
        if result["SMA_50"] and result["SMA_200"]:
            prev_50, prev_200 = float(sma50.iloc[-2]), float(sma200.iloc[-2])
            cur_50, cur_200 = float(sma50.iloc[-1]), float(sma200.iloc[-1])
            if prev_50 <= prev_200 and cur_50 > cur_200:
                result["cross"] = "golden"
            elif prev_50 >= prev_200 and cur_50 < cur_200:
                result["cross"] = "death"
            else:
                result["cross"] = "none"

        # ATR (14)
        atr = _atr(high, low, close, 14)
        result["ATR"] = round(float(atr.iloc[-1]), 2) if not np.isnan(atr.iloc[-1]) else None

        # Volume vs 20-day average
        vol_sma20 = vol.rolling(20).mean()
        last_vol_avg = float(vol_sma20.iloc[-1]) if not np.isnan(vol_sma20.iloc[-1]) else 0
        if last_vol_avg > 0:
            result["volume_ratio"] = round(float(vol.iloc[-1]) / last_vol_avg, 2)
            result["avg_volume_20d"] = int(last_vol_avg)
        else:
            result["volume_ratio"] = None
            result["avg_volume_20d"] = None
        result["volume"] = int(vol.iloc[-1])

        return result
