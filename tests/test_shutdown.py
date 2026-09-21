"""Shutdown must report uncertainty rather than claim protection."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from main import StockPilot


@pytest.mark.parametrize("settled,covered", [(True, True), (True, False), (False, False)])
def test_shutdown_reports_verified_coverage_only(settled, covered):
    bot = object.__new__(StockPilot)
    bot._shutdown_started = False
    bot.order_manager = SimpleNamespace(cancel_entries=AsyncMock(return_value=True))
    bot.trader = SimpleNamespace(reconcile_pending_orders=AsyncMock(return_value=settled))
    bot.alpaca = SimpleNamespace(
        get_orders=AsyncMock(return_value=[]),
        get_positions=AsyncMock(return_value=[]),
        close=AsyncMock(),
    )
    bot.stop_manager = SimpleNamespace(reconcile_stops=AsyncMock(return_value=covered))
    bot.sync_closed_positions = AsyncMock()
    bot.telegram = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
    for name in ("sec", "news", "fred", "db"):
        setattr(bot, name, SimpleNamespace(close=AsyncMock()))
    with patch("main.asyncio.sleep", new_callable=AsyncMock):
        asyncio.run(bot.shutdown())
    message = bot.telegram.send.await_args.args[0]
    assert ("Stop-Abdeckung bestätigt" in message) == (settled and covered)
    assert ("STOP-SCHUTZ NICHT BESTÄTIGT" in message) == (not (settled and covered))
    if not settled:
        bot.stop_manager.reconcile_stops.assert_not_awaited()
    bot.alpaca.close.assert_awaited_once()
