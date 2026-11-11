"""Minimal Telegram Bot API transport (long-polling) built on httpx.

Kept dependency-light on purpose: only httpx is needed, no framework version
juggling.  The bot uses text updates (commands) and sends formatted messages
with either HTML or MarkdownV2 parse mode.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

import httpx

log = logging.getLogger("predictbot.telegram")

API_BASE = "https://api.telegram.org"


class Telegram:
    def __init__(self, token: str, style: str = "HTML"):
        self.token = token
        self.style = "MarkdownV2" if style.upper() == "MARKDOWNV2" else "HTML"
        # The read timeout must exceed the getUpdates long-poll ``timeout``
        # (50s), otherwise a slow poll trips httpx and spams the log every ~33s.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15.0, read=65.0, write=15.0, pool=15.0)
        )
        self._offset: int | None = None
        self._stop = asyncio.Event()

    @property
    def base(self) -> str:
        return f"{API_BASE}/bot{self.token}"

    async def api(self, method: str, **params: Any) -> dict[str, Any]:
        url = f"{self.base}/{method}"
        resp = await self._client.post(url, json=params)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            log.warning("Telegram API %s failed: %s", method, data)
        return data

    async def get_me(self) -> dict[str, Any]:
        return await self.api("getMe")

    async def send(self, chat_id: int, text: str) -> Optional[dict[str, Any]]:
        """Send a message; fall back to plain text if formatting is rejected."""
        try:
            return await self.api("sendMessage", chat_id=chat_id, text=text, parse_mode=self.style)
        except httpx.HTTPStatusError as exc:
            # Telegram rejects malformed markup with a 400; retry unformatted.
            if exc.response.status_code == 400:
                try:
                    return await self.api("sendMessage", chat_id=chat_id, text=text)
                except Exception as send_exc:
                    log.error("Telegram send fallback failed: %s", send_exc)
            else:
                log.error("Telegram send failed: %s", exc)
        except Exception as exc:
            log.error("Telegram send failed: %s", exc)
        return None

    async def set_commands(self, commands: list[dict[str, str]]) -> None:
        try:
            await self.api("setMyCommands", commands=commands)
        except Exception as exc:
            log.warning("setMyCommands failed: %s", exc)

    async def run_polling(self, on_update: Callable[[dict[str, Any]], Any]) -> None:
        """Long-poll getUpdates and hand each update to ``on_update``."""
        self._stop.clear()
        log.info("Telegram long-polling started")
        while not self._stop.is_set():
            params: dict[str, Any] = {"timeout": 50, "allowed_updates": ["message"]}
            if self._offset is not None:
                params["offset"] = self._offset
            try:
                data = await self.api("getUpdates", **params)
                for update in data.get("result", []):
                    self._offset = int(update["update_id"]) + 1
                    try:
                        result = on_update(update)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as exc:
                        log.exception("Update handler error: %s", exc)
            except asyncio.CancelledError:
                raise
            except httpx.TimeoutException:
                # Normal for a slow long poll / network blip — retry right away.
                log.debug("getUpdates timed out; retrying")
            except Exception as exc:
                log.warning("getUpdates error (%s): %s", type(exc).__name__, exc)
                await asyncio.sleep(3)
        log.info("Telegram long-polling stopped")

    def stop(self) -> None:
        self._stop.set()

    async def aclose(self) -> None:
        await self._client.aclose()