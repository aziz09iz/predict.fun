"""Market schedule helpers for predict.fun 5-minute Up/Down markets.

Every crypto 5m market is named ``<coin>-updown-5m-<close_ts>`` where
``close_ts`` is the UTC 5-minute boundary at which the market closes.  The
open time is simply ``close_ts - 300``.  Markets for BTC, ETH and BNB all
share the same aligned boundaries (e.g. ``btc-updown-5m-1794391800``).
"""
from __future__ import annotations

import time

from .config import COIN_SYMBOLS, ALL_COINS

WINDOW_SECONDS = 300
BASE_URL = "https://predict.fun"


def window_open(ts: float | int | None = None) -> int:
    """The open timestamp (start) of the current 5-minute window."""
    t = int(ts if ts is not None else time.time())
    return t - (t % WINDOW_SECONDS)


def window_close(ts: float | int | None = None) -> int:
    """The close timestamp of the current 5-minute window."""
    return window_open(ts) + WINDOW_SECONDS


def slug(coin: str, close_ts: int) -> str:
    coin = coin.lower()
    if coin not in COIN_SYMBOLS:
        raise ValueError(f"unknown coin: {coin}")
    return f"{coin}-updown-5m-{int(close_ts)}"


def market_url(coin: str, close_ts: int) -> str:
    return f"{BASE_URL}/market/{slug(coin, close_ts)}"


def human_ts(ts: float | int, utc: bool = True) -> str:
    """UTC ``HH:MM:SS`` for display."""
    import datetime

    dt = datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc)
    return dt.strftime("%H:%M:%S UTC")


__all__ = [
    "WINDOW_SECONDS",
    "ALL_COINS",
    "COIN_SYMBOLS",
    "window_open",
    "window_close",
    "slug",
    "market_url",
    "human_ts",
]