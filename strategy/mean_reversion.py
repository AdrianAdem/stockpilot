import structlog

from config.strategies import MEAN_REVERSION
from storage.models import Action, Signal
from strategy.base import Strategy

logger = structlog.get_logger()


LARGE_CAP_PROXY_THRESHOLD = 50_000_000  # avg_volume * price > $50M daily value = large cap proxy


class MeanReversionStrategy(Strategy):
    name = "mean_reversion"

    def __init__(self, weight: float = 0.25):
        self.weight = weight

    def _is_large_cap(self, td: dict) -> bool:
        price = td.get("price", 0)
        avg_vol = td.get("avg_volume_20d", 0)
        if not price or not avg_vol:
            return False
        return price * avg_vol > LARGE_CAP_PROXY_THRESHOLD

    async def generate_signals(self, universe: list[str], tech_data: dict,
                               **kwargs) -> list[Signal]:
        signals = []
        news_aggregator = kwargs.get("news")

        for symbol in universe:
            td = tech_data.get(symbol)
            if not td or not td.get("RSI"):
                continue

            if not self._is_large_cap(td):
                continue

            buy_score = self._check_buy(td, symbol, news_aggregator)
            sell_score = self._check_sell(td)

            if buy_score > 0.3:
                signals.append(Signal(
                    symbol=symbol,
                    action=Action.BUY,
                    score=buy_score,
                    strategy=self.name,
                    target_price=td.get("BB_middle"),
                    stop_loss_price=self._calc_stop(td),
                    reasoning=self._reasoning(td),
                ))
            elif sell_score > 0.3:
                signals.append(Signal(
                    symbol=symbol,
                    action=Action.SELL,
                    score=sell_score,
                    strategy=self.name,
                    reasoning="price at mean, take profit",
                ))

        logger.info("mean_reversion_signals", count=len(signals))
        return signals

    def _check_buy(self, td: dict, symbol: str, news=None) -> float:
        score = 0.0

        if td.get("BB_position") == "below_lower":
            score += 0.40

        rsi = td.get("RSI")
        if rsi and rsi < MEAN_REVERSION.rsi_oversold:
            score += 0.35

        # Check no negative catalyst in news
        if news:
            symbol_news = news.get_news_for_symbol(symbol)
            if symbol_news:
                # If there's very negative news, reduce score
                negative = ["crash", "fraud", "bankruptcy", "investigation", "lawsuit"]
                for article in symbol_news[:5]:
                    title = article.get("title", "").lower()
                    if any(w in title for w in negative):
                        score -= 0.30
                        break

        # Bonus if volume spike (capitulation)
        vol = td.get("volume_ratio")
        if vol and vol > 2.0:
            score += 0.15

        return round(max(score, 0.0), 2)

    def _check_sell(self, td: dict) -> float:
        price = td.get("price")
        bb_mid = td.get("BB_middle")
        if price and bb_mid and price >= bb_mid:
            return 0.70
        return 0.0

    def _calc_stop(self, td: dict) -> float | None:
        price = td.get("price")
        atr = td.get("ATR")
        if not price or not atr:
            return None
        return round(price - 2.5 * atr, 2)

    def _reasoning(self, td: dict) -> str:
        parts = []
        if td.get("BB_position") == "below_lower":
            parts.append("below lower Bollinger Band")
        if (td.get("RSI") or 100) < MEAN_REVERSION.rsi_oversold:
            parts.append(f"RSI {td['RSI']} oversold")
        vol = td.get("volume_ratio")
        if vol and vol > 2.0:
            parts.append(f"volume spike {vol}x")
        return "; ".join(parts) if parts else "mean reversion setup"
