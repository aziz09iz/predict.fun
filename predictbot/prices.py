"""Real-time price feed from Binance WebSocket.

predict.fun resolves crypto Up/Down markets against the Chainlink BTC/USDT
(ETH/USDT, BNB/USDT) stream, which is itself the mid of Binance's top-of-book
``b + a / 2``.  So Binance's ``bookTicker`` mid is the exact price the market
closes on — we reuse it directly instead of needing a predict.fun API key.

Each received tick is (receive-time, mid) stamped into a bounded history per
coin so the engine can reconstruct the price *at* a given wall-clock instant
(the market open, open+1s, and close boundaries).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Any

from .config import COIN_SYMBOLS

log = logging.getLogger("predictbot.prices")

_HISTORY_MAX = 6000          # ~5+ minutes of ticks at ~1-2 Hz
_TRIM_CHUNK = 2000


class PriceFeed:
    # Endpoints are tried in order and rotated on repeated failures, so a
    # geo-blocked or blocked-port region still falls back to an alternate host.
    _FALLBACK_BASES = [
        "wss://data-stream.binance.vision/stream",
        "wss://stream.binance.com:443/stream",
        "wss://stream.binance.com:9443/stream",
    ]

    def __init__(self, ws_url: str, coins: list[str] | None = None):
        coins = coins or list(COIN_SYMBOLS)
        self.ws_url = ws_url.rstrip("/")
        self.coins = [c.lower() for c in coins if c.lower() in COIN_SYMBOLS]
        self._bases: list[str] = []
        for base in [self.ws_url, *self._FALLBACK_BASES]:
            if base not in self._bases:
                self._bases.append(base)
        self._base_idx = 0
        self._mid: dict[str, float] = {c: 0.0 for c in self.coins}
        self._last_ts: dict[str, float] = {c: 0.0 for c in self.coins}
        self._history: dict[str, list[tuple[float, float]]] = defaultdict(list)
        self._by_symbol = {COIN_SYMBOLS[c].lower(): c for c in self.coins}
        self._stop = asyncio.Event()
        self._connected = False

    # ── read API ────────────────────────────────────────────────────────────
    def latest(self, coin: str) -> float:
        return self._mid.get(coin.lower(), 0.0)

    def last_update(self, coin: str) -> float:
        return self._last_ts.get(coin.lower(), 0.0)

    def all_prices(self) -> dict[str, float]:
        return {c: self._mid[c] for c in self.coins}

    def price_at(self, coin: str, ts: float) -> float:
        """The mid price as of ``ts`` (newest tick at or before it)."""
        coin = coin.lower()
        hist = self._history.get(coin)
        if not hist:
            return self._mid.get(coin, 0.0)
        for i in range(len(hist) - 1, -1, -1):
            t, p = hist[i]
            if t <= ts:
                return p
        return hist[0][1]

    # ── connection ──────────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._connected

    def _streams_url(self, base: str) -> str:
        streams = "/".join(f"{COIN_SYMBOLS[c].lower()}@bookTicker" for c in self.coins)
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}streams={streams}"

    def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        data: dict[str, Any] = msg.get("data")
        if not isinstance(data, dict):
            return
        symbol = str(data.get("s", "")).lower()
        coin = self._by_symbol.get(symbol)
        if coin is None:
            return
        try:
            bid = float(data["b"])
            ask = float(data["a"])
        except (KeyError, ValueError, TypeError):
            return
        if bid <= 0 or ask <= 0:
            return
        mid = (bid + ask) / 2.0
        now = time.time()
        self._mid[coin] = mid
        self._last_ts[coin] = now
        hist = self._history[coin]
        hist.append((now, mid))
        if len(hist) > _HISTORY_MAX:
            del hist[:_TRIM_CHUNK]

    async def run(self) -> None:
        import websockets
        from websockets.exceptions import ConnectionClosed

        backoff = 1.0
        self._stop.clear()
        while not self._stop.is_set():
            base = self._bases[self._base_idx % len(self._bases)]
            url = self._streams_url(base)
            log.info("Price feed connecting to %s", url)
            try:
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20, close_timeout=5, max_size=2**20
                ) as ws:
                    self._connected = True
                    backoff = 1.0
                    log.info("Price feed connected")
                    async for raw in ws:
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", "replace")
                        self._handle(raw)
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as exc:
                self._connected = False
                log.warning("Price feed closed (%s); reconnecting in %.0fs", exc.code or "?", backoff)
            except Exception as exc:  # network/SSL/transient — rotate host and retry
                self._connected = False
                log.warning("Price feed error on %s (%s); reconnecting in %.0fs", base, exc, backoff)
                self._base_idx += 1
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
        self._connected = False

    def stop(self) -> None:
        self._stop.set()