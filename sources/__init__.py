"""Оркестрация источников по темам и общая очистка материалов."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import Counter
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from .base import normalize_url, sort_by_date
from .hh import HH_RSS_URL, TRUDVSEM_API, fetch_vacancies
from .rss import (
    AUDIT_IT_ACCOUNT_RSS,
    AUDIT_IT_ALL_RSS,
    AUDIT_IT_AUDIT_RSS,
    AUDIT_IT_MSFO_RSS,
    CONSULTANT_FD_RSS,
    CONSULTANT_HOTDOCS_RSS,
    GARANT_ACCOUNT_RSS,
    GARANT_NEWS_RSS,
    HABR_AI_RSS,
    KLERK_RSS,
    MICROSOFT_RESEARCH_RSS,
    MINFIN_RSS,
    NALOG_RSS,
    OPENAI_NEWS_RSS,
    fetch_rss,
)

logger = logging.getLogger(__name__)

MSFO_KEYWORDS = (
    "мсфо",
    "ifrs",
    "iasb",
    "мса",
    "аудит",
    "консолидирован",
)
NEWS_KEYWORDS = (
    "бухгалтер",
    "налог",
    "отчетност",
    "отчётност",
    "аудит",
    "мсфо",
    "ндс",
    "взнос",
    "штраф",
    "проверк",
    "учет",
    "учёт",
    "баланс",
    "финанс",
    "бюджет",
)
NEWS_TITLE_KEYWORDS = (
    "налог",
    "ндс",
    "взнос",
    "штраф",
    "проверк",
    "учет",
    "учёт",
    "отчетност",
    "отчётност",
    "баланс",
    "бюджет",
    "аудит",
    "мсфо",
    "бухгалтер",
    "финанс",
    "льгот",
    "тариф",
    "пенсион",
    "депозит",
    "контрол",
    "страхов",
    "ставк",
)
TECH_TITLE_RE = re.compile(
    r"(?<![a-z])(ai|ml|llm|gpt)(?![a-z])"
    r"|искусствен|нейросет|нейрон|агент|модел|машинн|робот|данн"
    r"|claude|nvidia|openai|microsoft|chatgpt|copilot|harness|харнесс|промпт",
    re.IGNORECASE,
)
MAX_RAW_ITEMS = 15
NEWS_SOURCE_LIMIT = 10
MSFO_SOURCE_LIMIT = 10
TECH_SOURCE_LIMIT = 10

# Пары «лента — подпись источника». Все адреса — RSS, без HTML-парсинга.
FEEDS: dict[str, tuple[tuple[str, str], ...]] = {
    "news": (
        (CONSULTANT_HOTDOCS_RSS, "КонсультантПлюс"),
        (CONSULTANT_FD_RSS, "КонсультантПлюс"),
        (GARANT_NEWS_RSS, "Гарант"),
        (KLERK_RSS, "Клерк"),
        (MINFIN_RSS, "Минфин"),
        (NALOG_RSS, "ФНС"),
    ),
    "msfo": (
        (AUDIT_IT_AUDIT_RSS, "Audit-it"),
        (AUDIT_IT_MSFO_RSS, "Audit-it"),
        (AUDIT_IT_ALL_RSS, "Audit-it"),
        (AUDIT_IT_ACCOUNT_RSS, "Audit-it"),
        (KLERK_RSS, "Клерк"),
        (MINFIN_RSS, "Минфин"),
        (GARANT_ACCOUNT_RSS, "Гарант"),
        (CONSULTANT_HOTDOCS_RSS, "КонсультантПлюс"),
        (CONSULTANT_FD_RSS, "КонсультантПлюс"),
    ),
    "tech": (
        (HABR_AI_RSS, "Habr"),
        (MICROSOFT_RESEARCH_RSS, "Microsoft Research"),
        (OPENAI_NEWS_RSS, "OpenAI"),
    ),
}


def _normalize(value: str) -> str:
    text = re.sub(r"[^\w\s]", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def dedupe(items: list[dict[str, Any]], limit: int = MAX_RAW_ITEMS) -> list[dict[str, Any]]:
    """Убрать повторы по нормализованному заголовку и по URL."""
    result: list[dict[str, Any]] = []
    titles: set[str] = set()
    urls: set[str] = set()
    for item in items:
        title = _normalize(item.get("title", ""))
        url = normalize_url(item.get("url", ""))
        if title and title in titles:
            continue
        if url and url in urls:
            continue
        if title:
            titles.add(title)
        if url:
            urls.add(url)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def builtin_sources() -> dict[str, tuple[tuple[str, str], ...]]:
    """Вшитые источники для справки: ``тема -> [(название, домен)]``."""
    result: dict[str, tuple[tuple[str, str], ...]] = {}
    for topic, feeds in FEEDS.items():
        seen: list[tuple[str, str]] = []
        for url, name in feeds:
            pair = (name, urlsplit(url).netloc.removeprefix("www."))
            if pair not in seen:
                seen.append(pair)
        result[topic] = tuple(seen)
    result["jobs"] = (
        ("hh.ru", urlsplit(HH_RSS_URL).netloc.removeprefix("www.")),
        ("Работа России", urlsplit(TRUDVSEM_API).netloc.removeprefix("www.")),
    )
    return result


def max_age_days(topic: str) -> int:
    """Лимит свежести: вакансии живут дольше новостей."""
    default = "14" if topic == "jobs" else "3"
    name = "DIGEST_MAX_AGE_DAYS_JOBS" if topic == "jobs" else "DIGEST_MAX_AGE_DAYS"
    try:
        value = int(os.getenv(name, default))
    except ValueError:
        value = int(default)
    return max(1, value)


async def _collect_feeds(topic: str, now: datetime | None) -> list[dict[str, Any]]:
    feeds = FEEDS.get(topic, ())
    age = max_age_days(topic)
    tasks = []
    for url, source in feeds:
        if topic == "msfo":
            keywords = MSFO_KEYWORDS
        elif topic == "news":
            keywords = NEWS_KEYWORDS
        else:
            keywords = ()
        limit = MSFO_SOURCE_LIMIT if topic == "msfo" else NEWS_SOURCE_LIMIT
        if topic == "tech":
            limit = TECH_SOURCE_LIMIT
        tasks.append(
            fetch_rss(
                url,
                limit=limit,
                keywords=keywords,
                source=source,
                max_age_days=age,
                now=now,
            )
        )
    groups = await asyncio.gather(*tasks)
    flat = [item for group in groups for item in group]
    if topic == "news":
        flat = [
            item
            for item in flat
            if any(word in item["title"].lower() for word in NEWS_TITLE_KEYWORDS)
        ]
    elif topic == "tech":
        flat = [
            item
            for item in flat
            if item.get("source") != "Habr" or TECH_TITLE_RE.search(item["title"])
        ]
    return flat


async def collect_topic_materials(
    topic: str,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Собрать свежие материалы темы, убрать дубли и отсортировать по дате."""
    age = max_age_days(topic)
    if topic == "jobs":
        flat = await fetch_vacancies(max_age_days=age, now=now)
    elif topic in FEEDS:
        flat = await _collect_feeds(topic, now)
    else:
        return []

    flat = sort_by_date(flat)
    result = dedupe(flat)
    counts = Counter(item.get("source", "?") for item in result)
    logger.info(
        "Тема %s: собрано %d материалов (лимит %d дн.), источники: %s",
        topic,
        len(result),
        age,
        dict(counts),
    )
    return result


__all__ = (
    "FEEDS",
    "MAX_RAW_ITEMS",
    "MSFO_KEYWORDS",
    "builtin_sources",
    "collect_topic_materials",
    "dedupe",
    "max_age_days",
)
