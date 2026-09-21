import asyncio
import math
import time
from datetime import UTC, datetime

import httpx
import structlog

from data.alpaca_client import AlpacaClient
from data.technical import _atr
from storage.db import Database
from storage.models import Action, Signal

logger = structlog.get_logger()


class StopLossManager:
    """Single owner of all position exits: continuous ATR trailing stop,
    partial take-profit, time stops. Entries place one standalone GTC stop
    (see Trader); this class ratchets it upward via ATR trailing.

    Trailing model: continuous ATR (backtest winner — Sharpe 1.38 vs 0.65 for
    the old fixed 2-stage). Once a position is profitable, the stop is pulled up
    to (current_price - trailing_factor * ATR), only ever upward.
    ATR = ATR(14) on daily bars — identical to the backtest.
    """

    def __init__(
        self,
        alpaca: AlpacaClient,
        db: Database,
        trailing_factor: float = 2.5,
        initial_stop_factor: float = 2.0,
        telegram=None,
    ):
        self.alpaca = alpaca
        self.db = db
        self.trailing_factor = trailing_factor
        self.initial_stop_factor = initial_stop_factor
        self.telegram = telegram
        self._last_protection_alert: dict[str, float] = {}
        self._exit_lock = asyncio.Lock()
        # None means submission may have reached the broker without an order ID.
        self._unresolved_exits: dict[str, str | None] = {}
        self._exit_context: dict[str, dict] = {}
        self._exit_stop_floors: dict[str, float] = {}

    async def _exit_unresolved(self, symbol: str) -> bool:
        if symbol not in self._unresolved_exits:
            return False
        order_id = self._unresolved_exits[symbol]
        if order_id:
            try:
                order = await self.alpaca.get_order(order_id)
                if self._terminal(order) and order.get("status") != "replaced":
                    if symbol in self._exit_context:
                        # A failed ID handoff may have left either durable marker.
                        await self.db.delete_pending_order(order_id)
                        await self.db.delete_pending_order(f"unknown:{symbol}")
                    context = self._exit_context.pop(symbol, None)
                    del self._unresolved_exits[symbol]
                    if context:
                        price = context["stop_price"]
                        qty = float(order.get("filled_qty") or 0)
                        fill_price = float(order.get("filled_avg_price") or 0)
                        if qty > 0:
                            price = max(price, context["filled_stop_price"] or 0)
                        self._exit_stop_floors[symbol] = max(
                            self._exit_stop_floors.get(symbol, 0), price
                        )
                        notification = context["notification"]
                        if qty > 0 and fill_price > 0 and notification and self.telegram:
                            title, entry, suffix = notification
                            pnl = (fill_price - entry) * qty
                            pnl_pct = (fill_price - entry) / entry * 100 if entry else 0
                            # Consume context before sending so later cycles cannot duplicate it.
                            await self.telegram.send(
                                f"{title}\nSOLD {qty:g}x {symbol} @ ${fill_price:.2f}\n"
                                f"PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%){suffix}"
                            )
                    return False
            except Exception as exc:
                logger.warning("exit_recovery_error", symbol=symbol, error=str(exc))
        return True

    @staticmethod
    def _terminal(order: dict) -> bool:
        return order.get("status") in {"filled", "canceled", "expired", "rejected", "replaced"}

    @classmethod
    def _pending_sell(cls, order: dict, symbol: str) -> bool:
        return (
            order.get("symbol") == symbol
            and order.get("side") == "sell"
            and order.get("type") != "stop"
            and not cls._terminal(order)
        )

    async def _wait_terminal(self, order_id: str) -> dict | None:
        for attempt in range(10):
            order = await self.alpaca.get_order(order_id)
            if self._terminal(order):
                return order
            if attempt < 9:
                await asyncio.sleep(0.3)
        return None

    async def _cancel_terminal(self, order_id: str) -> dict | None:
        try:
            await self.alpaca.cancel_order(order_id)
        except Exception as exc:
            # A fill can race cancellation; only the subsequent read decides.
            logger.warning("exit_cancel_error", order_id=order_id, error=str(exc))
        return await self._wait_terminal(order_id)

    async def _safe_exit(
        self,
        symbol: str,
        qty: int,
        stop_price: float,
        filled_stop_price: float | None = None,
        *,
        notification: tuple[str, float, str] | None = None,
    ) -> dict | None:
        async def mutate():
            async with self._exit_lock:
                return await self._exit_position(
                    symbol, qty, stop_price, filled_stop_price, notification
                )

        task = asyncio.create_task(mutate())
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
                if task.done():
                    result = task.result()
                    break
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _exit_position(
        self,
        symbol: str,
        qty: int,
        stop_price: float,
        filled_stop_price: float | None,
        notification: tuple[str, float, str] | None,
    ) -> dict | None:
        if symbol in self._unresolved_exits:
            await self._exit_unresolved(symbol)
            # Recovery consumes this cycle; the caller's quantity may predate the fill.
            return None
        if qty <= 0 or not (await self.alpaca.get_market_clock()).get("is_open"):
            return None
        orders = await self.alpaca.get_orders(status="open")
        if any(self._pending_sell(o, symbol) for o in orders):
            return None
        unresolved = False
        filled = None
        stop_price = max(stop_price, self._exit_stop_floors.get(symbol, 0))
        try:
            for order in orders:
                if (
                    order.get("symbol") == symbol
                    and order.get("side") == "sell"
                    and order.get("type") == "stop"
                    and not self._terminal(order)
                ):
                    stop_price = max(stop_price, float(order.get("stop_price") or 0))
                    unresolved = True
                    self._unresolved_exits[symbol] = order["id"]
                    terminal = await self._cancel_terminal(order["id"])
                    if not terminal:
                        return None
                    if terminal.get("status") == "replaced":
                        self._unresolved_exits[symbol] = None
                        return None
                    unresolved = False
                    self._unresolved_exits.pop(symbol, None)
            # Cancellation can race a stop fill, so never use the caller's holding.
            orders = await self.alpaca.get_orders(status="open")
            if any(
                o.get("symbol") == symbol and o.get("side") == "sell" and not self._terminal(o)
                for o in orders
            ):
                unresolved = True
                return None
            positions = await self.alpaca.get_positions()
            held = next((float(p["qty"]) for p in positions if p.get("symbol") == symbol), 0)
            sell_qty = min(qty, max(0, int(held)))
            if sell_qty <= 0:
                return None
            signal = Signal(
                symbol=symbol,
                action=Action.SELL,
                score=1,
                strategy="exit_recovery",
                stop_loss_price=stop_price,
            )
            unknown_id = f"unknown:{symbol}"
            # Commit intent before POST so a crash cannot hide a possible sale.
            await self.db.save_pending_order(unknown_id, signal)
            # A submission failure can be an ambiguous network acknowledgement.
            unresolved = True
            self._unresolved_exits[symbol] = None
            self._exit_context[symbol] = {
                "stop_price": stop_price,
                "filled_stop_price": filled_stop_price,
                "notification": notification,
            }
            try:
                order = await self.alpaca.submit_order(
                    symbol=symbol,
                    qty=sell_qty,
                    side="sell",
                    order_type="market",
                )
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                # Only a definite POST rejection releases the reservation. Timeouts,
                # conflicts and retry responses can conceal an accepted submission.
                if 400 <= status < 500 and status not in {408, 409, 425, 429}:
                    unresolved = False
                    self._unresolved_exits.pop(symbol, None)
                    self._exit_context.pop(symbol, None)
                    await self.db.delete_pending_order(unknown_id)
                raise
            order_id = order.get("id")
            if not order_id:
                return None
            self._unresolved_exits[symbol] = order_id
            # Persist the broker ID before removing the crash-safe unknown marker.
            await self.db.save_pending_order(order_id, signal)
            await self.db.delete_pending_order(unknown_id)
            terminal = await self._wait_terminal(order_id)
            if terminal is None:
                terminal = await self._cancel_terminal(order_id)
            if terminal is None:
                return None
            if terminal.get("status") == "replaced":
                self._unresolved_exits[symbol] = None
                return None
            await self.db.delete_pending_order(order_id)
            unresolved = False
            self._unresolved_exits.pop(symbol, None)
            self._exit_context.pop(symbol, None)
            if (
                float(terminal.get("filled_qty") or 0) > 0
                and float(terminal.get("filled_avg_price") or 0) > 0
            ):
                filled = terminal
            return filled
        except Exception as exc:
            logger.error("safe_exit_error", symbol=symbol, error=str(exc))
            return None
        finally:
            try:
                if unresolved:
                    await self._protection_alert(symbol)
                else:
                    orders = await self.alpaca.get_orders(status="open")
                    if any(self._pending_sell(o, symbol) for o in orders):
                        await self._protection_alert(symbol)
                    else:
                        positions = await self.alpaca.get_positions()
                        held = next(
                            (float(p["qty"]) for p in positions if p.get("symbol") == symbol), 0
                        )
                        price = max(stop_price, filled_stop_price or 0) if filled else stop_price
                        if held > 0 and not await self._replace_stop(symbol, held, price):
                            await self._protection_alert(symbol)
            except Exception as exc:
                logger.error("exit_protection_restore_error", symbol=symbol, error=str(exc))
                await self._protection_alert(symbol)

    @staticmethod
    def _is_live_stop(order: dict, symbol: str) -> bool:
        return (
            order.get("symbol") == symbol
            and order.get("side") == "sell"
            and order.get("type") == "stop"
            and order.get("time_in_force") == "gtc"
            and order.get("status") in {"new", "accepted"}
        )

    async def _protection_alert(self, symbol: str) -> None:
        logger.error("stop_protection_unverified", symbol=symbol)
        now = time.monotonic()
        if self.telegram and now - self._last_protection_alert.get(symbol, -3600) >= 3600:
            await self.telegram.send(
                f"⚠️ Stop-Schutz für {symbol} nicht bestätigt. "
                "Neue Käufe blockiert, bis alle Positionen abgesichert sind."
            )
            self._last_protection_alert[symbol] = now

    async def _verify_stop(self, order_id: str, symbol: str, qty: int, price: float) -> bool:
        """An HTTP success only acknowledges submission, not active protection."""
        for attempt in range(3):
            order = await self.alpaca.get_order(order_id)
            if order.get("status") in {"rejected", "canceled", "expired", "filled", "replaced"}:
                return False
            remaining = float(order.get("qty", 0)) - float(order.get("filled_qty", 0))
            if (
                self._is_live_stop(order, symbol)
                and remaining == qty
                and float(order.get("stop_price") or 0) >= price
            ):
                return True
            if attempt < 2:
                await asyncio.sleep(0.3)
        return False

    async def _current_atr(self, symbol: str) -> float | None:
        """ATR(14) on daily bars — same period/timeframe as the backtest."""
        try:
            bars = await self.alpaca.get_completed_daily_bars(symbol, limit=30)
            if bars.empty or len(bars) < 15:
                return None
            atr = _atr(bars["high"], bars["low"], bars["close"], 14)
            val = float(atr.iloc[-1])
            return val if val > 0 else None
        except Exception as e:
            logger.warning("atr_fetch_error", symbol=symbol, error=str(e))
            return None

    async def reconcile_stops(self, positions: list[dict]) -> bool:
        """Ensure every open position has exactly ONE sell-stop covering its
        FULL current quantity. Fixes desync from multiple entries / partial fills
        where stop qty drifts below position qty, leaving shares unprotected."""
        recovered = False
        for symbol in list(self._unresolved_exits):
            if not await self._exit_unresolved(symbol):
                recovered = True
        if recovered or self._exit_stop_floors:
            # The supplied snapshot can still contain shares sold by a late fill.
            positions = await self.alpaca.get_positions()
        held_symbols = {p.get("symbol") for p in positions if float(p.get("qty", 0)) > 0}
        for symbol in list(self._exit_stop_floors):
            if symbol not in held_symbols and symbol not in self._unresolved_exits:
                self._exit_stop_floors.pop(symbol, None)
        trades = await self.db.get_open_trades()
        # Most protective (highest) recorded stop per symbol
        stop_by_symbol: dict[str, float] = {}
        for t in trades:
            if t.stop_loss:
                stop_by_symbol[t.symbol] = max(stop_by_symbol.get(t.symbol, 0), t.stop_loss)
        for symbol, floor in self._exit_stop_floors.items():
            stop_by_symbol[symbol] = max(stop_by_symbol.get(symbol, 0), floor)

        try:
            orders = await self.alpaca.get_orders(status="open")
        except Exception as e:
            logger.error("reconcile_orders_fetch_error", error=str(e))
            await self._protection_alert("PORTFOLIO")
            return False

        # Count covered qty per symbol from existing sell-stops
        stop_qty: dict[str, int] = {}
        for o in orders:
            if self._is_live_stop(o, o.get("symbol", "")):
                sym = o.get("symbol", "")
                stop_qty[sym] = (
                    stop_qty.get(sym, 0) + float(o.get("qty", 0)) - float(o.get("filled_qty", 0))
                )

        verified = True
        for pos in positions:
            symbol = pos.get("symbol", "")
            pos_qty = int(pos.get("qty", 0))
            if pos_qty < 0:
                verified = False
                logger.error("unsupported_short_position", symbol=symbol, qty=pos_qty)
                await self._protection_alert(symbol)
                continue
            if pos_qty <= 0:
                continue
            if symbol in self._unresolved_exits or any(
                self._pending_sell(o, symbol) for o in orders
            ):
                verified = False
                await self._protection_alert(symbol)
                continue
            covered = stop_qty.get(symbol, 0)
            pending = [
                o
                for o in orders
                if o.get("symbol") == symbol
                and o.get("side") == "sell"
                and o.get("type") == "stop"
                and o.get("status")
                in {"pending_new", "pending_replace", "pending_cancel", "partially_filled"}
            ]
            if pending:
                verified = False
                await self._protection_alert(symbol)
                continue
            floor = self._exit_stop_floors.get(symbol, 0)
            if covered == pos_qty and all(
                float(o.get("stop_price") or 0) >= floor
                for o in orders
                if self._is_live_stop(o, symbol)
            ):
                self._last_protection_alert.pop(symbol, None)
                continue  # already fully and exactly covered

            # Desync: rebuild a single stop for full qty
            entry = float(pos.get("avg_entry_price", 0))
            current = float(pos.get("current_price", 0))
            stop_price = stop_by_symbol.get(symbol)
            if not stop_price or stop_price <= 0:
                # No recorded stop. Prefer entry, but the broker occasionally
                # reports avg_entry_price as 0 — fall back to the live price so
                # the position still gets protected instead of being skipped.
                basis = entry if entry > 0 else current
                stop_price = round(basis * 0.95, 2) if basis > 0 else None
            if not stop_price:
                logger.error("cannot_price_stop", symbol=symbol, qty=pos_qty)
                verified = False
                await self._protection_alert(symbol)
                continue
            if await self._replace_stop(symbol, pos_qty, stop_price):
                logger.info(
                    "stop_desync_fixed",
                    symbol=symbol,
                    pos_qty=pos_qty,
                    covered_qty=pos_qty,
                    stop_price=stop_price,
                )
                self._last_protection_alert.pop(symbol, None)
            else:
                verified = False
                await self._protection_alert(symbol)
        return verified

    async def _place_fresh_stop(self, symbol: str, qty: int, stop_price: float) -> bool:
        """Submit a new GTC sell-stop. Returns True on success."""
        try:
            order = await self.alpaca.submit_order(
                symbol=symbol,
                qty=qty,
                side="sell",
                order_type="stop",
                stop_price=round(stop_price, 2),
                time_in_force="gtc",
            )
            order_id = order.get("id")
            return bool(order_id) and await self._verify_stop(order_id, symbol, qty, stop_price)
        except Exception as e:
            logger.error(
                "stop_submit_failed",
                symbol=symbol,
                qty=qty,
                stop_price=round(stop_price, 2),
                error=str(e),
            )
            return False

    async def _replace_stop(self, symbol: str, qty: int, stop_price: float) -> bool:
        """Replace without deleting existing protection; confirm the resulting order.

        A failed or ambiguous PATCH is not permission to cancel a live stop.
        Leave it intact and let reconciliation re-read broker state next cycle.
        """
        if qty <= 0 or not math.isfinite(stop_price) or stop_price <= 0:
            return False
        if await self._exit_unresolved(symbol):
            return False
        stop_price = max(stop_price, self._exit_stop_floors.get(symbol, 0))
        try:
            orders = await self.alpaca.get_orders(status="open")
        except Exception as e:
            logger.error("stop_orders_fetch_error", symbol=symbol, error=str(e))
            return False

        existing = [
            o
            for o in orders
            if o.get("symbol") == symbol and o.get("type") == "stop" and o.get("side") == "sell"
        ]

        if any(self._pending_sell(o, symbol) for o in orders):
            return False

        if not existing:
            return await self._place_fresh_stop(symbol, qty, stop_price)

        if len(existing) != 1:
            logger.error("multiple_stops_require_reconciliation", symbol=symbol)
            return False

        # Never loosen a broker stop even when the local record is stale.
        stop_price = max(round(stop_price, 2), float(existing[0].get("stop_price") or 0))
        try:
            order = await self.alpaca.replace_order(
                existing[0]["id"], stop_price=round(stop_price, 2), qty=qty
            )
            order_id = order.get("id")
            confirmed = bool(order_id) and await self._verify_stop(
                order_id, symbol, qty, stop_price
            )
            if not confirmed:
                logger.error("stop_replace_unconfirmed", symbol=symbol)
            return confirmed
        except Exception as e:
            logger.error("stop_replace_failed_protection_preserved", symbol=symbol, error=str(e))
            return False

    async def update_trailing_stops(self, positions: list[dict]):
        """Continuous ATR trailing: once profitable, pull the stop up to
        current_price - trailing_factor*ATR. Upward only."""
        trades = await self.db.get_open_trades()
        trade_map = {t.symbol: t for t in trades}

        for pos in positions:
            symbol = pos.get("symbol", "")
            trade = trade_map.get(symbol)
            if not trade:
                continue

            entry = trade.price
            current = float(pos.get("current_price", 0))
            qty = int(pos.get("qty", 0))
            if current <= 0 or entry <= 0 or qty <= 0:
                continue

            # Only trail once the trade is in profit
            if current <= entry:
                continue

            atr = await self._current_atr(symbol)
            if not atr:
                continue

            new_stop = round(current - self.trailing_factor * atr, 2)
            cur_stop = trade.stop_loss or 0
            if new_stop > cur_stop:
                logger.info(
                    "trailing_stop_update",
                    symbol=symbol,
                    old_stop=cur_stop,
                    new_stop=new_stop,
                    price=current,
                    atr=round(atr, 2),
                    factor=self.trailing_factor,
                )
                if await self._replace_stop(symbol, qty, new_stop):
                    await self.db.update_stop_loss(symbol, new_stop)
                    trade.stop_loss = new_stop

    async def migrate_stops_to_atr(self, positions: list[dict]):
        """One-time on startup with the new exit logic: raise each open
        position's stop to the ATR trailing level if higher than the current
        stop (never lower). Logs old -> new per position."""
        trades = await self.db.get_open_trades()
        trade_map = {t.symbol: t for t in trades}
        logger.info("stop_migration_start", positions=len(positions))

        for pos in positions:
            symbol = pos.get("symbol", "")
            trade = trade_map.get(symbol)
            current = float(pos.get("current_price", 0))
            qty = int(pos.get("qty", 0))
            if not trade or current <= 0 or qty <= 0:
                continue
            cur_stop = trade.stop_loss or 0
            atr = await self._current_atr(symbol)
            if not atr:
                logger.warning("migration_no_atr", symbol=symbol)
                continue
            # Only trail up if profitable; otherwise keep initial stop.
            new_stop = (
                round(current - self.trailing_factor * atr, 2)
                if current > trade.price
                else cur_stop
            )
            if new_stop > cur_stop:
                if await self._replace_stop(symbol, qty, new_stop):
                    await self.db.update_stop_loss(symbol, new_stop)
                    logger.info(
                        "stop_migrated", symbol=symbol, old_stop=cur_stop, new_stop=new_stop
                    )
            else:
                logger.info("stop_kept", symbol=symbol, stop=cur_stop, would_be=new_stop)

    async def handle_take_profit(self, positions: list[dict]):
        trades = await self.db.get_open_trades()
        trade_map = {t.symbol: t for t in trades}

        for pos in positions:
            symbol = pos.get("symbol", "")
            trade = trade_map.get(symbol)
            if not trade or not trade.take_profit:
                continue

            current = float(pos.get("current_price", 0))
            qty = int(pos.get("qty", 0))
            if current < trade.take_profit or qty <= 0:
                continue

            # Partial TP fires once: skip if we already sold part of the entry
            # (current position smaller than the originally-recorded qty).
            if qty < trade.qty:
                continue

            sell_qty = max(qty // 2, 1)  # sell 50%
            try:
                fill = await self._safe_exit(
                    symbol,
                    sell_qty,
                    trade.stop_loss or round(trade.price * 0.95, 2),
                    filled_stop_price=round(trade.price, 2),
                    notification=("🎯 <b>TAKE-PROFIT (50%)</b>", trade.price, ""),
                )
                if not fill:
                    continue
                sell_qty = float(fill["filled_qty"])
                fill_price = float(fill["filled_avg_price"])
                logger.info(
                    "take_profit_partial", symbol=symbol, sold_qty=sell_qty, price=fill_price
                )
                # Notify — partial sale keeps the symbol held, so the main-loop
                # close-sync can't see it; report it here.
                if self.telegram:
                    pnl = (fill_price - trade.price) * sell_qty
                    pnl_pct = (fill_price - trade.price) / trade.price * 100
                    await self.telegram.send(
                        f"\U0001f3af <b>TAKE-PROFIT (50%)</b>\n"
                        f"SOLD {sell_qty:g}x {symbol} @ ${fill_price:.2f}\n"
                        f"PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%)"
                    )
            except Exception as e:
                logger.error("take_profit_error", symbol=symbol, error=str(e))

    async def evaluate_time_stops(self, positions: list[dict], claude=None):
        trades = await self.db.get_open_trades()

        for trade in trades:
            timestamp = trade.timestamp
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            days_held = (datetime.now(UTC) - timestamp).days
            if days_held < 10:
                continue

            pos = next((p for p in positions if p.get("symbol") == trade.symbol), None)
            if not pos:
                continue

            current = float(pos.get("current_price", 0))
            entry = trade.price
            if current <= 0 or entry <= 0:
                continue

            pnl_pct = (current - entry) / entry
            if pnl_pct >= 0.05:
                continue

            logger.info(
                "time_stop_triggered", symbol=trade.symbol, days=days_held, pnl_pct=f"{pnl_pct:.1%}"
            )

            # Claude re-evaluation: strong setup overrides the time stop
            if claude:
                try:
                    screen = await claude.quick_screen(trade.symbol, {"price": current})
                    if screen and screen.get("tradeable") and screen.get("score", 0) >= 7:
                        logger.info("time_stop_overridden_by_claude", symbol=trade.symbol)
                        continue
                except Exception as e:
                    logger.warning("time_stop_claude_error", error=str(e))

            try:
                qty = int(pos.get("qty", 0))
                if qty > 0:
                    fill = await self._safe_exit(
                        trade.symbol,
                        qty,
                        trade.stop_loss or round(entry * 0.95, 2),
                        notification=("⏱ <b>TIME-STOP</b>", entry, f" | {days_held}d ohne +5%"),
                    )
                    if not fill:
                        continue
                    qty = float(fill["filled_qty"])
                    fill_price = float(fill["filled_avg_price"])
                    pnl = (fill_price - entry) * qty
                    logger.info("time_stop_filled", symbol=trade.symbol, qty=qty, pnl=round(pnl, 2))
                    if self.telegram:
                        pnl_pct = (fill_price - entry) / entry * 100 if entry else 0
                        await self.telegram.send(
                            f"⏱ <b>TIME-STOP</b>\n"
                            f"SOLD {qty:g}x {trade.symbol} @ ${fill_price:.2f}\n"
                            f"PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%) | {days_held}d ohne +5%"
                        )
            except Exception as e:
                logger.error("time_stop_close_error", symbol=trade.symbol, error=str(e))
