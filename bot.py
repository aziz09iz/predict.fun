"""Predict.fun paper-trading bot — entry point.

Run with ``python bot.py``.  The bot:

  1. streams real-time BTC/ETH/BNB prices from Binance (the exact source
     predict.fun resolves its crypto markets against),
  2. for each 5-minute Up/Down window, records the open price, waits 1 second,
     and enters UP if live > open else DOWN,
  3. settles each trade at market close (UP wins on close > open, DOWN on the
     opposite, tie = push),
  4. exposes control + real-time status through a Telegram bot.

Configuration lives in ``config.json`` (see ``config.example.json``) and can
also be changed live from Telegram with ``/set``.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from typing import Any

from predictbot import config as cfgmod
from predictbot.config import (
    ALL_COINS,
    load_config,
    save_config,
    validate_setting,
)
from predictbot.db import Database
from predictbot.engine import Engine
from predictbot.format import Fmt, direction_tag, format_coin, outcome_tag
from predictbot.logger import setup_logging
from predictbot.market import WINDOW_SECONDS, human_ts, market_url, window_open
from predictbot.prices import PriceFeed
from predictbot.telegram import Telegram

log = logging.getLogger("predictbot")

ARROWS = {"UP": "📈", "DOWN": "📉"}

COMMANDS = [
    {"command": "start", "description": "Daftar / sapa bot"},
    {"command": "help", "description": "Daftar perintah"},
    {"command": "status", "description": "Saldo, PnL, posisi & harga live"},
    {"command": "prices", "description": "Harga realtime BTC/ETH/BNB"},
    {"command": "config", "description": "Lihat konfigurasi aktif"},
    {"command": "set", "description": "Ubah konfigurasi: /set <kunci> <nilai>"},
    {"command": "pause", "description": "Jeda trading"},
    {"command": "resume", "description": "Lanjutkan trading"},
    {"command": "history", "description": "Riwayat trade terakhir"},
    {"command": "settle", "description": "Selesai / settle posisi terbuka saat ini"},
]


class PaperBot:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.db = Database()
        self.fmt = Fmt(cfg["telegram"]["style"])
        self.feed = PriceFeed(cfg["prices"]["ws_url"], ALL_COINS)
        self.engine = Engine(cfg, self.db, self.feed)
        self.tg = Telegram(cfg["telegram"]["token"], cfg["telegram"]["style"])
        self._tasks: list[asyncio.Task] = []
        self._shutdown = False

    # ── telegram plumbing ───────────────────────────────────────────────────
    @property
    def admin(self) -> int | None:
        val = self.cfg["telegram"].get("chat_id")
        return int(val) if val is not None else None

    async def refresh_format(self) -> None:
        """Pick up style changes after /set telegram.style."""
        self.fmt = Fmt(self.cfg["telegram"]["style"])
        self.tg.style = self.fmt.style

    async def send_admin(self, text: str) -> None:
        if self.admin is not None:
            await self.tg.send(self.admin, text)

    async def on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            if event_type == "trade" and self.cfg["notify"]["on_trade"]:
                await self.send_admin(self._trade_message(payload))
            elif event_type == "settle" and self.cfg["notify"]["on_settle"]:
                await self.send_admin(self._settle_message(payload))
        except Exception as exc:
            log.exception("notification error: %s", exc)

    # ── message builders ────────────────────────────────────────────────────
    def _trade_message(self, t: dict[str, Any]) -> str:
        f = self.fmt
        label, emoji = direction_tag(t["direction"])
        coin = format_coin(t["coin"])
        odds_text = f"{t['odds']}x"
        lines = [
            f"{ARROWS.get(t['direction'], '🎯')} {f.b('ENTRY')} — {coin} 5m {emoji}",
            f"Direction: {f.b(label)}",
            f"Open {f.price(t['open_price'])} → Entry {f.price(t['entry_price'])}",
            f"Stake {f.money(t['stake'])} · Odds {f.b(odds_text)}",
            f.link("Buka market", market_url(t["coin"], t["close_ts"])),
        ]
        return "\n".join(lines)

    def _settle_message(self, t: dict[str, Any]) -> str:
        f = self.fmt
        label, emoji = outcome_tag(t["outcome"])
        coin = format_coin(t["coin"])
        dlabel, _ = direction_tag(t["direction"])
        lines = [
            f"{emoji} {f.b(label)} — {coin} 5m {dlabel}",
            f"Open {f.price(t['open_price'])} → Close {f.price(t['close_price'])}",
            f"PnL: {f.b(f.money(t['pnl'], with_sign=True))}",
            f"Balance: {f.money(self.db.get_balance())}",
            f.link("Market", market_url(t["coin"], t["close_ts"])),
        ]
        return "\n".join(lines)

    def _startup_message(self) -> str:
        f = self.fmt
        t = self.cfg["trading"]
        coins_str = " · ".join(format_coin(c) for c in t["coins"])
        mult_text = f"{self.cfg['payout']['win_multiplier']}x"
        prices = self.feed.all_prices()
        lines = [
            f"🚀 {f.b('Predict.fun Paper Bot')} online!",
            f"Markets: {f.b(coins_str)} — 5m Up/Down",
            f"Balance: {f.money(self.db.get_balance())} · Stake {f.money(t['stake'])} · Odds {f.b(mult_text)}",
            f"Trading: {f.b('ON' if t['enabled'] else 'OFF')}",
        ]
        live = [f"{format_coin(c)} {f.price(v)}" for c, v in prices.items() if v > 0]
        lines.append(f"Live: {' · '.join(live)}" if live else f"Live: {f.esc('menghubungkan price feed…')}")
        lines.append(f"{f.esc('/help')} untuk daftar perintah")
        return "\n".join(lines)

    def _status_message(self) -> str:
        f = self.fmt
        t = self.cfg["trading"]
        balance = self.db.get_balance()
        stats = self.db.stats()
        exposure = self.engine.exposure()
        prices = self.feed.all_prices()
        now = time.time()
        open_ts = window_open(now)
        next_open_in = WINDOW_SECONDS - (now - open_ts)
        pnl = balance - self.db.get_initial_balance()
        winrate_text = f"{stats['winrate']:.0f}%"
        lines = [
            f"📊 {f.b('Status')}",
            f"Balance {f.money(balance)} · PnL {f.money(pnl, with_sign=True)}",
            f"Win/Loss/Push: {stats['wins']}/{stats['losses']}/{stats['pushes']} · Winrate {f.b(winrate_text)}",
            f"Open: {len(self.engine.open_trades())} · Exposure {f.money(exposure)}",
            f"Trading {f.b('ON' if t['enabled'] else 'OFF')} · Stake {f.money(t['stake'])}",
        ]
        live_lines = [f"{format_coin(c)} {f.price(prices.get(c, 0.0))}" for c in ALL_COINS]
        lines.append(f"Live: {' · '.join(live_lines)}")
        lines.append(f"Market berikutnya buka ~{int(next_open_in)}s")
        return "\n".join(lines)

    def _prices_message(self) -> str:
        f = self.fmt
        prices = self.feed.all_prices()
        lines = [f"💹 {f.b('Harga Realtime')}"]
        for c in ALL_COINS:
            p = prices.get(c, 0.0)
            ago = time.time() - self.feed.last_update(c)
            stale = "" if ago < 2 else f.esc(f"  (~{int(ago)}s lalu)")
            lines.append(f"{format_coin(c)}: {f.b(f.price(p))}{stale}")
        return "\n".join(lines)

    def _config_message(self) -> str:
        f = self.fmt
        t = self.cfg["trading"]
        p = self.cfg["payout"]
        mult_text = f"{p['win_multiplier']}x"
        delay_text = f"{self.cfg['entry_delay_seconds']}s"
        coins_text = ", ".join(format_coin(c) for c in t["coins"])
        lines = [
            f"⚙️ {f.b('Konfigurasi')}",
            f"Trading: {f.b('ON' if t['enabled'] else 'OFF')}",
            f"Coins: {f.b(coins_text)}",
            f"Stake: {f.money(t['stake'])}",
            f"Min balance: {f.money(t['min_balance'])}",
            f"Odds (win x): {f.b(mult_text)}",
            f"Entry delay: {f.b(delay_text)}",
            f"Style: {f.b(self.cfg['telegram']['style'])}",
            f"{f.esc('Ubah dengan:')} /set {f.esc('<kunci> <nilai>')}",
        ]
        return "\n".join(lines)

    # ── command handling ────────────────────────────────────────────────────
    async def handle_update(self, update: dict[str, Any]) -> None:
        msg = update.get("message")
        if not msg:
            return
        chat_id = int((msg.get("chat") or {}).get("id", 0))
        text = (msg.get("text") or "").strip()
        if not chat_id or not text.startswith("/"):
            return

        if self.admin is None:
            if text.startswith("/start"):
                self.cfg["telegram"]["chat_id"] = chat_id
                save_config(self.cfg)
                log.info("Admin registered: chat_id=%s", chat_id)
                await self.tg.send(chat_id, self._startup_message())
            else:
                await self.tg.send(chat_id, "Kirim /start untuk mendaftarkan bot ini sebagai admin. 👋")
            return

        if chat_id != self.admin:
            await self.tg.send(chat_id, "Maaf, kamu bukan admin bot ini. 🙅")
            return

        parts = text.split(maxsplit=2)
        cmd = parts[0].split("@", 1)[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        rest = parts[2] if len(parts) > 2 else ""

        if cmd in ("/start", "/help"):
            await self.tg.send(chat_id, self._help_message())
        elif cmd == "/status":
            await self.tg.send(chat_id, self._status_message())
        elif cmd == "/prices":
            await self.tg.send(chat_id, self._prices_message())
        elif cmd in ("/config", "/settings"):
            await self.tg.send(chat_id, self._config_message())
        elif cmd == "/set":
            await self._handle_set(chat_id, arg, rest)
        elif cmd == "/pause":
            self.cfg["trading"]["enabled"] = False
            save_config(self.cfg)
            await self.tg.send(chat_id, f"⏸️ Trading di-{self.fmt.b('PAUSE')}. Gunakan /resume untuk lanjut.")
        elif cmd == "/resume":
            self.cfg["trading"]["enabled"] = True
            save_config(self.cfg)
            await self.tg.send(chat_id, f"▶️ Trading {self.fmt.b('LANJUT')}.")
        elif cmd == "/history":
            await self.tg.send(chat_id, self._history_message(arg))
        elif cmd == "/settle":
            await self._handle_settle(chat_id)
        else:
            await self.tg.send(chat_id, f"Perintah tidak dikenal. {self.fmt.esc('/help')}")

    def _help_message(self) -> str:
        f = self.fmt
        lines = [f"🤖 {f.b('Predict.fun Paper Bot')} — panduan singkat"]
        for c in COMMANDS:
            lines.append(f"/{c['command']} — {f.esc(c['description'])}")
        lines += [
            "",
            f.esc("Contoh /set:") + " " + f.code("trading.stake 25") + ", "
            + f.code("trading.coins btc,eth") + ", " + f.code("trading.enabled off"),
            "",
            f.esc("Kunci yang bisa diubah:") + " " + f.b(f.esc(
                "trading.stake, trading.coins, trading.enabled, trading.min_balance, "
                "payout.win_multiplier, entry_delay_seconds, telegram.style, "
                "notify.on_trade, notify.on_settle, notify.startup"
            )),
        ]
        return "\n".join(lines)

    async def _handle_set(self, chat_id: int, key: str, value: str) -> None:
        f = self.fmt
        if not key or not value:
            await self.tg.send(chat_id, "Format: /set <kunci> <nilai>. Contoh: /set trading.stake 25")
            return
        ok, parsed, msg = validate_setting(key, value)
        if not ok:
            await self.tg.send(chat_id, f"❌ {f.esc(msg or 'gagal')}")
            return
        cfgmod.set_key(self.cfg, key, parsed)
        save_config(self.cfg)
        if key == "telegram.style":
            await self.refresh_format()
        val_display = parsed if not isinstance(parsed, list) else ", ".join(parsed)
        await self.tg.send(chat_id, f"✅ {f.code(key)} = {f.b(str(val_display))}")

    def _history_message(self, arg: str) -> str:
        f = self.fmt
        try:
            n = min(int(arg), 20) if arg else 8
        except ValueError:
            n = 8
        rows = self.db.recent_trades(n)
        if not rows:
            return f"📜 {f.b('Tidak ada trade')}."
        lines = [f"📜 {f.b('Riwayat')}"]
        for r in rows:
            if r["status"] == "OPEN":
                dlabel, demoji = direction_tag(r["direction"])
                lines.append(f"{demoji} {format_coin(r['coin'])} {dlabel} · OPEN · {human_ts(r['open_ts'])}")
            else:
                olabel, oemoji = outcome_tag(r["outcome"])
                pnl = r["pnl"] or 0.0
                dlabel, _ = direction_tag(r["direction"])
                lines.append(f"{oemoji} {format_coin(r['coin'])} {dlabel} {f.money(pnl, with_sign=True)} · {human_ts(r['close_ts'])}")
        return "\n".join(lines)

    async def _handle_settle(self, chat_id: int) -> None:
        f = self.fmt
        open_trades = self.engine.open_trades()
        if not open_trades:
            await self.tg.send(chat_id, "Tidak ada posisi terbuka.")
            return
        await self.tg.send(chat_id, f"🏁 Menyelesaikan {f.b(str(len(open_trades)))} posisi terbuka…")
        self.engine.settle_now()
        await self.tg.send(chat_id, "Selesai. ✅")

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def _wait_for_prices(self, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if sum(1 for v in self.feed.all_prices().values() if v > 0) >= 3:
                return
            await asyncio.sleep(0.3)

    async def run(self) -> None:
        self.db.connect()
        if self.db.get_initial_balance() == 0.0:
            self.db.set_initial_balance(float(self.cfg["initial_balance"]))
            self.db.set_balance(float(self.cfg["initial_balance"]))
            log.info("Initialised paper balance: %.2f", self.cfg["initial_balance"])

        self.engine.on_event = self.on_event

        token = self.cfg["telegram"]["token"]
        if not token:
            log.critical(
                "Missing Telegram token. Set telegram.token in config.json or "
                "PREDICTFUN_TELEGRAM_TOKEN env var."
            )
            return
        await self.tg.set_commands(COMMANDS)

        me = await self.tg.get_me()
        log.info("Telegram bot @%s ready", me["result"].get("username"))

        self._tasks = [
            asyncio.create_task(self.feed.run(), name="price-feed"),
            asyncio.create_task(self.engine.run(), name="engine"),
            asyncio.create_task(self.tg.run_polling(self.handle_update), name="telegram"),
        ]

        await self._wait_for_prices()
        if self.cfg["notify"]["startup"] and self.admin is not None:
            await self.send_admin(self._startup_message())
        elif self.admin is None:
            log.info("No admin chat set — open Telegram and send /start to register.")

        log.info("Bot running. Balance=%.2f", self.db.get_balance())

        try:
            await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            pass

    async def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        log.info("Shutting down…")
        self.feed.stop()
        self.engine.stop()
        self.tg.stop()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.tg.aclose()
        self.db.close()
        log.info("Shutdown complete")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Predict.fun paper-trading bot")
    parser.add_argument("--config", default=None, help="path to config.json")
    parser.add_argument("--log-level", default="INFO", help="DEBUG|INFO|WARNING|ERROR")
    return parser


async def main() -> None:
    args = build_parser().parse_args()
    setup_logging(args.log_level)

    cfg = load_config()
    bot = PaperBot(cfg)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.shutdown()))
        except NotImplementedError:
            pass  # Windows has no SIGTERM signal handler support

    try:
        await bot.run()
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted.")
        sys.exit(0)