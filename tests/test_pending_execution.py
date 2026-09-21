"""Uncertain executions retain durable intent until broker evidence settles them."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from execution.trader import Trader
from storage.db import Database
from storage.models import Action, Signal


def setup_trader():
    saved = {}

    async def save(order_id, signal):
        saved[order_id] = signal.model_copy(deep=True)

    async def delete(order_id):
        saved.pop(order_id, None)

    db = SimpleNamespace(
        save_pending_order=AsyncMock(side_effect=save),
        get_pending_orders=AsyncMock(side_effect=lambda: list(saved.items())),
        delete_pending_order=AsyncMock(side_effect=delete),
        log_trade=AsyncMock(),
    )
    broker = SimpleNamespace(
        get_market_clock=AsyncMock(return_value={"is_open": True}),
        get_orders=AsyncMock(return_value=[]),
        submit_order=AsyncMock(return_value={"id": "entry", "status": "new"}),
        get_order=AsyncMock(return_value={"id": "entry", "status": "pending_cancel"}),
        cancel_order=AsyncMock(),
    )
    pm = SimpleNamespace(
        can_open_position=Mock(return_value=True), check_sector_limit=Mock(return_value=True)
    )
    signal = Signal(
        symbol="AAPL",
        action=Action.BUY,
        score=0.8,
        strategy="original",
        stop_loss_price=95,
        target_price=120,
    )
    return Trader(broker, db, pm), broker, db, saved, signal


def fill(**changes):
    return {
        "id": "entry",
        "status": "filled",
        "filled_qty": "3",
        "filled_avg_price": "101.25",
        "filled_at": "2026-09-16T15:30:01Z",
        **changes,
    }


@pytest.fixture(autouse=True)
def no_delays(monkeypatch):
    monkeypatch.setattr("execution.trader.asyncio.sleep", AsyncMock())


def test_happy_path_persists_actual_fill_before_stop():
    async def check():
        trader, broker, db, saved, signal = setup_trader()
        broker.submit_order.side_effect = [fill(), {"id": "stop"}]
        trade = await trader.execute(signal, 10, {}, [], 100)
        assert trade.qty == 3
        assert trade.price == 101.25
        assert trade.timestamp == datetime.fromisoformat("2026-09-16T15:30:01+00:00")
        db.log_trade.assert_awaited_once_with(trade)
        assert broker.submit_order.await_args_list[1].kwargs["qty"] == 3
        assert not saved and not trader.pending_orders

    asyncio.run(check())


def test_unresolved_cancel_is_bounded_and_restart_restores_original_signal():
    async def check():
        trader, broker, db, saved, signal = setup_trader()
        assert await trader.execute(signal, 10, {}, [], 100) is None
        assert broker.get_order.await_count == 90
        assert set(saved) == {"entry"}
        signal.strategy = "changed"
        signal.stop_loss_price = 1
        restored = Trader(broker, db, trader.pm)
        assert not await restored.reconcile_pending_orders()
        assert broker.get_order.await_count == 91
        assert await restored.execute(signal, 10, {}, [], 100) is None
        broker.submit_order.assert_awaited_once()
        broker.cancel_order.side_effect = RuntimeError("already filled")
        broker.get_order.return_value = fill(status="canceled")
        assert await restored.reconcile_pending_orders()
        trade = db.log_trade.await_args.args[0]
        assert (trade.strategy, trade.stop_loss, trade.qty, trade.price) == (
            "original",
            95,
            3,
            101.25,
        )
        assert not saved

    asyncio.run(check())


@pytest.mark.parametrize("outcome", [TimeoutError("lost response"), {}, None])
def test_unknown_submission_survives_restart_and_blocks_other_symbols(outcome):
    async def check():
        trader, broker, db, saved, signal = setup_trader()
        if isinstance(outcome, Exception):
            broker.submit_order.side_effect = outcome
        else:
            broker.submit_order.return_value = outcome
        assert await trader.execute(signal, 10, {}, [], 100) is None
        assert set(saved) == {"unknown:AAPL"}
        restored = Trader(broker, db, trader.pm)
        assert not await restored.reconcile_pending_orders()
        signal.symbol = "MSFT"
        assert await restored.execute(signal, 10, {}, [], 100) is None
        broker.submit_order.assert_awaited_once()
        broker.get_order.assert_not_awaited()
        broker.cancel_order.assert_not_awaited()

    asyncio.run(check())


@pytest.mark.parametrize(
    "status,blocked",
    [(400, False), (403, False), (422, False), (429, False), (408, True), (500, True)],
)
def test_http_rejection_is_distinct_from_ambiguous_outcome(status, blocked):
    async def check():
        trader, broker, _, saved, signal = setup_trader()
        response = httpx.Response(
            status, request=httpx.Request("POST", "https://broker.invalid/orders")
        )
        broker.submit_order.side_effect = httpx.HTTPStatusError(
            "failed", request=response.request, response=response
        )
        assert await trader.execute(signal, 10, {}, [], 100) is None
        assert bool(saved) == blocked
        assert bool(trader.pending_orders) == blocked

    asyncio.run(check())


@pytest.mark.parametrize(
    "changes",
    [
        {"filled_at": None},
        {"filled_avg_price": None},
        {"filled_qty": "1.5"},
        {"filled_qty": "nan"},
        {"filled_at": "2026-09-16T15:30:01"},
    ],
)
def test_incomplete_fill_never_guesses_or_releases(changes):
    async def check():
        trader, broker, db, saved, signal = setup_trader()
        broker.submit_order.return_value = fill(**changes)
        assert await trader.execute(signal, 10, {}, [], 100) is None
        broker.get_order.return_value = fill(**changes)
        assert not await trader.reconcile_pending_orders()
        assert set(saved) == {"entry"}
        db.log_trade.assert_not_awaited()

    asyncio.run(check())


def test_poll_error_then_database_error_retains_fill_until_persisted():
    async def check():
        trader, broker, db, saved, signal = setup_trader()
        broker.get_order.side_effect = TimeoutError("poll failed")
        assert await trader.execute(signal, 10, {}, [], 100) is None
        assert set(saved) == {"entry"}
        broker.get_order.side_effect = None
        broker.get_order.return_value = fill()
        db.log_trade.side_effect = RuntimeError("database unavailable")
        assert not await trader.reconcile_pending_orders()
        assert set(saved) == {"entry"}
        db.log_trade.side_effect = None
        assert await trader.reconcile_pending_orders()
        assert not saved

    asyncio.run(check())


def test_refresh_picks_up_external_pending_orders_and_cancel_precedes_poll():
    async def check():
        trader, broker, db, saved, signal = setup_trader()
        assert await trader.reconcile_pending_orders()
        await db.save_pending_order("entry", signal)
        events = []
        broker.cancel_order.side_effect = lambda _: events.append("cancel")

        async def poll(_):
            events.append("poll")
            return fill(status="canceled", filled_qty="0")

        broker.get_order.side_effect = poll
        assert await trader.reconcile_pending_orders()
        assert events == ["cancel", "poll"]
        assert not saved
        db.log_trade.assert_not_awaited()

    asyncio.run(check())


def test_last_poll_fill_is_checked_before_cancel():
    async def check():
        trader, broker, _, _, _ = setup_trader()
        broker.get_order.side_effect = [{"id": "entry", "status": "new"}] * 29 + [fill()]
        assert await trader._wait_for_fill({"id": "entry", "status": "new"}) == fill()
        broker.cancel_order.assert_not_awaited()

    asyncio.run(check())


def test_stop_post_lost_response_blocks_without_losing_persisted_entry():
    async def check():
        trader, broker, db, saved, signal = setup_trader()
        broker.submit_order.side_effect = [fill(), TimeoutError("stop acknowledgement lost")]
        assert await trader.execute(signal, 10, {}, [], 100) is None
        db.log_trade.assert_awaited_once()
        assert set(saved) == {"unknown:AAPL"}
        assert saved["unknown:AAPL"].action == Action.SELL
        assert not await trader.reconcile_pending_orders()

    asyncio.run(check())


def test_failed_intent_write_prevents_post_and_keeps_local_block():
    async def check():
        trader, broker, db, _, signal = setup_trader()
        db.save_pending_order.side_effect = RuntimeError("disk full")
        assert await trader.execute(signal, 10, {}, [], 100) is None
        assert await trader.execute(signal, 10, {}, [], 100) is None
        broker.submit_order.assert_not_awaited()
        assert not await trader.reconcile_pending_orders()

    asyncio.run(check())


def test_execute_refreshes_external_unknown_after_initial_empty_load():
    async def check():
        trader, broker, db, _, signal = setup_trader()
        assert await trader.reconcile_pending_orders()
        await db.save_pending_order("unknown:MSFT", signal.model_copy(update={"symbol": "MSFT"}))
        assert await trader.execute(signal, 10, {}, [], 100) is None
        broker.submit_order.assert_not_awaited()

    asyncio.run(check())


def test_cancel_failure_still_polls_terminal_fill():
    async def check():
        trader, broker, _, _, _ = setup_trader()
        broker.get_order.side_effect = [{"id": "entry", "status": "new"}] * 30 + [fill()]
        broker.cancel_order.side_effect = TimeoutError("cancel response lost")
        assert await trader._wait_for_fill({"id": "entry", "status": "new"}) == fill()
        broker.cancel_order.assert_awaited_once_with("entry")

    asyncio.run(check())


def test_real_database_restart_and_delete_retry_do_not_duplicate_fill(tmp_path):
    async def check():
        trader, broker, _, _, signal = setup_trader()
        db = Database(tmp_path / "pending.sqlite")
        await db.connect()
        await db.save_pending_order("entry", signal)
        await db.close()
        await db.connect()
        try:
            restored = Trader(broker, db, trader.pm)
            broker.get_order.return_value = fill()
            delete = db.delete_pending_order
            db.delete_pending_order = AsyncMock(side_effect=RuntimeError("delete failed"))
            assert not await restored.reconcile_pending_orders()
            assert len(await db.get_pending_orders()) == 1
            db.delete_pending_order = delete
            assert await restored.reconcile_pending_orders()
            trades = await db.get_open_trades()
            assert len(trades) == 1
            assert trades[0].strategy == "original"
            assert not await db.get_pending_orders()
        finally:
            await db.close()

    asyncio.run(check())
