"""Две раздельные задачи: сбор в 08:30 и доставка по часам пользователя.

Сбор — единственный прогон источников на день: 4 вызова LLM и 4 блока в кеше.
Доставка только читает кеш и потому одинаково дешёвая для любого числа людей.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from aiogram import Bot

from bot.utils import send_html
from database.db import TOPICS, get_users_for_delivery, mark_delivered
from services.cache import EMPTY_TTL_SECONDS, TTL_SECONDS, get_digest, set_digest
from services.digest import render_user_digest
from services.llm import summarize
from services.render import empty_topic_block, render_topic_block
from sources import collect_topic_materials
from sources.base import MOSCOW, UTC

logger = logging.getLogger(__name__)


async def ensure_digests(force: bool = False) -> None:
    """Сгенерировать недостающие блоки: один вызов LLM на тему.

    Пустая тема тоже кешируется, но на 30 минут: пользователь видит честный
    текст, а к вечеру блок может дозаполниться без лишних вызовов LLM.
    """
    for topic in TOPICS:
        if not force and await get_digest(topic):
            continue
        materials = await collect_topic_materials(topic)
        if not materials:
            logger.info("Тема %s пуста после фильтра свежести", topic)
            await set_digest(topic, empty_topic_block(topic), ttl=EMPTY_TTL_SECONDS)
            continue
        llm = await summarize(topic, materials)
        block = render_topic_block(topic, materials, llm)
        has_summaries = bool(llm.get("items")) if isinstance(llm, dict) else False
        ttl = TTL_SECONDS if has_summaries else EMPTY_TTL_SECONDS
        await set_digest(topic, block, ttl=ttl)
        logger.info(
            "Тема %s сгенерирована и закэширована (конспектов LLM: %d)",
            topic,
            len(llm.get("items") or {}) if isinstance(llm, dict) else 0,
        )


async def build_digest_for(
    topics: dict[str, Any],
    now: datetime | None = None,
    user_id: int | None = None,
) -> str | None:
    """Для ручного запроса: наполнить кеш, затем собрать текст под темы."""
    await ensure_digests()
    return await render_user_digest(topics, now, user_id)


async def collect_digests_job() -> None:
    """Ежедневный сбор в 08:30: свежие блоки для всех тем, независимо от людей."""
    logger.info("Плановый сбор дайджеста: обновляю все темы")
    await ensure_digests(force=True)
    logger.info("Плановый сбор завершён, блоки готовы к рассылке из кеша")


async def deliver_digests(
    bot: Bot,
    hour: int | None = None,
    now: datetime | None = None,
) -> None:
    """Раз в час раздать кеш тем, кто выбрал этот час, без новых вызовов LLM."""
    current = (now or datetime.now(UTC)).astimezone(MOSCOW)
    target_hour = current.hour if hour is None else int(hour)
    day = current.strftime("%Y-%m-%d")
    subscribers = get_users_for_delivery(target_hour, day)
    if not subscribers:
        logger.debug("Рассылка в %02d:00 — получателей нет", target_hour)
        return
    # Если пользователь выбрал час раньше планового сбора, отправлять нечего:
    # лучше промолчать, чем прислать блоки «материалов не найдено».
    cached = [await get_digest(topic, current) for topic in TOPICS]
    if not any(cached):
        logger.info("Рассылка в %02d:00 пропущена: кеш за сегодня ещё не собран", target_hour)
        return
    logger.info("Рассылка в %02d:00: получателей %d", target_hour, len(subscribers))
    for subscriber in subscribers:
        text = await render_user_digest(subscriber["topics"], current, subscriber["user_id"])
        if not text:
            continue
        try:
            await send_html(bot, subscriber["user_id"], text)
        except Exception as exc:
            logger.warning(
                "Не удалось отправить дайджест пользователю %s: %s",
                subscriber["user_id"],
                exc,
            )
            continue
        mark_delivered(subscriber["user_id"], day)


__all__ = (
    "build_digest_for",
    "collect_digests_job",
    "deliver_digests",
    "ensure_digests",
)
