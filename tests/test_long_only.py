"""Long-only contracts for signal confirmation, execution, and stop coverage."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from analysis.signal_combiner import SignalCombiner
from execution.trader import Trader
from main import StockPilot
from risk.stop_loss import StopLossManager
from storage.models import Action, Side, Signal


def signal(action: Action, symbol: str = "AAPL") -> Signal:
    return Signal(
        symbol=symbol,
        action=action,
        score=0.9,
        strategy="momentum",
        target_price=120.0,
        stop_loss_price=95.0,
        timeframe="swing",
        reasoning="test signal",
    )


def test_combiner_returns_signal_when_claude_action_matches_strategy():
    strategy = SimpleNamespace(
        name="momentum",
        weight=1.0,
        generate_signals=AsyncMock(return_value=[signal(Action.BUY)]),
    )
    claude = SimpleNamespace(
        api_healthy=True,
        analyze_for_signal=AsyncMock(return_value=signal(Action.BUY)),
    )

    result = asyncio.run(
        SignalCombiner([strategy], claude).generate_combined_signals(["AAPL"], {"AAPL": {}})
    )

    assert len(result) == 1
    assert result[0].action is Action.BUY


@pytest.mark.parametrize(
    ("strategy_action", "claude_action"),
    [
        (Action.BUY, Action.SELL),
        (Action.BUY, Action.HOLD),
        (Action.BUY, Action.SKIP),
        (Action.SELL, Action.BUY),
        (Action.SELL, Action.HOLD),
        (Action.SELL, Action.SKIP),
    ],
)
def test_combiner_discards_every_claude_action_mismatch(strategy_action, claude_action):
    strategy = SimpleNamespace(
        name="momentum",
        weight=1.0,
        generate_signals=AsyncMock(return_value=[signal(strategy_action)]),
    )
    claude = SimpleNamespace(
        api_healthy=True,
        analyze_for_signal=AsyncMock(return_value=signal(claude_action)),
    )

    result = asyncio.run(
        SignalCombiner([strategy], claude).generate_combined_signals(["AAPL"], {"AAPL": {}})
    )

    assert result == []


def stockpilot_for_signal(sig: Signal, positions: list[dict]) -> StockPilot:
    bot = object.__new__(StockPilot)
    bot.config = SimpleNamespace(
        extra_watchlist=[],
        strategy=SimpleNamespace(min_signal_score=0.65, scan_interval_seconds=0),
    )
    bot.alpaca = SimpleNamespace(
        get_market_clock=AsyncMock(return_value={"is_open": True}),
        get_account=AsyncMock(
            return_value={"equity": "100000", "cash": "100000", "buying_power": "200000"}
        ),
        get_positions=AsyncMock(return_value=positions),
        get_bars_multi=AsyncMock(return_value={}),
    )
    bot.db = SimpleNamespace(snapshot_portfolio=AsyncMock(), log_signal=AsyncMock())
    bot.stop_manager = SimpleNamespace(
        update_trailing_stops=AsyncMock(),
        handle_take_profit=AsyncMock(),
        evaluate_time_stops=AsyncMock(),
        reconcile_stops=AsyncMock(return_value=True),
    )
    bot.order_manager = SimpleNamespace(sync_orders=AsyncMock())
    bot.portfolio_manager = SimpleNamespace(can_trade=AsyncMock(return_value=True))
    bot.screener = SimpleNamespace(filter_universe=Mock(return_value=[sig.symbol]))
    bot.news = SimpleNamespace(update=AsyncMock())
    bot.fred = SimpleNamespace(update=AsyncMock())
    bot.claude = SimpleNamespace(api_healthy=True, last_error=None)
    bot.combiner = SimpleNamespace(generate_combined_signals=AsyncMock(return_value=[sig]))
    bot.position_sizer = SimpleNamespace(calculate=Mock())
    bot.trader = SimpleNamespace(
        execute=AsyncMock(return_value=None), reconcile_pending_orders=AsyncMock(return_value=True)
    )
    bot.telegram = SimpleNamespace(send=AsyncMock(), send_trade=AsyncMock())
    bot.sync_closed_positions = AsyncMock()
    bot._maybe_heartbeat = AsyncMock()
    return bot


def test_trading_loop_sells_entire_positive_long_without_position_sizer():
    sell = signal(Action.SELL)
    positions = [
        {"symbol": "MSFT", "qty": "3"},
        {"symbol": "AAPL", "qty": "7"},
    ]
    bot = stockpilot_for_signal(sell, positions)

    with (
        patch("main.get_universe", new=AsyncMock(return_value=["AAPL"])),
        patch("main.asyncio.sleep", new_callable=AsyncMock),
    ):
        asyncio.run(bot._trading_loop())

    bot.position_sizer.calculate.assert_not_called()
    bot.trader.execute.assert_awaited_once_with(
        signal=sell,
        qty=7,
        account={"equity": "100000", "cash": "100000", "buying_power": "200000"},
        positions=positions,
        current_price=None,
    )


def test_unresolved_execution_stops_cycle_before_next_signal_or_repair():
    sig = signal(Action.SELL)
    bot = stockpilot_for_signal(sig, [{"symbol": "AAPL", "qty": "7"}])
    bot.combiner.generate_combined_signals.return_value = [sig, sig]
    bot.trader.reconcile_pending_orders.side_effect = [True, True, True, False]
    with (
        patch("main.get_universe", new=AsyncMock(return_value=["AAPL"])),
        patch("main.asyncio.sleep", new_callable=AsyncMock),
    ):
        asyncio.run(bot._trading_loop())
    bot.trader.execute.assert_awaited_once()
    # One pre-scan repair only; no repair after the uncertain broker mutation.
    bot.stop_manager.reconcile_stops.assert_awaited_once()


@pytest.mark.parametrize(
    "positions",
    [
        [],
        [{"symbol": "MSFT", "qty": "4"}],
        [{"symbol": "AAPL", "qty": "0"}],
        [{"symbol": "AAPL", "qty": "-2"}],
    ],
)
def test_trading_loop_skips_sell_without_positive_long_position(positions):
    bot = stockpilot_for_signal(signal(Action.SELL), positions)

    with (
        patch("main.get_universe", new=AsyncMock(return_value=["AAPL"])),
        patch("main.asyncio.sleep", new_callable=AsyncMock),
    ):
        asyncio.run(bot._trading_loop())

    bot.position_sizer.calculate.assert_not_called()
    bot.trader.execute.assert_not_awaited()


def trader_setup():
    filled = {
        "id": "sell-1",
        "status": "filled",
        "filled_qty": "7",
        "filled_avg_price": "110.00",
        "filled_at": "2026-09-16T17:00:00Z",
    }
    broker = SimpleNamespace(
        get_market_clock=AsyncMock(return_value={"is_open": True}),
        get_orders=AsyncMock(side_effect=[[], []]),
        cancel_order=AsyncMock(),
        get_position=AsyncMock(return_value={"symbol": "AAPL", "qty": "7"}),
        get_order=AsyncMock(return_value=filled),
        submit_order=AsyncMock(return_value=filled),
    )
    db = SimpleNamespace(
        log_trade=AsyncMock(),
        get_pending_orders=AsyncMock(return_value=[]),
        save_pending_order=AsyncMock(),
        delete_pending_order=AsyncMock(),
    )
    portfolio_manager = SimpleNamespace(
        can_open_position=Mock(return_value=True),
        check_sector_limit=Mock(return_value=True),
    )
    return Trader(broker, db, portfolio_manager), broker, db


def test_trader_sells_existing_long_without_logging_new_open_trade():
    trader, broker, db = trader_setup()
    sell = signal(Action.SELL)

    trade = asyncio.run(
        trader.execute(
            sell,
            qty=7,
            account={"equity": "100000"},
            positions=[{"symbol": "AAPL", "qty": "7"}],
            current_price=110.0,
        )
    )

    assert trade is not None
    assert trade.side is Side.SELL
    broker.submit_order.assert_awaited_once_with(
        symbol="AAPL", qty=7, side="sell", order_type="market"
    )
    db.log_trade.assert_not_awaited()


def test_trader_cancels_and_confirms_stop_before_full_long_exit():
    trader, broker, _ = trader_setup()
    protective = {"id": "stop-1", "symbol": "AAPL", "side": "sell", "type": "stop"}
    broker.get_orders.side_effect = [[protective], []]

    trade = asyncio.run(
        trader.execute(
            signal(Action.SELL),
            qty=7,
            account={"equity": "100000"},
            positions=[{"symbol": "AAPL", "qty": "7"}],
            current_price=110.0,
        )
    )

    assert trade is not None
    broker.cancel_order.assert_awaited_once_with("stop-1")
    assert broker.get_orders.await_count == 2
    broker.get_position.assert_awaited_once_with("AAPL")


def test_trader_blocks_exit_when_stop_cancellation_is_unconfirmed():
    trader, broker, _ = trader_setup()
    protective = {"id": "stop-1", "symbol": "AAPL", "side": "sell", "type": "stop"}
    broker.get_orders.side_effect = [[protective], [protective]]

    trade = asyncio.run(
        trader.execute(
            signal(Action.SELL),
            qty=7,
            account={"equity": "100000"},
            positions=[{"symbol": "AAPL", "qty": "7"}],
            current_price=110.0,
        )
    )

    assert trade is None
    broker.submit_order.assert_not_awaited()


@pytest.mark.parametrize("action", [Action.BUY, Action.SELL])
def test_trader_blocks_duplicate_pending_order(action):
    trader, broker, _ = trader_setup()
    broker.get_orders.side_effect = None
    broker.get_orders.return_value = [
        {"id": "pending", "symbol": "AAPL", "side": "sell", "type": "market"}
    ]
    result = asyncio.run(
        trader.execute(
            signal(action),
            7,
            {"equity": "100000"},
            [{"symbol": "AAPL", "qty": "7"}],
            current_price=110,
        )
    )
    assert result is None
    broker.submit_order.assert_not_awaited()
    broker.cancel_order.assert_not_awaited()


@pytest.mark.parametrize(
    "positions",
    [
        [],
        [{"symbol": "MSFT", "qty": "7"}],
        [{"symbol": "AAPL", "qty": "0"}],
        [{"symbol": "AAPL", "qty": "-7"}],
    ],
)
def test_trader_blocks_sell_without_positive_long_position(positions):
    trader, broker, db = trader_setup()

    result = asyncio.run(
        trader.execute(
            signal(Action.SELL),
            qty=7,
            account={"equity": "100000"},
            positions=positions,
            current_price=110.0,
        )
    )

    assert result is None
    broker.submit_order.assert_not_awaited()
    db.log_trade.assert_not_awaited()


def test_reconcile_returns_false_and_alerts_for_negative_position_quantity():
    broker = SimpleNamespace(get_orders=AsyncMock(return_value=[]))
    db = SimpleNamespace(get_open_trades=AsyncMock(return_value=[]))
    manager = StopLossManager(broker, db)
    manager._protection_alert = AsyncMock()
    manager._replace_stop = AsyncMock()

    result = asyncio.run(
        manager.reconcile_stops(
            [
                {
                    "symbol": "AAPL",
                    "qty": "-3",
                    "avg_entry_price": "100.00",
                    "current_price": "95.00",
                }
            ]
        )
    )

    assert result is False
    manager._protection_alert.assert_awaited_once_with("AAPL")
    manager._replace_stop.assert_not_awaited()


def test_closed_short_uses_later_buy_fill_and_short_pnl_direction():
    bot = object.__new__(StockPilot)
    short = SimpleNamespace(
        symbol="CARR",
        side=Side.SELL,
        qty=89,
        price=55.54,
        order_id="short-entry",
        timestamp=datetime.fromisoformat("2026-09-15T18:03:27"),
    )
    bot.db = SimpleNamespace(
        get_open_trades=AsyncMock(return_value=[short]),
        close_trade=AsyncMock(),
    )
    bot.alpaca = SimpleNamespace(
        get_orders=AsyncMock(
            return_value=[
                {
                    "symbol": "CARR",
                    "side": "sell",
                    "status": "filled",
                    "filled_avg_price": "55.54",
                    "filled_qty": "89",
                    "filled_at": "2026-09-15T18:03:29Z",
                },
                {
                    "symbol": "CARR",
                    "side": "buy",
                    "status": "filled",
                    "filled_avg_price": "55.07",
                    "filled_qty": "89",
                    "filled_at": "2026-09-16T17:05:50Z",
                },
            ]
        )
    )

    asyncio.run(bot.sync_closed_positions([], notify=False))

    expected_pnl = (55.54 - 55.07) * 89
    args = bot.db.close_trade.await_args.args
    assert args[0] == "short-entry"
    assert args[1] == pytest.approx(55.07)
    assert args[2] == pytest.approx(expected_pnl)


@pytest.mark.parametrize("partial_status", ["filled", "canceled", "expired"])
def test_closed_long_aggregates_partial_and_final_exit_fills(partial_status):
    bot = object.__new__(StockPilot)
    trade = SimpleNamespace(
        symbol="AAPL",
        side=Side.BUY,
        qty=10,
        price=100.0,
        order_id="buy-entry",
        timestamp=datetime.fromisoformat("2026-09-15T14:00:00"),
    )
    bot.db = SimpleNamespace(
        get_open_trades=AsyncMock(return_value=[trade]), close_trade=AsyncMock()
    )
    bot.alpaca = SimpleNamespace(
        get_orders=AsyncMock(
            return_value=[
                {
                    "symbol": "AAPL",
                    "side": "sell",
                    "status": partial_status,
                    "filled_avg_price": "110",
                    "filled_qty": "5",
                    "filled_at": "2026-09-15T16:00:00Z",
                },
                {
                    "symbol": "AAPL",
                    "side": "sell",
                    "status": "filled",
                    "filled_avg_price": "90",
                    "filled_qty": "5",
                    "filled_at": "2026-09-16T16:00:00Z",
                },
            ]
        )
    )

    asyncio.run(bot.sync_closed_positions([], notify=False))

    bot.db.close_trade.assert_awaited_once_with("buy-entry", 100.0, 0.0)


def test_buy_trade_uses_confirmed_broker_fill_and_timestamp():
    trader, broker, db = trader_setup()
    filled = {
        "id": "buy-1",
        "status": "filled",
        "filled_qty": "7",
        "filled_avg_price": "101.25",
        "filled_at": "2026-09-16T15:30:01Z",
    }
    broker.submit_order.side_effect = [filled, {"id": "stop-1"}]

    trade = asyncio.run(
        trader.execute(
            signal(Action.BUY),
            qty=7,
            account={"equity": "100000"},
            positions=[],
            current_price=100.0,
        )
    )

    assert trade is not None
    assert trade.price == 101.25
    assert trade.timestamp.isoformat() == "2026-09-16T15:30:01+00:00"
    db.log_trade.assert_awaited_once()


def test_cancelled_buy_execution_finishes_protective_stop_before_propagating_cancel():
    trader, broker, db = trader_setup()
    filled = {
        "id": "buy-1",
        "status": "filled",
        "filled_qty": "7",
        "filled_avg_price": "101.25",
        "filled_at": "2026-09-16T15:30:01Z",
    }
    broker.submit_order.side_effect = [filled, {"id": "stop-1"}]

    async def cancel_between_fill_and_stop():
        fill_logged = asyncio.Event()
        continue_to_stop = asyncio.Event()

        async def hold_after_fill(_trade):
            fill_logged.set()
            await continue_to_stop.wait()

        db.log_trade.side_effect = hold_after_fill
        task = asyncio.create_task(
            trader.execute(
                signal(Action.BUY),
                qty=7,
                account={"equity": "100000"},
                positions=[],
                current_price=100.0,
            )
        )

        await fill_logged.wait()
        task.cancel()
        await asyncio.sleep(0)
        continue_to_stop.set()

        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_between_fill_and_stop())

    assert broker.submit_order.await_count == 2
    assert broker.submit_order.await_args_list[1].kwargs == {
        "symbol": "AAPL",
        "qty": 7,
        "side": "sell",
        "order_type": "stop",
        "stop_price": 95.0,
        "time_in_force": "gtc",
    }


def test_wait_for_fill_keeps_polling_after_cancel_until_terminal_status():
    trader, broker, _ = trader_setup()
    broker.get_order.side_effect = [
        *[{"id": "buy-1", "status": "new"} for _ in range(30)],
        {"id": "buy-1", "status": "pending_cancel"},
        {"id": "buy-1", "status": "canceled", "filled_qty": "0"},
    ]

    with (
        patch("execution.trader.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(TimeoutError, match="canceled without a fill"),
    ):
        asyncio.run(trader._wait_for_fill({"id": "buy-1", "status": "new"}))

    broker.cancel_order.assert_awaited_once_with("buy-1")
    assert broker.get_order.await_count == 32


def test_sync_closed_positions_accepts_aware_utc_trade_timestamp():
    bot = object.__new__(StockPilot)
    trade = SimpleNamespace(
        symbol="AAPL",
        side=Side.BUY,
        qty=2,
        price=100.0,
        order_id="buy-entry",
        timestamp=datetime.fromisoformat("2026-09-15T14:00:00+00:00"),
    )
    bot.db = SimpleNamespace(
        get_open_trades=AsyncMock(return_value=[trade]), close_trade=AsyncMock()
    )
    bot.alpaca = SimpleNamespace(
        get_orders=AsyncMock(
            return_value=[
                {
                    "symbol": "AAPL",
                    "side": "sell",
                    "status": "filled",
                    "filled_avg_price": "110",
                    "filled_qty": "2",
                    "filled_at": "2026-09-16T16:00:00Z",
                }
            ]
        )
    )

    asyncio.run(bot.sync_closed_positions([], notify=False))

    bot.db.close_trade.assert_awaited_once_with("buy-entry", 110.0, 20.0)


def test_time_stop_accepts_aware_utc_trade_timestamp():
    trade = SimpleNamespace(
        symbol="AAPL",
        qty=2,
        price=100.0,
        order_id="buy-entry",
        timestamp=datetime.fromisoformat("2020-01-01T00:00:00+00:00"),
        stop_loss=95.0,
    )
    broker = SimpleNamespace(
        get_orders=AsyncMock(return_value=[]),
        cancel_order=AsyncMock(),
        submit_order=AsyncMock(return_value={"id": "sell-1"}),
    )
    db = SimpleNamespace(get_open_trades=AsyncMock(return_value=[trade]), close_trade=AsyncMock())
    manager = StopLossManager(broker, db)

    manager._safe_exit = AsyncMock(
        return_value={
            "id": "sell-1",
            "filled_qty": "2",
            "filled_avg_price": "100.00",
        }
    )

    asyncio.run(
        manager.evaluate_time_stops([{"symbol": "AAPL", "qty": "2", "current_price": "100.00"}])
    )

    manager._safe_exit.assert_awaited_once()
    assert manager._safe_exit.await_args.args == ("AAPL", 2, 95.0)
    db.close_trade.assert_not_awaited()


def test_shutdown_request_is_idempotent_and_cancels_active_cycle_once():
    bot = object.__new__(StockPilot)
    bot.running = True
    bot._trading_task = Mock()
    bot._trading_task.done.return_value = False
    bot._shutdown_event = Mock()

    bot.request_shutdown()
    bot.request_shutdown()

    assert bot.running is False
    bot._shutdown_event.set.assert_called_once_with()
    bot._trading_task.cancel.assert_called_once_with()
