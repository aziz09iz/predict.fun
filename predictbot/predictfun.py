"""Predict.fun public market-data client (no API key).

predict.fun's 5-minute crypto markets resolve against Chainlink's BTC/USDT
(ETH/USDT, BNB/USDT) stream.  The authoritative open/close prices for a market
are embedded in the public market page as ``marketData`` → ``startPrice`` /
``endPrice``.  This module fetches that page and extracts those exact values so
the paper bot settles against predict.fun's real numbers rather than a local
Binance approximation.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

from .market import slug

log = logging.getLogger("predictbot.predictfun")

BASE_URL = "https://predict.fun"
USER_AGENT = "Mozilla/5.0 (PredictFun paper bot)"


class PredictFun:
    def __init__(self, base: str = BASE_URL, timeout: float = 15.0):
        self.base = base.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )

    async def market_data(self, coin: str, open_ts: int) -> Optional[dict]:
        """Return ``{symbol, provider, start_price, end_price, resolved}``.

        ``end_price`` is ``None`` while a market is still running; ``start_price``
        becomes available as soon as the market opens.  Returns ``None`` on any
        network/parse failure so callers can fall back gracefully.
        """
        url = f"{self.base}/market/{slug(coin, open_ts)}"
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
        except Exception as exc:  # network / HTTP error — caller falls back
            log.debug("predict.fun fetch failed %s: %s", url, exc)
            return None
        return self._extract(resp.text)

    @staticmethod
    def _extract(html: str) -> Optional[dict]:
        i = html.find("marketData")
        if i == -1:
            return None
        b = html.find("[", i)
        e = html.find("]", b)
        if b == -1 or e == -1:
            return None
        # Inside the Next.js RSC payload the JSON quotes are escaped (\"),
        # so undo that one level before parsing.
        fixed = html[b:e + 1].replace('\\"', '"')
        try:
            arr = json.loads(fixed)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(arr, list) or not arr:
            return None
        d = arr[0]
        return {
            "symbol": d.get("priceFeedSymbol"),
            "provider": d.get("priceFeedProvider"),
            "start_price": d.get("startPrice"),
            "end_price": d.get("endPrice"),
            "start_time": d.get("startPricePublishTime"),
            "end_time": d.get("endPricePublishTime"),
            "resolved": d.get("endPrice") is not None,
        }

    async def aclose(self) -> None:
        await self._client.aclose()