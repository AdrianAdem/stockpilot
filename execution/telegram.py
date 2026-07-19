import asyncio
from datetime import datetime

import httpx
import structlog

from storage.models import DailySummary, Signal, TradeRecord

logger = structlog.get_logger()


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self._client = httpx.AsyncClient(timeout=15)
        self._last_update_id = 0
        self._command_handlers: dict = {}
        self._polling = False

    async def close(self):
        self._polling = False
        await self._client.aclose()

    async def send(self, text: str):
        # Retry a few times — transient DNS/network blips otherwise silently
        # drop trade notifications and heartbeats.
        for attempt in range(3):
            try:
                await self._client.post(
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                    },
                )
                return
            except Exception as e:
                if attempt == 2:
                    logger.error("telegram_send_error", error=str(e))
                else:
                    await asyncio.sleep(3 * (attempt + 1))

    async def send_trade(self, trade: TradeRecord, signal: Signal):
        emoji = "\U0001f4c8" if trade.side.value == "BUY" else "\U0001f534"
        stop_pct = ""
        target_pct = ""
        if trade.price > 0:
            if trade.stop_loss:
                stop_pct = f" ({(trade.stop_loss - trade.price) / trade.price * 100:.1f}%)"
            if trade.take_profit:
                target_pct = f" ({(trade.take_profit - trade.price) / trade.price * 100:.1f}%)"

        text = f"""{emoji} <b>TRADE EXECUTED</b>
{trade.side.value} {trade.qty}x {trade.symbol} @ ${trade.price:.2f}
Strategy: {signal.strategy}
Signal Score: {signal.score:.2f}
Stop-Loss: {f"${trade.stop_loss:.2f}{stop_pct}" if trade.stop_loss else "N/A"}
Target: {f"${trade.take_profit:.2f}{target_pct}" if trade.take_profit else "N/A"}
Reasoning: {signal.reasoning[:100]}"""
        await self.send(text)

    async def send_stop_loss(self, symbol: str, qty: int, price: float,
                              loss: float, days_held: int):
        text = f"""\U0001f534 <b>STOP-LOSS HIT</b>
SOLD {qty}x {symbol} @ ${price:.2f}
Loss: ${loss:.2f}
Hold Time: {days_held} days"""
        await self.send(text)

    async def send_position_closed(self, symbol: str, qty: int, exit_price: float,
                                    pnl: float, pnl_pct: float, days_held: int):
        emoji = "\U0001f7e2" if pnl >= 0 else "\U0001f534"
        text = f"""{emoji} <b>POSITION CLOSED</b>
SOLD {qty}x {symbol} @ ${exit_price:.2f}
PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%)
Hold Time: {days_held} days"""
        await self.send(text)

    async def send_daily_summary(self, summary: DailySummary):
        text = f"""\U0001f4ca <b>DAILY SUMMARY</b>
Portfolio: ${summary.portfolio_value:,.2f} ({summary.daily_pnl_pct:+.1f}% today)
Trades: {summary.buys} buys, {summary.sells} sells, {summary.stop_losses} stop-losses
Win Rate (30d): {summary.win_rate_30d:.0f}%
Open Positions: {summary.open_positions}/{summary.max_positions}"""

        if summary.top_performer:
            text += f"\nTop: {summary.top_performer} {summary.top_performer_pct:+.1f}%"
        if summary.worst_performer:
            text += f"\nWorst: {summary.worst_performer} {summary.worst_performer_pct:+.1f}%"
        text += f"\nAPI Cost: ${summary.api_cost_today:.2f}"

        await self.send(text)

    async def send_error(self, error: str):
        await self.send(f"\U0001f534 <b>ERROR</b>\n{error[:500]}")

    async def send_pause(self, reason: str):
        await self.send(f"⚠️ <b>TRADING PAUSED</b>\n{reason}")

    # === COMMAND POLLING ===

    def register_command(self, command: str, handler):
        self._command_handlers[command] = handler

    async def start_polling(self):
        self._polling = True
        logger.info("telegram_polling_started")
        while self._polling:
            try:
                await self._poll_updates()
            except Exception as e:
                logger.error("telegram_poll_error", error=str(e))
            await asyncio.sleep(2)

    async def _poll_updates(self):
        try:
            resp = await self._client.get(
                f"{self.base_url}/getUpdates",
                params={"offset": self._last_update_id + 1, "timeout": 5},
                timeout=10,
            )
            data = resp.json()
            for update in data.get("result", []):
                self._last_update_id = update["update_id"]
                message = update.get("message", {})
                text = message.get("text", "")
                chat_id = str(message.get("chat", {}).get("id", ""))

                # Only respond to authorized chat
                if chat_id != self.chat_id:
                    continue

                if text.startswith("/"):
                    command = text.split()[0].lstrip("/").split("@")[0]
                    handler = self._command_handlers.get(command)
                    if handler:
                        try:
                            await handler()
                        except Exception as e:
                            await self.send(f"\U0001f534 Command error: {e}")
                    else:
                        await self.send(
                            "Commands: /status /positions /history /pause /resume /kill"
                        )
        except httpx.ReadTimeout:
            pass


