"""Tests for exchange-local completed daily candle selection."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from data.alpaca_client import AlpacaClient


@pytest.mark.parametrize(
    "as_of,expected_end",
    [
        ("2026-09-16T15:00:00+00:00", "2026-09-15T23:59:59-04:00"),
        ("2026-09-16T01:00:00+00:00", "2026-09-14T23:59:59-04:00"),
        ("2026-01-16T15:00:00+00:00", "2026-01-15T23:59:59-05:00"),
    ],
)
def test_request_uses_exchange_date_and_latest_page(as_of, expected_end):
    client = object.__new__(AlpacaClient)
    client.data_url = "https://data.alpaca.markets"
    client._request = AsyncMock(return_value={"bars": []})
    asyncio.run(client.get_completed_daily_bars("QCOM", as_of=datetime.fromisoformat(as_of)))
    params = client._request.await_args.kwargs["params"]
    assert params["end"] == expected_end
    assert params["sort"] == "desc"
    assert params["limit"] == 30


def test_bars_sorted_and_current_session_excluded():
    client = object.__new__(AlpacaClient)
    client.data_url = "https://data.alpaca.markets"
    client._request = AsyncMock(
        return_value={
            "bars": [
                {
                    "t": f"2026-09-{day}T04:00:00Z",
                    "o": 100,
                    "h": 101,
                    "l": 99,
                    "c": 100,
                    "v": 1000,
                }
                for day in [16, 15, 14]
            ],
            "next_page_token": "older-history",
        }
    )
    bars = asyncio.run(
        client.get_completed_daily_bars(
            "QCOM", as_of=datetime.fromisoformat("2026-09-16T15:00:00+00:00")
        )
    )
    assert list(bars.index.day) == [14, 15]
    client._request.assert_awaited_once()
