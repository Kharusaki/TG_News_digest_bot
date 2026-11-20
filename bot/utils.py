"""Отправка длинных HTML-сообщений без разрыва тегов."""

from __future__ import annotations

from typing import Any

TELEGRAM_LIMIT = 4096
SAFE_LIMIT = 3900


def split_message(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """Разбить текст по строкам: каждая ссылка остаётся целой."""
    chunks: list[str] = []
    current = ""
    for line in str(text or "").split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if current and len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]


async def send_html(bot: Any, chat_id: int, text: str) -> None:
    """Отправить сообщение с HTML-разметкой и без превью ссылок."""
    for chunk in split_message(text):
        await bot.send_message(
            chat_id,
            chunk,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


__all__ = ("SAFE_LIMIT", "TELEGRAM_LIMIT", "send_html", "split_message")