class TelegramCommandHandler:
    """Registers all bot commands with their handlers."""

    def __init__(self, telegram: TelegramNotifier, bot):
        self.tg = telegram
        self.bot = bot
        self._register()

    def _register(self):
        self.tg.register_command("status", self.cmd_status)
        self.tg.register_command("positions", self.cmd_positions)
        self.tg.register_command("history", self.cmd_history)
        self.tg.register_command("pause", self.cmd_pause)
        self.tg.register_command("resume", self.cmd_resume)
        self.tg.register_command("kill", self.cmd_kill)

    async def cmd_status(self):
        account = await self.bot.alpaca.get_account()
        positions = await self.bot.alpaca.get_positions()
        equity = float(account.get("equity", 0))
        cash = float(account.get("cash", 0))
        buying_power = float(account.get("buying_power", 0))
        invested_pct = ((equity - cash) / equity * 100) if equity > 0 else 0
        paused = self.bot.portfolio_manager.is_paused

        text = f"""\U0001f4ca <b>STATUS</b>
Equity: ${equity:,.2f}
Cash: ${cash:,.2f}
Buying Power: ${buying_power:,.2f}
Invested: {invested_pct:.0f}%
Positions: {len(positions)}/15
Trading: {"PAUSED" if paused else "ACTIVE"}"""
        await self.tg.send(text)

    async def cmd_positions(self):
        positions = await self.bot.alpaca.get_positions()
        if not positions:
            await self.tg.send("No open positions.")
            return

        lines = ["\U0001f4cb <b>POSITIONS</b>"]
        for p in positions:
            sym = p.get("symbol", "?")
            qty = p.get("qty", "?")
            entry = float(p.get("avg_entry_price", 0))
            current = float(p.get("current_price", 0))
            pnl = float(p.get("unrealized_pl", 0))
            pnl_pct = float(p.get("unrealized_plpc", 0)) * 100
            emoji = "\U0001f7e2" if pnl >= 0 else "\U0001f534"
            lines.append(f"{emoji} {sym}: {qty}x @ ${entry:.2f} | ${current:.2f} ({pnl_pct:+.1f}%) ${pnl:+.2f}")

        await self.tg.send("\n".join(lines))

    async def cmd_history(self):
        trades = await self.bot.db.get_recent_trades(7)
        if not trades:
            await self.tg.send("No recent trades.")
            return

        lines = ["\U0001f4c4 <b>RECENT TRADES (7d)</b>"]
        for t in trades[:15]:
            emoji = "\U0001f7e2" if t.side.value == "BUY" else "\U0001f534"
            pnl_str = f" PnL: ${t.pnl:+.2f}" if t.pnl is not None else ""
            lines.append(f"{emoji} {t.side.value} {t.qty}x {t.symbol} @ ${t.price:.2f}{pnl_str}")

        await self.tg.send("\n".join(lines))

    async def cmd_pause(self):
        self.bot.portfolio_manager.pause()
        await self.tg.send("⏸ <b>Trading PAUSED</b>\nNo new trades until /resume")

    async def cmd_resume(self):
        self.bot.portfolio_manager.resume()
        await self.tg.send("▶️ <b>Trading RESUMED</b>\nDrawdown counters reset.")

    async def cmd_kill(self):
        await self.tg.send("\U0001f6a8 <b>KILLING ALL POSITIONS...</b>")
        positions = await self.bot.alpaca.get_positions()

        # Cancel all orders first
        await self.bot.order_manager.cancel_all()

        closed = 0
        for p in positions:
            symbol = p.get("symbol", "")
            qty = int(p.get("qty", 0))
            if qty > 0:
                try:
                    await self.bot.alpaca.submit_order(
                        symbol=symbol, qty=qty, side="sell",
                        order_type="market",
                    )
                    closed += 1
                except Exception as e:
                    await self.tg.send(f"\U0001f534 Failed to close {symbol}: {e}")

        self.bot.portfolio_manager.pause()
        await self.tg.send(
            f"\U0001f6d1 <b>KILL COMPLETE</b>\n"
            f"Closed {closed} positions. Trading paused.\n"
            f"Use /resume to restart."
        )
