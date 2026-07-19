import structlog

from config.strategies import MOMENTUM
from storage.models import Action, Signal
from strategy.base import Strategy

logger = structlog.get_logger()


class MomentumStrategy(Strategy):
    name = "momentum"

    def __init__(self, weight: float = 0.30):
        self.weight = weight

    async def generate_signals(self, universe: list[str], tech_data: dict,
                               **kwargs) -> list[Signal]:
        signals = []

        for symbol in universe:
            td = tech_data.get(symbol)
            if not td or not td.get("RSI"):
                continue

            buy_score = self._check_buy(td)
            sell_score = self._check_sell(td)

            if buy_score > 0.3:
                signals.append(Signal(
                    symbol=symbol,
                    action=Action.BUY,
                    score=buy_score,
                    strategy=self.name,
                    stop_loss_price=self._calc_stop(td, "long"),
                    reasoning=self._buy_reasoning(td),
                ))
            elif sell_score > 0.3:
                signals.append(Signal(
                    symbol=symbol,
                    action=Action.SELL,
                    score=sell_score,
                    strategy=self.name,
                    reasoning=self._sell_reasoning(td),
                ))

        logger.info("momentum_signals", count=len(signals))
        return signals

    def _check_buy(self, td: dict) -> float:
        # Continuous scoring (0-1) so genuinely strong setups rank above weak
        # ones and can clear the 0.7 gate, instead of everything pinning at 0.75.
        score = 0.0

        # RSI component (0-0.25): peaks in the 48-60 sweet spot, tapers at edges
        rsi = td.get("RSI")
        if rsi and MOMENTUM.rsi_low <= rsi <= MOMENTUM.rsi_high:
            center = (MOMENTUM.rsi_low + MOMENTUM.rsi_high) / 2  # ~52.5
            half = (MOMENTUM.rsi_high - MOMENTUM.rsi_low) / 2
            closeness = 1 - abs(rsi - center) / half  # 1 at center, 0 at edges
            score += 0.25 * (0.6 + 0.4 * closeness)    # 0.15-0.25
        elif rsi and rsi < MOMENTUM.rsi_low:
            score += 0.08  # oversold-but-recovering, weak credit

        # MACD component (0-0.25): fresh crossover best, else positive histogram
        if td.get("MACD_bullish_recent"):
            score += 0.25
        elif td.get("MACD_crossover") == "bullish":
            score += 0.20
        elif (td.get("MACD_histogram") or 0) > 0:
            score += 0.12

        # Trend component (0-0.30): both SMAs + golden cross + distance over SMA50
        if td.get("above_SMA50") and td.get("above_SMA200"):
            score += 0.22
            if td.get("cross") == "golden":
                score += 0.05
            price, sma50 = td.get("price"), td.get("SMA_50")
            if price and sma50 and sma50 > 0:
                ext = (price - sma50) / sma50           # how far above SMA50
                score += min(max(ext, 0), 0.05) * 0.6   # small bonus, capped
        elif td.get("above_SMA50"):
            score += 0.12

        # Volume component (0-0.25): scaled by how far above the 20d average
        vol_ratio = td.get("volume_ratio")
        if vol_ratio:
            if vol_ratio >= MOMENTUM.volume_multiplier:
                # 1.5x -> 0.18, 2.5x+ -> 0.25
                score += min(0.18 + (vol_ratio - 1.5) * 0.07, 0.25)
            elif vol_ratio >= 1.0:
                score += 0.08

        return round(min(score, 1.0), 3)

    def _check_sell(self, td: dict) -> float:
        score = 0.0

        rsi = td.get("RSI")
        if rsi and rsi > MOMENTUM.rsi_overbought:
            score += 0.35

        if td.get("MACD_crossover") == "bearish":
            score += 0.35

        if td.get("above_SMA50") is False:
            score += 0.30

        return round(min(score, 1.0), 2)

    def _calc_stop(self, td: dict, direction: str) -> float | None:
        price = td.get("price")
        atr = td.get("ATR")
        if not price or not atr:
            return None
        if direction == "long":
            return round(price - 2 * atr, 2)
        return round(price + 2 * atr, 2)

    def _buy_reasoning(self, td: dict) -> str:
        parts = []
        if MOMENTUM.rsi_low <= (td.get("RSI") or 0) <= MOMENTUM.rsi_high:
            parts.append(f"RSI {td['RSI']} in momentum zone")
        if td.get("MACD_bullish_recent"):
            parts.append("recent MACD bullish crossover")
        if td.get("above_SMA50") and td.get("above_SMA200"):
            parts.append("above both SMAs")
        vol = td.get("volume_ratio")
        if vol and vol >= MOMENTUM.volume_multiplier:
            parts.append(f"volume {vol}x avg")
        return "; ".join(parts) if parts else "momentum setup"

    def _sell_reasoning(self, td: dict) -> str:
        parts = []
        if (td.get("RSI") or 0) > MOMENTUM.rsi_overbought:
            parts.append(f"RSI {td['RSI']} overbought")
        if td.get("MACD_crossover") == "bearish":
            parts.append("MACD bearish crossover")
        if td.get("above_SMA50") is False:
            parts.append("below SMA50")
        return "; ".join(parts) if parts else "momentum sell"
