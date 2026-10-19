"""Broker fill corrections must never rewrite completed trades."""

import asyncio
from datetime import UTC, datetime

from storage.db import Database
from storage.models import Side, TradeRecord


def test_correct_only_open_entry_by_exact_order_id(tmp_path):
    async def check():
        db = Database(tmp_path / "test.sqlite")
        await db.connect()
        try:
            entry = TradeRecord(
                symbol="AAPL",
                side=Side.BUY,
                qty=10,
                price=100,
                order_id="entry",
                strategy="test",
                signal_score=0.8,
            )
            await db.log_trade(entry)
            at = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
            await db.update_entry_fill("entry", 9, 101.25, at)
            corrected = (await db.get_open_trades())[0]
            assert (corrected.qty, corrected.price, corrected.timestamp) == (9, 101.25, at)
            await db.close_trade("entry", 102, 6.75)
            await db.update_entry_fill("entry", 1, 1, at)
            row = await (await db._db.execute("SELECT qty,price FROM trades")).fetchone()
            assert tuple(row) == (9, 101.25)
        finally:
            await db.close()

    asyncio.run(check())
