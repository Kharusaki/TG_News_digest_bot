"""Чтение RSS-лент. HTML-страницы не парсим: только официальные ленты.

Все адреса проверены 20.11.2026: отдают XML, содержат дату публикации и ссылку
на материал. Поэтому каждый пункт дайджеста можно проверить и открыть.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import feedparser
import httpx

from .base import clean_text, is_fresh, make_item, parse_date

logger = logging.getLogger(__name__)

CONSULTANT_HOTDOCS_RSS = "https://www.consultant.ru/rss/hotdocs.xml"
CONSULTANT_FD_RSS = "https://www.consultant.ru/rss/fd.xml"
GARANT_NEWS_RSS = "https://rss.garant.ru/categories/news/"
KLERK_RSS = "https://www.klerk.ru/xml/index.xml"
MINFIN_RSS = "https://minfin.gov.ru/rss_news?mod=news&lim=50"
NALOG_RSS = "https://www.nalog.gov.ru/rn77/rss/"
AUDIT_IT_AUDIT_RSS = "https://www.audit-it.ru/rss/news_audit.xml"
AUDIT_IT_MSFO_RSS = "https://www.audit-it.ru/rss/news_msfo.xml"
AUDIT_IT_ALL_RSS = "https://www.audit-it.ru/rss/news_all.xml"
AUDIT_IT_ACCOUNT_RSS = "https://www.audit-it.ru/rss/news_account.xml"
GARANT_ACCOUNT_RSS = "https://rss.garant.ru/consult/account/"
HABR_AI_RSS = "https://habr.com/ru/rss/hubs/artificial_intelligence/articles/?fl=ru"
MICROSOFT_RESEARCH_RSS = "https://news.microsoft.com/source/feed/"
OPENAI_NEWS_RSS = "https://openai.com/news/rss.xml"

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AI-Digest-Bot/2.0)"}


def entry_date(entry: Any) -> datetime | None:
    """Дата записи: сначала структура feedparser, затем строка."""
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        value = getattr(entry, field, None)
        if value:
            parsed = parse_date(value)
            if parsed is not None:
                return parsed
    for field in ("published", "updated", "created", "dc_date"):
        value = getattr(entry, field, None)
        if value:
            parsed = parse_date(value)
            if parsed is not None:
                return parsed
    return None


def _source_name(url: str) -> str:
    return urlsplit(url).netloc.removeprefix("www.") or url


async def fetch_rss(
    url: str,
    limit: int = 10,
    keywords: tuple[str, ...] = (),
    *,
    source: str = "",
    max_age_days: int | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Прочитать RSS и вернуть только свежие записи с датой и ссылкой."""
    try:
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers=_HEADERS,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.content
    except Exception as exc:
        logger.warning("Не удалось прочитать RSS %s: %s", url, exc)
        return []

    try:
        parsed = await asyncio.to_thread(feedparser.parse, payload)
    except Exception as exc:
        logger.warning("Не удалось разобрать RSS %s: %s", url, exc)
        return []

    lowered = tuple(keyword.lower() for keyword in keywords)
    items: list[dict[str, Any]] = []
    for entry in parsed.entries:
        title = clean_text(getattr(entry, "title", ""))
        text = clean_text(
            getattr(entry, "summary", "")
            or getattr(entry, "description", "")
            or getattr(entry, "content", "")
        )
        haystack = f"{title} {text}".lower()
        if lowered and not any(word in haystack for word in lowered):
            continue
        published_at = entry_date(entry)
        if max_age_days is not None and not is_fresh(published_at, max_age_days, now):
            continue
        item = make_item(
            title,
            getattr(entry, "link", ""),
            text,
            source or _source_name(url),
            published_at,
        )
        if item is not None:
            items.append(item)
        if len(items) >= limit:
            break
    return items


__all__ = (
    "AUDIT_IT_ACCOUNT_RSS",
    "AUDIT_IT_ALL_RSS",
    "AUDIT_IT_AUDIT_RSS",
    "AUDIT_IT_MSFO_RSS",
    "CONSULTANT_FD_RSS",
    "CONSULTANT_HOTDOCS_RSS",
    "GARANT_ACCOUNT_RSS",
    "GARANT_NEWS_RSS",
    "HABR_AI_RSS",
    "KLERK_RSS",
    "MICROSOFT_RESEARCH_RSS",
    "MINFIN_RSS",
    "NALOG_RSS",
    "OPENAI_NEWS_RSS",
    "entry_date",
    "fetch_rss",
)
