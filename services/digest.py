"""Сборка персонального дайджеста из готовых блоков.

Блоки уже отрендерены с датами и ссылками и лежат в кеше по дате. Здесь только
склейка: LLM не вызывается, поэтому число вызовов не зависит от пользователей.
Свои ленты пользователя добавляются отдельным блоком тоже без LLM.
"""

from __future__ import annotations

from datetime import datetime

from database.db import TOPICS, list_custom_sources
from services.cache import get_digest
from services.render import digest_header, empty_topic_block, render_custom_block
from sources import max_age_days
from sources.custom import fetch_custom_items

HEADER = "📬 Ваш утренний дайджест"


def _footer() -> str:
    news_days = max_age_days("news")
    job_days = max_age_days("jobs")
    return (
        f"<i>Свежесть: новости, технологии и аудит — {news_days} дн., "
        f"вакансии — {job_days} дн.</i>"
    )


async def render_user_digest(
    topics: dict[str, bool],
    now: datetime | None = None,
    user_id: int | None = None,
) -> str | None:
    """Собрать сообщение только из включённых тем.

    Если для включённой темы кеша нет, показываем честный текст «материалов не
    найдено», а не теряем тему молча. Для ``user_id`` добавляем блок «Мои
    источники» — он читается свежим, но не влияет на общий кеш и LLM.
    """
    blocks: list[str] = []
    for topic in TOPICS:
        if not topics.get(topic):
            continue
        text = await get_digest(topic, now)
        blocks.append(text.strip() if text and text.strip() else empty_topic_block(topic, now))
    if not blocks:
        return None
    if user_id:
        sources = list_custom_sources(user_id)
        if sources:
            items = await fetch_custom_items(sources, topics, max_age_days, now)
            custom_block = render_custom_block(items, now)
            if custom_block:
                blocks.append(custom_block)
    return f"{digest_header(now)}\n\n" + "\n\n".join(blocks) + f"\n\n{_footer()}"


__all__ = ("HEADER", "render_user_digest")
