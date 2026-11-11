"""Market schedule helpers for predict.fun 5-minute Up/Down markets.

Every crypto 5m market is named ``<coin>-updown-5m-<open_ts>`` where ``open_ts``
is the UTC 5-minute boundary the market **starts** at (verified against the live
market data: a market's embedded ``startPricePublishTime`` equals its slug
timestamp).  The close time is therefore ``open_ts + 300``.

BTC, ETH and BNB markets all share the same aligned boundaries
(e.g. ``btc-updown-5m-1794397200`` = 11:40:00Z open, 11:45:00Z close).
"""
from __future__ import annotations

import datetime
import time

from .config import COIN_SYMBOLS

WINDOW_SECONDS = 300
BASE_URL = "https://predict.fun"


def window_open(ts: float | int | None = None) -> int:
    """The open timestamp (start) of the current 5-minute window."""
    t = int(ts if ts is not None else time.time())
    return t - (t % WINDOW_SECONDS)


def window_close(ts: float | int | None = None) -> int:
    """The close timestamp of the current 5-minute window."""
    return window_open(ts) + WINDOW_SECONDS


def slug(coin: str, open_ts: int) -> str:
    """Build a predict.fun market slug from the open (start) timestamp."""
    coin = coin.lower()
    if coin not in COIN_SYMBOLS:
        raise ValueError(f"unknown coin: {coin}")
    return f"{coin}-updown-5m-{int(open_ts)}"


def market_url(coin: str, open_ts: int) -> str:
    return f"{BASE_URL}/market/{slug(coin, open_ts)}"


def human_ts(ts: float | int) -> str:
    """UTC ``HH:MM:SS`` for display."""
    dt = datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc)
    return dt.strftime("%H:%M:%S UTC")


__all__ = [
    "WINDOW_SECONDS",
    "COIN_SYMBOLS",
    "window_open",
    "window_close",
    "slug",
    "market_url",
    "human_ts",
]