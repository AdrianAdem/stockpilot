import asyncio
import math
from datetime import datetime

import httpx
import structlog

from data.alpaca_client import AlpacaClient
from risk.portfolio_manager import PortfolioManager
from storage.db import Database
from storage.models import Action, Side, Signal, TradeRecord

logger = structlog.get_logger()


class Trader:
    """Executes long entries and signal-driven exits using confirmed fills."""

    def __init__(self, alpaca: AlpacaClient, db: Database, portfolio_manager: PortfolioManager):
        self.alpaca = alpaca
        self.db = db
        self.pm = portfolio_manager
        self.pending_orders: dict[str, Signal] = {}

    async def _load_pending_orders(self) -> None:
        # Other execution owners also journal orders in this table. Preserve local
        # entries whose writes failed while incorporating fresh persisted context.
        for order_id, signal in await self.db.get_pending_orders():
            self.pending_orders.setdefault(order_id, signal.model_copy(deep=True))

    async def _remember_pending(self, order_id: str, signal: Signal) -> None:
        self.pending_orders[order_id] = signal.model_copy(deep=True)
        await self.db.save_pending_order(order_id, self.pending_orders[order_id])

    async def _forget_pending(self, order_id: str) -> None:
        await self.db.delete_pending_order(order_id)
        self.pending_orders.pop(order_id, None)

    async def _submit_tracked(self, signal: Signal, **kwargs) -> dict:
        # Journal before POST: a lost acknowledgement must survive process restart.
        unknown_id = f"unknown:{signal.symbol}"
        await self._remember_pending(unknown_id, signal)
        try:
            order = await self.alpaca.submit_order(**kwargs)
        except httpx.HTTPStatusError as exc:
            # Request timeout does not prove that the broker rejected the order.
            if 400 <= exc.response.status_code < 500 and exc.response.status_code != 408:
                await self._forget_pending(unknown_id)
            raise
        if not order or not order.get("id"):
            raise RuntimeError("Broker submission outcome unknown; manual inspection required")
        await self._remember_pending(order["id"], signal)
        await self._forget_pending(unknown_id)
        return order

    @staticmethod
    def _trade_from_fill(order_id: str, signal: Signal, order: dict) -> TradeRecord:
        qty = float(order.get("filled_qty") or 0)
        price = float(order.get("filled_avg_price") or 0)
        filled_at = order.get("filled_at")
        if not math.isfinite(qty) or qty <= 0 or not qty.is_integer():
            raise RuntimeError(f"Order {order_id} has unsupported broker fill quantity")
        if not math.isfinite(price) or price <= 0 or not filled_at:
            raise RuntimeError(f"Order {order_id} has incomplete broker fill data")
        timestamp = datetime.fromisoformat(filled_at.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise RuntimeError(f"Order {order_id} has no broker timestamp timezone")
        return TradeRecord(
            symbol=signal.symbol,
            side=Side(signal.action.value),
            qty=int(qty),
            price=price,
            order_id=order_id,
            strategy=signal.strategy,
            signal_score=signal.score,
            stop_loss=signal.stop_loss_price,
            take_profit=signal.target_price,
            timestamp=timestamp,
        )

    async def reconcile_pending_orders(self) -> bool:
        """Cancel known remainders, persist terminal BUY fills, and report readiness.

        Each call makes one cancellation and one poll per known order. Unknown
        submissions require manual broker inspection; open-order absence is no proof.
        The owner must reconcile protective stops after this method succeeds.
        """
        try:
            await self._load_pending_orders()
        except Exception as exc:
            logger.error("pending_orders_load_failed", error=str(exc))
            return False
        for order_id, signal in list(self.pending_orders.items()):
            if order_id.startswith("unknown:"):
                logger.error("unknown_submission_manual_inspection", order_id=order_id)
                continue
            try:
                # Retry journal writes that may have failed after a broker response.
                await self.db.save_pending_order(order_id, signal)
                try:
                    await self.alpaca.cancel_order(order_id)
                except Exception as exc:
                    logger.warning("pending_cancel_failed", order_id=order_id, error=str(exc))
                order = await self.alpaca.get_order(order_id)
                if order.get("status") not in {"filled", "canceled", "expired", "rejected"}:
                    continue
                if order.get("id", order_id) != order_id:
                    raise RuntimeError("Broker returned a different order id")
                qty = float(order.get("filled_qty"))
                if not math.isfinite(qty) or qty < 0:
                    raise RuntimeError("Invalid broker fill quantity")
                if qty > 0 or order.get("status") == "filled":
                    trade = self._trade_from_fill(order_id, signal, order)
                    if signal.action.value == "BUY":
                        await self.db.log_trade(trade)
                await self._forget_pending(order_id)
            except Exception as exc:
                logger.error("pending_order_unresolved", order_id=order_id, error=str(exc))
        return not self.pending_orders

    async def _prepare_long_exit(self, symbol: str, requested_qty: int) -> int:
        """Release shares from protective stops, then re-read broker position."""
        orders = await self.alpaca.get_orders(status="open")
        if any(
            order.get("symbol") == symbol
            and order.get("side") == "sell"
            and order.get("type") != "stop"
            for order in orders
        ):
            raise RuntimeError(f"An exit order is already pending for {symbol}")
        stops = [
            order
            for order in orders
            if order.get("symbol") == symbol
            and order.get("side") == "sell"
            and order.get("type") == "stop"
        ]
        for order in stops:
            await self.alpaca.cancel_order(order["id"])
            for _ in range(30):
                canceled = await self.alpaca.get_order(order["id"])
                if canceled.get("status") in {"canceled", "filled", "expired", "rejected"}:
                    break
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError(f"Stop cancellation unresolved for {symbol}")

        remaining = await self.alpaca.get_orders(status="open")
        if any(
            order.get("symbol") == symbol
            and order.get("side") == "sell"
            and order.get("type") == "stop"
            for order in remaining
        ):
            raise RuntimeError(f"Protective stop cancellation not confirmed for {symbol}")

        position = await self.alpaca.get_position(symbol)
        held_qty = float(position.get("qty", 0)) if position else 0
        if held_qty <= 0:
            return 0
        if int(held_qty) != requested_qty:
            logger.warning(
                "sell_quantity_refreshed",
                symbol=symbol,
                requested=requested_qty,
                broker_qty=int(held_qty),
            )
        return int(held_qty)

    async def _wait_for_fill(self, order: dict) -> dict:
        """Return broker fill data; cancel an unusually slow market remainder."""
        order_id = order.get("id")
        if not order_id:
            raise RuntimeError("Broker order response has no id")
        current = order
        for _ in range(30):
            if current.get("status") == "filled":
                return current
            if current.get("status") in {"canceled", "expired", "rejected"}:
                if float(current.get("filled_qty") or 0) > 0:
                    return current
                raise RuntimeError(f"Order {order_id} ended as {current.get('status')}")
            await asyncio.sleep(0.5)
            current = await self.alpaca.get_order(order_id)

        # The final polling response must be inspected before canceling.
        if current.get("status") in {"filled", "canceled", "expired", "rejected"}:
            return current
        try:
            await self.alpaca.cancel_order(order_id)
        except Exception as exc:
            logger.warning("order_cancel_failed", order_id=order_id, error=str(exc))
        for _ in range(60):
            current = await self.alpaca.get_order(order_id)
            if current.get("status") == "filled":
                return current
            if current.get("status") in {"canceled", "expired", "rejected"}:
                if float(current.get("filled_qty") or 0) > 0:
                    return current
                raise TimeoutError(f"Order {order_id} was canceled without a fill")
            await asyncio.sleep(0.5)
        raise RuntimeError(f"Order {order_id} cancellation was not confirmed")

    async def execute(
        self,
        signal: Signal,
        qty: int,
        account: dict,
        positions: list[dict],
        current_price: float | None = None,
    ) -> TradeRecord | None:
        """Complete broker mutations even when the owner task is cancelled.

        A SIGTERM must not interrupt the interval between an entry fill and its
        protective stop, or between removing an old stop and submitting an exit.
        """
        operation = asyncio.create_task(
            self._execute(signal, qty, account, positions, current_price)
        )
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            logger.warning(
                "order_execution_cancellation_deferred",
                symbol=signal.symbol,
                side=signal.action.value,
            )
            await operation
            raise

    async def _execute(
        self,
        signal: Signal,
        qty: int,
        account: dict,
        positions: list[dict],
        current_price: float | None = None,
    ) -> TradeRecord | None:
        # Final safety checks
        try:
            await self._load_pending_orders()
        except Exception as exc:
            logger.error("pending_orders_load_failed", error=str(exc))
            return None
        if self.pending_orders:
            logger.warning("order_blocked", symbol=signal.symbol, reason="unresolved_submission")
            return None
        clock = await self.alpaca.get_market_clock()
        if not clock.get("is_open"):
            logger.warning("market_closed_skip_trade", symbol=signal.symbol)
            return None

        # BUY gates: no averaging-in + hard 5% per-position cap, then sector limit
        if signal.action.value == "BUY":
            orders = await self.alpaca.get_orders(status="open")
            if any(o.get("symbol") == signal.symbol for o in orders):
                logger.warning("order_blocked", symbol=signal.symbol, reason="pending_order")
                return None
            if not signal.stop_loss_price or signal.stop_loss_price <= 0:
                logger.warning("order_blocked", symbol=signal.symbol, reason="missing_stop")
                return None
            intended_value = qty * (current_price or signal.stop_loss_price or 0)
            if not self.pm.can_open_position(signal.symbol, positions, account, intended_value):
                return None
            if not self.pm.check_sector_limit(signal.symbol, positions, account):
                logger.warning("order_blocked", symbol=signal.symbol, reason="sector_limit")
                return None
        elif signal.action.value == "SELL":
            held_qty = sum(
                max(float(p.get("qty", 0)), 0)
                for p in positions
                if p.get("symbol") == signal.symbol
            )
            if held_qty <= 0:
                logger.warning("order_blocked", symbol=signal.symbol, reason="sell_without_long")
                return None
            qty = min(qty, int(held_qty))

        # Determine order type
        # Large cap liquid stocks get market orders
        order_type = "market"
        limit_price = None

        try:
            if signal.action.value == "BUY":
                # Manual exit management: market entry + a single GTC stop-loss.
                # StopLossManager owns ALL exits (trailing, 50% partial take-profit,
                # time stops). No bracket order — its attached TP leg would fight
                # the partial-TP logic and double-sell. Single exit owner.
                order = await self._submit_tracked(
                    signal,
                    symbol=signal.symbol,
                    qty=qty,
                    side="buy",
                    order_type=order_type,
                    limit_price=limit_price,
                )
                order = await self._wait_for_fill(order)
                trade = self._trade_from_fill(order["id"], self.pending_orders[order["id"]], order)
                qty = trade.qty
                await self.db.log_trade(trade)
                await self._forget_pending(order["id"])
                if signal.stop_loss_price:
                    stop_order = await self._submit_tracked(
                        signal.model_copy(update={"action": Action.SELL}),
                        symbol=signal.symbol,
                        qty=qty,
                        side="sell",
                        order_type="stop",
                        stop_price=signal.stop_loss_price,
                        time_in_force="gtc",
                    )
                    # Acknowledged protection is managed by StopLossManager.
                    await self._forget_pending(stop_order["id"])
                    logger.info(
                        "stop_loss_placed",
                        symbol=signal.symbol,
                        qty=qty,
                        stop_price=signal.stop_loss_price,
                        order_id=stop_order.get("id"),
                    )
                else:
                    logger.warning(
                        "no_stop_loss_for_buy",
                        symbol=signal.symbol,
                        reason="signal_had_no_stop_price",
                    )

            elif signal.action.value == "SELL":
                qty = await self._prepare_long_exit(signal.symbol, qty)
                if qty <= 0:
                    logger.info("sell_skipped_position_already_closed", symbol=signal.symbol)
                    return None
                order = await self._submit_tracked(
                    signal,
                    symbol=signal.symbol,
                    qty=qty,
                    side="sell",
                    order_type=order_type,
                )
                order = await self._wait_for_fill(order)
                trade = self._trade_from_fill(order["id"], self.pending_orders[order["id"]], order)
                qty = trade.qty
                await self._forget_pending(order["id"])
            else:
                return None

        except Exception as e:
            logger.error("order_execution_failed", symbol=signal.symbol, error=str(e))
            return None

        logger.info(
            "trade_executed",
            symbol=signal.symbol,
            side=signal.action.value,
            qty=qty,
            order_id=order.get("id"),
            strategy=signal.strategy,
            score=signal.score,
        )

        return trade
