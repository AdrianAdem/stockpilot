import structlog

from data.alpaca_client import AlpacaClient
from risk.portfolio_manager import PortfolioManager
from storage.db import Database
from storage.models import Side, Signal, TradeRecord

logger = structlog.get_logger()


class Trader:
    """Places entry orders after the risk layer has approved them.

    Exits are deliberately NOT handled here — StopLossManager owns every exit
    so that trailing stops, take-profit and time stops cannot fight each other.
    """

    def __init__(self, alpaca: AlpacaClient, db: Database, portfolio_manager: PortfolioManager):
        self.alpaca = alpaca
        self.db = db
        self.pm = portfolio_manager

    async def execute(
        self,
        signal: Signal,
        qty: int,
        account: dict,
        positions: list[dict],
        current_price: float | None = None,
    ) -> TradeRecord | None:
        # Final safety checks
        clock = await self.alpaca.get_market_clock()
        if not clock.get("is_open"):
            logger.warning("market_closed_skip_trade", symbol=signal.symbol)
            return None

        # BUY gates: no averaging-in + hard 5% per-position cap, then sector limit
        if signal.action.value == "BUY":
            intended_value = qty * (current_price or signal.stop_loss_price or 0)
            if not self.pm.can_open_position(signal.symbol, positions, account, intended_value):
                return None
            if not self.pm.check_sector_limit(signal.symbol, positions, account):
                logger.warning("order_blocked", symbol=signal.symbol, reason="sector_limit")
                return None

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
                order = await self.alpaca.submit_order(
                    symbol=signal.symbol,
                    qty=qty,
                    side="buy",
                    order_type=order_type,
                    limit_price=limit_price,
                )
                if signal.stop_loss_price:
                    stop_order = await self.alpaca.submit_order(
                        symbol=signal.symbol,
                        qty=qty,
                        side="sell",
                        order_type="stop",
                        stop_price=signal.stop_loss_price,
                        time_in_force="gtc",
                    )
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
                order = await self.alpaca.submit_order(
                    symbol=signal.symbol,
                    qty=qty,
                    side="sell",
                    order_type=order_type,
                )
            else:
                return None

        except Exception as e:
            logger.error("order_execution_failed", symbol=signal.symbol, error=str(e))
            return None

        # Log trade
        filled_price = float(order.get("filled_avg_price") or order.get("limit_price") or 0)
        if filled_price == 0:
            filled_price = current_price or signal.stop_loss_price or 0

        trade = TradeRecord(
            symbol=signal.symbol,
            side=Side.BUY if signal.action.value == "BUY" else Side.SELL,
            qty=qty,
            price=filled_price,
            order_id=order.get("id", ""),
            strategy=signal.strategy,
            signal_score=signal.score,
            stop_loss=signal.stop_loss_price,
            take_profit=signal.target_price,
        )
        await self.db.log_trade(trade)
        # signal logging happens in main loop for ALL signals, not just trades

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
