"""Predict.fun paper-trading bot — entry point.

Run with ``python bot.py``.  The bot:

  1. streams real-time BTC/ETH/BNB prices from Binance (the source predict.fun
     resolves its crypto markets against),
  2. anchors open/close to predict.fun's real Chainlink ``startPrice``/``endPrice``,
  3. for each 5-minute Up/Down window, enters UP if live > open else DOWN at
     ``open + entry_delay_seconds``,
  4. settles against predict.fun's actual resolution data,
  5. exposes control + status through a Telegram bot.

Configuration lives in ``config.json`` and is editable live from Telegram.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import signal
import sys
import time
from typing import Any

from predictbot.config import (
    ALL_COINS,
    SETTABLE_KEYS,
    get,
    get_source,
    load_config_state,
    save_config,
    set_key,
    validate_setting,
)
from predictbot.db import Database
from predictbot.engine import Engine
from predictbot.format import Fmt, direction_tag, format_coin, outcome_tag
from predictbot.logger import setup_logging
from predictbot.market import human_ts, market_url, window_close, window_open
from predictbot.predictfun import PredictFun
from predictbot.prices import PriceFeed
from predictbot.telegram import Telegram

log = logging.getLogger("predictbot")

ARROWS = {"UP": "📈", "DOWN": "📉"}

# Grouped command list — shown by /help and registered as the bot menu.
COMMANDS = [
    {"command": "start", "description": "Daftar / sapa bot"},
    {"command": "help", "description": "Daftar perintah"},
    {"command": "trade", "description": "on|off — mulai/berhenti trading"},
    {"command": "status", "description": "Status engine & market"},
    {"command": "positions", "description": "Posisi terbuka"},
    {"command": "sell", "description": "<id> — jual posisi manual"},
    {"command": "account", "description": "Balance, winrate, PnL hari ini"},
    {"command": "history", "description": "[N] — riwayat trade"},
    {"command": "config", "description": "Setting efektif + asal nilainya"},
    {"command": "mode", "description": "Mode saat ini (paper/live)"},
    {"command": "notify", "description": "full|compact|settle — level notifikasi"},
    {"command": "set", "description": "<kunci> <nilai> — ubah konfigurasi"},
    {"command": "prices", "description": "Harga realtime BTC/ETH/BNB"},
]

CONFIG_ROWS = [
    "mode",
    "trading.enabled",
    "trading.coins",
    "trading.stake",
    "trading.min_balance",
    "payout.win_multiplier",
    "entry_delay_seconds",
    "telegram.style",
    "notify.level",
    "notify.startup",
    "api.real_fill",
]

def _display_value(v: Any) -> str:
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, list):
        return ", ".join(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else f"{v:g}"
    if v is None or v == "":
        return "—"
    return str(v)


def _utc_day_start_iso() -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


class PaperBot:
    def __init__(self, cfg: dict[str, Any], origins: dict[str, Any] | None = None):
        self.cfg = cfg
        self.origins = origins or {}
        self.db = Database()
        self.fmt = Fmt(cfg["telegram"]["style"])
        self.feed = PriceFeed(cfg["prices"]["ws_url"], ALL_COINS)
        self.predfun = PredictFun(
            api_key=cfg["api"].get("key", ""),
            api_base=cfg["api"].get("base_url", "https://api.predict.fun"),
            rate_limits=cfg["api"].get("rate_limits"),
        )
        self.engine = Engine(cfg, self.db, self.feed, self.predfun)
        self.tg = Telegram(cfg["telegram"]["token"], cfg["telegram"]["style"])
        self._tasks: list[asyncio.Task] = []
        self._shutdown = False

    # ── telegram plumbing ───────────────────────────────────────────────────
    @property
    def admin(self) -> int | None:
        val = self.cfg["telegram"].get("chat_id")
        return int(val) if val is not None else None

    async def refresh_format(self) -> None:
        self.fmt = Fmt(self.cfg["telegram"]["style"])
        self.tg.style = self.fmt.style

    async def send_admin(self, text: str) -> None:
        if self.admin is not None:
            await self.tg.send(self.admin, text)

    async def on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            level = self.cfg["notify"]["level"]
            compact = level == "compact"
            if event_type == "trade":
                if level in ("full", "compact"):
                    await self.send_admin(self._trade_message(payload, compact=compact))
            elif event_type == "settle":
                if payload.get("source") == "manual":
                    return  # /sell replies directly
                await self.send_admin(self._settle_message(payload, compact=compact))
        except Exception as exc:
            log.exception("notification error: %s", exc)

    # ── message builders ────────────────────────────────────────────────────
    def _fee_text(self, fee_bps: Any) -> str:
        if isinstance(fee_bps, (int, float)):
            return f" · Fee {fee_bps / 100:.2f}%"
        return ""

    def _trade_message(self, t: dict[str, Any], compact: bool = False) -> str:
        f = self.fmt
        label, emoji = direction_tag(t["direction"])
        coin = format_coin(t["coin"])
        arrow = ARROWS.get(t["direction"], '🎯')
        if compact:
            return f"{arrow} {coin} 5m {emoji} {label} · {f.price(t['open_price'])} → {f.price(t['entry_price'])} · {f.money(t['stake'])}"
        lines = [
            f"{arrow} {f.b('ENTRY')} — {coin} 5m {emoji}",
            f"Direction: {f.b(label)}",
            f"Open {f.price(t['open_price'])} → Entry {f.price(t['entry_price'])}",
            f"Stake {f.money(t['stake'])}",
        ]
        fp = t.get("fill_price")
        if isinstance(fp, (int, float)) and 0 < fp < 1:
            lines.append(f"Fill {f.b(f'{fp:.4f}')} · Payout {f.b(f'{1 / fp:.2f}x')}{f.esc(self._fee_text(t.get('fee_bps')))}")
        else:
            lines.append(f"Odds {f.b(f'{t['odds']}x')} (flat)")
        lines.append(f.link("Buka market", market_url(t["coin"], t["open_ts"])))
        return "\n".join(lines)

    def _settle_message(self, t: dict[str, Any], compact: bool = False) -> str:
        f = self.fmt
        label, emoji = outcome_tag(t["outcome"])
        coin = format_coin(t["coin"])
        dlabel, _ = direction_tag(t["direction"])
        if compact:
            return f"{emoji} {coin} 5m {dlabel} {label} {f.money(t['pnl'], with_sign=True)} · bal {f.money(self.db.get_balance())}"
        lines = [
            f"{emoji} {f.b(label)} — {coin} 5m {dlabel}",
            f"Open {f.price(t['open_price'])} → Close {f.price(t['close_price'])}",
        ]
        fp = t.get("fill_price")
        if isinstance(fp, (int, float)) and 0 < fp < 1:
            lines.append(f"Fill {f.b(f'{fp:.4f}')} · Payout {f.b(f'{1 / fp:.2f}x')}")
        lines.append(f"PnL: {f.b(f.money(t['pnl'], with_sign=True))}")
        lines.append(f"Balance: {f.money(self.db.get_balance())}")
        lines.append(f.link("Market", market_url(t["coin"], t["open_ts"])))
        return "\n".join(lines)

    def _startup_message(self) -> str:
        f = self.fmt
        t = self.cfg["trading"]
        coins_str = " · ".join(format_coin(c) for c in t["coins"])
        mult_text = f"{self.cfg['payout']['win_multiplier']}x"
        mode = self.cfg["mode"]
        prices = self.feed.all_prices()
        lines = [
            f"🚀 {f.b('Predict.fun Paper Bot')} online!",
            f"Mode: {f.b(mode)} · Markets: {f.b(coins_str)} — 5m Up/Down",
            f"Balance {f.money(self.db.get_balance())} · Stake {f.money(t['stake'])} · Odds {f.b(mult_text)}",
            f"Trading: {f.b('ON' if t['enabled'] else 'OFF')}",
        ]
        live = [f"{format_coin(c)} {f.price(v)}" for c, v in prices.items() if v > 0]
        lines.append(f"Live: {' · '.join(live)}" if live else f"Live: {f.esc('menghubungkan…')}")
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
        close_ts = window_close(now)
        winrate_text = f"{stats['winrate']:.0f}%"
        pnl = balance - self.db.get_initial_balance()
        fill_mode = "real" if (self.cfg["api"].get("key") and self.cfg["api"].get("real_fill", True)) else "flat"
        lines = [
            f"📊 {f.b('Status')}",
            f"Mode {f.b(self.cfg['mode'])} · Trading {f.b('ON' if t['enabled'] else 'OFF')} · Fill {f.b(fill_mode)}",
            f"Balance {f.money(balance)} · PnL {f.money(pnl, with_sign=True)}",
            f"Win/Loss/Push: {stats['wins']}/{stats['losses']}/{stats['pushes']} · Winrate {f.b(winrate_text)}",
            f"Open: {len(self.engine.open_trades())} · Exposure {f.money(exposure)} · Stake {f.money(t['stake'])}",
        ]
        live_lines = [f"{format_coin(c)} {f.price(prices.get(c, 0.0))}" for c in ALL_COINS]
        lines.append(f"Live: {' · '.join(live_lines)}")
        lines.append(f"Market: {human_ts(open_ts)} → {human_ts(close_ts)} · tutup ~{int(close_ts - now)}s")
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

    def _account_message(self) -> str:
        f = self.fmt
        balance = self.db.get_balance()
        stats = self.db.stats()
        today = self.db.stats_since(_utc_day_start_iso())
        winrate_text = f"{stats['winrate']:.0f}%"
        lines = [
            f"👤 {f.b('Akun')}",
            f"Balance {f.money(balance)}",
            f"PnL hari ini {f.money(today['pnl'], with_sign=True)}",
            f"Winrate {f.b(winrate_text)} · {stats['wins']}W/{stats['losses']}L/{stats['pushes']}P",
            f"Total trade {stats['total']} · Mode {f.b(self.cfg['mode'])}",
        ]
        return "\n".join(lines)

    def _positions_message(self) -> str:
        f = self.fmt
        positions = self.engine.open_trades()
        if not positions:
            return f"📌 {f.b('Tidak ada posisi terbuka')}."
        lines = [f"📌 {f.b(f'Posisi ({len(positions)})')}"]
        for p in positions:
            dlabel, demoji = direction_tag(p["direction"])
            coin = format_coin(p["coin"])
            lines.append(
                f"{demoji} #{p['id']} {coin} {dlabel} · "
                f"{f.price(p['open_price'])} → {f.price(p['entry_price'])} · {f.money(p['stake'])}"
            )
        lines.append(f.esc("Tutup: /sell <id>"))
        return "\n".join(lines)

    def _config_message(self) -> str:
        f = self.fmt
        lines = [f"⚙️ {f.b('Konfigurasi (efektif)')}"]
        for row in CONFIG_ROWS:
            value = _display_value(get(self.cfg, row))
            origin = get_source(self.origins, row)
            lines.append(f"{f.code(row)} = {f.b(f.esc(value))}  ({f.esc(origin)})")
        lines.append("")
        lines.append(f"Ubah: {f.esc('/set <kunci> <nilai>')} · daftar kunci: {f.esc('/set')}")
        return "\n".join(lines)

    def _set_help_message(self) -> str:
        f = self.fmt
        lines = [f"🛠 {f.b('Kunci yang bisa diubah')}"]
        for key, example, desc in SETTABLE_KEYS:
            lines.append(f"{f.code(key)}  {f.esc(f'({example}) — {desc}')}")
        lines.append("")
        lines.append(f"Contoh: {f.code('trading.stake 25')}, {f.code('trading.coins btc,eth')}, {f.code('trading.enabled off')}")
        return "\n".join(lines)

    def _help_message(self) -> str:
        f = self.fmt
        lines = [f"🤖 {f.b('Predict.fun Paper Bot')} — perintah"]
        sections = [
            ("Trading", ["trade", "status", "positions", "sell"]),
            ("Info", ["account", "history", "config", "mode", "prices"]),
            ("Notifikasi", ["notify"]),
            ("Config", ["set"]),
        ]
        index = {c["command"]: c["description"] for c in COMMANDS}
        for title, cmds in sections:
            lines.append(f"")
            lines.append(f.b(title))
            for c in cmds:
                lines.append(f"/{c} — {f.esc(index[c])}")
        return "\n".join(lines)

    def _history_message(self, arg: str) -> str:
        f = self.fmt
        try:
            n = min(int(arg), 20) if arg else 20
        except ValueError:
            n = 20
        rows = self.db.recent_trades(n)
        if not rows:
            return f"📜 {f.b('Belum ada trade')}."
        lines = [f"📜 {f.b('Riwayat')}"]
        for r in rows:
            if r["status"] == "OPEN":
                dlabel, demoji = direction_tag(r["direction"])
                lines.append(f"{demoji} {format_coin(r['coin'])} {dlabel} · OPEN · {human_ts(r['open_ts'])}")
            else:
                olabel, oemoji = outcome_tag(r["outcome"])
                dlabel, _ = direction_tag(r["direction"])
                lines.append(
                    f"{oemoji} {format_coin(r['coin'])} {dlabel} {f.money(r['pnl'] or 0.0, with_sign=True)} · {human_ts(r['close_ts'])}"
                )
        return "\n".join(lines)

    # ── config mutation helper ──────────────────────────────────────────────
    def _apply_setting(self, key: str, value: Any, origin: str = "telegram") -> None:
        set_key(self.cfg, key, value)
        if self.origins:
            set_key(self.origins, key, origin)
        save_config(self.cfg)

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
                if self.origins:
                    set_key(self.origins, "telegram.chat_id", "telegram")
                save_config(self.cfg)
                log.info("Admin registered: chat_id=%s", chat_id)
                await self.tg.send(chat_id, self._startup_message())
            else:
                await self.tg.send(chat_id, "Kirim /start untuk mendaftarkan bot ini sebagai admin. 👋")
            return

        if chat_id != self.admin:
            await self.tg.send(chat_id, "Maaf, kamu bukan admin bot ini. 🙅")
            return

        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@", 1)[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/start", "/help"):
            await self.tg.send(chat_id, self._help_message())
        elif cmd == "/status":
            await self.tg.send(chat_id, self._status_message())
        elif cmd == "/prices":
            await self.tg.send(chat_id, self._prices_message())
        elif cmd == "/account":
            await self.tg.send(chat_id, self._account_message())
        elif cmd == "/positions":
            await self.tg.send(chat_id, self._positions_message())
        elif cmd == "/history":
            await self.tg.send(chat_id, self._history_message(arg))
        elif cmd == "/config":
            await self.tg.send(chat_id, self._config_message())
        elif cmd == "/trade":
            await self._handle_trade(chat_id, arg)
        elif cmd == "/sell":
            await self._handle_sell(chat_id, arg)
        elif cmd == "/mode":
            await self._handle_mode(chat_id, arg)
        elif cmd == "/notify":
            await self._handle_notify(chat_id, arg)
        elif cmd == "/set":
            await self._handle_set(chat_id, arg)
        else:
            await self.tg.send(chat_id, f"Perintah tidak dikenal. {self.fmt.esc('/help')}")

    async def _handle_trade(self, chat_id: int, arg: str) -> None:
        f = self.fmt
        if not arg:
            state = "ON" if self.cfg["trading"]["enabled"] else "OFF"
            await self.tg.send(chat_id, f"Trading: {f.b(state)}. Gunakan /trade on|off.")
            return
        ok, parsed, msg = validate_setting("trading.enabled", arg)
        if not ok:
            await self.tg.send(chat_id, f"❌ {f.esc(msg)}")
            return
        self._apply_setting("trading.enabled", parsed)
        await self.tg.send(chat_id, f"{'▶️' if parsed else '⏸️'} Trading {f.b('ON' if parsed else 'OFF')}.")

    async def _handle_sell(self, chat_id: int, arg: str) -> None:
        f = self.fmt
        if not arg.isdigit():
            await self.tg.send(chat_id, f"Format: /sell <id>. Lihat /positions.")
            return
        result, err = self.engine.sell(int(arg))
        if err:
            await self.tg.send(chat_id, f"❌ {f.esc(err)}")
            return
        await self.send_admin(self._settle_message(result))

    async def _handle_mode(self, chat_id: int, arg: str) -> None:
        f = self.fmt
        if not arg:
            await self.tg.send(chat_id, f"Mode saat ini: {f.b(self.cfg['mode'])}.")
            return
        ok, parsed, msg = validate_setting("mode", arg)
        if not ok:
            await self.tg.send(chat_id, f"❌ {f.esc(msg)}")
            return
        if parsed == "live":
            await self.tg.send(chat_id, f"⚠️ Mode {f.b('live')} belum tersedia — bot tetap {f.b('paper')} (simulasi dengan data nyata predict.fun).")
            return
        self._apply_setting("mode", parsed)
        await self.tg.send(chat_id, f"✅ Mode: {f.b(parsed)}.")

    async def _handle_notify(self, chat_id: int, arg: str) -> None:
        f = self.fmt
        if not arg:
            await self.tg.send(chat_id, f"Notifikasi: {f.b(self.cfg['notify']['level'])}. Pilihan: full|compact|settle.")
            return
        ok, parsed, msg = validate_setting("notify.level", arg)
        if not ok:
            await self.tg.send(chat_id, f"❌ {f.esc(msg)}")
            return
        self._apply_setting("notify.level", parsed)
        desc = {"full": "entry + settle (detail)", "compact": "entry + settle (ringkas)", "settle": "hanya settle"}[parsed]
        await self.tg.send(chat_id, f"✅ Notifikasi: {f.b(parsed)} ({f.esc(desc)}).")

    async def _handle_set(self, chat_id: int, arg: str) -> None:
        f = self.fmt
        if not arg:
            await self.tg.send(chat_id, self._set_help_message())
            return
        parts = arg.split(maxsplit=1)
        key = parts[0].strip()
        value = parts[1].strip() if len(parts) > 1 else ""
        if not value:
            await self.tg.send(chat_id, f"Format: /set <kunci> <nilai>. Contoh: /set trading.stake 25")
            return
        ok, parsed, msg = validate_setting(key, value)
        if not ok:
            await self.tg.send(chat_id, f"❌ {f.esc(msg or 'gagal')}")
            return
        self._apply_setting(key, parsed)
        if key == "telegram.style":
            await self.refresh_format()
        val_display = parsed if not isinstance(parsed, list) else ", ".join(parsed)
        await self.tg.send(chat_id, f"✅ {f.code(key)} = {f.b(str(val_display))}")

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
        await self.predfun.aclose()
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

    cfg, origins = load_config_state()
    bot = PaperBot(cfg, origins)
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