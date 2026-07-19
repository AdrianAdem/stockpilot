import asyncio
from datetime import datetime

import httpx
import structlog

logger = structlog.get_logger()

SERIES = {
    "CPIAUCSL": "CPI",
    "FEDFUNDS": "Fed Funds Rate",
    "UNRATE": "Unemployment Rate",
    "GDP": "GDP",
    "VIXCLS": "VIX",
    "T10Y2Y": "Yield Curve (10Y-2Y)",
    "DGS10": "10Y Treasury",
}


class FredClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.stlouisfed.org/fred"
        self._client = httpx.AsyncClient(timeout=15)
        self._cache: dict[str, dict] = {}
        self._last_update: datetime | None = None

    async def close(self):
        await self._client.aclose()

    async def get_series(self, series_id: str) -> dict | None:
        try:
            resp = await self._client.get(
                f"{self.base_url}/series/observations",
                params={
                    "series_id": series_id,
                    "api_key": self.api_key,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": 5,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            obs = data.get("observations", [])
            if obs:
                latest = obs[0]
                prev = obs[1] if len(obs) > 1 else None
                val = float(latest["value"]) if latest["value"] != "." else None
                prev_val = float(prev["value"]) if prev and prev["value"] != "." else None
                return {
                    "value": val,
                    "date": latest["date"],
                    "previous": prev_val,
                    "change": round(val - prev_val, 4) if val and prev_val else None,
                    "name": SERIES.get(series_id, series_id),
                }
        except Exception as e:
            logger.warning("fred_error", series=series_id, error=str(e))
        return None

    async def update(self):
        if self._last_update and (datetime.utcnow() - self._last_update).total_seconds() < 3600:
            return
        for series_id in SERIES:
            data = await self.get_series(series_id)
            if data:
                self._cache[series_id] = data
            await asyncio.sleep(0.5)
        self._last_update = datetime.utcnow()
        logger.info("fred_updated", series_count=len(self._cache))

    def get_macro_summary(self) -> dict:
        summary = {}
        for series_id, data in self._cache.items():
            summary[SERIES.get(series_id, series_id)] = data
        return summary

    def get_vix(self) -> float | None:
        vix = self._cache.get("VIXCLS")
        return vix["value"] if vix else None

    def is_macro_bearish(self) -> bool:
        vix = self.get_vix()
        if vix and vix > 25:
            return True
        yield_curve = self._cache.get("T10Y2Y")
        if yield_curve and yield_curve["value"] and yield_curve["value"] < 0:
            return True
        return False
