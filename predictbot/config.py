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
        "style": "HTML",          # "HTML" or "MarkdownV2"
    },
    "mode": "paper",              # "paper" (simulated). "live" is not implemented.
    "trading": {
        "enabled": True,
        "coins": ["btc", "eth", "bnb"],
        "stake": 1.0,             # USDT risked per trade
        "min_balance": 0.0,       # stop opening trades below this cash level
    },
    "payout": {
        "win_multiplier": 1.8,    # fallback odds when a real fill can't be fetched
    },
    "entry_delay_seconds": 1,     # decide direction 1s after market open
    "notify": {
        "level": "full",          # "full" | "compact" | "settle"
        "startup": True,
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
    ("payout.win_multiplier", "angka", "odds fallback saat fill real tak tersedia (> 1)"),
    ("entry_delay_seconds", "angka", "delay entry setelah open (0..120)"),
    ("telegram.style", "HTML|MarkdownV2", "format pesan"),
    ("notify.level", "full|compact|settle", "level notifikasi"),
    ("notify.startup", "on|off", "pesan start saat boot"),
    ("api.real_fill", "on|off", "order fill real (butuh api.key)"),
]


def validate_setting(dotted: str, raw_value: str) -> tuple[bool, Any, str | None]:
    """Return ``(ok, parsed_value, message)`` for a whitelisted config key.

    Only these keys are editable from Telegram; everything else is rejected so
    a stray message cannot poison the running bot.
    """
    v = raw_value.strip()
    if dotted in ("trading.enabled", "notify.startup", "api.real_fill"):
        if v.lower() not in _BOOLS:
            return False, None, "Gunakan `on` atau `off`"
        return True, _BOOLS[v.lower()], None
    if dotted == "trading.coins":
        coins = [c.strip().lower() for c in v.replace(",", " ").split() if c.strip()]
        bad = [c for c in coins if c not in ALL_COINS]
        if bad:
            return False, None, f"Coin tidak dikenal: {', '.join(bad)}. Pilihan: {', '.join(ALL_COINS)}"
        if not coins:
            return False, None, "Minimal satu coin (btc,eth,bnb)"
        return True, coins, None
    if dotted in ("trading.stake", "trading.min_balance", "payout.win_multiplier", "entry_delay_seconds"):
        try:
            val = float(v)
        except ValueError:
            return False, None, f"{dotted} harus angka"
        if dotted == "trading.stake" and val <= 0:
            return False, None, "Stake harus > 0"
        if dotted == "trading.min_balance" and val < 0:
            return False, None, "Tidak boleh negatif"
        if dotted == "payout.win_multiplier" and val <= 1.0:
            return False, None, "Multiplier harus > 1.0"
        if dotted == "entry_delay_seconds" and not (0 <= val <= 120):
            return False, None, "Delay antara 0 dan 120 detik"
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