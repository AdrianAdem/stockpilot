from datetime import datetime

import structlog

from data.alpaca_client import AlpacaClient
from data.technical import _atr
from storage.db import Database

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

    async def _current_atr(self, symbol: str) -> float | None:
        """ATR(14) on daily bars — same period/timeframe as the backtest."""
        try:
            bars = await self.alpaca.get_bars(symbol, "1Day", limit=30)
            if bars.empty or len(bars) < 15:
                return None
            atr = _atr(bars["high"], bars["low"], bars["close"], 14)
            val = float(atr.iloc[-1])
            return val if val > 0 else None
        except Exception as e:
            logger.warning("atr_fetch_error", symbol=symbol, error=str(e))
            return None

    async def reconcile_stops(self, positions: list[dict]):
        """Ensure every open position has exactly ONE sell-stop covering its
        FULL current quantity. Fixes desync from multiple entries / partial fills
        where stop qty drifts below position qty, leaving shares unprotected."""
        trades = await self.db.get_open_trades()
        # Most protective (highest) recorded stop per symbol
        stop_by_symbol: dict[str, float] = {}
        for t in trades:
            if t.stop_loss:
                stop_by_symbol[t.symbol] = max(stop_by_symbol.get(t.symbol, 0), t.stop_loss)

        try:
            orders = await self.alpaca.get_orders()
        except Exception as e:
            logger.error("reconcile_orders_fetch_error", error=str(e))
            return

        # Count covered qty per symbol from existing sell-stops
        stop_qty: dict[str, int] = {}
        for o in orders:
            if o.get("type") == "stop" and o.get("side") == "sell":
                sym = o.get("symbol", "")
                stop_qty[sym] = stop_qty.get(sym, 0) + int(o.get("qty", 0))

        for pos in positions:
            symbol = pos.get("symbol", "")
            pos_qty = int(pos.get("qty", 0))
            if pos_qty <= 0:
                continue
            covered = stop_qty.get(symbol, 0)
            if covered == pos_qty:
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
                continue
            logger.warning(
                "stop_desync_fixed",
                symbol=symbol,
                pos_qty=pos_qty,
                covered_qty=covered,
                stop_price=stop_price,
            )
            await self._replace_stop(symbol, pos_qty, stop_price)

    async def _place_fresh_stop(self, symbol: str, qty: int, stop_price: float) -> bool:
        """Submit a new GTC sell-stop. Returns True on success."""
        try:
            await self.alpaca.submit_order(
                symbol=symbol,
                qty=qty,
                side="sell",
                order_type="stop",
                stop_price=round(stop_price, 2),
                time_in_force="gtc",
            )
            return True
        except Exception as e:
            logger.error(
                "stop_submit_failed",
                symbol=symbol,
                qty=qty,
                stop_price=round(stop_price, 2),
                error=str(e),
            )
            return False

    async def _replace_stop(self, symbol: str, qty: int, stop_price: float):
        """Ensure exactly one sell-stop for symbol at stop_price/qty.

        Prefers an atomic PATCH replace (no unprotected window), but Alpaca
        rejects a replace whenever the order is no longer in a replaceable
        state (422/403). In that case the old order is cancelled and a fresh
        stop submitted — leaving the position unprotected is never acceptable,
        so a failed replace must always fall through to cancel-and-recreate.
        """
        if qty <= 0:
            return
        try:
            orders = await self.alpaca.get_orders()
        except Exception as e:
            logger.error("stop_orders_fetch_error", symbol=symbol, error=str(e))
            return

        existing = [
            o
            for o in orders
            if o.get("symbol") == symbol and o.get("type") == "stop" and o.get("side") == "sell"
        ]

        if not existing:
            await self._place_fresh_stop(symbol, qty, stop_price)
            return

        # Try the atomic path first.
        try:
            await self.alpaca.replace_order(
                existing[0]["id"], stop_price=round(stop_price, 2), qty=qty
            )
            replaced = True
        except Exception as e:
            logger.warning("stop_replace_rejected_falling_back", symbol=symbol, error=str(e)[:120])
            replaced = False

        if not replaced:
            # Cancel every stale stop, then re-create. Verify the new order
            # actually lands — a silent failure here means no protection.
            for o in existing:
                try:
                    await self.alpaca.cancel_order(o["id"])
                except Exception as e:
                    logger.warning("stop_cancel_failed", symbol=symbol, error=str(e)[:120])
            if not await self._place_fresh_stop(symbol, qty, stop_price):
                logger.error("position_left_unprotected", symbol=symbol, qty=qty)
            return

        # Replace succeeded — clear any duplicate stops left over from a desync.
        for extra in existing[1:]:
            try:
                await self.alpaca.cancel_order(extra["id"])
            except Exception as e:
                logger.warning("stop_cancel_failed", symbol=symbol, error=str(e)[:120])

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
                await self._replace_stop(symbol, qty, new_stop)
                await self.db.update_stop_loss(symbol, new_stop)
                trade.stop_loss = new_stop  # avoid re-triggering this run

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
                await self._replace_stop(symbol, qty, new_stop)
                await self.db.update_stop_loss(symbol, new_stop)
                logger.info("stop_migrated", symbol=symbol, old_stop=cur_stop, new_stop=new_stop)
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
            remaining = qty - sell_qty
            try:
                await self.alpaca.submit_order(
                    symbol=symbol,
                    qty=sell_qty,
                    side="sell",
                    order_type="market",
                )
                logger.info(
                    "take_profit_partial", symbol=symbol, sold_qty=sell_qty, remaining=remaining
                )
                # Notify — partial sale keeps the symbol held, so the main-loop
                # close-sync can't see it; report it here.
                if self.telegram:
                    pnl = (current - trade.price) * sell_qty
                    pnl_pct = (current - trade.price) / trade.price * 100
                    await self.telegram.send(
                        f"\U0001f3af <b>TAKE-PROFIT (50%)</b>\n"
                        f"SOLD {sell_qty}x {symbol} @ ${current:.2f}\n"
                        f"PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%) | {remaining}x läuft weiter"
                    )
                # Resize stop to remaining qty and tighten to break-even.
                if remaining > 0:
                    await self._replace_stop(symbol, remaining, round(trade.price, 2))
            except Exception as e:
                logger.error("take_profit_error", symbol=symbol, error=str(e))

    async def evaluate_time_stops(self, positions: list[dict], claude=None):
        trades = await self.db.get_open_trades()

        for trade in trades:
            days_held = (datetime.utcnow() - trade.timestamp).days
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
                    # Cancel the standing stop first so it can't fire on the freed shares.
                    orders = await self.alpaca.get_orders()
                    for order in orders:
                        if (
                            order.get("symbol") == trade.symbol
                            and order.get("type") == "stop"
                            and order.get("side") == "sell"
                        ):
                            await self.alpaca.cancel_order(order["id"])

                    await self.alpaca.submit_order(
                        symbol=trade.symbol,
                        qty=qty,
                        side="sell",
                        order_type="market",
                    )
                    pnl = (current - entry) * qty
                    await self.db.close_trade(trade.order_id, current, pnl)
                    logger.info("time_stop_closed", symbol=trade.symbol, pnl=round(pnl, 2))
                    if self.telegram:
                        pnl_pct = (current - entry) / entry * 100 if entry else 0
                        await self.telegram.send(
                            f"⏱ <b>TIME-STOP</b>\n"
                            f"SOLD {qty}x {trade.symbol} @ ${current:.2f}\n"
                            f"PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%) | {days_held}d ohne +5%"
                        )
            except Exception as e:
                logger.error("time_stop_close_error", symbol=trade.symbol, error=str(e))
