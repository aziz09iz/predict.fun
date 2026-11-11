"""Paper-trading engine.

Strategy (as specified):
    * wait ``entry_delay_seconds`` (default 1) after the 5-minute market opens,
    * if the live price is above the open price -> enter UP,
    * if it is below the open price -> enter DOWN.

Settlement mirrors predict.fun's rules: UP wins when the close price is above
the open price, DOWN wins when it is below, and an exact tie is a push.

Paper PnL is a transparent "coin-flip" model: a winning stake returns
``stake * win_multiplier``, a losing stake returns nothing.  The default
``win_multiplier = 2.0`` makes it EV-neutral, so the accumulated balance is a
pure measure of the strategy's directional skill.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

from . import market as mkt
from .config import COIN_SYMBOLS
from .db import Database
from .prices import PriceFeed

log = logging.getLogger("predictbot.engine")

EventHandler = Callable[[str, dict[str, Any]], Any]


def settle_result(direction: str | None, open_price: float, close_price: float,
                  stake: float, mult: float) -> tuple[str, float]:
    if open_price <= 0 or close_price <= 0:
        return "UNRESOLVED", 0.0
    if close_price > open_price:
        won = "UP"
    elif close_price < open_price:
        won = "DOWN"
    else:
        return "PUSH", 0.0
    if direction == won:
        return "WIN", stake * (mult - 1.0)
    return "LOSS", -stake


class Engine:
    def __init__(self, cfg: dict[str, Any], db: Database, feed: PriceFeed):
        self.cfg = cfg
        self.db = db
        self.feed = feed
        self.on_event: Optional[EventHandler] = None
        self._stop = asyncio.Event()
        self._pending: dict[tuple[str, int], tuple[int, float]] = {}
        self._captured: dict[int, set[str]] = {}
        self._open: dict[int, dict[str, Any]] = {}

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Load any OPEN trades left over from a previous run."""
        for row in self.db.open_trades():
            self._open[row["id"]] = dict(row)
        stale = [t for t in self._open.values() if t["close_ts"] <= int(time.time())]
        for trade in stale:
            log.warning("Expiring unresolved trade #%s (%s %s)", trade["id"], trade["coin"], trade["market_slug"])
            self.db.expire_trade(trade["id"])
            self._open.pop(trade["id"], None)
        if self._open:
            log.info("Resumed %d open trade(s) for settlement", len(self._open))

    async def run(self) -> None:
        self._stop.clear()
        await self.start()
        log.info("Engine started (stake=%.2f, delay=%.1fs)", self.cfg["trading"]["stake"], self.cfg["entry_delay_seconds"])
        while not self._stop.is_set():
            now = time.time()
            try:
                await self._tick(now)
            except Exception as exc:  # keep the loop alive no matter what
                log.exception("Engine tick error: %s", exc)
            await asyncio.sleep(0.2)

    def stop(self) -> None:
        self._stop.set()

    # ── tick ────────────────────────────────────────────────────────────────
    async def _tick(self, now: float) -> None:
        open_ts = mkt.window_open(now)
        close_ts = open_ts + mkt.WINDOW_SECONDS
        await self._capture_and_entry(now, open_ts, close_ts)
        await self._settle(now)
        self._prune(open_ts)

    async def _capture_and_entry(self, now: float, open_ts: int, close_ts: int) -> None:
        coins = [c for c in self.cfg["trading"]["coins"] if c in COIN_SYMBOLS]
        captured = self._captured.setdefault(close_ts, set())

        # Phase 1: record the "open price" the first instant we see a window.
        for coin in coins:
            key = (coin, close_ts)
            if key in self._pending or coin in captured:
                continue
            open_price = self.feed.price_at(coin, open_ts)
            if open_price <= 0:
                continue  # feed still warming up
            captured.add(coin)
            self._pending[key] = (open_ts, open_price)

        # Phase 2: decide direction at open + delay, then open the paper trade.
        delay = float(self.cfg["entry_delay_seconds"])
        stake = float(self.cfg["trading"]["stake"])
        mult = float(self.cfg["payout"]["win_multiplier"])
        for coin in coins:
            key = (coin, close_ts)
            if key not in self._pending:
                continue
            open_ts_p, open_price = self._pending[key]
            if now < open_ts_p + delay:
                continue
            if not self._can_open(stake):
                self._pending.pop(key, None)
                log.info("Skipping %s: balance limit reached", coin)
                continue
            entry_price = self.feed.price_at(coin, open_ts_p + delay)
            direction = None if entry_price == open_price or entry_price <= 0 else (
                "UP" if entry_price > open_price else "DOWN"
            )
            if direction is None:
                self._pending.pop(key, None)
                log.info("Skipping %s 5m: price equal to open at +%.1fs", coin, delay)
                continue
            self._pending.pop(key, None)
            self._place_trade(coin, close_ts, open_ts_p, open_price, entry_price, direction, stake, mult)

    def _can_open(self, stake: float) -> bool:
        if not self.cfg["trading"]["enabled"]:
            return False
        balance = self.db.get_balance()
        exposure = sum(float(t["stake"]) for t in self._open.values())
        return (balance - exposure - stake) >= float(self.cfg["trading"]["min_balance"])

    def _place_trade(self, coin, close_ts, open_ts, open_price, entry_price, direction, stake, mult) -> None:
        slug = mkt.slug(coin, close_ts)
        trade_id = self.db.insert_trade(
            coin=coin, market_slug=slug, open_ts=open_ts, close_ts=close_ts,
            open_price=open_price, entry_price=entry_price, direction=direction,
            stake=stake, odds=mult,
        )
        trade = {
            "id": trade_id, "coin": coin, "market_slug": slug,
            "open_ts": open_ts, "close_ts": close_ts,
            "open_price": open_price, "entry_price": entry_price,
            "direction": direction, "stake": stake, "odds": mult,
        }
        self._open[trade_id] = trade
        log.info("ENTRY %s %s %s open=%.2f entry=%.2f", coin.upper(), direction, slug, open_price, entry_price)
        self._emit("trade", dict(trade))

    async def _settle(self, now: float) -> None:
        for trade_id, trade in list(self._open.items()):
            if now < trade["close_ts"]:
                continue
            coin = trade["coin"]
            close_price = self.feed.price_at(coin, trade["close_ts"])
            if close_price <= 0:
                close_price = self.feed.latest(coin)
            self._resolve(trade_id, trade, close_price)

    def settle_now(self) -> None:
        """Force-settle every open trade at the current live price (used by /settle)."""
        for trade_id, trade in list(self._open.items()):
            close_price = self.feed.latest(trade["coin"])
            self._resolve(trade_id, trade, close_price)

    def _resolve(self, trade_id: int, trade: dict[str, Any], close_price: float) -> None:
        coin = trade["coin"]
        outcome, pnl = settle_result(
            trade["direction"], float(trade["open_price"]), close_price,
            float(trade["stake"]), float(trade["odds"]),
        )
        self.db.settle_trade(trade_id, close_price, outcome, pnl)
        balance = self.db.get_balance() + pnl
        self.db.set_balance(balance)
        self.db.record_balance_event(
            "settle", pnl, balance, trade_id=trade_id,
            note=f"{coin.upper()} {trade['direction']} 5m {outcome}",
        )
        self._open.pop(trade_id, None)
        result = {**trade, "close_price": close_price, "outcome": outcome, "pnl": pnl}
        log.info(
            "SETTLE %s %s %s %s pnl=%.2f balance=%.2f",
            coin.upper(), trade["direction"], outcome, trade["market_slug"], pnl, balance,
        )
        self._emit("settle", result)

    def remove_open(self, trade_id: int) -> None:
        """Drop a trade from the in-memory open set (after external settlement)."""
        self._open.pop(trade_id, None)

    def _prune(self, open_ts: int) -> None:
        for close_ts in list(self._captured):
            if close_ts < open_ts - mkt.WINDOW_SECONDS:
                self._captured.pop(close_ts, None)
        for key in list(self._pending):
            if key[1] < open_ts - mkt.WINDOW_SECONDS:
                self._pending.pop(key, None)

    # ── helpers ─────────────────────────────────────────────────────────────
    def exposure(self) -> float:
        return sum(float(t["stake"]) for t in self._open.values())

    def open_trades(self) -> list[dict[str, Any]]:
        return [dict(t) for t in self._open.values()]

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.on_event is not None:
            asyncio.create_task(self.on_event(event_type, payload))