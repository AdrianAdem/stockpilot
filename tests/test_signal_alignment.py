"""Live signal path must match what was backtested.

The backtest computes signals on completed daily bars, fills at the next
open with a stop at entry - 2*ATR, trades only while SPY is above its 50-day
SMA, and applies the 0.65 threshold to the undiscounted strategy score.
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from analysis.claude_analyst import ClaudeAnalyst
from data.alpaca_client import AlpacaClient
from execution.trader import Trader
from strategy.regime import market_uptrend


def _bar(day: int, close: float = 100.0) -> dict:
    return {
        "t": f"2026-09-{day:02d}T04:00:00Z",
        "o": close,
        "h": close,
        "l": close,
        "c": close,
        "v": 1000,
    }


class TestCompletedBarsMulti:
    def test_current_session_bar_is_excluded(self):
        client = object.__new__(AlpacaClient)
        client.data_url = "https://data.alpaca.markets"
        client._request = AsyncMock(return_value={"bars": {"AAPL": [_bar(14), _bar(15), _bar(16)]}})
        bars = asyncio.run(
            client.get_bars_multi(
                ["AAPL"],
                "1Day",
                limit=3,
                completed_only=True,
                as_of=datetime.fromisoformat("2026-09-16T15:00:00+00:00"),
            )
        )
        assert list(bars["AAPL"].index.day) == [14, 15]
        assert client._request.await_args.kwargs["params"]["end"] == "2026-09-15T23:59:59-04:00"

    def test_default_keeps_current_session(self):
        client = object.__new__(AlpacaClient)
        client.data_url = "https://data.alpaca.markets"
        client._request = AsyncMock(return_value={"bars": {"AAPL": [_bar(15), _bar(16)]}})
        bars = asyncio.run(client.get_bars_multi(["AAPL"], "1Day", limit=2))
        assert len(bars["AAPL"]) == 2
        assert "end" not in client._request.await_args.kwargs["params"]

    def test_completed_only_rejects_intraday_timeframes(self):
        client = object.__new__(AlpacaClient)
        client.data_url = "https://data.alpaca.markets"
        client._request = AsyncMock()
        with pytest.raises(ValueError):
            asyncio.run(client.get_bars_multi(["AAPL"], "1Hour", completed_only=True))


class TestRegime:
    @staticmethod
    def _bars(closes):
        return pd.DataFrame({"close": closes})

    def test_uptrend_when_last_close_above_sma(self):
        assert market_uptrend(self._bars([100.0] * 49 + [110.0]), sma_length=50) is True

    def test_no_uptrend_when_last_close_below_sma(self):
        assert market_uptrend(self._bars([100.0] * 49 + [90.0]), sma_length=50) is False

    def test_undecidable_without_enough_history(self):
        assert market_uptrend(self._bars([100.0] * 10), sma_length=50) is None
        assert market_uptrend(None) is None
        assert market_uptrend(pd.DataFrame()) is None


class TestStopAnchoredToFill:
    def test_gap_up_keeps_atr_distance_below_fill(self):
        # signal: close 100, stop 96 (2*ATR = 4); market fills at 103
        assert Trader._anchor_stop(96.0, 100.0, 103.0) == 99.0

    def test_gap_down_keeps_stop_below_market(self):
        # unanchored, a 96 stop would sit above a 95 fill and be rejected
        assert Trader._anchor_stop(96.0, 100.0, 95.0) == 91.0

    def test_unusable_inputs_fall_back_to_signal_stop(self):
        assert Trader._anchor_stop(96.0, None, 103.0) == 96.0
        assert Trader._anchor_stop(None, 100.0, 103.0) is None
        assert Trader._anchor_stop(101.0, 100.0, 103.0) == 101.0


class TestClaudeCostControls:
    @staticmethod
    def _analyst():
        analyst = object.__new__(ClaudeAnalyst)
        analyst.db = None
        analyst.api_healthy = True
        analyst.last_error = None
        analyst._failure_count = 0
        analyst._paused_until = None
        analyst._decisions = {}
        return analyst

    def test_one_decision_per_symbol_per_session(self):
        analyst = self._analyst()
        analyst._analyze = AsyncMock(return_value=None)
        for _ in range(3):
            asyncio.run(analyst.analyze_for_signal("AAPL", {}, {}, [], {}))
        analyst._analyze.assert_awaited_once()

    def test_failed_call_is_not_cached(self):
        analyst = self._analyst()

        async def failing(*_args):
            await analyst._log_failure("haiku", "APIConnectionError: timeout")
            return None

        analyst._analyze = AsyncMock(side_effect=failing)
        asyncio.run(analyst.analyze_for_signal("AAPL", {}, {}, [], {}))
        asyncio.run(analyst.analyze_for_signal("AAPL", {}, {}, [], {}))
        assert analyst._analyze.await_count == 2

    def test_empty_credit_balance_pauses_calls(self):
        analyst = self._analyst()

        async def broke(*_args):
            await analyst._log_failure(
                "haiku", "BadRequestError: Your credit balance is too low to access the API"
            )
            return None

        analyst._analyze = AsyncMock(side_effect=broke)
        for symbol in ("AAPL", "MSFT", "NVDA"):
            asyncio.run(analyst.analyze_for_signal(symbol, {}, {}, [], {}))
        analyst._analyze.assert_awaited_once()
        assert analyst.api_healthy is False


class TestRegimeGateInLoop:
    """The gate blocks new longs only; exits must keep working."""

    @staticmethod
    def _run(action, spy_closes, positions):
        from unittest.mock import patch

        from storage.models import Action
        from tests.test_long_only import signal, stockpilot_for_signal

        sig = signal(Action[action])
        bot = stockpilot_for_signal(sig, positions)
        bot.regime_sma = 50
        bot._entries_allowed = True
        spy = pd.DataFrame({"close": spy_closes})
        bot.alpaca.get_bars_multi = AsyncMock(return_value={"SPY": spy})
        bot.position_sizer.calculate.return_value = type("S", (), {"shares": 5})()
        with (
            patch("main.get_universe", new=AsyncMock(return_value=["AAPL"])),
            patch("main.asyncio.sleep", new_callable=AsyncMock),
        ):
            asyncio.run(bot._trading_loop())
        return bot

    def test_downtrend_blocks_buy_and_notifies_once(self):
        bot = self._run("BUY", [100.0] * 49 + [90.0], [])
        bot.trader.execute.assert_not_awaited()
        bot.telegram.send.assert_awaited_once()
        assert "keine neuen Käufe" in bot.telegram.send.await_args.args[0]
        kwargs = bot.alpaca.get_bars_multi.await_args.kwargs
        assert kwargs["completed_only"] is True
        assert "SPY" in bot.alpaca.get_bars_multi.await_args.args[0]

    def test_downtrend_still_allows_exits(self):
        bot = self._run("SELL", [100.0] * 49 + [90.0], [{"symbol": "AAPL", "qty": "7"}])
        bot.trader.execute.assert_awaited_once()

    def test_uptrend_allows_buy(self):
        bot = self._run("BUY", [100.0] * 49 + [110.0], [])
        bot.trader.execute.assert_awaited_once()
        bot.telegram.send.assert_not_awaited()
