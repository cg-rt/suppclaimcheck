"""Helpers for tests that run async code."""

import asyncio
import sys


def run_async(coro):
    """Runs a coroutine to completion. psycopg's async mode needs a selector event loop on Windows."""
    if sys.platform == "win32":
        return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(coro)
