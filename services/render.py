"""Детерминированная сборка HTML-блока темы.

Заголовок, дата и ссылка берутся только из материала источника. LLM отдаёт
исключительно «суть», поэтому выдуманных ссылок или дат в сообщении быть не
может.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any

from sources import max_age_days
from sources.base import MOSCOW, UTC, date_label

TOPIC_HEADERS = {
    "news": "📰 Новости отрасли",
    "jobs": "💼 Вакансии",
    "tech": "🧠 Технологии",
    "msfo": "📊 Аудит и МСФО",
    "custom": "🔌 Мои источники",
}
FALLBACK_CHARS = 240
MAX_RENDER_ITEMS = 7


def _escaped(value: Any) -> str:
    return html.escape(str(value or "").strip(), quote=False)


def _link(url: str) -> str:
    safe = html.escape(str(url or "").strip(), quote=True)
    return f'<a href="{safe}">Открыть источник</a>'


def _fallback(item: dict[str, Any]) -> str:
    text = str(item.get("text") or "").strip()
    if not text:
        return ""
    if len(text) <= FALLBACK_CHARS:
        return text
    return text[:FALLBACK_CHARS].rsplit(" ", 1)[0] + "…"


def render_topic_block(
    topic: str,
    items: list[dict[str, Any]],
    llm: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> str:
    """Собрать один блок: шапка, пункты с датой и ссылкой, примечание."""
    header = TOPIC_HEADERS.get(topic, topic)
    if not items:
        return empty_topic_block(topic, now)

    data = llm or {}
    summaries = data.get("items") or {}
    lines = [f"<b>{header}</b>"]
    intro = str(data.get("intro") or "").strip()
    if intro:
        lines.append(_escaped(intro))

    shown = 0
    for index, item in enumerate(items, start=1):
        if shown >= MAX_RENDER_ITEMS:
            break
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        title = _escaped(item.get("title")) or "Без заголовка"
        published = item.get("published_at")
        label = date_label(published if isinstance(published, datetime) else None)
        source = _escaped(item.get("source")) or "Источник"
        stamp = f"🗓 {label} · {source}" if label else f"🗓 {source}"

        shown += 1
        lines.append(f"{shown}. <b>{title}</b>")
        lines.append(stamp)
        summary = str(summaries.get(index) or "").strip() if isinstance(summaries, dict) else ""
        body = _escaped(summary) if summary else _escaped(_fallback(item))
        if body:
            lines.append(body)
        lines.append(_link(url))

    if shown == 0:
        return empty_topic_block(topic, now)
    return "\n".join(lines)


def _days_word(days: int) -> str:
    if days % 10 == 1 and days % 100 != 11:
        return "день"
    if 2 <= days % 10 <= 4 and not 12 <= days % 100 <= 14:
        return "дня"
    return "дней"


def empty_topic_block(topic: str, now: datetime | None = None) -> str:
    """Текст для темы, где после фильтра свежести не осталось материалов."""
    header = TOPIC_HEADERS.get(topic, topic)
    days = max_age_days(topic)
    noun = "свежих вакансий" if topic == "jobs" else "свежих материалов"
    return f"<b>{header}</b>\nЗа последние {days} {_days_word(days)} {noun} не найдено."


def render_custom_block(
    items: list[dict[str, Any]],
    now: datetime | None = None,
) -> str:
    """Блок пользовательских лент без LLM: только проверенные поля источника."""
    if not items:
        return ""
    return render_topic_block("custom", items, None, now)


def digest_header(now: datetime | None = None) -> str:
    current = (now or datetime.now(UTC)).astimezone(MOSCOW)
    return f"📬 Дайджест за {current.strftime('%d.%m.%Y')}"



__all__ = (
    "TOPIC_HEADERS",
    "digest_header",
    "empty_topic_block",
    "render_custom_block",
    "render_topic_block",
)
