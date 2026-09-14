"""Broker protection contracts; all I/O is mocked, with no client construction."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from execution.order_manager import OrderManager
from risk.stop_loss import StopLossManager


def order(**overrides):
    result = {
        "id": "stop-1", "symbol": "AAPL", "side": "sell", "type": "stop",
        "status": "new", "time_in_force": "gtc", "stop_price": "95.00",
        "qty": "10", "filled_qty": "0", "order_class": "simple", "legs": None,
        "created_at": "2026-09-14T09:00:00Z",
    }
    return result | overrides


POSITION = {
    "symbol": "AAPL", "qty": "10", "avg_entry_price": "100.00",
    "current_price": "110.00",
}


@pytest.fixture
def setup():
    broker = SimpleNamespace(
        get_orders=AsyncMock(return_value=[]),
        get_order=AsyncMock(return_value=order()),
        submit_order=AsyncMock(return_value=order()),
        replace_order=AsyncMock(return_value=order(id="replacement-1")),
        cancel_order=AsyncMock(), cancel_all_orders=AsyncMock(),
    )
    trade = SimpleNamespace(symbol="AAPL", price=100.0, stop_loss=95.0)
    db = SimpleNamespace(
        get_open_trades=AsyncMock(return_value=[trade]), update_stop_loss=AsyncMock(),
    )
    return StopLossManager(broker, db), broker, db, trade


def test_fresh_stop_succeeds_only_after_get_confirms_remaining_quantity(setup):
    manager, broker, _, _ = setup
    for status in ("accepted", "new"):
        broker.get_order.reset_mock()
        broker.get_order.return_value = order(status=status, qty="12", filled_qty="2")
        assert asyncio.run(manager._place_fresh_stop("AAPL", 10, 95.0)) is True
        broker.get_order.assert_awaited_once_with("stop-1")
    broker.submit_order.assert_awaited_with(
        symbol="AAPL", qty=10, side="sell", order_type="stop",
        stop_price=95.0, time_in_force="gtc",
    )


def test_fresh_stop_rejects_each_invalid_confirmation_field(setup):
    manager, broker, _, _ = setup
    invalid = [
        {"status": "pending_new"}, {"status": "rejected"}, {"status": "canceled"},
        {"status": "filled"}, {"status": "partially_filled"},
        {"symbol": "MSFT"}, {"side": "buy"},
        {"type": "limit"}, {"time_in_force": "day"}, {"stop_price": "94.99"},
        {"qty": "9"}, {"qty": "11"}, {"filled_qty": "1"},
    ]
    results = []
    for fields in invalid:
        broker.get_order.return_value = order(**fields)
        results.append((fields, asyncio.run(manager._place_fresh_stop("AAPL", 10, 95.0))))
    assert all(result is False for _, result in results), results


def test_fresh_stop_returns_false_when_confirmation_fetch_fails(setup):
    manager, broker, _, _ = setup
    broker.get_order.side_effect = RuntimeError("confirmation unavailable")
    assert asyncio.run(manager._place_fresh_stop("AAPL", 10, 95.0)) is False
    broker.cancel_order.assert_not_awaited()


def test_replace_patches_single_stop_without_lowering_broker_price(setup):
    manager, broker, _, _ = setup
    broker.get_orders.return_value = [order(stop_price="102.00")]
    broker.get_order.return_value = order(id="replacement-1", stop_price="102.00")
    assert asyncio.run(manager._replace_stop("AAPL", 10, 100.0)) is True
    assert broker.get_orders.await_args.kwargs.get("status", "open") == "open"
    broker.replace_order.assert_awaited_once_with("stop-1", stop_price=102.0, qty=10)
    broker.get_order.assert_awaited_with("replacement-1")
    broker.cancel_order.assert_not_awaited()
    broker.submit_order.assert_not_awaited()


def test_replace_failure_preserves_existing_stop_without_cancel_fallback(setup):
    manager, broker, _, _ = setup
    broker.get_orders.return_value = [order()]
    broker.replace_order.side_effect = RuntimeError("422 replacement rejected")
    result = asyncio.run(manager._replace_stop("AAPL", 10, 100.0))
    broker.cancel_order.assert_not_awaited()
    broker.submit_order.assert_not_awaited()
    assert result is False


def test_replace_returns_false_when_patch_is_not_confirmed(setup):
    manager, broker, _, _ = setup
    broker.get_orders.return_value = [order()]
    broker.get_order.return_value = order(id="replacement-1", status="rejected")
    assert asyncio.run(manager._replace_stop("AAPL", 10, 100.0)) is False
    broker.get_order.assert_awaited_with("replacement-1")
    broker.cancel_order.assert_not_awaited()


def test_reconcile_returns_true_for_correct_stops_and_empty_positions(setup):
    manager, broker, _, _ = setup
    broker.get_orders.return_value = [order()]
    manager._replace_stop = AsyncMock(return_value=False)
    assert asyncio.run(manager.reconcile_stops([POSITION])) is True
    assert asyncio.run(manager.reconcile_stops([])) is True
    manager._replace_stop.assert_not_awaited()
    assert broker.get_orders.await_args.kwargs.get("status", "open") == "open"


def test_reconcile_returns_false_for_unconfirmed_quantity_or_status_repairs(setup):
    manager, broker, _, _ = setup
    manager._replace_stop = AsyncMock(return_value=False)
    results = []
    for fields in ({"filled_qty": "2"}, {"qty": "9"}, {"status": "canceled"}):
        broker.get_orders.return_value = [order(**fields)]
        manager._replace_stop.reset_mock()
        result = asyncio.run(manager.reconcile_stops([POSITION]))
        results.append((fields, result, manager._replace_stop.await_count))
    assert all(result is False and count > 0 for _, result, count in results), results


def test_trailing_persists_only_confirmed_stop_price(setup):
    manager, _, db, trade = setup
    manager._current_atr = AsyncMock(return_value=2.0)
    manager._replace_stop = AsyncMock(return_value=False)
    asyncio.run(manager.update_trailing_stops([POSITION]))
    manager._replace_stop.assert_awaited_once_with("AAPL", 10, 105.0)
    db.update_stop_loss.assert_not_awaited()
    assert trade.stop_loss == 95.0
    manager._replace_stop.return_value = True
    asyncio.run(manager.update_trailing_stops([POSITION]))
    db.update_stop_loss.assert_awaited_once_with("AAPL", 105.0)


def test_migration_persists_only_confirmed_stop_price(setup):
    manager, _, db, _ = setup
    manager._current_atr = AsyncMock(return_value=2.0)
    manager._replace_stop = AsyncMock(return_value=False)
    asyncio.run(manager.migrate_stops_to_atr([POSITION]))
    manager._replace_stop.assert_awaited_once_with("AAPL", 10, 105.0)
    db.update_stop_loss.assert_not_awaited()
    manager._replace_stop.return_value = True
    asyncio.run(manager.migrate_stops_to_atr([POSITION]))
    db.update_stop_loss.assert_awaited_once_with("AAPL", 105.0)


def mixed_orders():
    return [
        order(id="entry-market", side="buy", type="market"),
        order(id="entry-limit", side="buy", type="limit", filled_qty="2"),
        order(id="protective-stop"), order(id="sell-limit", type="limit"),
        order(id="buy-stop", side="buy"),
        *[order(id=kind, side="buy", type="limit", order_class=kind)
          for kind in ("bracket", "oco", "oto")],
        order(id="with-legs", side="buy", type="limit", legs=[order(id="leg")]),
    ]


def test_sync_cancels_only_expired_simple_buys_without_legs(setup):
    _, broker, db, _ = setup
    manager = OrderManager(broker, db)
    broker.get_orders.return_value = mixed_orders() + [
        order(id="recent", side="buy", type="limit"),
        order(id="half-filled", side="buy", type="limit", filled_qty="5"),
    ]
    manager._order_timestamps = {
        item["id"]: datetime.utcnow() - timedelta(minutes=6)
        for item in broker.get_orders.return_value if item["id"] != "recent"
    }
    asyncio.run(manager.sync_orders())
    assert broker.cancel_order.await_args_list == [call("entry-market"), call("entry-limit")]
    broker.get_orders.assert_awaited_once_with(status="open")
    broker.cancel_all_orders.assert_not_awaited()


def test_shutdown_cancels_only_simple_buys_without_legs(setup):
    _, broker, db, _ = setup
    broker.get_orders.side_effect = [mixed_orders(), [order()]]
    assert asyncio.run(OrderManager(broker, db).cancel_entries()) is True
    assert broker.cancel_order.await_args_list == [call("entry-market"), call("entry-limit")]
    assert broker.get_orders.await_args_list == [call(status="open"), call(status="open")]
    broker.cancel_all_orders.assert_not_awaited()


def test_pending_duplicate_stop_blocks_entries(setup):
    manager, broker, _, _ = setup
    broker.get_orders.return_value = [order(), order(id="pending", status="pending_new")]
    assert asyncio.run(manager.reconcile_stops([POSITION])) is False
    broker.submit_order.assert_not_awaited()
    broker.cancel_order.assert_not_awaited()


def test_shutdown_continues_after_cancel_failure_and_reports_unconfirmed(setup):
    _, broker, db, _ = setup
    entries = [order(id="one", side="buy", type="market"),
               order(id="two", side="buy", type="limit")]
    broker.get_orders.side_effect = [entries, [entries[0]]]
    broker.cancel_order.side_effect = [RuntimeError("failed"), None]
    assert asyncio.run(OrderManager(broker, db).cancel_entries()) is False
    assert broker.cancel_order.await_args_list == [call("one"), call("two")]
