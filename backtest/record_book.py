"""Merekam data REAL predict.fun (btc/eth/bnb) untuk backtest — versi WebSocket.

Data historis order book predict.fun tidak tersedia, jadi data real harus direkam
mulai sekarang. Recorder ini:
  1. resolve market id tiap window 5m via series rounds (REST),
  2. subscribe `predictTopOfBook/{marketId}` di wss://ws.predict.fun/ws — pesan
     dorong (push) setiap book berubah (event-driven, bukan polling),
  3. sekali per window: snapshot full-depth via REST /markets/{id}/orderbook,
  4. merekam harga underlying (BTC/ETH/BNB mid) dari Binance bookTicker WS.

Pemakaian:
    PREDICTFUN_API_KEY=... python record_book.py            # btc,eth,bnb
    PREDICTFUN_API_KEY=... python record_book.py btc,bnb    # pilih coin

Output per coin/hari di folder data/:
    predictfun-book-{coin}-{YYYY-MM-DD}.csv
      kolom: ts_ms, sec, market_id, bid, ask, bid_size, ask_size,
             bid_down, ask_down, price
      (bid/ask = sisi UP/Yes; bid_down = 1-bid, ask_down = 1-ask; price = mid
       underlying dari Binance pada saat pesan tiba)
    predictfun-orderbook-{coin}-{YYYY-MM-DD}.jsonl  (full-depth, detik 0)
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import sys
import time
import datetime as dt
from pathlib import Path

import httpx
import websockets

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
BASE = "https://api.predict.fun"
WS_BASE = "wss://ws.predict.fun/ws"
BINANCE_WS = "wss://data-stream.binance.vision/stream"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
WINDOW = 300
ALL_COINS = ("btc", "eth", "bnb")


def window_open(ts: float) -> int:
    return int(ts) - (int(ts) % WINDOW)


def iso_of(open_ts: int) -> str:
    return dt.datetime.fromtimestamp(open_ts, tz=dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:00.000Z")


def now_ms() -> int:
    return int(time.time() * 1000)


class Recorder:
    def __init__(self, coins: tuple[str, ...]) -> None:
        key = (os.environ.get("PREDICTFUN_API_KEY") or "").strip()
        if not key:
            print("set PREDICTFUN_API_KEY dulu", file=sys.stderr)
            sys.exit(1)
        self.coins = coins
        self.client = httpx.Client(
            base_url=BASE, headers={"x-api-key": key, "User-Agent": UA}, timeout=15)
        self.ids: dict[str, int] = {}
        self.window: int = 0
        self.price: dict[str, float] = {c: 0.0 for c in coins}

    def _get(self, path: str, params=None):
        r = self.client.get(path, params=params)
        if r.status_code == 429:
            time.sleep(5)
            r = self.client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def resolve_markets(self, open_ts: int) -> bool:
        """Cari market id window berjalan di tiap coin (paginasi series rounds)."""
        want = iso_of(open_ts)
        found_any = False
        for coin in self.coins:
            found = None
            after = None
            for _page in range(6):
                params = {"first": 100}
                if after:
                    params["after"] = after
                body = self._get(f"/v1/series/{coin}-updown-5m/rounds", params=params)
                for r0 in body.get("data", []):
                    if r0.get("startsAt") == want:
                        found = int(r0["markets"][0]["id"])
                        break
                if found or not body.get("cursor"):
                    break
                after = body.get("cursor")
            if found:
                self.ids[coin] = found
                found_any = True
        return found_any

    def write_row(self, coin: str, open_ts: int, q: dict, price: float) -> None:
        mid = self.ids.get(coin)
        if not mid:
            return
        day = dt.datetime.fromtimestamp(open_ts, tz=dt.timezone.utc).strftime("%Y-%m-%d")
        path = DATA_DIR / f"predictfun-book-{coin}-{day}.csv"
        new_file = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["ts_ms", "sec", "market_id", "bid", "ask",
                            "bid_size", "ask_size", "bid_down", "ask_down", "price"])
            w.writerow([now_ms(), int(time.time()) - open_ts, mid,
                        q.get("bid"), q.get("ask"),
                        q.get("bid_size"), q.get("ask_size"),
                        round(1 - (q.get("bid") or 0), 6),
                        round(1 - (q.get("ask") or 0), 6),
                        price])

    def snapshot_book(self, open_ts: int) -> None:
        """Full-depth snapshot per coin di awal window (JSONL per hari)."""
        day = dt.datetime.fromtimestamp(open_ts, tz=dt.timezone.utc).strftime("%Y-%m-%d")
        for coin in self.coins:
            mid = self.ids.get(coin)
            if not mid:
                continue
            try:
                body = self._get(f"/v1/markets/{mid}/orderbook")
                path = DATA_DIR / f"predictfun-orderbook-{coin}-{day}.jsonl"
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "ts_ms": now_ms(), "market_id": mid,
                        "book": body.get("data", body),
                    }) + "\n")
            except Exception as exc:
                print(f"orderbook {coin}: {exc}", file=sys.stderr)

    async def price_task(self) -> None:
        """Harga underlying (Binance bookTicker mid) via WS, update terus."""
        syms = "/".join(f"{c}usdt@bookTicker" for c in self.coins)
        url = f"{BINANCE_WS}?streams={syms}"
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, max_size=2**20) as ws:
                    async for raw in ws:
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", "replace")
                        msg = json.loads(raw)
                        data = msg.get("data") or {}
                        sym = str(data.get("s", "")).lower()
                        for coin in self.coins:
                            if sym == f"{coin}usdt":
                                bid = float(data.get("b") or 0)
                                ask = float(data.get("a") or 0)
                                if bid > 0 and ask > 0:
                                    self.price[coin] = (bid + ask) / 2
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"binance ws: {exc} — reconnect 5s", file=sys.stderr)
                await asyncio.sleep(5)

    async def record_window(self, open_ts: int) -> bool:
        """Satu window 5m via predict.fun WS. Return True jika WS sukses dipakai."""
        import inspect
        headers = {"x-api-key": (os.environ.get("PREDICTFUN_API_KEY") or "").strip()}
        params = inspect.signature(websockets.connect).parameters
        hdr_key = "additional_headers" if "additional_headers" in params else "extra_headers"
        topic_of = {self.ids[c]: c for c in self.coins if c in self.ids}
        try:
            async with websockets.connect(
                WS_BASE, **{hdr_key: headers}, ping_interval=20,
                max_size=2**22,
            ) as ws:
                for i, mid in enumerate(topic_of, 1):
                    await ws.send(json.dumps({
                        "method": "subscribe", "requestId": i,
                        "params": [f"predictTopOfBook/{mid}"],
                    }))
                while True:
                    remaining = open_ts + WINDOW - time.time()
                    if remaining <= 0:
                        return True
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=min(5.0, remaining))
                    except asyncio.TimeoutError:
                        continue
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", "replace")
                    msg = json.loads(raw)
                    if msg.get("topic") == "heartbeat":
                        await ws.send(json.dumps({
                            "method": "heartbeat", "data": msg.get("data")}))
                        continue
                    if msg.get("type") != "M":
                        continue
                    data = msg.get("data") or {}
                    mid = data.get("marketId")
                    if mid not in topic_of:
                        continue
                    coin = topic_of[mid]
                    self.write_row(coin, open_ts, {
                        "bid": (data.get("bestBid") or [None])[0],
                        "ask": (data.get("bestAsk") or [None])[0],
                        "bid_size": (data.get("bestBid") or [None, None])[1],
                        "ask_size": (data.get("bestAsk") or [None, None])[1],
                    }, self.price.get(coin, 0.0))
        except Exception as exc:
            print(f"predict ws: {exc}", file=sys.stderr)
            return False

    async def run(self) -> None:
        price_t = asyncio.create_task(self.price_task())
        poll_fallback = 0
        while True:
            now = time.time()
            open_ts = window_open(now)
            if self.window != open_ts:
                self.ids = {}
                self.window = open_ts
                if not self.resolve_markets(open_ts):
                    await asyncio.sleep(1)
                    continue
                self.snapshot_book(open_ts)
                print(f"window {open_ts} -> ids {self.ids}", flush=True)
            ws_ok = await self.record_window(open_ts)
            if not ws_ok:
                poll_fallback += 1
                print(f"WS gagal (#{poll_fallback}) — fallback polling 1s untuk "
                      f"window ini", file=sys.stderr)
                await self.poll_until_window_end(open_ts)
            else:
                poll_fallback = 0
            await asyncio.sleep(0.2)

    async def poll_until_window_end(self, open_ts: int) -> None:
        """Fallback: batch-quotes REST 1x/detik bila WS tidak tersedia."""
        while time.time() < open_ts + WINDOW:
            try:
                ids = [self.ids[c] for c in self.coins if c in self.ids]
                body = self._get("/v1/markets/quotes",
                                 params=[("ids", i) for i in ids])
                for q in (body.get("data") or []):
                    for coin in self.coins:
                        if q.get("marketId") == self.ids.get(coin):
                            self.write_row(coin, open_ts, {
                                "bid": (q.get("bestBid") or {}).get("price"),
                                "ask": (q.get("bestAsk") or {}).get("price"),
                                "bid_size": (q.get("bestBid") or {}).get("size"),
                                "ask_size": (q.get("bestAsk") or {}).get("size"),
                            }, self.price.get(coin, 0.0))
            except Exception as exc:
                print(f"poll error: {exc}", file=sys.stderr)
            await asyncio.sleep(1.0)


if __name__ == "__main__":
    coins_arg = sys.argv[1] if len(sys.argv) > 1 else "btc,eth,bnb"
    sel = tuple(c.strip().lower() for c in coins_arg.split(",") if c.strip())
    bad = [c for c in sel if c not in ALL_COINS]
    if bad or not sel:
        print(f"coin tidak valid: {bad} — pilihan {ALL_COINS}", file=sys.stderr)
        sys.exit(1)
    asyncio.run(Recorder(sel).run())
