import structlog

from config.universe import get_universe, resolve_ticker
from data.sec_filings import TRACKED_FUNDS, SECFilingsClient
from storage.db import Database
from storage.models import WhaleHolding

logger = structlog.get_logger()


class WhaleTracker:
    """Turns raw 13F filings into a per-ticker institutional buy/sell consensus."""

    def __init__(self, sec_client: SECFilingsClient, db: Database):
        self.sec = sec_client
        self.db = db

    async def update_all(self):
        """Refresh holdings for every tracked fund and store them by ticker.

        Filings are diffed against the previous quarter, and issuer names are
        resolved to tradeable tickers; unresolvable names are skipped.
        """
        logger.info("whale_tracker_update_start")
        # Ensure the S&P fetch ran so the name->ticker map is populated —
        # off-hours calls this before the trading loop ever loads the universe.
        await get_universe()
        # Wipe stale rows (older runs stored company names instead of tickers)
        try:
            await self.db._db.execute("DELETE FROM whale_holdings")
            await self.db._db.commit()
        except Exception as e:
            logger.error("whale_clear_error", error=str(e))
        for cik, name in TRACKED_FUNDS.items():
            try:
                changes = await self.sec.compare_filings(cik)
                holdings = []
                matched = 0
                for c in changes:
                    # Resolve 13F issuer name -> tradeable ticker. Skip names not
                    # in our universe (we only act on tradeable tickers, and
                    # whale_follow queries by ticker).
                    ticker = resolve_ticker(c.get("name", ""))
                    if not ticker:
                        continue
                    matched += 1
                    holdings.append(
                        WhaleHolding(
                            fund_name=name,
                            cik=cik,
                            symbol=ticker,
                            shares=c.get("shares", 0),
                            value_usd=c.get("value", 0),
                            change_type=c["change_type"],
                            change_pct=c.get("change_pct"),
                            filing_date="latest",
                        )
                    )
                if holdings:
                    await self.db.save_whale_holdings(holdings)
                logger.info(
                    "whale_holdings_saved",
                    fund=name,
                    total_changes=len(changes),
                    matched_tickers=matched,
                )
            except Exception as e:
                logger.error("whale_tracker_error", fund=name, error=str(e))

    async def get_top_buys(self, min_funds: int = 2) -> list[dict]:
        consensus = {}
        for cik, name in TRACKED_FUNDS.items():
            try:
                changes = await self.sec.compare_filings(cik)
                for c in changes:
                    if c["change_type"] in ("NEW", "INCREASED"):
                        sym = c.get("name", "")
                        if sym not in consensus:
                            consensus[sym] = []
                        consensus[sym].append(
                            {
                                "fund": name,
                                "change_type": c["change_type"],
                                "change_pct": c.get("change_pct"),
                            }
                        )
            except Exception as e:
                logger.error("whale_top_buys_error", fund=name, error=str(e))

        top = [
            {"symbol": sym, "funds": funds, "fund_count": len(funds)}
            for sym, funds in consensus.items()
            if len(funds) >= min_funds
        ]
        return sorted(top, key=lambda x: x["fund_count"], reverse=True)
