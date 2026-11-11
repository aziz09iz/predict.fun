"""Message formatting for Telegram (HTML and MarkdownV2).

The bot composes messages through one small helper so a single code path can
emit either parse mode.  Dynamic values (prices, balances, slugs) are escaped
for the active style; static formatting wrappers add the right markup.
"""
from __future__ import annotations

import html
import re

_MDV2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!])")


def esc_html(text: str) -> str:
    return html.escape(str(text), quote=False)


def esc_mdv2(text: str) -> str:
    return _MDV2_SPECIAL.sub(r"\\\1", str(text))


class Fmt:
    def __init__(self, style: str = "HTML"):
        self.style = "MarkdownV2" if style and style.upper() == "MARKDOWNV2" else "HTML"

    def esc(self, text: str) -> str:
        return esc_mdv2(text) if self.style == "MarkdownV2" else esc_html(text)

    def b(self, text: str) -> str:
        if self.style == "MarkdownV2":
            return f"*{esc_mdv2(text)}*"
        return f"<b>{esc_html(text)}</b>"

    def code(self, text: str) -> str:
        if self.style == "MarkdownV2":
            return f"`{esc_mdv2(text)}`"
        return f"<code>{esc_html(text)}</code>"

    def link(self, text: str, url: str) -> str:
        if self.style == "MarkdownV2":
            return f"[{esc_mdv2(text)}]({esc_mdv2(url)})"
        return f'<a href="{esc_html(url)}">{esc_html(text)}</a>'

    def money(self, value: float, with_sign: bool = False) -> str:
        sign = ""
        if with_sign and value > 0:
            sign = "+"
        return f"{sign}${value:,.2f}"

    def price(self, value: float) -> str:
        return f"${value:,.2f}"


def format_coin(c: str) -> str:
    """Upper-case the ticker for display."""
    return c.upper()


def direction_tag(direction: str | None) -> tuple[str, str]:
    """Return (label, emoji) for a direction."""
    if direction == "UP":
        return "UP", "🟢"
    if direction == "DOWN":
        return "DOWN", "🔴"
    return "—", "⚪"


def outcome_tag(outcome: str | None) -> tuple[str, str]:
    if outcome == "WIN":
        return "WIN", "✅"
    if outcome == "LOSS":
        return "LOSS", "❌"
    if outcome == "PUSH":
        return "PUSH", "➖"
    if outcome == "UNRESOLVED":
        return "SKIPPED", "〰️"
    return "OPEN", "⏳"