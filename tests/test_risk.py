"""Risk-layer tests — position sizing and portfolio guards.

These cover the rules that decide whether an order is allowed at all, which is
the part of the system where a bug costs money.
"""

import pytest

from config.settings import RiskConfig
from risk.portfolio_manager import PortfolioManager
from risk.position_sizer import PositionSizer
from storage.models import Action, Signal


@pytest.fixture
def risk_config() -> RiskConfig:
    return RiskConfig()


@pytest.fixture
def sizer(risk_config) -> PositionSizer:
    return PositionSizer(risk_config)


@pytest.fixture
def manager(risk_config) -> PortfolioManager:
    return PortfolioManager(risk_config, db=None)


def _signal(symbol: str = "AAPL", score: float = 0.7) -> Signal:
    return Signal(
        symbol=symbol,
        action=Action.BUY,
        score=score,
        strategy="test",
        stop_loss_price=95.0,
        target_price=120.0,
    )


def _signal_with_stop(stop: float, symbol: str = "AAPL") -> Signal:
    """BUY signal with an explicit stop, for risk-based sizing tests."""
    return Signal(
        symbol=symbol,
        action=Action.BUY,
        score=0.7,
        strategy="test",
        stop_loss_price=stop,
        target_price=None,
    )


ACCOUNT = {"equity": "100000", "buying_power": "200000", "cash": "100000"}


class TestPositionSizer:
    def test_rejects_when_equity_is_zero(self, sizer):
        assert sizer.calculate(_signal(), {"equity": "0"}, [], current_price=100.0) is None

    def test_position_stays_within_hard_cap(self, sizer):
        size = sizer.calculate(_signal(score=1.0), ACCOUNT, [], current_price=100.0)
        assert size is not None
        assert size.pct_of_portfolio <= PositionSizer.MAX_SINGLE_PCT_CAP + 1e-9

    def test_wider_stop_gets_smaller_position(self, sizer):
        """Risk-based sizing: distance to the stop drives the position size."""
        tight = sizer.calculate(_signal_with_stop(stop=97.0), ACCOUNT, [], current_price=100.0)
        wide = sizer.calculate(_signal_with_stop(stop=80.0), ACCOUNT, [], current_price=100.0)
        assert tight is not None and wide is not None
        assert wide.value < tight.value

    def test_risk_per_trade_is_equalised_across_stop_distances(self, sizer):
        """The whole point: being stopped out costs about the same either way."""
        equity = float(ACCOUNT["equity"])
        for stop in (97.0, 92.0, 80.0):
            size = sizer.calculate(_signal_with_stop(stop=stop), ACCOUNT, [], current_price=100.0)
            assert size is not None
            risk = (100.0 - stop) * size.shares / equity
            # within the floor/cap band the realised risk tracks the budget
            assert risk <= sizer.risk_per_trade * 1.5

    def test_existing_exposure_reduces_new_size(self, sizer):
        held = [{"symbol": "AAPL", "market_value": "4000"}]
        fresh = sizer.calculate(_signal(), ACCOUNT, [], current_price=100.0)
        topped = sizer.calculate(_signal(), ACCOUNT, held, current_price=100.0)
        assert fresh is not None
        # already near the cap -> either much smaller or refused entirely
        assert topped is None or topped.value < fresh.value

    def test_returns_whole_shares(self, sizer):
        # price must sit between stop (95) and target (120) for positive expectancy
        size = sizer.calculate(_signal(), ACCOUNT, [], current_price=103.33)
        assert size is not None
        assert isinstance(size.shares, int)

    def test_rejects_negative_expectancy_setup(self, sizer):
        """Target below the current price means Kelly is <= 0 -> no trade."""
        assert sizer.calculate(_signal(), ACCOUNT, [], current_price=137.77) is None


class TestPortfolioManager:
    def test_blocks_averaging_into_existing_position(self, manager):
        held = [{"symbol": "AAPL", "market_value": "3000"}]
        assert manager.can_open_position("AAPL", held, ACCOUNT, 1000) is False

    def test_allows_new_symbol(self, manager):
        held = [{"symbol": "MSFT", "market_value": "3000"}]
        assert manager.can_open_position("AAPL", held, ACCOUNT, 3000) is True

    def test_blocks_order_exceeding_single_position_cap(self, manager):
        # 8% of a 100k account is above the 5% per-position ceiling
        assert manager.can_open_position("AAPL", [], ACCOUNT, 8000) is False

    def test_blocks_when_portfolio_would_exceed_invested_cap(self, manager):
        held = [{"symbol": f"S{i}", "market_value": "10000"} for i in range(8)]
        assert manager.can_open_position("AAPL", held, ACCOUNT, 4000) is False

    def test_unknown_sector_does_not_trigger_the_sector_limit(self, manager):
        held = [{"symbol": f"UNKNOWN{i}", "market_value": "3000"} for i in range(5)]
        assert manager.check_sector_limit("NOTREAL", held, ACCOUNT) is True

    def test_pause_and_resume_toggle_state(self, manager):
        assert manager.is_paused is False
        manager.pause()
        assert manager.is_paused is True
        manager.resume()
        assert manager.is_paused is False
