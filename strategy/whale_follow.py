import structlog

from config.strategies import WHALE_FOLLOW
from storage.models import Action, Signal
from strategy.base import Strategy

logger = structlog.get_logger()


class WhaleFollowStrategy(Strategy):
    name = "whale_follow"

    def __init__(self, weight: float = 0.25, sec_client=None, db=None):
        self.weight = weight
        self.sec = sec_client
        self.db = db

    async def generate_signals(self, universe: list[str], tech_data: dict,
                               **kwargs) -> list[Signal]:
        signals = []

        if not self.db:
            return signals

        for symbol in universe:
            td = tech_data.get(symbol, {})

            consensus = await self.db.get_whale_consensus(symbol)
            buyer_count = len(consensus.get("buyers", []))
            seller_count = len(consensus.get("sellers", []))

            if buyer_count < WHALE_FOLLOW.min_whale_count:
                continue

            # Technical filter: not bearish
            rsi = td.get("RSI")
            if rsi and rsi > WHALE_FOLLOW.max_rsi:
                continue
            if td.get("above_SMA200") is False:
                continue

            score = min(0.3 + buyer_count * 0.15, 1.0)

            # Reduce if sellers present
            if seller_count > 0:
                score -= seller_count * 0.10

            if score < 0.3:
                continue

            whale_info = ", ".join(
                f"{b['fund']} +{b.get('change_pct', '?')}%"
                for b in consensus["buyers"][:5]
            )

            signals.append(Signal(
                symbol=symbol,
                action=Action.BUY,
                score=round(score, 2),
                strategy=self.name,
                stop_loss_price=self._calc_stop(td),
                timeframe="weeks",
                reasoning=f"Whale accumulation: {whale_info}",
            ))

        logger.info("whale_signals", count=len(signals))
        return signals

    def _calc_stop(self, td: dict) -> float | None:
        price = td.get("price")
        atr = td.get("ATR")
        if not price or not atr:
            return None
        return round(price - 3 * atr, 2)  # wider stop for longer holds
