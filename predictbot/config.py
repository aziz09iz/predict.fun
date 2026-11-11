"""Configuration loading, defaults, validation, and persistence.

The bot keeps a single ``config.json`` next to the project root.  Entries are
merged onto defaults at boot so a partial file still works.  Sensitive values
(the Telegram token / chat id) can also come from environment variables so the
systemd unit can inject them without writing secrets into the JSON file.
"""
from __future__ import annotations

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

DEFAULTS: dict[str, Any] = {
    "telegram": {
        "token": "",
        "chat_id": None,          # admin chat id (settable via /start when empty)
        "style": "HTML",          # "HTML" or "MarkdownV2"
    },
    "trading": {
        "enabled": True,
        "coins": ["btc", "eth", "bnb"],
        "stake": 10.0,            # USDT risked per trade
        "min_balance": 0.0,       # stop opening trades below this cash level
    },
    "payout": {
        "win_multiplier": 2.0,    # 2.0 == fair coin flip: +stake on win, -stake on loss
    },
    "entry_delay_seconds": 1,     # strategy: decide direction 1s after market open
    "notify": {
        "startup": True,
        "on_trade": True,
        "on_settle": True,
    },
    "prices": {
        "ws_url": "wss://data-stream.binance.vision/stream",
    },
    "api": {
        "key": "",                # optional predict.fun x-api-key (not required for paper mode)
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


def load_config() -> dict[str, Any]:
    """Load ``config.json``, creating it from defaults if it does not exist."""
    if not CONFIG_FILE.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        save_config(DEFAULTS)
        return json.loads(json.dumps(DEFAULTS))

    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raw = {}
    cfg = _deep_merge(json.loads(json.dumps(DEFAULTS)), raw)

    # Environment overrides (used by systemd to avoid storing the token in JSON).
    token = os.environ.get("PREDICTFUN_TELEGRAM_TOKEN", "").strip()
    if token:
        cfg["telegram"]["token"] = token
    chat_id = os.environ.get("PREDICTFUN_TELEGRAM_CHAT_ID", "").strip()
    if chat_id and chat_id.lstrip("-").isdigit():
        cfg["telegram"]["chat_id"] = int(chat_id)

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


def set_key(cfg: dict[str, Any], dotted: str, value: Any) -> None:
    """Set a nested key, creating intermediate dicts as needed."""
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


# ── validation for the /set command ────────────────────────────────────────
_BOOLS = {"on": True, "off": False, "true": True, "false": False, "1": True, "0": False}


def validate_setting(dotted: str, raw_value: str) -> tuple[bool, Any, str]:
    """Return ``(ok, parsed_value, message)`` for a whitelisted config key.

    Only these keys are editable from Telegram; everything else is rejected so
    a stray message cannot poison the running bot.
    """
    v = raw_value.strip()
    if dotted == "trading.enabled":
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
    if dotted == "trading.stake":
        try:
            val = float(v)
        except ValueError:
            return False, None, "Stake harus angka"
        if val <= 0:
            return False, None, "Stake harus > 0"
        return True, val, None
    if dotted == "trading.min_balance":
        try:
            val = float(v)
        except ValueError:
            return False, None, "Angka yang valid"
        if val < 0:
            return False, None, "Tidak boleh negatif"
        return True, val, None
    if dotted == "payout.win_multiplier":
        try:
            val = float(v)
        except ValueError:
            return False, None, "Multiplier harus angka"
        if val <= 1.0:
            return False, None, "Multiplier harus > 1.0"
        return True, val, None
    if dotted == "entry_delay_seconds":
        try:
            val = float(v)
        except ValueError:
            return False, None, "Delay harus angka"
        if not 0 <= val <= 120:
            return False, None, "Delay antara 0 dan 120 detik"
        return True, val, None
    if dotted == "telegram.style":
        style = v.upper()
        if style not in ("HTML", "MARKDOWNV2", "MARKDOWN"):
            return False, None, "Pilih HTML atau MarkdownV2"
        return True, style, None
    if dotted == "telegram.chat_id":
        try:
            val = int(v)
        except ValueError:
            return False, None, "chat_id harus angka"
        return True, val, None
    if dotted == "notify.on_trade" or dotted == "notify.on_settle" or dotted == "notify.startup":
        if v.lower() not in _BOOLS:
            return False, None, "Gunakan `on` atau `off`"
        return True, _BOOLS[v.lower()], None
    return False, None, f"`{dotted}` tidak bisa diubah. Cek /settings untuk daftar kunci."