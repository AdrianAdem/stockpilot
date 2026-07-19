from datetime import datetime

import structlog

from config.settings import RiskConfig
from storage.db import Database

logger = structlog.get_logger()

SECTOR_MAP = {
    "AAPL": "Technology",
    "MSFT": "Technology",
    "GOOGL": "Technology",
    "AMZN": "Consumer Discretionary",
    "META": "Technology",
    "NVDA": "Technology",
    "TSLA": "Consumer Discretionary",
    "AMD": "Technology",
    "PLTR": "Technology",
    "COIN": "Financials",
    "JPM": "Financials",
    "BAC": "Financials",
    "JNJ": "Healthcare",
    "UNH": "Healthcare",
    "PFE": "Healthcare",
    "XOM": "Energy",
    "CVX": "Energy",
    "COP": "Energy",
    "PG": "Consumer Staples",
    "KO": "Consumer Staples",
    "PEP": "Consumer Staples",
}


class PortfolioManager:
    def __init__(self, config: RiskConfig, db: Database):
        self.config = config
        self.db = db
        self._daily_start_value: float | None = None
        self._daily_start_date: str | None = None
        self._weekly_start_value: float | None = None
        self._weekly_start_date: str | None = None
        self._paused = False

    def get_sector(self, symbol: str) -> str:
        # Real GICS sectors from the S&P fetch; fall back to the small static
        # map, then "Unknown". Avoids dumping every unmapped name into one
        # fake "Unknown" sector that trips the 3-per-sector limit.
        from config.universe import get_sector_for

        sector = get_sector_for(symbol)
        if sector and sector != "Unknown":
            return sector
        return SECTOR_MAP.get(symbol, "Unknown")

    async def can_trade(self, account: dict, positions: list[dict] | None = None) -> bool:
        if self._paused:
            logger.warning("trading_paused")
            return False

        equity = float(account.get("equity", 0))
        today = datetime.utcnow().strftime("%Y-%m-%d")

        # Track daily drawdown
        if self._daily_start_date != today:
            self._daily_start_value = equity
            self._daily_start_date = today

        if self._daily_start_value and self._daily_start_value > 0:
            daily_dd = (equity - self._daily_start_value) / self._daily_start_value
            if daily_dd < -self.config.daily_drawdown_limit:
                logger.warning("daily_drawdown_limit", drawdown=f"{daily_dd:.2%}")
                return False

        # Track weekly drawdown
        weekday = datetime.utcnow().weekday()
        if (weekday == 0 or self._weekly_start_value is None) and (
            self._weekly_start_date != today or self._weekly_start_value is None
        ):
            self._weekly_start_value = equity
            self._weekly_start_date = today

        if self._weekly_start_value and self._weekly_start_value > 0:
            weekly_dd = (equity - self._weekly_start_value) / self._weekly_start_value
            if weekly_dd < -self.config.weekly_drawdown_limit:
                logger.warning("weekly_drawdown_limit", drawdown=f"{weekly_dd:.2%}")
                self._paused = True
                return False

        return True

    # Hard per-position cap (test phase): no single name above 5% of equity.
    MAX_SINGLE_PCT = 0.05

    def can_open_position(
        self, symbol: str, positions: list[dict], account: dict, intended_value: float = 0.0
    ) -> bool:
        """Gate a BUY before execution.

        - Blocks if a position in this symbol already exists (no averaging-in
          during the test phase).
        - Hard-enforces the per-position cap: existing + intended exposure must
          not exceed 5% of equity, even across multiple buys.
        Every block is logged with its reason.
        """
        equity = float(account.get("equity", 0))
        if equity <= 0:
            logger.warning("order_blocked", symbol=symbol, reason="no_equity")
            return False

        existing_value = 0.0
        held = False
        for p in positions:
            if p.get("symbol") == symbol:
                held = True
                existing_value += abs(float(p.get("market_value", 0)))

        # 1. No adding to an existing position in the test phase
        if held:
            logger.warning(
                "order_blocked",
                symbol=symbol,
                reason="already_holding_no_averaging",
                existing_value=round(existing_value, 2),
            )
            return False

        # 2. Hard 5% cap on total resulting exposure
        projected_pct = (existing_value + intended_value) / equity
        if projected_pct > self.MAX_SINGLE_PCT:
            logger.warning(
                "order_blocked",
                symbol=symbol,
                reason="max_position_pct_exceeded",
                projected_pct=f"{projected_pct:.2%}",
                limit=f"{self.MAX_SINGLE_PCT:.0%}",
            )
            return False

        # 3. Hard portfolio-invested cap (default 80%)
        invested = sum(abs(float(p.get("market_value", 0))) for p in positions)
        projected_invested = (invested + intended_value) / equity
        if projected_invested > self.config.max_portfolio_invested:
            logger.warning(
                "order_blocked",
                symbol=symbol,
                reason="max_portfolio_invested_exceeded",
                projected=f"{projected_invested:.1%}",
                limit=f"{self.config.max_portfolio_invested:.0%}",
            )
            return False

        return True

    def check_sector_limit(self, symbol: str, positions: list[dict], account: dict) -> bool:
        sector = self.get_sector(symbol)
        equity = float(account.get("equity", 0))
        if equity <= 0:
            return False

        # "Unknown" is not a real sector — don't cluster unmapped names together.
        if sector == "Unknown":
            return True

        sector_value = 0.0
        sector_count = 0
        for p in positions:
            if self.get_sector(p.get("symbol", "")) == sector:
                sector_value += float(p.get("market_value", 0))
                sector_count += 1

        if sector_count >= 3:
            logger.info("sector_count_limit", sector=sector, count=sector_count)
            return False

        if equity > 0 and sector_value / equity > self.config.max_sector_pct:
            logger.info("sector_pct_limit", sector=sector, pct=f"{sector_value / equity:.1%}")
            return False

        return True

    def pause(self):
        self._paused = True
        logger.warning("trading_paused_manual")

    def resume(self):
        self._paused = False
        self._daily_start_value = None
        self._weekly_start_value = None
        logger.info("trading_resumed")

    @property
    def is_paused(self) -> bool:
        return self._paused
