"""Exit orders must settle before protection is rebuilt or fills are reported."""

import asyncio
from datetime import UTC, datetime, timedelta
from functools import wraps
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from risk.stop_loss import StopLossManager
from storage.models import Action


def run_async(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return run


def setup_manager():
    stop = {
        "id": "stop",
        "symbol": "AAPL",
        "side": "sell",
        "type": "stop",
        "status": "new",
        "stop_price": "95",
    }
    fill = {"id": "exit", "status": "filled", "filled_qty": "5", "filled_avg_price": "111"}
    position = {"symbol": "AAPL", "qty": "10", "current_price": "120"}
    broker = SimpleNamespace(
        get_market_clock=AsyncMock(return_value={"is_open": True}),
        get_orders=AsyncMock(side_effect=[[stop], [], []]),
        cancel_order=AsyncMock(),
        get_order=AsyncMock(side_effect=[{"status": "canceled"}, fill]),
        get_positions=AsyncMock(side_effect=[[position], [dict(position, qty="5")]]),
        submit_order=AsyncMock(return_value={"id": "exit"}),
    )
    trade = SimpleNamespace(
        symbol="AAPL",
        qty=10,
        price=100,
        stop_loss=95,
        take_profit=115,
        order_id="entry",
        timestamp=datetime.now(UTC) - timedelta(days=11),
    )
    db = SimpleNamespace(
        get_open_trades=AsyncMock(return_value=[trade]),
        close_trade=AsyncMock(),
        save_pending_order=AsyncMock(),
        get_pending_orders=AsyncMock(return_value=[]),
        delete_pending_order=AsyncMock(),
    )
    manager = StopLossManager(broker, db, telegram=SimpleNamespace(send=AsyncMock()))
    manager._replace_stop = AsyncMock(return_value=True)
    return manager, broker, position


@pytest.fixture(autouse=True)
def no_poll_delay(monkeypatch):
    monkeypatch.setattr("risk.stop_loss.asyncio.sleep", AsyncMock())


@run_async
async def test_take_profit_reports_fill_and_restores_fresh_quantity():
    manager, broker, pos = setup_manager()
    await manager.handle_take_profit([pos])
    broker.cancel_order.assert_awaited_once_with("stop")
    broker.submit_order.assert_awaited_once_with(
        symbol="AAPL", qty=5, side="sell", order_type="market"
    )
    manager._replace_stop.assert_awaited_once_with("AAPL", 5, 100)
    message = manager.telegram.send.call_args.args[0]
    assert "SOLD 5x AAPL @ $111.00" in message
    assert "PnL: $+55.00" in message
    manager.db.close_trade.assert_not_awaited()


@run_async
async def test_time_stop_clamps_to_holdings_and_leaves_database_for_sync():
    manager, broker, pos = setup_manager()
    pos["current_price"] = "101"
    broker.get_positions.side_effect = [[dict(pos, qty="3")], []]
    broker.get_order.side_effect = [
        {"status": "filled"},
        {"status": "filled", "filled_qty": "3", "filled_avg_price": "99"},
    ]
    await manager.evaluate_time_stops([pos])
    assert broker.submit_order.call_args.kwargs["qty"] == 3
    manager.db.close_trade.assert_not_awaited()
    manager._replace_stop.assert_not_awaited()
    assert "SOLD 3x AAPL @ $99.00" in manager.telegram.send.call_args.args[0]


@run_async
@pytest.mark.parametrize("status", ["rejected", "canceled", "expired"])
async def test_no_fill_does_not_notify(status):
    manager, broker, pos = setup_manager()
    broker.get_order.side_effect = [{"status": "canceled"}, {"status": status, "filled_qty": "0"}]
    broker.get_positions.side_effect = [[pos], [pos]]
    await manager.handle_take_profit([pos])
    manager.telegram.send.assert_not_awaited()
    manager._replace_stop.assert_awaited_once_with("AAPL", 10, 95)


@run_async
async def test_partial_fill_cancels_remainder_before_protection():
    manager, broker, pos = setup_manager()
    broker.get_order.side_effect = (
        [{"status": "canceled"}]
        + [{"status": "partially_filled", "filled_qty": "2"}] * 10
        + [{"status": "canceled", "filled_qty": "3", "filled_avg_price": "109"}]
    )
    broker.get_positions.side_effect = [[pos], [dict(pos, qty="7")]]
    await manager.handle_take_profit([pos])
    assert [call.args[0] for call in broker.cancel_order.call_args_list] == ["stop", "exit"]
    manager._replace_stop.assert_awaited_once_with("AAPL", 7, 100)
    assert "SOLD 3x AAPL @ $109.00" in manager.telegram.send.call_args.args[0]


@run_async
@pytest.mark.parametrize("pending_stop", [True, False])
async def test_unresolved_cancellation_never_adds_competing_order(pending_stop):
    manager, broker, pos = setup_manager()
    broker.get_order.side_effect = None
    broker.get_order.return_value = {"status": "pending_cancel"}
    if not pending_stop:
        broker.get_orders.side_effect = [[], []]
    assert await manager._safe_exit("AAPL", 5, 95) is None
    manager._replace_stop.assert_not_awaited()
    if pending_stop:
        broker.submit_order.assert_not_awaited()
    else:
        broker.cancel_order.assert_awaited_once_with("exit")


@run_async
async def test_existing_sell_blocks_exit_and_reconcile():
    manager, broker, pos = setup_manager()
    broker.get_orders.side_effect = None
    broker.get_orders.return_value = [
        {"symbol": "AAPL", "side": "sell", "type": "limit", "status": "new"}
    ]
    assert await manager._safe_exit("AAPL", 5, 95) is None
    assert not await manager.reconcile_stops([pos])
    broker.cancel_order.assert_not_awaited()
    broker.submit_order.assert_not_awaited()
    manager._replace_stop.assert_not_awaited()


@run_async
async def test_closed_market_does_not_mutate():
    manager, broker, _ = setup_manager()
    broker.get_market_clock.return_value = {"is_open": False}
    assert await manager._safe_exit("AAPL", 5, 95) is None
    broker.cancel_order.assert_not_awaited()
    broker.submit_order.assert_not_awaited()


@run_async
async def test_shutdown_waits_for_mutation_and_protection_even_with_repeated_cancel():
    manager, broker, _ = setup_manager()
    entered, release = asyncio.Event(), asyncio.Event()

    async def cancel(order_id):
        entered.set()
        await release.wait()

    broker.cancel_order.side_effect = cancel
    task = asyncio.create_task(manager._safe_exit("AAPL", 5, 95))
    await entered.wait()
    task.cancel()
    # Let the caller observe cancellation while the broker operation remains blocked.
    tick = asyncio.Event()
    asyncio.get_running_loop().call_soon(tick.set)
    await tick.wait()
    assert not task.done()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    broker.submit_order.assert_awaited_once()
    manager._replace_stop.assert_awaited_once_with("AAPL", 5, 95)


@run_async
async def test_ambiguous_submission_does_not_restore_competing_stop():
    manager, broker, _ = setup_manager()
    broker.submit_order.side_effect = TimeoutError("unknown broker outcome")
    assert await manager._safe_exit("AAPL", 5, 95) is None
    manager._replace_stop.assert_not_awaited()


@run_async
async def test_cancel_error_still_checks_actual_terminal_state():
    manager, broker, _ = setup_manager()
    broker.cancel_order.side_effect = RuntimeError("already filled or canceled")
    assert await manager._safe_exit("AAPL", 5, 95)
    broker.submit_order.assert_awaited_once()


@run_async
async def test_unknown_post_remains_blocked_when_open_orders_omit_it():
    manager, broker, pos = setup_manager()
    broker.submit_order.side_effect = TimeoutError("lost acknowledgement")
    await manager._safe_exit("AAPL", 5, 95)
    broker.get_orders.side_effect = None
    broker.get_orders.return_value = []
    assert "AAPL" in manager._unresolved_exits
    assert await manager._safe_exit("AAPL", 5, 95) is None
    assert not await manager.reconcile_stops([pos])
    broker.submit_order.assert_awaited_once()
    manager._replace_stop.assert_not_awaited()


@run_async
async def test_known_pending_exit_blocks_until_direct_terminal_confirmation():
    manager, broker, pos = setup_manager()
    broker.get_orders.side_effect = None
    broker.get_orders.return_value = []
    broker.get_order.side_effect = None
    broker.get_order.return_value = {"status": "pending_cancel"}
    await manager._safe_exit("AAPL", 5, 95)
    assert manager._unresolved_exits == {"AAPL": "exit"}
    assert not await manager.reconcile_stops([pos])
    assert await manager._safe_exit("AAPL", 5, 95) is None
    broker.submit_order.assert_awaited_once()
    manager._replace_stop.assert_not_awaited()
    broker.get_order.return_value = {"status": "canceled"}
    assert await manager.reconcile_stops([dict(pos, qty="5")])
    assert not manager._unresolved_exits
    manager._replace_stop.assert_awaited_once_with("AAPL", 5, 95)


@run_async
@pytest.mark.parametrize("status", [400, 403, 422])
async def test_definite_submit_rejection_restores_stop_and_releases_pending(status):
    manager, broker, pos = setup_manager()
    response = httpx.Response(status, request=httpx.Request("POST", "https://broker/orders"))
    broker.submit_order.side_effect = httpx.HTTPStatusError(
        "order rejected", request=response.request, response=response
    )
    broker.get_positions.side_effect = [[pos], [dict(pos, qty="8")]]
    await manager.handle_take_profit([pos])
    broker.cancel_order.assert_awaited_once_with("stop")
    manager._replace_stop.assert_awaited_once_with("AAPL", 8, 95)
    assert not manager._unresolved_exits
    assert not manager._exit_context
    manager.telegram.send.assert_not_awaited()
    manager.db.delete_pending_order.assert_awaited_once_with("unknown:AAPL")


@run_async
@pytest.mark.parametrize("status", [408, 409, 425, 429, 500])
async def test_ambiguous_http_submission_keeps_pending_reservation(status):
    manager, broker, _ = setup_manager()
    response = httpx.Response(status, request=httpx.Request("POST", "https://broker/orders"))
    broker.submit_order.side_effect = httpx.HTTPStatusError(
        "unknown outcome", request=response.request, response=response
    )
    assert await manager._safe_exit("AAPL", 5, 95) is None
    assert manager._unresolved_exits == {"AAPL": None}
    manager._replace_stop.assert_not_awaited()
    assert manager.db.save_pending_order.call_args.args[0] == "unknown:AAPL"
    manager.db.delete_pending_order.assert_not_awaited()


@run_async
@pytest.mark.parametrize("take_profit", [True, False])
async def test_late_partial_fill_notifies_once_and_restores_fresh_protection(take_profit):
    manager, broker, pos = setup_manager()
    broker.get_order.side_effect = [{"status": "canceled"}] + [
        {"status": "pending_cancel", "filled_qty": "2"}
    ] * 20
    if take_profit:
        await manager.handle_take_profit([pos])
    else:
        pos["current_price"] = "101"
        await manager.evaluate_time_stops([pos])
    assert manager._unresolved_exits == {"AAPL": "exit"}
    manager.telegram.send.reset_mock()
    broker.get_order.side_effect = None
    broker.get_order.return_value = {
        "status": "canceled",
        "filled_qty": "3",
        "filled_avg_price": "109",
    }
    broker.get_orders.side_effect = None
    # Even exact quantity coverage must be tightened after a late TP fill.
    broker.get_orders.return_value = [
        {
            "id": "restored",
            "symbol": "AAPL",
            "side": "sell",
            "type": "stop",
            "time_in_force": "gtc",
            "status": "new",
            "qty": "7",
            "stop_price": "94",
        }
    ]
    broker.get_positions.side_effect = None
    broker.get_positions.return_value = [dict(pos, qty="7")]
    assert await manager.reconcile_stops([pos])
    manager._replace_stop.assert_awaited_once_with("AAPL", 7, 100 if take_profit else 95)
    message = manager.telegram.send.call_args.args[0]
    assert "SOLD 3x AAPL @ $109.00" in message
    assert "PnL: $+27.00" in message
    assert ("TAKE-PROFIT" if take_profit else "TIME-STOP") in message
    assert not manager._unresolved_exits
    assert not manager._exit_context
    assert await manager.reconcile_stops([pos])
    manager.telegram.send.assert_awaited_once()
    broker.submit_order.assert_awaited_once()
    manager.db.close_trade.assert_not_awaited()


@run_async
async def test_late_full_fill_notifies_even_when_no_position_remains():
    manager, broker, pos = setup_manager()
    broker.get_order.side_effect = [{"status": "canceled"}] + [{"status": "new"}] * 20
    pos["current_price"] = "101"
    await manager.evaluate_time_stops([pos])
    manager.telegram.send.reset_mock()
    broker.get_order.side_effect = None
    broker.get_order.return_value = {
        "status": "filled",
        "filled_qty": "10",
        "filled_avg_price": "101",
    }
    broker.get_orders.side_effect = None
    broker.get_orders.return_value = []
    broker.get_positions.side_effect = None
    broker.get_positions.return_value = []
    assert await manager.reconcile_stops([])
    assert "SOLD 10x AAPL @ $101.00" in manager.telegram.send.call_args.args[0]
    assert await manager.reconcile_stops([])
    manager.telegram.send.assert_awaited_once()
    manager._replace_stop.assert_not_awaited()
    manager.db.close_trade.assert_not_awaited()


@run_async
async def test_durable_intent_precedes_post_and_id_handoff_precedes_fill_cleanup():
    manager, broker, _ = setup_manager()
    pending = {}
    events = []

    async def save(order_id, signal):
        pending[order_id] = signal
        events.append(("save", order_id))

    async def delete(order_id):
        if order_id == "unknown:AAPL":
            assert "exit" in pending
        else:
            assert broker.get_order.await_count == 2
        pending.pop(order_id, None)
        events.append(("delete", order_id))

    async def submit(**kwargs):
        assert list(pending) == ["unknown:AAPL"]
        signal = pending["unknown:AAPL"]
        assert signal.symbol == "AAPL"
        assert signal.action == Action.SELL
        assert signal.strategy == "exit_recovery"
        assert signal.score == 1
        assert signal.stop_loss_price == 95  # Preserve the higher broker stop.
        events.append(("post", "exit"))
        return {"id": "exit"}

    manager.db.save_pending_order.side_effect = save
    manager.db.delete_pending_order.side_effect = delete
    broker.submit_order.side_effect = submit
    assert await manager._safe_exit("AAPL", 5, 90)
    assert events == [
        ("save", "unknown:AAPL"),
        ("post", "exit"),
        ("save", "exit"),
        ("delete", "unknown:AAPL"),
        ("delete", "exit"),
    ]
    assert not pending


@run_async
async def test_unconfirmed_protective_cancel_is_not_persisted_as_exit():
    manager, broker, _ = setup_manager()
    broker.get_order.side_effect = None
    broker.get_order.return_value = {"status": "pending_cancel"}
    assert await manager._safe_exit("AAPL", 5, 95) is None
    manager.db.save_pending_order.assert_not_awaited()
    broker.get_order.return_value = {"status": "canceled"}
    assert not await manager._exit_unresolved("AAPL")
    manager.db.delete_pending_order.assert_not_awaited()


@run_async
async def test_failed_intent_write_prevents_post_and_restores_protection():
    manager, broker, _ = setup_manager()
    manager.db.save_pending_order.side_effect = RuntimeError("database unavailable")
    assert await manager._safe_exit("AAPL", 5, 95) is None
    broker.submit_order.assert_not_awaited()
    manager._replace_stop.assert_awaited_once_with("AAPL", 5, 95)
    assert not manager._unresolved_exits


@run_async
async def test_failed_id_handoff_retains_unknown_until_terminal_recovery():
    manager, broker, _ = setup_manager()
    pending = {}

    async def save(order_id, signal):
        if order_id == "exit":
            raise RuntimeError("ID write failed")
        pending[order_id] = signal

    async def delete(order_id):
        pending.pop(order_id, None)

    manager.db.save_pending_order.side_effect = save
    manager.db.delete_pending_order.side_effect = delete
    assert await manager._safe_exit("AAPL", 5, 95) is None
    assert list(pending) == ["unknown:AAPL"]
    assert manager._unresolved_exits == {"AAPL": "exit"}
    manager._replace_stop.assert_not_awaited()
    manager.db.delete_pending_order.assert_not_awaited()
    assert not await manager._exit_unresolved("AAPL")
    assert not pending
    assert not manager._unresolved_exits
