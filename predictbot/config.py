"""Configuration loading, defaults, validation, and persistence.

The bot keeps a single ``config.json`` next to the project root.  Entries are
merged onto defaults at boot so a partial file still works.  Sensitive values
(the Telegram token / chat id) can also come from environment variables so the
systemd unit can inject them without writing secrets into the JSON file.

Each effective value also carries an *origin* (default / file / env / telegram)
so ``/config`` can show not just what is configured but where it came from.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
LOG_FILE = DATA_DIR / "bot.log"
DB_FILE = DATA_DIR / "predictbot.db"
CONFIG_FILE = BASE_DIR / "config.json"

COIN_SYMBOLS = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "bnb": "BNBUSDT",
}
ALL_COINS = list(COIN_SYMBOLS)

# Value origin labels shown by /config.
ORIGIN_DEFAULT = "default"
ORIGIN_FILE = "file"
ORIGIN_ENV = "env"
ORIGIN_TELEGRAM = "telegram"

DEFAULTS: dict[str, Any] = {
    "telegram": {
        "token": "",
        "chat_id": None,          # admin chat id (settable via /start when empty)
        "style": "HTML",          # "HTML" (default) | "MarkdownV2"
    },
    "mode": "paper",              # "paper" (simulated). "live" is not implemented.
    "trading": {
        "enabled": True,
        "coins": ["btc", "eth", "bnb"],
        "stake": 1.0,             # USDT risked per trade
        "min_balance": 0.0,       # stop opening trades below this cash level
    },
    "time_entry": [1.0, 10.0],    # entry window [start, end] seconds after open
    "entry_max_price": 0.55,      # max fill (0..1); 1.0 disables the limit
    "exit_on_reversal": True,     # close early if price crosses back over the open price
    # Per-coin overrides (best params per coin, from the 1-day backtest). Any key
    # omitted here falls back to the global value above.
    "per_coin": {
        "btc": {"time_entry": [45.0, 165.0], "entry_max_price": 0.65, "exit_on_reversal": False},
        "eth": {"time_entry": [10.0, 15.0], "entry_max_price": 0.65, "exit_on_reversal": False},
        "bnb": {"time_entry": [20.0, 35.0], "entry_max_price": 0.52, "exit_on_reversal": False},
    },
    "notify": {
        "level": "full",          # "full" | "compact" | "settle"
        "startup": True,
        "daily_summary": True,    # send an automatic daily recap
        "daily_summary_time": "00:05",  # UTC HH:MM
    },
    "prices": {
        "ws_url": "wss://data-stream.binance.vision/stream",
    },
    "api": {
        "key": "",                       # predict.fun x-api-key (optional; enables real fills)
        "base_url": "https://api.predict.fun",
        "real_fill": True,               # use live order book fill when a key is present
        "rate_limits": {                 # basic-tier limits (requests per minute)
            "general": 250,
            "trading": 500,
            "search": 120,
            "trading_per_second": 40,
        },
    },
    "initial_balance": 100.0,
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _origin_tree(value: Any, origin: str) -> Any:
    if isinstance(value, dict):
        return {k: _origin_tree(v, origin) for k, v in value.items()}
    return origin


def _merge_origins(base: dict, override: dict, origin: str) -> dict:
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _merge_origins(out[key], value, origin)
        else:
            out[key] = origin
    return out


def load_config_state() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(cfg, origins)`` with defaults merged under any config file/env.

    ``origins`` is a tree mirroring ``cfg`` where each leaf is one of
    ``ORIGIN_DEFAULT`` / ``ORIGIN_FILE`` / ``ORIGIN_ENV``.
    """
    defaults = copy.deepcopy(DEFAULTS)
    cfg = copy.deepcopy(DEFAULTS)
    origins = _origin_tree(defaults, ORIGIN_DEFAULT)

    if CONFIG_FILE.exists():
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw = {}
        cfg = _deep_merge(cfg, raw)
        origins = _merge_origins(origins, raw, ORIGIN_FILE)
        # Legacy mapping: config.json written before ``time_entry`` used the
        # ``entry_delay_seconds``/``entry_delay_max_seconds`` pair.  Translate it
        # so an old VPS config keeps behaving the same after the upgrade.
        if "time_entry" not in raw and isinstance(raw.get("entry_delay_seconds"), (int, float)):
            start = float(raw["entry_delay_seconds"])
            end = float(raw.get("entry_delay_max_seconds", start))
            cfg["time_entry"] = [start, max(start, end)]
            origins["time_entry"] = ORIGIN_FILE

    token = os.environ.get("PREDICTFUN_TELEGRAM_TOKEN", "").strip()
    if token:
        cfg["telegram"]["token"] = token
        origins["telegram"]["token"] = ORIGIN_ENV
    chat_id = os.environ.get("PREDICTFUN_TELEGRAM_CHAT_ID", "").strip()
    if chat_id and chat_id.lstrip("-").isdigit():
        cfg["telegram"]["chat_id"] = int(chat_id)
        origins["telegram"]["chat_id"] = ORIGIN_ENV
    api_key = os.environ.get("PREDICTFUN_API_KEY", "").strip()
    if api_key:
        cfg["api"]["key"] = api_key
        origins["api"]["key"] = ORIGIN_ENV

    return cfg, origins


def load_config() -> dict[str, Any]:
    """Convenience wrapper returning just the effective config dict."""
    cfg, _ = load_config_state()
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def get(cfg: dict[str, Any], dotted: str) -> Any:
    """Read a nested key like ``trading.stake`` from the config dict."""
    node: Any = cfg
    for part in dotted.split("."):
        node = node[part]
    return node


def get_source(origins: dict[str, Any], dotted: str) -> str:
    node: Any = origins
    for part in dotted.split("."):
        node = node[part]
    return node


