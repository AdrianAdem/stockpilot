import math

import structlog

from config.settings import RiskConfig
from storage.models import Signal

logger = structlog.get_logger()


class PositionSize:
    def __init__(self, shares: int, value: float, pct_of_portfolio: float):
        self.shares = shares
        self.value = value
        self.pct_of_portfolio = pct_of_portfolio


class PositionSizer:
    """Risk-based position sizing.

    Every trade is sized so that being stopped out costs roughly the same
    fraction of equity (`risk_per_trade`), regardless of how far the stop sits.
    Flat percentage sizing gave a wide-stop name (ATR 9% of price -> stop ~20%
    away) the same weight as a quiet one, so it risked 3-4x more per trade;
    backtesting that change halved the worst single loss (-$506 -> -$267) and
    cut max drawdown (-5.5% -> -4.6%) at equal Sharpe.
    """

    MAX_SINGLE_PCT_CAP = 0.05  # absolute per-position ceiling (matches risk rule)
    MIN_SINGLE_PCT = 0.005  # floor, so a very wide stop still gets a real position

    def __init__(self, config: RiskConfig, risk_per_trade: float = 0.0025):
        self.config = config
        self.risk_per_trade = risk_per_trade

    def calculate(
        self,
        signal: Signal,
        account: dict,
        existing_positions: list[dict],
        current_price: float | None = None,
    ) -> PositionSize | None:
        equity = float(account.get("equity", 0))
        buying_power = float(account.get("buying_power", 0))

        if equity <= 0:
            return None

        price = current_price or signal.stop_loss_price
        if not price or price <= 0:
            return None

        # Existing exposure in THIS symbol — caps are on the TOTAL position,
        # not the single order, so subtract what we already hold (prevents
        # pyramiding the same name across loops past the 3%/5% limits).
        existing_value = 0.0
        for p in existing_positions:
            if p.get("symbol") == signal.symbol:
                existing_value += abs(float(p.get("market_value", 0)))

        # Risk-based sizing: pick the weight so that (price - stop) * shares
        # equals risk_per_trade of equity. A wide stop therefore gets a small
        # position and a tight stop a larger one, equalising risk per trade.
        stop = signal.stop_loss_price
        if stop and 0 < stop < price:
            risk_frac = (price - stop) / price  # stop distance as % of price
            target_pct = self.risk_per_trade / risk_frac
        else:
            # No usable stop -> fall back to the conservative base weight
            target_pct = self.config.max_position_pct
            logger.info("sizing_without_stop", symbol=signal.symbol, stop=stop)

        target_pct = max(self.MIN_SINGLE_PCT, min(target_pct, self.MAX_SINGLE_PCT_CAP))
        max_value = equity * target_pct - existing_value
        if max_value <= 0:
            logger.info(
                "position_cap_reached",
                symbol=signal.symbol,
                existing_value=round(existing_value, 2),
            )
            return None

        # Kelly criterion as upper bound
        if signal.stop_loss_price and signal.target_price and price:
            win_pct = signal.score  # use signal confidence as win probability
            risk = abs(price - signal.stop_loss_price) / price
            reward = abs(signal.target_price - price) / price
            if risk > 0 and reward > 0:
                b = reward / risk
                kelly = (win_pct * b - (1 - win_pct)) / b
                kelly = max(kelly, 0)
                kelly_value = equity * kelly
                max_value = min(max_value, kelly_value)

        # Check cash reserve (keep 20% cash)
        cash = float(account.get("cash", 0))
        min_cash = equity * (1 - self.config.max_portfolio_invested)
        available = cash - min_cash
        if available <= 0:
            logger.info("no_cash_available", cash=cash, min_cash=min_cash)
            return None
        max_value = min(max_value, available)

        # Check max single stock exposure (total position, minus existing)
        max_single = equity * 0.05 - existing_value
        max_value = min(max_value, max_single)
        if max_value <= 0:
            return None

        # Minimum position size
        if max_value < self.config.min_position_usd:
            logger.info(
                "position_too_small", value=max_value, min_required=self.config.min_position_usd
            )
            return None

        # Check buying power
        max_value = min(max_value, buying_power)

        # Round to whole shares
        shares = math.floor(max_value / price)
        if shares <= 0:
            return None

        value = shares * price
        pct = value / equity

        # Check max positions
        if len(existing_positions) >= self.config.max_positions:
            # Check if we already hold this stock
            held = any(p.get("symbol") == signal.symbol for p in existing_positions)
            if not held:
                logger.info(
                    "max_positions_reached",
                    current=len(existing_positions),
                    max=self.config.max_positions,
                )
                return None

        logger.info(
            "position_sized",
            symbol=signal.symbol,
            shares=shares,
            value=round(value, 2),
            pct=round(pct * 100, 1),
        )

        return PositionSize(shares=shares, value=round(value, 2), pct_of_portfolio=round(pct, 4))
