import asyncio
import os
import signal
import sys
from datetime import UTC, datetime

import structlog

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.ConsoleRenderer()
        if sys.stdout.isatty()
        else structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(20),
)

from analysis.claude_analyst import ClaudeAnalyst
from analysis.screener import Screener
from analysis.signal_combiner import SignalCombiner
from analysis.whale_tracker import WhaleTracker
from config.settings import load_config
from config.universe import get_universe
from data.alpaca_client import AlpacaClient
from data.fred import FredClient
from data.news import NewsAggregator
from data.sec_filings import SECFilingsClient
from data.technical import TechnicalAnalysis
from execution.order_manager import OrderManager
from execution.telegram import TelegramCommandHandler, TelegramNotifier
from execution.trader import Trader
from risk.portfolio_manager import PortfolioManager
from risk.position_sizer import PositionSizer
from risk.stop_loss import StopLossManager
from storage.db import Database
from strategy.mean_reversion import MeanReversionStrategy
from strategy.momentum import MomentumStrategy
from strategy.whale_follow import WhaleFollowStrategy

logger = structlog.get_logger()


class StockPilot:
    def __init__(self):
        self.config = load_config()
        self.running = True
        self._shutdown_started = False
        self._trading_task: asyncio.Task | None = None
        self._shutdown_event = asyncio.Event()

    async def start(self):
        logger.info("stockpilot_starting")

        # Initialize components
        self.db = Database()
        await self.db.connect()

        self.alpaca = AlpacaClient(self.config.alpaca)
        account = await self.alpaca.verify_paper_account()
        logger.info(
            "paper_account_confirmed",
            equity=account.get("equity"),
            buying_power=account.get("buying_power"),
        )

        self.sec = SECFilingsClient(self.config.sec_user_agent)
        self.technical = TechnicalAnalysis()
        self.news = NewsAggregator()
        self.fred = FredClient(self.config.fred_api_key)
        self.claude = ClaudeAnalyst(self.config.anthropic.api_key, self.db)
        self.screener = Screener(
            min_volume=int(os.getenv("SCREENER_MIN_VOLUME", "200000")),
            min_price=float(os.getenv("SCREENER_MIN_PRICE", "5")),
        )
        self.whale_tracker = WhaleTracker(self.sec, self.db)

        self.telegram = TelegramNotifier(
            self.config.telegram.bot_token, self.config.telegram.chat_id
        )

        # Risk management
        self.portfolio_manager = PortfolioManager(self.config.risk, self.db)
        self.position_sizer = PositionSizer(
            self.config.risk,
            risk_per_trade=float(os.getenv("RISK_PER_TRADE", "0.0025")),
        )
        self.stop_manager = StopLossManager(
            self.alpaca,
            self.db,
            trailing_factor=float(os.getenv("ATR_TRAILING_FACTOR", "2.5")),
            initial_stop_factor=float(os.getenv("ATR_INITIAL_STOP_FACTOR", "2.0")),
            telegram=self.telegram,
        )
        self.order_manager = OrderManager(self.alpaca, self.db)

        # Trader
        self.trader = Trader(self.alpaca, self.db, self.portfolio_manager)
        self._orders_settled = await self.trader.reconcile_pending_orders()

        # Strategies
        strategies = [
            MomentumStrategy(weight=self.config.strategy.momentum_weight),
            MeanReversionStrategy(weight=self.config.strategy.mean_reversion_weight),
            WhaleFollowStrategy(
                weight=self.config.strategy.whale_follow_weight,
                sec_client=self.sec,
                db=self.db,
            ),
        ]
        self.combiner = SignalCombiner(
            strategies,
            self.claude,
            weight_claude=self.config.strategy.claude_weight,
            min_score=self.config.strategy.min_signal_score,
            max_claude_calls=int(os.getenv("MAX_CLAUDE_CALLS_PER_SCAN", "12")),
        )

        # Register Telegram commands
        self.cmd_handler = TelegramCommandHandler(self.telegram, self)

        await self.telegram.send(
            "\U0001f680 <b>StockPilot started</b>\n"
            f"Account: ${account.get('equity', '?')}\n"
            f"Mode: PAPER TRADING"
        )

        # Migrate existing open positions to ATR trailing stops (upward only),
        # then reconcile so EVERY position ends with a live, full-qty stop
        # (covers names whose stop was kept but no longer exists at the broker).
        try:
            open_positions = await self.alpaca.get_positions()
            await self.sync_entry_fills()
            # Clean up any ghost DB trades (closed at broker while bot was down)
            # silently — one summary instead of a burst of Telegram pings.
            before = len(await self.db.get_open_trades())
            await self.sync_closed_positions(open_positions, notify=False)
            after = len(await self.db.get_open_trades())
            if before != after:
                await self.telegram.send(
                    f"\U0001f9f9 Startup-Sync: {before - after} verwaiste Trades "
                    f"geschlossen (bei Alpaca längst verkauft). DB jetzt konsistent."
                )
            if self._orders_settled:
                await self.stop_manager.migrate_stops_to_atr(open_positions)
                await self.stop_manager.reconcile_stops(open_positions)
            else:
                await self.telegram.send(
                    "⚠️ Ungeklärte Order-Ausführung. Trading blockiert; Broker prüfen."
                )
        except Exception as e:
            logger.error("stop_migration_error", error=str(e))

        # Initial data load
        await self.fred.update()
        await self.sec.update_filings()

        logger.info("stockpilot_ready")

    async def run(self):
        await self.start()

        # Setup graceful shutdown
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.request_shutdown)

        # Background tasks: Dashboard + Telegram polling
        from dashboard.app import start_dashboard

        asyncio.create_task(start_dashboard(self.db))
        asyncio.create_task(self.telegram.start_polling())
        logger.info("background_tasks_started", dashboard="http://0.0.0.0:8000")

        try:
            while self.running:
                self._trading_task = asyncio.create_task(self._trading_loop())
                try:
                    await self._trading_task
                except asyncio.CancelledError:
                    if self.running:
                        raise
                except Exception as e:
                    logger.error("main_loop_error", error=str(e), exc_info=True)
                    await self.telegram.send_error(str(e))
                    try:
                        await asyncio.wait_for(self._shutdown_event.wait(), timeout=60)
                    except TimeoutError:
                        pass
                finally:
                    self._trading_task = None
        finally:
            await self.shutdown()

    def request_shutdown(self):
        """Signal-safe request; actual resource cleanup runs once in `run`."""
        if not self.running:
            return
        logger.info("shutdown_requested")
        self.running = False
        self._shutdown_event.set()
        if self._trading_task and not self._trading_task.done():
            self._trading_task.cancel()

    async def sync_entry_fills(self):
        """Replace legacy estimated entry values only with the exact broker fill."""
        for trade in await self.db.get_open_trades():
            try:
                order = await self.alpaca.get_order(trade.order_id)
                if (
                    order.get("id") != trade.order_id
                    or order.get("symbol") != trade.symbol
                    or order.get("side") != trade.side.value.lower()
                    or order.get("status") not in {"filled", "canceled", "expired"}
                ):
                    continue
                qty = float(order.get("filled_qty") or 0)
                price = float(order.get("filled_avg_price") or 0)
                if qty <= 0 or not qty.is_integer() or price <= 0 or not order.get("filled_at"):
                    continue
                timestamp = datetime.fromisoformat(order["filled_at"].replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    continue
                await self.db.update_entry_fill(trade.order_id, int(qty), price, timestamp)
                logger.info(
                    "entry_fill_reconciled",
                    symbol=trade.symbol,
                    previous_price=trade.price,
                    broker_price=price,
                    qty=int(qty),
                )
            except Exception as exc:
                logger.error(
                    "entry_fill_reconciliation_failed", symbol=trade.symbol, error=str(exc)
                )

    async def sync_closed_positions(self, positions: list[dict], notify: bool = True):
        """Reconcile DB open trades against real Alpaca positions. Any DB trade
        whose symbol is no longer held was closed at the broker (stop fill etc.)
        — record the exit + PnL and notify. Fixes silent stop-loss exits."""
        held = {p.get("symbol") for p in positions}
        open_trades = await self.db.get_open_trades()
        gone = [t for t in open_trades if t.symbol not in held]
        if not gone:
            return

        closed_orders: list[dict] = []
        try:
            closed_orders = await self.alpaca.get_orders(status="closed")
        except Exception as e:
            logger.warning("sync_closed_orders_error", error=str(e))

        for t in gone:
            exit_side = "sell" if t.side.value == "BUY" else "buy"
            entry_time = t.timestamp
            if entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=UTC)
            else:
                entry_time = entry_time.astimezone(UTC)
            candidates = []
            for order in closed_orders:
                # Canceled partially filled orders still contributed real shares.
                filled_at = order.get("filled_at") or order.get("updated_at")
                if not filled_at:
                    continue
                try:
                    fill_time = datetime.fromisoformat(filled_at.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if (
                    order.get("symbol") == t.symbol
                    and order.get("side") == exit_side
                    and order.get("status") in {"filled", "canceled", "expired"}
                    and float(order.get("filled_qty") or 0) > 0
                    and order.get("filled_avg_price")
                    and fill_time >= entry_time
                ):
                    candidates.append((fill_time, order))
            if not candidates:
                logger.error("exit_fill_unresolved", symbol=t.symbol, entry_order_id=t.order_id)
                continue
            remaining_qty = t.qty
            exit_value = 0.0
            exit_qty = 0
            for _, order in sorted(candidates, key=lambda item: item[0]):
                filled_qty = int(float(order.get("filled_qty") or 0))
                used_qty = min(filled_qty, remaining_qty)
                if used_qty <= 0:
                    continue
                exit_value += used_qty * float(order["filled_avg_price"])
                exit_qty += used_qty
                remaining_qty -= used_qty
                if remaining_qty == 0:
                    break
            if remaining_qty > 0 or exit_qty == 0:
                logger.error(
                    "exit_fill_quantity_unresolved",
                    symbol=t.symbol,
                    expected=t.qty,
                    matched=exit_qty,
                )
                continue
            price = exit_value / exit_qty
            direction = 1 if t.side.value == "BUY" else -1
            pnl = (price - t.price) * t.qty * direction
            pnl_pct = ((price - t.price) / t.price * 100 * direction) if t.price else 0
            timestamp = t.timestamp
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            days = (datetime.now(UTC) - timestamp).days
            await self.db.close_trade(t.order_id, price, pnl)
            logger.info(
                "position_closed_synced",
                symbol=t.symbol,
                qty=t.qty,
                exit=round(price, 2),
                pnl=round(pnl, 2),
            )
            if notify:
                await self.telegram.send_position_closed(t.symbol, t.qty, price, pnl, pnl_pct, days)

    async def send_daily_summary(self):
        """End-of-day report built from live account data + today's DB activity."""
        account = await self.alpaca.get_account()
        positions = await self.alpaca.get_positions()
        equity = float(account.get("equity", 0))
        last_equity = float(account.get("last_equity", 0)) or equity
        day_pnl = equity - last_equity
        day_pct = (day_pnl / last_equity * 100) if last_equity else 0

        today = datetime.utcnow().strftime("%Y-%m-%d")
        cur = await self.db._db.execute(
            "SELECT side, COUNT(*) FROM trades WHERE timestamp LIKE ? GROUP BY side", (f"{today}%",)
        )
        by_side = {r[0]: r[1] for r in await cur.fetchall()}

        cur = await self.db._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl),0), "
            "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) "
            "FROM trades WHERE closed_at LIKE ?",
            (f"{today}%",),
        )
        n_closed, closed_pnl, closed_wins = await cur.fetchone()

        api_cost = await self.db.get_api_cost_today()

        # best / worst open position today
        best = worst = None
        if positions:
            srt = sorted(positions, key=lambda p: float(p.get("unrealized_plpc", 0)))
            worst, best = srt[0], srt[-1]

        emoji = "\U0001f4c8" if day_pnl >= 0 else "\U0001f4c9"
        lines = [
            f"{emoji} <b>TAGESABSCHLUSS {today}</b>",
            f"Equity: ${equity:,.0f} ({day_pnl:+,.0f} / {day_pct:+.2f}%)",
            f"Positionen: {len(positions)} | Cash: ${float(account.get('cash', 0)):,.0f}",
            f"Käufe heute: {by_side.get('BUY', 0)} | Verkäufe: {by_side.get('SELL', 0)}",
            f"Geschlossen: {n_closed or 0} (davon {closed_wins or 0} Gewinner), "
            f"realisiert ${closed_pnl or 0:+,.0f}",
        ]
        if best is not None:
            lines.append(
                f"Bester: {best['symbol']} {float(best.get('unrealized_plpc', 0)) * 100:+.1f}%"
            )
        if worst is not None and worst is not best:
            lines.append(
                f"Schlechtester: {worst['symbol']} "
                f"{float(worst.get('unrealized_plpc', 0)) * 100:+.1f}%"
            )
        lines.append(f"API-Kosten heute: ${api_cost:.2f}")

        await self.telegram.send("\n".join(lines))
        logger.info("daily_summary_sent", equity=equity, day_pnl=round(day_pnl, 2))

    async def _maybe_heartbeat(self, equity: float, n_positions: int, extra: str = ""):
        """Hourly heartbeat during market hours — fires in ANY state (trading
        or risk-paused) so a halt never looks like a dead bot."""
        now_ts = datetime.utcnow().timestamp()
        if now_ts - getattr(self, "_last_heartbeat", 0) > 3600:
            self._last_heartbeat = now_ts
            await self.telegram.send(
                f"💓 Heartbeat: Bot aktiv | Equity ${equity:,.0f} | "
                f"Positionen {n_positions}{(' | ' + extra) if extra else ''}"
            )

    async def _trading_loop(self):
        if not await self.trader.reconcile_pending_orders():
            logger.error("trading_blocked_pending_order_outcome")
            account = await self.alpaca.get_account()
            positions = await self.alpaca.get_positions()
            await self._maybe_heartbeat(
                float(account.get("equity", 0)),
                len(positions),
                "ORDER-STATUS UNGEKLÄRT – BROKER PRÜFEN",
            )
            await asyncio.sleep(30)
            return
        clock = await self.alpaca.get_market_clock()

        if not clock.get("is_open"):
            next_open = clock.get("next_open", "?")
            logger.info("market_closed", next_open=next_open)
            today = datetime.utcnow().strftime("%Y-%m-%d")

            # Daily summary on the open->closed TRANSITION. The old 10-minute
            # near-close window was never hit (the loop only scans every 15 min),
            # so no summary ever fired. Fires once, only on days we actually traded.
            if (
                getattr(self, "_market_was_open_on", None) == today
                and getattr(self, "_summary_sent_on", None) != today
            ):
                self._summary_sent_on = today
                try:
                    await self.send_daily_summary()
                except Exception as e:
                    logger.error("daily_summary_error", error=str(e))

            # Stop coverage must hold outside market hours too: GTC stops can be
            # placed any time, and a position that lost its stop overnight would
            # otherwise stay naked until the next open.
            try:
                closed_positions = await self.alpaca.get_positions()
                await self.stop_manager.reconcile_stops(closed_positions)
            except Exception as e:
                logger.error("offhours_reconcile_error", error=str(e))

            # Off-hours: refresh 13F filings at most once per day (they change
            # quarterly — no point re-pulling every 5 min).
            if getattr(self, "_filings_updated_on", None) != today:
                self._filings_updated_on = today
                await self.sec.update_filings()
                await self.whale_tracker.update_all()
            await asyncio.sleep(300)
            return

        # Mark that the market was open today — the close-transition summary
        # only fires on days we actually traded.
        self._market_was_open_on = datetime.utcnow().strftime("%Y-%m-%d")

        # 1. Get account + positions
        account = await self.alpaca.get_account()
        positions = await self.alpaca.get_positions()

        # 1b. Detect broker-side exits (stop fills etc.) — close them in the DB
        #     and notify. Without this, stop-triggered sells are invisible.
        await self.sync_closed_positions(positions, notify=True)

        # Snapshot portfolio
        equity = float(account.get("equity", 0))
        cash = float(account.get("cash", 0))
        await self.db.snapshot_portfolio(equity, cash, len(positions))

        # 2. Update trailing stops, take-profit, then reconcile stop coverage
        await self.stop_manager.update_trailing_stops(positions)
        await self.stop_manager.handle_take_profit(positions)
        if not await self.trader.reconcile_pending_orders():
            logger.error("cycle_blocked_unresolved_take_profit")
            await asyncio.sleep(30)
            return
        # Refresh positions (partial TP may have changed qty) then reconcile
        positions = await self.alpaca.get_positions()

        # 3. Time stops
        await self.stop_manager.evaluate_time_stops(positions, self.claude)
        if not await self.trader.reconcile_pending_orders():
            logger.error("cycle_blocked_unresolved_time_stop")
            await asyncio.sleep(30)
            return

        # 4. Sync orders
        await self.order_manager.sync_orders()

        # Exit fills can change quantities. Check coverage after all order work,
        # not before a task that could invalidate that coverage.
        positions = await self.alpaca.get_positions()
        if not await self.stop_manager.reconcile_stops(positions):
            logger.error("entries_blocked_unverified_stop_coverage")
            await self._maybe_heartbeat(equity, len(positions), "STOP-SCHUTZ NICHT BESTÄTIGT")
            await asyncio.sleep(self.config.strategy.scan_interval_seconds)
            return

        # 5. Can we trade?
        if not await self.portfolio_manager.can_trade(account, positions):
            logger.warning("risk_limits_no_trade")
            # Hourly heartbeat even while halted — so it never looks dead.
            await self._maybe_heartbeat(equity, len(positions), "⏸ PAUSIERT (Risk-Limit)")
            await asyncio.sleep(self.config.strategy.scan_interval_seconds)
            return

        # 6. Get universe
        universe = await get_universe(self.config.extra_watchlist)

        # 7. Calculate technicals — one multi-symbol fetch instead of N calls
        tech_data = {}
        try:
            bars_by_symbol = await self.alpaca.get_bars_multi(universe, "1Day", limit=200)
        except Exception as e:
            logger.error("bars_multi_error", error=str(e))
            bars_by_symbol = {}
        for symbol, bars in bars_by_symbol.items():
            try:
                if not bars.empty:
                    tech_data[symbol] = self.technical.calculate_all(bars)
            except Exception as e:
                logger.debug("tech_calc_skip", symbol=symbol, error=str(e))

        # 8. Screen universe
        filtered = self.screener.filter_universe(universe, tech_data)

        # 9. Update news for filtered stocks
        await self.news.update(self.alpaca, filtered[:50])

        # 10. Generate combined signals
        claude_was_healthy = self.claude.api_healthy
        all_signals = await self.combiner.generate_combined_signals(
            universe=filtered,
            tech_data=tech_data,
            news=self.news,
            fred=self.fred,
            portfolio=positions,
            db=self.db,
        )

        # Alert once on Claude health transition (healthy -> offline)
        if claude_was_healthy and not self.claude.api_healthy:
            await self.telegram.send(
                f"⚠️ Claude API nicht erreichbar: {self.claude.last_error}. "
                f"Bot läuft im Fallback-Modus (nur Technicals)."
            )
        elif not claude_was_healthy and self.claude.api_healthy:
            await self.telegram.send("✅ Claude API wieder erreichbar.")

        # 11. Execute signals
        # Log EVERY combined signal (not just executed trades) so Claude's
        # contribution is auditable even when no trade results.
        for sig in all_signals:
            await self.db.log_signal(
                sig.symbol, sig.action.value, sig.score, sig.strategy, sig.reasoning
            )

        trades_executed = 0
        for sig in all_signals:
            if sig.score < self.config.strategy.min_signal_score:
                continue

            current_price = tech_data.get(sig.symbol, {}).get("price")
            if sig.action.value == "SELL":
                held = next(
                    (
                        p
                        for p in positions
                        if p.get("symbol") == sig.symbol and float(p.get("qty", 0)) > 0
                    ),
                    None,
                )
                if not held:
                    logger.warning("order_blocked", symbol=sig.symbol, reason="sell_without_long")
                    continue
                shares = int(float(held["qty"]))
            else:
                size = self.position_sizer.calculate(
                    signal=sig,
                    account=account,
                    existing_positions=positions,
                    current_price=current_price,
                )
                if not size:
                    continue
                shares = size.shares

            trade = await self.trader.execute(
                signal=sig,
                qty=shares,
                account=account,
                positions=positions,
                current_price=current_price,
            )
            if trade:
                await self.telegram.send_trade(trade, sig)
                trades_executed += 1

            if not await self.trader.reconcile_pending_orders():
                logger.error("cycle_blocked_unresolved_execution", symbol=sig.symbol)
                await asyncio.sleep(30)
                return

            # A failed response can hide a filled entry: inspect even on None.
            positions = await self.alpaca.get_positions()
            account = await self.alpaca.get_account()
            await self.sync_closed_positions(positions, notify=True)
            if not await self.stop_manager.reconcile_stops(positions):
                logger.error("entries_blocked_unverified_stop_coverage")
                break

        if trades_executed:
            logger.info("trades_executed", count=trades_executed)

        # Hourly heartbeat (fires in any state via the shared helper)
        top = all_signals[0] if all_signals else None
        top_str = f"{top.symbol} {top.score:.2f}" if top else "keine"
        await self._maybe_heartbeat(
            float(account.get("equity", 0)),
            len(positions),
            f"Signale {len(all_signals)} (Top: {top_str}, Schwelle "
            f"{self.config.strategy.min_signal_score})",
        )

        # 12. (Daily summary now fires on the market open->closed transition —
        #      the old 10-min near-close window never hit the 15-min scan loop.)

        # 13. Update FRED data hourly
        await self.fred.update()

        # 14. Wait for next scan
        await asyncio.sleep(self.config.strategy.scan_interval_seconds)

    def _is_near_close(self, clock: dict) -> bool:
        next_close = clock.get("next_close", "")
        if not next_close:
            return False
        try:
            close_time = datetime.fromisoformat(next_close.replace("Z", "+00:00"))
            now = datetime.now(close_time.tzinfo)
            minutes_to_close = (close_time - now).total_seconds() / 60
            return 0 < minutes_to_close < 10
        except Exception:
            return False

    async def shutdown(self):
        if self._shutdown_started:
            return
        self._shutdown_started = True
        logger.info("shutting_down")
        self.running = False

        # Broker-held protection must survive a stopped/offline process.
        entries_cancelled = False
        try:
            entries_cancelled = await self.order_manager.cancel_entries()
        except Exception as e:
            logger.error("shutdown_cancel_error", error=str(e))

        # A cancellation can arrive next to a broker fill. Re-read broker state
        # before closing clients and restore any missing protective stop.
        stops_safe = False
        orders_settled = False
        try:
            for _ in range(3):
                # Settle non-protective exits too; otherwise they may expire
                # after shutdown with their protective stop already canceled.
                open_orders = await self.alpaca.get_orders(status="open")
                pending = [
                    o
                    for o in open_orders
                    if o.get("type") in {"market", "limit"}
                    and not o.get("legs")
                    and o.get("order_class", "simple") in {"simple", ""}
                ]
                for order in pending:
                    try:
                        await self.alpaca.cancel_order(order["id"])
                    except Exception as exc:
                        logger.warning(
                            "shutdown_order_cancel_failed", order_id=order["id"], error=str(exc)
                        )
                journal_settled = await self.trader.reconcile_pending_orders()
                terminal = True
                for order in pending:
                    final = await self.alpaca.get_order(order["id"])
                    terminal = terminal and final.get("status") in {
                        "filled",
                        "canceled",
                        "expired",
                        "rejected",
                    }
                if journal_settled and terminal:
                    orders_settled = True
                    break
                await asyncio.sleep(0.5)
            positions = await self.alpaca.get_positions()
            await self.sync_closed_positions(positions, notify=False)
            if orders_settled:
                stops_safe = await self.stop_manager.reconcile_stops(positions)
            logger.info("shutdown_stop_reconciliation", safe=stops_safe)
        except Exception as e:
            logger.error("shutdown_stop_reconciliation_failed", error=str(e), exc_info=True)

        status = (
            "Entry cancellations confirmed"
            if entries_cancelled
            else "WARNING: entry cancellations NOT confirmed"
        )
        protection = (
            "Stop-Abdeckung bestätigt."
            if stops_safe
            else "⚠️ STOP-SCHUTZ NICHT BESTÄTIGT. Broker und offene Orders manuell prüfen."
        )
        await self.telegram.send(f"🛑 <b>StockPilot stopped</b>\n{status}\n{protection}")

        await self.alpaca.close()
        await self.sec.close()
        await self.news.close()
        await self.fred.close()
        await self.telegram.close()
        await self.db.close()

        logger.info("shutdown_complete")


async def main():
    bot = StockPilot()
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
