"""Complete order history is required for exit reconciliation."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from data.alpaca_client import AlpacaClient


def test_history_pages_by_id_with_identical_submission_times():
    client = object.__new__(AlpacaClient)
    client.base_url = "https://paper-api.alpaca.markets"
    first = [{"id": str(i), "submitted_at": "2026-09-01T10:00:00Z"} for i in range(500)]
    client._request = AsyncMock(side_effect=[first, [{"id": "last"}]])
    result = asyncio.run(client.get_orders("closed"))
    assert len(result) == 501
    assert client._request.await_args.kwargs["params"]["before_order_id"] == "499"


def test_history_rejects_repeated_page_instead_of_duplicate_fills():
    client = object.__new__(AlpacaClient)
    client.base_url = "https://paper-api.alpaca.markets"
    page = [{"id": str(i)} for i in range(500)]
    client._request = AsyncMock(return_value=page)
    with pytest.raises(RuntimeError, match="did not advance"):
        asyncio.run(client.get_orders("closed"))


def test_history_deduplicates_overlapping_pages():
    client = object.__new__(AlpacaClient)
    client.base_url = "https://paper-api.alpaca.markets"
    page = [{"id": str(i)} for i in range(500)]
    client._request = AsyncMock(side_effect=[page, [{"id": "499"}, {"id": "new"}]])
    result = asyncio.run(client.get_orders("closed"))
    assert len(result) == 501
    assert len({o["id"] for o in result}) == 501
