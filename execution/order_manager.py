import asyncio
from datetime import datetime, timedelta

import structlog

from data.alpaca_client import AlpacaClient
from storage.db import Database

logger = structlog.get_logger()


class OrderManager:
    def __init__(self, alpaca: AlpacaClient, db: Database):
        self.alpaca = alpaca
        self.db = db
        self._order_timestamps: dict[str, datetime] = {}

    async def sync_orders(self):
        try:
            orders = await self.alpaca.get_orders(status="open")
            now = datetime.utcnow()

            for order in orders:
                order_id = order.get("id", "")
                created = order.get("created_at", "")
                symbol = order.get("symbol", "")
                filled_qty = int(order.get("filled_qty", 0))
                total_qty = int(order.get("qty", 0))

                if order_id not in self._order_timestamps:
                    self._order_timestamps[order_id] = now

                elapsed = (now - self._order_timestamps[order_id]).total_seconds()

                # Cancel if not filled after 5 minutes
                if elapsed > 300:
                    if filled_qty == 0:
                        logger.info("order_timeout_cancel", symbol=symbol,
                                    order_id=order_id, elapsed=elapsed)
                        await self.alpaca.cancel_order(order_id)
                        del self._order_timestamps[order_id]
                    elif filled_qty < total_qty * 0.5:
                        # Partial fill < 50%: cancel rest
                        logger.info("order_partial_cancel", symbol=symbol,
                                    filled=filled_qty, total=total_qty)
                        await self.alpaca.cancel_order(order_id)
                        del self._order_timestamps[order_id]

            # Clean up tracked orders that are no longer open
            open_ids = {o.get("id") for o in orders}
            stale = [oid for oid in self._order_timestamps if oid not in open_ids]
            for oid in stale:
                del self._order_timestamps[oid]

            logger.debug("orders_synced", open_count=len(orders))

        except Exception as e:
            logger.error("order_sync_error", error=str(e))

    async def cancel_all(self):
        try:
            await self.alpaca.cancel_all_orders()
            self._order_timestamps.clear()
            logger.info("all_orders_cancelled")
        except Exception as e:
            logger.error("cancel_all_error", error=str(e))

    async def get_filled_orders_since(self, since: datetime) -> list[dict]:
        try:
            orders = await self.alpaca.get_orders(status="closed")
            filled = []
            for o in orders:
                if o.get("status") == "filled":
                    filled_at = o.get("filled_at", "")
                    if filled_at and filled_at > since.isoformat():
                        filled.append(o)
            return filled
        except Exception as e:
            logger.error("get_filled_error", error=str(e))
            return []
