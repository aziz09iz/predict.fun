"""Predict.fun market-data client.

Two independent sources are combined:

* **Public page scrape** (no key) — ``market_data()`` reads the authoritative
  Chainlink ``startPrice``/``endPrice`` embedded in ``predict.fun/market/<slug>``.
  This is enough for exact settlement.

* **Authenticated REST** (``x-api-key``) — ``market_quote()`` / ``market_detail()`` /
  ``fill_for()`` read the live order book top-of-book (best bid/ask) and fee so the
  paper bot can fill "like a real" order instead of a flat odds model.

Fill convention (from the API reference): the quote is expressed against the *Yes*
outcome (indexSet=1). To buy the complementary *No* side: ``No ask = 1 - Yes bid``
and ``No bid = 1 - Yes ask`` (same sizes, round to 6 decimals).
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

from .market import slug

log = logging.getLogger("predictbot.predictfun")

BASE_URL = "https://predict.fun"
API_BASE = "https://api.predict.fun"
USER_AGENT = "Mozilla/5.0 (PredictFun paper bot)"


class PredictFun:
    def __init__(self, base: str = BASE_URL, api_key: str = "", api_base: str = API_BASE,
                 timeout: float = 15.0):
        self.base = base.rstrip("/")
        self.api_key = (api_key or "").strip()
        self.api_base = api_base.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        # Separate client so the secret header never touches the public-scrape path.
        self._api_client: Optional[httpx.AsyncClient] = None
        if self.api_key:
            self._api_client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
                follow_redirects=True,
                headers={"x-api-key": self.api_key, "User-Agent": USER_AGENT},
            )

    # ── public (no key) ─────────────────────────────────────────────────────
    async def market_data(self, coin: str, open_ts: int) -> Optional[dict]:
        """Authoritative ``startPrice``/``endPrice`` from the public market page."""
        url = f"{self.base}/market/{slug(coin, open_ts)}"
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
        except Exception as exc:
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

    # ── authenticated REST (x-api-key) ──────────────────────────────────────
    async def _api_get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        if self._api_client is None:
            return None
        url = f"{self.api_base}/v1{path}"
        try:
            resp = await self._api_client.get(url, params=params)
            resp.raise_for_status()
            body = resp.json()
            return body if isinstance(body, dict) else None
        except Exception as exc:
            log.debug("predict.fun API %s failed: %s", path, exc)
            return None

    async def market_quote(self, coin: str, open_ts: int) -> Optional[dict]:
        """Top-of-book for the Yes side; ``{best_bid, best_ask, update_ts}``."""
        body = await self._api_get(f"/markets/{slug(coin, open_ts)}/quote")
        if not body or not body.get("success"):
            return None
        data = body.get("data") or {}
        if "bestAsk" not in data and "bestBid" not in data:
            return None
        return {
            "market_id": data.get("marketId"),
            "update_ts": data.get("updateTimestampMs"),
            "best_bid": (data.get("bestBid") or {}).get("price"),
            "best_ask": (data.get("bestAsk") or {}).get("price"),
            "bid_size": (data.get("bestBid") or {}).get("size"),
            "ask_size": (data.get("bestAsk") or {}).get("size"),
        }

    async def market_detail(self, coin: str, open_ts: int) -> Optional[dict]:
        """Fee + variant data via the authenticated market endpoint."""
        body = await self._api_get(f"/markets/{slug(coin, open_ts)}")
        if not body or not body.get("success"):
            return None
        data = body.get("data") or {}
        return {
            "fee_rate_bps": data.get("feeRateBps"),
            "maker_fee_bps": data.get("makerFeeBps"),
            "outcomes": data.get("outcomes"),
            "start_price": (data.get("variantData") or {}).get("startPrice"),
            "end_price": (data.get("variantData") or {}).get("endPrice"),
            "symbol": (data.get("variantData") or {}).get("priceFeedSymbol"),
        }

    async def fill_for(self, coin: str, open_ts: int) -> Optional[dict]:
        """Resolve a realistic fill for ``coin`` right now.

        Returns ``{fill_up, fill_down, fee_bps, source}`` where ``fill_up`` is the
        all-in price-per-share to BUY Yes (Up) and ``fill_down`` to BUY No (Down).
        ``None`` means no quote is available and the caller should fall back.
        """
        q = await self.market_quote(coin, open_ts)
        if not q:
            return None
        bid = q.get("best_bid")
        ask = q.get("best_ask")
        if not isinstance(ask, (int, float)) or not isinstance(bid, (int, float)):
            return None
        fill_up = float(ask)
        fill_down = round(1.0 - float(bid), 6)
        if not (0.0 < fill_up < 1.0) or not (0.0 < fill_down < 1.0):
            return None
        fee_bps = None
        det = await self.market_detail(coin, open_ts)
        if det and isinstance(det.get("fee_rate_bps"), (int, float)):
            fee_bps = int(det["fee_rate_bps"])
        return {"fill_up": fill_up, "fill_down": fill_down, "fee_bps": fee_bps, "source": "api"}

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._api_client is not None:
            await self._api_client.aclose()