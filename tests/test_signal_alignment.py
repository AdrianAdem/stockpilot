"""Live signal path must match what was backtested.

The backtest computes signals on completed daily bars, fills each signal
once at the next open with a stop at entry - 2*ATR, and applies the 0.65
threshold to the plain strategy score.
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from analysis.claude_analyst import ClaudeAnalyst
from data.alpaca_client import AlpacaClient
from execution.trader import Trader


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
        assert Trader._anchor_stop(101.0, 100.0, 103.0) == 101.0  # still below the fill


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


def _strategy(score: float, stop: float = 95.0):
    from types import SimpleNamespace

    from storage.models import Action, Signal

    sig = Signal(
        symbol="AAPL",
        action=Action.BUY,
        score=score,
        strategy="momentum",
        stop_loss_price=stop,
    )
    return SimpleNamespace(
        name="momentum", weight=1.0, generate_signals=AsyncMock(return_value=[sig])
    )


def _claude(answer):
    from types import SimpleNamespace

    return SimpleNamespace(api_healthy=True, analyze_for_signal=AsyncMock(return_value=answer))


class TestClaudeIsVetoOnly:
    """Claude may drop a setup; it must never move the score the threshold sees."""

    @staticmethod
    def _combine(score, claude, **kwargs):
        from analysis.signal_combiner import SignalCombiner

        combiner = SignalCombiner([_strategy(score)], claude, min_score=0.65)
        return asyncio.run(combiner.generate_combined_signals(["AAPL"], {"AAPL": {}}, **kwargs))

    def test_confirmation_keeps_strategy_score_and_stop(self):
        from storage.models import Action, Signal

        confirm = Signal(
            symbol="AAPL", action=Action.BUY, score=0.55, strategy="claude", stop_loss_price=50.0
        )
        [sig] = self._combine(0.66, _claude(confirm))
        assert sig.score == 0.66  # a lukewarm "yes" no longer drags it below 0.65
        assert sig.stop_loss_price == 95.0  # the strategy's backtested stop, not Claude's

    def test_skip_keeps_strategy_score(self):
        [sig] = self._combine(0.66, _claude(None))
        assert sig.score == 0.66

    def test_below_threshold_is_not_sent_to_claude(self):
        claude = _claude(None)
        assert self._combine(0.60, claude) == []
        claude.analyze_for_signal.assert_not_awaited()


class TestEntryTiming:
    """One fill attempt per signal, near the open — as in the backtest."""

    @staticmethod
    def _bot(window: int, tech: dict | None = None):
        from types import SimpleNamespace
        from unittest.mock import Mock

        from storage.models import Action
        from tests.test_long_only import signal, stockpilot_for_signal

        bot = stockpilot_for_signal(signal(Action.BUY), [])
        bot.entry_window_minutes = window
        bot._entry_attempts = {}
        bot.initial_stop_factor = 2.0
        bot.position_sizer.calculate.return_value = SimpleNamespace(shares=5)
        if tech is not None:
            bot.alpaca.get_bars_multi = AsyncMock(
                return_value={"AAPL": pd.DataFrame({"close": [1]})}
            )
            bot.technical = SimpleNamespace(calculate_all=Mock(return_value=tech))
        return bot

    @staticmethod
    def _scan(bot, ny_time: str):
        from unittest.mock import patch
        from zoneinfo import ZoneInfo

        fixed = datetime.fromisoformat(f"2026-10-19T{ny_time}").replace(
            tzinfo=ZoneInfo("America/New_York")
        )

        class FrozenClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

        with (
            patch("main.datetime", FrozenClock),
            patch("main.get_universe", new=AsyncMock(return_value=["AAPL"])),
            patch("main.asyncio.sleep", new_callable=AsyncMock),
        ):
            asyncio.run(bot._trading_loop())

    def test_entry_inside_window(self):
        bot = self._bot(window=60)
        self._scan(bot, "09:45:00")
        bot.trader.execute.assert_awaited_once()

    def test_no_entry_after_window(self):
        bot = self._bot(window=60)
        self._scan(bot, "11:00:00")
        bot.trader.execute.assert_not_awaited()

    def test_symbol_is_not_rebought_the_same_day(self):
        # e.g. stopped out between scans: the completed-bar signal is unchanged
        bot = self._bot(window=60)
        self._scan(bot, "09:35:00")
        self._scan(bot, "09:50:00")
        bot.trader.execute.assert_awaited_once()

    def test_initial_stop_is_two_atr_for_every_strategy(self):
        bot = self._bot(window=0, tech={"price": 100.0, "ATR": 3.0})
        self._scan(bot, "10:00:00")
        sent = bot.trader.execute.await_args.kwargs["signal"]
        assert sent.stop_loss_price == 94.0


class TestTraderStopGuards:
    def test_anchor_never_returns_a_stop_at_or_above_the_fill(self):
        assert Trader._anchor_stop(5.0, 100.0, 4.0) is None
        assert Trader._anchor_stop(5.0, None, 4.0) is None