def set_key(cfg: dict[str, Any], dotted: str, value: Any) -> None:
    """Set a nested key, creating intermediate dicts as needed."""
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


# ── validation for the /set command ────────────────────────────────────────
_BOOLS = {"on": True, "off": False, "true": True, "false": False, "1": True, "0": False}

# Curated, human-readable list for `/set` without arguments.
SETTABLE_KEYS = [
    ("trading.enabled", "on|off", "hidup/mati trading"),
    ("trading.coins", "btc,eth,bnb", "subset coin (pisah koma)"),
    ("trading.stake", "angka", "USDT per trade (> 0)"),
    ("trading.min_balance", "angka", "saldo minimum (>= 0)"),
    ("time_entry", "start,end", "jendela entry: detik mulai & batas setelah open (contoh 1,10)"),
    ("entry_max_price", "angka", "maks fill price 0..1 (1.0 = nonaktif)"),
    ("exit_on_reversal", "on|off", "close posisi saat harga balik melewati open price"),
    ("per_coin.btc.time_entry", "start,end", "jendela entry khusus BTC (contoh 45,165)"),
    ("per_coin.eth.entry_max_price", "angka", "maks fill khusus ETH (0..1)"),
    ("per_coin.bnb.exit_on_reversal", "on|off", "exit-on-reversal khusus BNB"),
    # pola yang sama untuk coin lain: per_coin.<coin>.<time_entry|entry_max_price|exit_on_reversal>
    ("telegram.style", "HTML|MarkdownV2", "format pesan"),
    ("notify.level", "full|compact|settle", "level notifikasi"),
    ("notify.startup", "on|off", "pesan start saat boot"),
    ("notify.daily_summary", "on|off", "rekap harian otomatis"),
    ("notify.daily_summary_time", "HH:MM", "jam rekap (UTC)"),
    ("api.real_fill", "on|off", "order fill real (butuh api.key)"),
]


def validate_setting(dotted: str, raw_value: str) -> tuple[bool, Any, str | None]:
    """Return ``(ok, parsed_value, message)`` for a whitelisted config key.

    Only these keys are editable from Telegram; everything else is rejected so
    a stray message cannot poison the running bot.
    """
    v = raw_value.strip()
    # per-coin override: per_coin.<coin>.<time_entry|entry_max_price|exit_on_reversal>
    if dotted.startswith("per_coin."):
        parts = dotted.split(".")
        if len(parts) != 3 or parts[1] not in ALL_COINS or parts[2] not in (
                "time_entry", "entry_max_price", "exit_on_reversal"):
            return False, None, "Format: per_coin.<coin>.<kunci> — coin: btc/eth/bnb"
        dotted = parts[2]  # divalidasi dengan aturan yang sama seperti kunci global
    if dotted in ("trading.enabled", "notify.startup", "api.real_fill", "notify.daily_summary",
                  "exit_on_reversal"):
        if v.lower() not in _BOOLS:
            return False, None, "Gunakan `on` atau `off`"
        return True, _BOOLS[v.lower()], None
    if dotted == "time_entry":
        parts = [p for p in v.replace(",", " ").split() if p]
        if len(parts) != 2:
            return False, None, "Format: start,end (contoh: 1,10)"
        try:
            start, end = float(parts[0]), float(parts[1])
        except ValueError:
            return False, None, "time_entry harus dua angka (detik)"
        if not (0 <= start <= end <= 299):
            return False, None, "Rentang 0..299 detik, dan start <= end"
        return True, [start, end], None
    if dotted == "trading.coins":
        coins = [c.strip().lower() for c in v.replace(",", " ").split() if c.strip()]
        bad = [c for c in coins if c not in ALL_COINS]
        if bad:
            return False, None, f"Coin tidak dikenal: {', '.join(bad)}. Pilihan: {', '.join(ALL_COINS)}"
        if not coins:
            return False, None, "Minimal satu coin (btc,eth,bnb)"
        return True, coins, None
    if dotted in ("trading.stake", "trading.min_balance", "entry_max_price"):
        try:
            val = float(v)
        except ValueError:
            return False, None, f"{dotted} harus angka"
        if dotted == "trading.stake" and val <= 0:
            return False, None, "Stake harus > 0"
        if dotted == "trading.min_balance" and val < 0:
            return False, None, "Tidak boleh negatif"
        if dotted == "entry_max_price" and not (0.0 < val <= 1.0):
            return False, None, "max_price antara 0 dan 1 (1.0 = nonaktif)"
        return True, val, None
    if dotted == "telegram.style":
        s = v.lower()
        if s == "html":
            return True, "HTML", None
        if s in ("markdown", "markdownv2"):
            return True, "MarkdownV2", None
        return False, None, "Pilih HTML atau MarkdownV2"
    if dotted == "telegram.chat_id":
        try:
            val = int(v)
        except ValueError:
            return False, None, "chat_id harus angka"
        return True, val, None
    if dotted == "notify.daily_summary_time":
        try:
            hour, minute = v.split(":")
            hour, minute = int(hour), int(minute)
        except (ValueError, AttributeError):
            return False, None, "Format HH:MM (contoh 00:05)"
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return False, None, "Jam 0-23, menit 0-59"
        return True, f"{hour:02d}:{minute:02d}", None
    if dotted == "notify.level":
        level = v.lower()
        if level not in ("full", "compact", "settle"):
            return False, None, "Pilih full, compact, atau settle"
        return True, level, None
    if dotted == "mode":
        mode = v.lower()
        if mode not in ("paper", "live"):
            return False, None, "Pilih paper atau live"
        return True, mode, None
    return False, None, f"`{dotted}` tidak bisa diubah. Kirim /set untuk daftar kunci."