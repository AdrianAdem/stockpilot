import asyncio
from datetime import datetime, timedelta

import httpx
import pandas as pd
import structlog

from config.settings import AlpacaConfig

logger = structlog.get_logger()

MAX_RETRIES = 3
RATE_LIMIT_DELAY = 0.3  # ~200 req/min


class AlpacaClient:
    def __init__(self, config: AlpacaConfig):
        if "paper-api.alpaca.markets" not in config.base_url:
            raise RuntimeError("SAFETY: Only paper trading allowed!")

        self.base_url = config.base_url.rstrip("/")
        self.data_url = "https://data.alpaca.markets"
        self.headers = {
            "APCA-API-KEY-ID": config.api_key,
            "APCA-API-SECRET-KEY": config.secret_key,
        }
        self._client = httpx.AsyncClient(timeout=30)

    async def close(self):
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs) -> dict | list:
        for attempt in range(MAX_RETRIES):
            try:
                await asyncio.sleep(RATE_LIMIT_DELAY)
                resp = await self._client.request(
                    method, url, headers=self.headers, **kwargs
                )
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", "5"))
                    logger.warning("rate_limited", wait=wait)
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                # DELETE (cancel order) returns 204 No Content — no JSON body.
                if resp.status_code == 204 or not resp.content:
                    return {}
                return resp.json()
            except httpx.HTTPStatusError as e:
                logger.error("alpaca_http_error", status=e.response.status_code,
                             url=url, attempt=attempt + 1)
                if attempt == MAX_RETRIES - 1:
                    raise
                await asyncio.sleep(2 ** attempt)
            except httpx.RequestError as e:
                logger.error("alpaca_request_error", error=str(e), attempt=attempt + 1)
                if attempt == MAX_RETRIES - 1:
                    raise
                await asyncio.sleep(2 ** attempt)
        return {}

    async def get_account(self) -> dict:
        return await self._request("GET", f"{self.base_url}/v2/account")

    async def get_positions(self) -> list[dict]:
        return await self._request("GET", f"{self.base_url}/v2/positions")

    async def get_position(self, symbol: str) -> dict | None:
        try:
            return await self._request("GET", f"{self.base_url}/v2/positions/{symbol}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    def _default_start(self, timeframe: str, limit: int) -> str:
        """Compute a start date so the endpoint returns ~limit bars.
        Without `start`, the bars endpoint only returns the most recent bar."""
        if timeframe == "1Day":
            cal_days = int(limit * 1.5) + 10  # weekends/holidays headroom
        elif timeframe == "1Hour":
            cal_days = int(limit / 6) + 5
        else:
            cal_days = limit + 5
        return (datetime.utcnow() - timedelta(days=cal_days)).strftime("%Y-%m-%d")

    async def get_bars(self, symbol: str, timeframe: str = "1Day",
                       limit: int = 200, start: str | None = None) -> pd.DataFrame:
        params = {
            "timeframe": timeframe, "limit": limit, "feed": "iex",
            "start": start or self._default_start(timeframe, limit),
        }
        data = await self._request(
            "GET", f"{self.data_url}/v2/stocks/{symbol}/bars", params=params
        )
        return self._bars_to_df(data.get("bars", []))

    def _bars_to_df(self, bars: list[dict]) -> pd.DataFrame:
        if not bars:
            return pd.DataFrame()
        df = pd.DataFrame(bars)
        df["t"] = pd.to_datetime(df["t"])
        df = df.rename(columns={
            "t": "timestamp", "o": "open", "h": "high",
            "l": "low", "c": "close", "v": "volume",
        })
        return df.set_index("timestamp").sort_index()

    async def get_bars_multi(self, symbols: list[str], timeframe: str = "1Day",
                             limit: int = 200) -> dict[str, pd.DataFrame]:
        """Fetch bars for many symbols in batched multi-symbol requests.

        Collapses N per-symbol calls into ceil(N/200) calls — the real fix for
        the rate limit (200 req/min), since concurrency can't beat that ceiling.
        """
        result: dict[str, pd.DataFrame] = {}
        # Multi-symbol endpoint caps the symbol list per request; chunk to stay safe.
        CHUNK = 100
        for i in range(0, len(symbols), CHUNK):
            chunk = symbols[i:i + CHUNK]
            params = {
                "symbols": ",".join(chunk),
                "timeframe": timeframe,
                # Multi-symbol limit is total points across all symbols, so scale
                # it up and paginate via next_page_token below.
                "limit": min(limit * len(chunk), 10000),
                "feed": "iex",
                "start": self._default_start(timeframe, limit),
            }
            next_token = None
            while True:
                if next_token:
                    params["page_token"] = next_token
                data = await self._request(
                    "GET", f"{self.data_url}/v2/stocks/bars", params=params
                )
                bars_by_symbol = data.get("bars", {}) or {}
                for sym, bars in bars_by_symbol.items():
                    df = self._bars_to_df(bars)
                    if sym in result and not result[sym].empty:
                        result[sym] = pd.concat([result[sym], df]).sort_index()
                    else:
                        result[sym] = df
                next_token = data.get("next_page_token")
                if not next_token:
                    break
        return result

    async def get_latest_quote(self, symbol: str) -> dict:
        data = await self._request(
            "GET", f"{self.data_url}/v2/stocks/{symbol}/quotes/latest",
            params={"feed": "iex"},
        )
        return data.get("quote", {})

    async def get_latest_trade(self, symbol: str) -> dict:
        data = await self._request(
            "GET", f"{self.data_url}/v2/stocks/{symbol}/trades/latest",
            params={"feed": "iex"},
        )
        return data.get("trade", {})

    async def get_market_clock(self) -> dict:
        return await self._request("GET", f"{self.base_url}/v2/clock")

    async def submit_order(self, symbol: str, qty: int, side: str,
                           order_type: str = "market",
                           time_in_force: str = "day",
                           limit_price: float | None = None,
                           stop_price: float | None = None,
                           take_profit: float | None = None,
                           stop_loss_price: float | None = None) -> dict:
        body = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side.lower(),
            "type": order_type,
            "time_in_force": time_in_force,
        }
        if limit_price:
            body["limit_price"] = str(limit_price)
        if stop_price:
            body["stop_price"] = str(stop_price)

        # Bracket order: entry + attached take-profit + stop-loss.
        # NOT "oco" — OCO has no entry leg (it's exit-only on an existing position).
        if take_profit and stop_loss_price:
            body["order_class"] = "bracket"
            body["take_profit"] = {"limit_price": str(take_profit)}
            body["stop_loss"] = {"stop_price": str(stop_loss_price)}

        logger.info("submitting_order", order=body)
        return await self._request("POST", f"{self.base_url}/v2/orders", json=body)

    async def get_orders(self, status: str = "open") -> list[dict]:
        return await self._request(
            "GET", f"{self.base_url}/v2/orders", params={"status": status}
        )

    async def cancel_order(self, order_id: str) -> None:
        await self._request("DELETE", f"{self.base_url}/v2/orders/{order_id}")
        logger.info("order_cancelled", order_id=order_id)

    async def replace_order(self, order_id: str, stop_price: float | None = None,
                            limit_price: float | None = None,
                            qty: int | None = None) -> dict:
        """Atomically replace an existing order (PATCH) — Alpaca swaps it
        server-side with no window where the position is unprotected."""
        body = {}
        if stop_price is not None:
            body["stop_price"] = str(stop_price)
        if limit_price is not None:
            body["limit_price"] = str(limit_price)
        if qty is not None:
            body["qty"] = str(qty)
        result = await self._request(
            "PATCH", f"{self.base_url}/v2/orders/{order_id}", json=body
        )
        logger.info("order_replaced", order_id=order_id, **body)
        return result

    async def cancel_all_orders(self) -> None:
        await self._request("DELETE", f"{self.base_url}/v2/orders")
        logger.info("all_orders_cancelled")

    async def get_news(self, symbols: list[str] | None = None,
                       limit: int = 50) -> list[dict]:
        params = {"limit": limit}
        if symbols:
            params["symbols"] = ",".join(symbols[:50])
        data = await self._request(
            "GET", f"{self.data_url}/v1beta1/news", params=params
        )
        # Endpoint returns {"news": [...], "next_page_token": ...} — unwrap the list.
        return data.get("news", []) if isinstance(data, dict) else []

    async def is_paper(self) -> bool:
        account = await self.get_account()
        return account.get("account_number", "").startswith("PA")

    async def verify_paper_account(self):
        account = await self.get_account()
        is_paper = "paper-api" in self.base_url
        logger.info(
            "account_verified",
            paper=is_paper,
            equity=account.get("equity"),
            buying_power=account.get("buying_power"),
            account_number=account.get("account_number"),
        )
        if not is_paper:
            raise RuntimeError("SAFETY: Not a paper trading account!")
        return account
