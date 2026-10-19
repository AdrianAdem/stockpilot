"""Embedded HTTP server must preserve the orchestrator's signal handlers."""

import asyncio
import signal
from unittest.mock import patch

from dashboard.app import start_dashboard


def test_embedded_dashboard_preserves_process_signal_handlers():
    async def check():
        original = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

        async def serve(server, sockets=None):
            assert {sig: signal.getsignal(sig) for sig in original} == original

        with patch("uvicorn.Server._serve", serve):
            await start_dashboard(None)

    asyncio.run(check())
