"""Sliding-window rate limiter for the predict.fun public API.

Basic-tier limits (from the developer console):
    general  — 250 requests/minute  (markets, orderbooks, account reads)
    trading  — 500 requests/minute, with a 40 requests/second burst
    search   — 120 requests/minute  (free-text search)

The limiter is non-blocking under normal load (this bot makes only a handful of
``general`` calls per 5-minute window), but it throttles bursts and — combined
with 429 ``Retry-After`` handling — keeps the key safely inside its tier limits.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Iterable

# category -> [(max_requests, window_seconds), ...]
Rule = Iterable[tuple[int, float]]


class RateLimiter:
    def __init__(self, rules: dict[str, Rule]):
        self._rules = {k: list(v) for k, v in rules.items()}
        self._times: dict[tuple[str, float], deque] = defaultdict(deque)

    async def acquire(self, category: str) -> None:
        """Wait (if necessary) until one request slot is available."""
        rules = self._rules.get(category)
        if not rules:
            return
        while True:
            now = time.monotonic()
            wait = 0.0
            blocked = False
            for max_req, window in rules:
                dq = self._times[(category, window)]
                while dq and now - dq[0] >= window:
                    dq.popleft()
                if len(dq) >= max_req:
                    wait = max(wait, window - (now - dq[0]))
                    blocked = True
            if not blocked:
                for _, window in rules:
                    self._times[(category, window)].append(now)
                return
            await asyncio.sleep(max(wait, 0.001))