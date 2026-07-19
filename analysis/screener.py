import structlog

logger = structlog.get_logger()


class Screener:
    """Liquidity and price filter applied before any strategy runs.

    Volume is measured on the IEX feed, which carries roughly 2-3% of
    consolidated US volume — thresholds are calibrated accordingly.
    """

    # NOTE: min_volume is measured on the IEX feed (~2-3% of consolidated US
    # volume). 200k IEX ≈ ~8-10M real daily volume — solidly liquid. The old
    # 1M IEX default ≈ ~40M real and let only ~29 mega-caps through, starving
    # the signal source. See SCREENER_MIN_VOLUME in .env.
    def __init__(self, min_volume: int = 200_000, min_price: float = 5.0):
        self.min_volume = min_volume
        self.min_price = min_price

    def filter_universe(self, universe: list[str], tech_data: dict) -> list[str]:
        """Return the subset of `universe` that is liquid and priced high enough."""
        filtered = []
        for symbol in universe:
            td = tech_data.get(symbol, {})
            price = td.get("price")
            avg_vol = td.get("avg_volume_20d")

            if not price or not avg_vol:
                continue
            if price < self.min_price:
                continue
            if avg_vol < self.min_volume:
                continue

            filtered.append(symbol)

        logger.info("screener_filtered", before=len(universe), after=len(filtered))
        return filtered
