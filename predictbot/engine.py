"""Paper-trading engine.

Strategy (as specified):
    * wait ``entry_delay_seconds`` (default 1) after the 5-minute market opens,
    * if the live price is above the open price -> enter UP,
    * if it is below the open price -> enter DOWN.

Pricing is anchored to predict.fun's real data:
    * the authoritative **open** price is predict.fun's ``startPrice`` (Chainlink),
    * the authoritative **close** price is predict.fun's ``endPrice`` (published at close),
    * Binance book-ticker mid is used only for the live price and as a fallback.

Settlement mirrors predict.fun exactly: UP wins when close > open, DOWN wins
when close < open, and an exact tie is a push.
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
from .predictfun import PredictFun

log = logging.getLogger("predictbot.engine")

EventHandler = Callable[[str, dict[str, Any]], Any]

SETTLE_GRACE = 90   # seconds to wait for predict.fun endPrice after close
FETCH_RETRY = 5     # seconds between settlement re-fetches
FEED_STALE_SECONDS = 10  # skip entry if the Binance mid is older than this


def settle_result(direction: str | None, open_price: float, close_price: float,
                  stake: float, odds: float) -> tuple[str, float]:
    """Odds is the real fill multiplier ``1 / fill_price``.

    Win returns ``stake × (odds − 1)`` (i.e. ``shares − stake``), loss ``−stake``.
    """
    if open_price <= 0 or close_price <= 0:
        return "UNRESOLVED", 0.0
    if close_price > open_price:
        won = "UP"
    elif close_price < open_price:
        won = "DOWN"
    else:
        return "PUSH", 0.0
    if direction == won:
        return "WIN", stake * (odds - 1.0)
    return "LOSS", -stake


class Engine:
    def __init__(self, cfg: dict[str, Any], db: Database, feed: PriceFeed,
                 predfun: Optional[PredictFun] = None):
        self.cfg = cfg
        self.db = db
        self.feed = feed
        self.predfun = predfun or PredictFun()
        self.on_event: Optional[EventHandler] = None
        self._stop = asyncio.Event()
        self._pending: dict[tuple[str, int], dict[str, Any]] = {}
        self._captured: dict[int, set[str]] = {}
        self._open: dict[int, dict[str, Any]] = {}

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Resume any OPEN trades from a previous run."""
        for row in self.db.open_trades():
            self._open[row["id"]] = dict(row)
        deadline = int(time.time()) - SETTLE_GRACE
        stale = [t for t in self._open.values() if t["close_ts"] <= deadline]
        for trade in stale:
            log.warning("Expiring unresolved trade #%s (%s %s)", trade["id"], trade["coin"], trade["market_slug"])
            self.db.expire_trade(trade["id"])
            self._open.pop(trade["id"], None)
        if self._open:
            log.info("Resumed %d open trade(s) for settlement", len(self._open))

    async def run(self) -> None:
        self._stop.clear()
        await self.start()
        log.info(
            "Engine started (stake=%.4f, delay=%.1fs, coins=%s)",
            self.cfg["trading"]["stake"],
            self.cfg["entry_delay_seconds"],
            ",".join(self.cfg["trading"]["coins"]),
        )
        while not self._stop.is_set():
            now = time.time()
            try:
                await self._tick(now)
            except Exception as exc:
                log.exception("Engine tick error: %s", exc)
            await asyncio.sleep(0.2)

    def stop(self) -> None:
        self._stop.set()

    # ── tick ────────────────────────────────────────────────────────────────
    async def _tick(self, now: float) -> None:
        open_ts = mkt.window_open(now)
        close_ts = open_ts + mkt.WINDOW_SECONDS
        await self._capture_and_entry(now, open_ts, close_ts)
        await self._monitor_flips(now, open_ts, close_ts)
        await self._monitor_exits(now)
        await self._settle(now)
        self._prune(open_ts)

    async def _capture_and_entry(self, now: float, open_ts: int, close_ts: int) -> None:
        coins = [c for c in self.cfg["trading"]["coins"] if c in COIN_SYMBOLS]
        captured = self._captured.setdefault(close_ts, set())

        # Phase 1: record the open price the first instant we see a window.
        for coin in coins:
            key = (coin, close_ts)
            if key in self._pending or coin in captured:
                continue
            local_open = self.feed.price_at(coin, open_ts)
            if local_open <= 0:
                continue  # feed still warming up
            captured.add(coin)
            self._pending[key] = {"open_ts": open_ts, "local_open": local_open, "start_price": None}

        # Phase 2: decide direction at open + delay, then open the paper trade.
        # If the fill for that direction is more expensive than ``entry_max_price``,
        # keep re-quoting until ``entry_delay_max_seconds`` hoping for a cheaper
        # fill; at the deadline the entry is dropped (like an unfilled limit order).
        delay = float(self.cfg["entry_delay_seconds"])
        max_delay = max(delay, float(self.cfg.get("entry_delay_max_seconds", delay)))
        max_price = float(self.cfg.get("entry_max_price", 1.0))
        due = [k for k, p in self._pending.items() if now >= p["open_ts"] + delay]
        if not due:
            return

        # Enrich the authoritative startPrice once per window (skip on later ticks).
        need_enrich = [k for k in due if self._pending[k].get("start_price") is None]
        if need_enrich:
            await self._enrich_start_prices(need_enrich)

        # In flip mode, entries are handled continuously by _monitor_flips
        # (reverse on each confirmed cross), so skip the one-shot entry here.
        if self.cfg.get("flip_on_reversal", False):
            return

        # Decide direction once per coin (the signal is the open+delay instant).
        stake = float(self.cfg["trading"]["stake"])
        for key in list(due):
            info = self._pending.get(key)
            if info is None or "direction" in info:
                continue
            coin = key[0]
            open_price = info["start_price"] if info["start_price"] else info["local_open"]
            entry_price = self.feed.latest(coin)
            if open_price <= 0 or entry_price <= 0:
                self._pending.pop(key, None)
                continue
            # A stale feed collapses open & entry to the same snapshot, which
            # would otherwise surface as a bogus "price equal to open".
            stale_secs = time.time() - self.feed.last_update(coin)
            if stale_secs > FEED_STALE_SECONDS:
                log.warning("Skipping %s 5m: price feed stale (%.0fs)", coin, stale_secs)
                self._pending.pop(key, None)
                continue
            # Flat market: no usable signal.
            if entry_price == open_price:
                log.info("Skipping %s 5m: price equal to open", coin)
                self._pending.pop(key, None)
                continue
            if not self._can_open(stake):
                log.info("Skipping %s 5m: balance limit reached", coin)
                self._pending.pop(key, None)
                continue
            info["direction"] = "UP" if entry_price > open_price else "DOWN"
            info["fill"] = None
            info["_fill_at"] = 0.0  # fetch the first quote immediately

        # Re-quote fills at most once per second while any coin is still waiting.
        fill_due = [k for k in due if k in self._pending and now >= self._pending[k].get("_fill_at", 0.0)]
        if fill_due:
            fills = await self._fetch_fills([k[0] for k in fill_due], open_ts)
            for key in fill_due:
                info = self._pending.get(key)
                if info is None:
                    continue
                info["fill"] = fills.get(key[0])
                info["_fill_at"] = now + 1.0

        for key in list(due):
            info = self._pending.get(key)
            if info is None or info.get("direction") is None:
                continue
            coin = key[0]
            direction = info["direction"]
            fill = info.get("fill")
            if not fill:
                log.warning("Skipping %s 5m %s: no real fill available", coin, direction)
                self._pending.pop(key, None)
                continue
            fp = fill["fill_up"] if direction == "UP" else fill["fill_down"]
            if not (isinstance(fp, (int, float)) and 0.0 < fp < 1.0):
                log.warning("Skipping %s 5m %s: invalid fill price %r", coin, direction, fp)
                self._pending.pop(key, None)
                continue
            if fp > max_price:
                if now < info["open_ts"] + max_delay:
                    continue  # still too expensive — keep waiting for a cheaper quote
                log.info("Skipping %s 5m %s: fill %.4f above max %.4f at deadline",
                         coin, direction, fp, max_price)
                self._pending.pop(key, None)
                continue
            # Fill is within the limit (or the deadline passed with a good price).
            self._pending.pop(key, None)
            open_price = info["start_price"] if info["start_price"] else info["local_open"]
            entry_price = self.feed.latest(coin)
            fill_price = float(fp)
            odds = 1.0 / fill_price
            fee_bps = fill.get("fee_bps")
            self._place_trade(coin, info["open_ts"], key[1], open_price, entry_price,
                              direction, stake, odds, fill_price, fee_bps)

    async def _fetch_fills(self, coins: list[str], open_ts: int) -> dict[str, dict]:
        """Best-effort live fills from predict.fun's authenticated quote endpoint.

        Returns ``{coin: {fill_up, fill_down, fee_bps}}``.  Empty when no API key
        is configured or real fills are disabled — entries are then skipped until
        a live fill is available again (no flat fallback).
        """
        if not (self.cfg["api"].get("key") and self.cfg["api"].get("real_fill", True)):
            return {}

        async def one(coin: str):
            fill = await self.predfun.fill_for(coin, open_ts)
            return coin, fill

        results = await asyncio.gather(*(one(c) for c in coins), return_exceptions=True)
        out: dict[str, dict] = {}
        for r in results:
            if isinstance(r, tuple) and len(r) == 2 and isinstance(r[1], dict):
                out[r[0]] = r[1]
        return out

    async def _enrich_start_prices(self, keys: list[tuple[str, int]]) -> None:
        """Fetch predict.fun's authoritative startPrice for each due coin."""
        async def one(key: tuple[str, int]):
            coin, _ = key
            info = self._pending.get(key)
            if info is None:
                return key, None
            md = await self.predfun.market_data(coin, info["open_ts"])
            sp = md.get("start_price") if md else None
            return key, sp if isinstance(sp, (int, float)) and sp > 0 else None

        results = await asyncio.gather(*(one(k) for k in keys), return_exceptions=True)
        for r in results:
            if isinstance(r, tuple) and len(r) == 2:
                key, sp = r
                if sp is not None and key in self._pending:
                    self._pending[key]["start_price"] = float(sp)

    def _can_open(self, stake: float) -> bool:
        if not self.cfg["trading"]["enabled"]:
            return False
        balance = self.db.get_balance()
        exposure = sum(float(t["stake"]) for t in self._open.values())
        return (balance - exposure - stake) >= float(self.cfg["trading"]["min_balance"])

    def _place_trade(self, coin, open_ts, close_ts, open_price, entry_price,
                     direction, stake, odds, fill_price=None, fee_bps=None) -> None:
        slug = mkt.slug(coin, open_ts)
        shares = stake / fill_price if (fill_price and 0.0 < fill_price < 1.0) else 0.0
        trade_id = self.db.insert_trade(
            coin=coin, market_slug=slug, open_ts=open_ts, close_ts=close_ts,
            open_price=open_price, entry_price=entry_price, direction=direction,
            stake=stake, odds=odds, fill_price=fill_price, fee_bps=fee_bps,
        )
        trade = {
            "id": trade_id, "coin": coin, "market_slug": slug,
            "open_ts": open_ts, "close_ts": close_ts,
            "open_price": open_price, "entry_price": entry_price,
            "direction": direction, "stake": stake, "odds": odds,
            "fill_price": fill_price, "fee_bps": fee_bps, "shares": shares,
        }
        self._open[trade_id] = trade
        log.info(
            "ENTRY %s %s %s open=%.4f entry=%.4f fill=%.4f shares=%.4f odds=%.4f",
            coin.upper(), direction, slug, open_price, entry_price,
            float(fill_price or 0.0), shares, odds,
        )
        self._emit("trade", dict(trade))

    # ── settlement ──────────────────────────────────────────────────────────
    async def _settle(self, now: float) -> None:
        for trade_id, trade in list(self._open.items()):
            if now < trade["close_ts"]:
                continue
            coin = trade["coin"]
            open_ts = trade["open_ts"]

            # Throttled fetch of predict.fun's real start/end price.
            if now >= trade.get("_fetch_at", 0.0):
                trade["_fetch_at"] = now + FETCH_RETRY
                md = await self.predfun.market_data(coin, open_ts)
                if md and isinstance(md.get("start_price"), (int, float)):
                    trade["_pf_start"] = float(md["start_price"])
                if md and isinstance(md.get("end_price"), (int, float)):
                    open_price = trade.get("_pf_start") or float(trade["open_price"])
                    self._resolve(trade_id, trade, open_price, float(md["end_price"]), source="predictfun")
                    continue

            # endPrice not published yet — wait up to the grace period.
            if now < trade["close_ts"] + SETTLE_GRACE:
                continue

            # Fallback: Binance mid at close, best-known open.
            end_price = self.feed.price_at(coin, trade["close_ts"]) or self.feed.latest(coin)
            if end_price <= 0:
                continue
            open_price = trade.get("_pf_start") or float(trade["open_price"])
            self._resolve(trade_id, trade, open_price, end_price, source="binance")

    # ── early exit on reversal ──────────────────────────────────────────────
    async def _monitor_exits(self, now: float) -> None:
        """Close open positions early when the price crosses back over the open.

        For an UP position the entry signal was ``price > open``; if the live price
        later falls back below ``open`` the thesis is invalidated, so we sell the
        shares at the current market instead of holding to binary resolution.
        Symmetric for DOWN.  Disabled via ``exit_on_reversal = off``.
        """
        if not self.cfg.get("exit_on_reversal", True):
            return
        # During the flip entry window, _monitor_flips owns the position (it
        # closes+reopens on each confirmed cross), so don't double-handle here.
        flip_on = self.cfg.get("flip_on_reversal", False)
        _d = float(self.cfg["entry_delay_seconds"])
        flip_max_delay = max(_d, float(self.cfg.get("entry_delay_max_seconds", _d)))
        for trade_id, trade in list(self._open.items()):
            direction = trade.get("direction")
            if direction not in ("UP", "DOWN"):
                continue
            if flip_on and now < float(trade["open_ts"]) + flip_max_delay:
                continue
            open_price = float(trade.get("open_price") or 0.0)
            if open_price <= 0:
                continue
            coin = trade["coin"]
            live = self.feed.latest(coin)
            if live <= 0:
                continue
            crossed = (direction == "UP" and live < open_price) or \
                      (direction == "DOWN" and live > open_price)
            if not crossed:
                continue
            # Throttle exit-quote attempts to once per second per position.
            if now < trade.get("_exit_at", 0.0):
                continue
            trade["_exit_at"] = now + 1.0
            await self._exit_reversal(trade_id, trade, live)

    async def _exit_reversal(self, trade_id: int, trade: dict[str, Any], live_price: float) -> None:
        """Sell the position at the current market and realize PnL (reversal stop)."""
        coin = trade["coin"]
        direction = trade["direction"]
        stake = float(trade["stake"])
        fill_price = trade.get("fill_price")
        if not (isinstance(fill_price, (int, float)) and 0.0 < fill_price < 1.0):
            return  # cannot value the shares without a real entry fill
        shares = stake / float(fill_price)
        ex = await self.predfun.exit_for(coin, trade["open_ts"], direction)
        if not ex:
            log.warning("Reversal exit %s %s: no exit quote; holding", coin.upper(), direction)
            return
        exit_price = float(ex["exit_price"])
        proceeds = shares * exit_price
        pnl = proceeds - stake
        outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "PUSH")
        self.db.exit_trade(trade_id, live_price, outcome, pnl, exit_price)
        balance = self.db.get_balance() + pnl
        self.db.set_balance(balance)
        self.db.record_balance_event(
            "exit", pnl, balance, trade_id=trade_id,
            note=f"{coin.upper()} {direction} 5m reversal exit",
        )
        self._open.pop(trade_id, None)
        result = {
            **trade,
            "close_price": live_price,
            "exit_price": exit_price,
            "shares": shares,
            "outcome": outcome,
            "pnl": pnl,
            "reason": "reversal",
            "source": "exit",
        }
        log.info(
            "EXIT %s %s %s reversal live=%.4f exit=%.4f shares=%.4f pnl=%.4f balance=%.2f",
            coin.upper(), direction, trade["market_slug"], live_price, exit_price, shares, pnl, balance,
        )
        self._emit("exit", result)

    # ── flip on reversal (stop-and-reverse scalping) ────────────────────────
    async def _monitor_flips(self, now: float, open_ts: int, close_ts: int) -> None:
        """Flip mode: reverse the position on each confirmed cross of the open price.

        Runs every tick during ``[open+delay, open+max_delay]``.  A cross only
        counts after the price has held the new side for ``flip_confirm_seconds``
        (time confirmation, to filter noise).  On a confirmed flip the existing
        position is sold at market and a new one is opened in the new direction
        (subject to ``entry_max_price`` and balance).
        """
        if not self.cfg.get("flip_on_reversal", False):
            return
        delay = float(self.cfg["entry_delay_seconds"])
        max_delay = max(delay, float(self.cfg.get("entry_delay_max_seconds", delay)))
        confirm = float(self.cfg.get("flip_confirm_seconds", 1.0))
        coins = [c for c in self.cfg["trading"]["coins"] if c in COIN_SYMBOLS]
        for coin in coins:
            key = (coin, close_ts)
            info = self._pending.get(key)
            if info is None:
                continue
            open_price = info["start_price"] if info["start_price"] else info["local_open"]
            if open_price <= 0:
                continue
            live = self.feed.latest(coin)
            if live <= 0:
                continue
            if now - self.feed.last_update(coin) > FEED_STALE_SECONDS:
                continue
            side = "UP" if live > open_price else ("DOWN" if live < open_price else None)
            if side is None:
                continue
            # Time confirmation: the price must hold this side for `confirm` seconds.
            if side != info.get("_raw_side"):
                info["_raw_side"] = side
                info["_raw_since"] = now
            if now - info.get("_raw_since", now) < confirm:
                continue
            if info.get("_confirmed_side") == side:
                continue  # already flipped to this side
            # Only act inside the entry window.
            if now < info["open_ts"] + delay or now > info["open_ts"] + max_delay:
                continue
            info["_confirmed_side"] = side
            await self._do_flip(coin, key, info, side, open_price, live)

    async def _do_flip(self, coin: str, key: tuple[str, int], info: dict[str, Any],
                       side: str, open_price: float, live: float) -> None:
        """Close the current position (if any) and open one in ``side``."""
        open_ts = info["open_ts"]
        close_ts = key[1]
        existing = None
        for tid, t in list(self._open.items()):
            if t["coin"] == coin and t["close_ts"] == close_ts:
                existing = (tid, t)
                break
        if existing:
            tid, t = existing
            if t.get("direction") == side:
                return  # already aligned; nothing to flip
            await self._exit_reversal(tid, t, live)
        await self._enter_direction(coin, open_ts, close_ts, open_price, side)

    async def _enter_direction(self, coin: str, open_ts: int, close_ts: int,
                               open_price: float, direction: str) -> bool:
        """Fetch a live fill and open a position in ``direction`` (flip entry).

        Respects ``entry_max_price`` and balance limits.  Returns True if placed.
        """
        stake = float(self.cfg["trading"]["stake"])
        if not self._can_open(stake):
            log.info("Flip skip %s %s: balance limit reached", coin, direction)
            return False
        fill = await self.predfun.fill_for(coin, open_ts)
        if not fill:
            log.warning("Flip skip %s %s: no real fill available", coin, direction)
            return False
        fp = fill["fill_up"] if direction == "UP" else fill["fill_down"]
        if not (isinstance(fp, (int, float)) and 0.0 < fp < 1.0):
            log.warning("Flip skip %s %s: invalid fill price %r", coin, direction, fp)
            return False
        max_price = float(self.cfg.get("entry_max_price", 1.0))
        if fp > max_price:
            log.info("Flip skip %s %s: fill %.4f above max %.4f", coin, direction, fp, max_price)
            return False
        entry_price = self.feed.latest(coin)
        fill_price = float(fp)
        odds = 1.0 / fill_price
        fee_bps = fill.get("fee_bps")
        self._place_trade(coin, open_ts, close_ts, open_price, entry_price,
                          direction, stake, odds, fill_price, fee_bps)
        return True

    def sell(self, trade_id: int, price: float | None = None) -> tuple[Optional[dict], Optional[str]]:
        """Manually close an open position now (used by /sell)."""
        trade = self._open.get(trade_id)
        if trade is None:
            return None, "Posisi tidak ditemukan atau sudah ditutup."
        coin = trade["coin"]
        close_price = price or self.feed.latest(coin)
        if close_price <= 0:
            return None, "Harga belum tersedia."
        result = self._resolve(trade_id, trade, float(trade["open_price"]), close_price, source="manual")
        return result, None

    def _resolve(self, trade_id: int, trade: dict[str, Any], open_price: float,
                 close_price: float, source: str = "predictfun") -> dict[str, Any]:
        outcome, pnl = settle_result(
            trade["direction"], open_price, close_price,
            float(trade["stake"]), float(trade["odds"]),
        )
        self.db.settle_trade(trade_id, close_price, outcome, pnl)
        self.db.update_open_price(trade_id, open_price)
        balance = self.db.get_balance() + pnl
        self.db.set_balance(balance)
        self.db.record_balance_event(
            "settle", pnl, balance, trade_id=trade_id,
            note=f"{trade['coin'].upper()} {trade['direction']} 5m {outcome}",
        )
        self._open.pop(trade_id, None)
        result = {
            **trade,
            "open_price": open_price,
            "close_price": close_price,
            "outcome": outcome,
            "pnl": pnl,
            "source": source,
        }
        log.info(
            "SETTLE %s %s %s %s pnl=%.4f balance=%.2f (src=%s)",
            trade["coin"].upper(), trade["direction"], outcome, trade["market_slug"],
            pnl, balance, source,
        )
        self._emit("settle", result)
        return result

    def _prune(self, open_ts: int) -> None:
        cutoff = open_ts - mkt.WINDOW_SECONDS
        for close_ts in list(self._captured):
            if close_ts < cutoff:
                self._captured.pop(close_ts, None)
        for key in list(self._pending):
            if key[1] < cutoff:
                self._pending.pop(key, None)

    # ── helpers ─────────────────────────────────────────────────────────────
    def exposure(self) -> float:
        return sum(float(t["stake"]) for t in self._open.values())

    def open_trades(self) -> list[dict[str, Any]]:
        return [dict(t) for t in self._open.values()]

    def get_position(self, trade_id: int) -> Optional[dict[str, Any]]:
        t = self._open.get(trade_id)
        return dict(t) if t else None

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.on_event is not None:
            asyncio.create_task(self.on_event(event_type, payload))