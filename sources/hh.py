"""Вакансии: RSS hh.ru и резервный открытый API «Работа России».

`api.hh.ru` блокирует IP этой машины (403), но публичная RSS-выдача
`hh.ru/search/vacancy/rss` работает напрямую, без прокси. Она отдаёт дату
публикации и ссылку на вакансию. Если RSS недоступен, используем
`opendata.trudvsem.ru`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from .base import UTC, is_fresh, make_item, parse_date, sort_by_date
from .rss import fetch_rss

logger = logging.getLogger(__name__)

HH_RSS_URL = "https://hh.ru/search/vacancy/rss"
TRUDVSEM_API = "https://opendata.trudvsem.ru/api/v1/vacancies"
QUERIES: tuple[str, ...] = ("аудитор", "МСФО", "бухгалтер")
AREA_RUSSIA = "113"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AI-Digest-Bot/2.0)"}


def _salary(raw: Any) -> str:
    """Привести зарплату из hh/trudvsem к короткой строке."""
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if not isinstance(raw, dict):
        return ""
    start = raw.get("from", raw.get("min"))
    end = raw.get("to", raw.get("max"))
    if start is None and end is None:
        return ""
    currency = str(raw.get("currency") or "").strip()
    if start is not None and end is not None:
        value = f"{start}–{end}"
    elif start is not None:
        value = f"от {start}"
    else:
        value = f"до {end}"
    return f"{value} {currency}".strip()


def _hh_url(query: str) -> str:
    params = urlencode(
        {"text": query, "area": AREA_RUSSIA, "search_field": "name"}
    )
    return f"{HH_RSS_URL}?{params}"


async def _fetch_hh(per_query: int, max_age_days: int, now: datetime) -> list[dict[str, Any]]:
    """По одному RSS-запросу на профессию; hh отдаёт максимум 20 записей."""
    groups = await asyncio.gather(
        *(
            fetch_rss(
                _hh_url(query),
                limit=per_query,
                source="hh.ru",
                max_age_days=max_age_days,
                now=now,
            )
            for query in QUERIES
        )
    )
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            key = item["url"] or item["title"].lower()
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(item)
    return items


def _trudvsem_text(vacancy: dict[str, Any]) -> str:
    parts = [
        str((vacancy.get("company") or {}).get("name") or "").strip(),
        str((vacancy.get("region") or {}).get("name") or "").strip(),
        _salary(vacancy.get("salary")),
    ]
    duty = str(vacancy.get("duty") or "").strip()
    if duty:
        parts.append(duty[:300])
    return " · ".join(part for part in parts if part)


async def fetch_trudvsem(
    max_age_days: int = 14,
    per_query: int = 100,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Резервный источник вакансий: открытые данные «Работа России»."""
    current = now or datetime.now(UTC)
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    async with httpx.AsyncClient(timeout=20, headers=HEADERS) as client:
        for query in QUERIES:
            try:
                response = await client.get(
                    TRUDVSEM_API,
                    params={"text": query, "limit": per_query},
                )
                response.raise_for_status()
                rows = (response.json().get("results") or {}).get("vacancies") or []
            except Exception as exc:
                logger.warning("Trudvsem недоступен (%s): %s", query, exc)
                continue
            for row in rows:
                vacancy = row.get("vacancy") or {}
                url = str(vacancy.get("vac_url") or "").strip()
                if not url or url in seen:
                    continue
                published_at = parse_date(vacancy.get("creation-date"))
                if not is_fresh(published_at, max_age_days, current):
                    continue
                item = make_item(
                    vacancy.get("job-name"),
                    url,
                    _trudvsem_text(vacancy),
                    "Работа России",
                    published_at,
                )
                if item is not None:
                    seen.add(url)
                    results.append(item)
    return results


async def fetch_vacancies(
    per_query: int = 20,
    max_age_days: int = 14,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Собрать свежие вакансии: сначала hh.ru, при пустоте — «Работа России»."""
    current = now or datetime.now(UTC)
    items = await _fetch_hh(per_query, max_age_days, current)
    if not items:
        logger.info("hh.ru RSS пуст — переключаюсь на «Работу России»")
        items = await fetch_trudvsem(max_age_days=max_age_days, now=current)
    items = sort_by_date(items)
    logger.info("Вакансии: собрано %d свежих материалов", len(items))
    return items


__all__ = (
    "HH_RSS_URL",
    "QUERIES",
    "TRUDVSEM_API",
    "fetch_trudvsem",
    "fetch_vacancies",
)
