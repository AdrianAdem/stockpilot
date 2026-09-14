"""Order mutations must not be duplicated after ambiguous transport failures."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from data.alpaca_client import AlpacaClient


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_ambiguous_order_response_is_not_retried(method):
    client = object.__new__(AlpacaClient)
    client.headers = {}
    client._client = SimpleNamespace(request=AsyncMock(side_effect=httpx.ReadTimeout("lost")))
    with patch("data.alpaca_client.asyncio.sleep", new_callable=AsyncMock), pytest.raises(httpx.ReadTimeout):
        asyncio.run(client._request(method, "https://paper-api.alpaca.markets/v2/orders"))
    assert client._client.request.await_count == 1


def test_exhausted_rate_limit_is_failure_not_empty_success():
    client = object.__new__(AlpacaClient)
    client.headers = {}
    client._client = SimpleNamespace(request=AsyncMock(return_value=SimpleNamespace(
        status_code=429, headers={"Retry-After": "0"},
    )))
    with patch("data.alpaca_client.asyncio.sleep", new_callable=AsyncMock), pytest.raises(RuntimeError, match="exhausted"):
        asyncio.run(client._request("GET", "https://paper-api.alpaca.markets/v2/orders"))
    assert client._client.request.await_count == 3
