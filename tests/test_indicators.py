"""Technical indicator tests — pure functions, no network or credentials."""

import numpy as np
import pandas as pd
import pytest

from data.technical import TechnicalAnalysis, _atr, _ema, _rsi


def _series(values: list[float]) -> pd.Series:
    return pd.Series(values, dtype="float64")


def _ohlcv(n: int = 260, start: float = 100.0, step: float = 0.5) -> pd.DataFrame:
    """Deterministic rising series with a fixed intraday range."""
    close = np.array([start + i * step for i in range(n)])
    return pd.DataFrame(
        {
            "open": close - 0.2,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        },
        index=pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC"),
    )


class TestRSI:
    def test_all_gains_approaches_100(self):
        rsi = _rsi(_series(list(range(1, 40))), length=14)
        assert rsi.iloc[-1] > 99

    def test_all_losses_approaches_zero(self):
        rsi = _rsi(_series([40 - i for i in range(39)]), length=14)
        assert rsi.iloc[-1] < 1

    def test_stays_within_bounds(self):
        prices = _series([100 + (i % 7) - 3 for i in range(60)])
        rsi = _rsi(prices, length=14).dropna()
        assert ((rsi >= 0) & (rsi <= 100)).all()


class TestEMA:
    def test_constant_series_equals_constant(self):
        assert _ema(_series([5.0] * 30), 10).iloc[-1] == pytest.approx(5.0)

    def test_reacts_faster_than_longer_span(self):
        prices = _series([10.0] * 20 + [20.0] * 5)
        assert _ema(prices, 5).iloc[-1] > _ema(prices, 20).iloc[-1]


class TestATR:
    def test_matches_constant_true_range(self):
        n = 50
        close = _series([100.0] * n)
        high = _series([102.0] * n)
        low = _series([98.0] * n)
        # constant 4.0 range with no gaps -> ATR converges to 4.0
        assert _atr(high, low, close, 14).iloc[-1] == pytest.approx(4.0, abs=0.01)

    def test_is_positive(self):
        df = _ohlcv()
        assert _atr(df["high"], df["low"], df["close"], 14).iloc[-1] > 0


class TestCalculateAll:
    def test_returns_empty_for_short_history(self):
        assert TechnicalAnalysis().calculate_all(_ohlcv(n=10)) == {}

    def test_returns_empty_for_empty_frame(self):
        assert TechnicalAnalysis().calculate_all(pd.DataFrame()) == {}

    def test_uptrend_produces_expected_fields(self):
        result = TechnicalAnalysis().calculate_all(_ohlcv())
        for key in ("RSI", "MACD", "BB_upper", "SMA_50", "SMA_200", "ATR", "price"):
            assert key in result, f"missing indicator: {key}"

    def test_uptrend_is_above_both_moving_averages(self):
        result = TechnicalAnalysis().calculate_all(_ohlcv())
        assert result["above_SMA50"] is True
        assert result["above_SMA200"] is True

    def test_bollinger_bands_are_ordered(self):
        r = TechnicalAnalysis().calculate_all(_ohlcv())
        assert r["BB_lower"] < r["BB_middle"] < r["BB_upper"]

    def test_volume_ratio_is_one_for_flat_volume(self):
        assert TechnicalAnalysis().calculate_all(_ohlcv())["volume_ratio"] == pytest.approx(
            1.0, abs=0.01
        )
