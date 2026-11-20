"""Пользовательские RSS/Atom-ленты: проверка перед сохранением и чтение.

Чужую ленту нельзя «просто доверить»: если в ней нет даты или ссылки, пункт
нельзя проверить и он не попадёт в дайджест. Поэтому при добавлении бот сам
читает ленту и показывает, что нашёл, а пользователь видит результат сразу.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import feedparser
import httpx

from database.db import (
    DEFAULT_CUSTOM_LIMIT,
    MAX_CUSTOM_DIGEST_ITEMS,
    normalize_custom_limit,
)

from . import dedupe
from .base import clean_text, sort_by_date
from .rss import _HEADERS, entry_date, fetch_rss

MAX_CUSTOM_ITEMS = MAX_CUSTOM_DIGEST_ITEMS
MAX_FEED_BYTES = 2_000_000
FETCH_LIMIT_PER_SOURCE = 50


def compile_keywords(raw: str) -> list[re.Pattern[str]]:
    """Собрать шаблоны ключевых слов.

    Слова до 4 букв (например ``евро``) ищутся только целиком, чтобы
    ``евротройка`` и ``Европе`` не проходили под ключ ``евро``. Слова в 5–6 букв
    ищутся по основе — ``ставка`` находит ``ставки``, но не ``ставший``.
    Звёздочка на конце (``евро*``) явно включает все словоформы и приставки.
    """
    patterns: list[re.Pattern[str]] = []
    for part in str(raw or "").split(","):
        token = part.strip().lower()
        if not token:
            continue
        force_prefix = token.endswith("*")
        if force_prefix:
            token = token[:-1].strip()
        if not token:
            continue
        if force_prefix or len(token) >= 5:
            # Короткие слова («рубль», «ставка») режем на одну букву:
            # «рубл» не поймает «рубежом», «ставк» — «ставший».
            # От семи букв режем на две, чтобы «котировки» ловила «котировок».
            if force_prefix:
                stem = token
            elif len(token) <= 6:
                stem = token[:-1]
            else:
                stem = token[:-2]
            patterns.append(re.compile(rf"(?<!\w){re.escape(stem)}\w*", re.IGNORECASE))
        else:
            patterns.append(re.compile(rf"(?<!\w){re.escape(token)}(?!\w)", re.IGNORECASE))
    return patterns


def matches_text(patterns: Iterable[re.Pattern[str]], *texts: Any) -> bool:
    """Ключ может быть в заголовке или в тексте записи."""
    return any(
        pattern.search(str(value or ""))
        for value in texts
        for pattern in patterns
    )


def validate_source_url(url: str) -> str | None:
    """Вернуть причину отказа или ``None``, если адрес можно проверять."""
    raw = str(url or "").strip()
    if not raw:
        return "Адрес пустой. Пришлите ссылку на RSS-ленту."
    if len(raw) > 500:
        return "Адрес слишком длинный."
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return "Нужен публичный адрес вида https://…/rss.xml"
    host = (parts.hostname or "").lower()
    if host == "localhost" or host.endswith((".local", ".internal")):
        return "Локальные адреса не поддерживаются."
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if address.is_private or address.is_loopback or address.is_link_local:
        return "Локальные и внутренние адреса не поддерживаются."
    return None


def inspect_feed(payload: bytes, url: str = "", keywords: str = "") -> dict[str, Any]:
    """Разобрать ленту и подготовить отчёт для пользователя."""
    try:
        parsed = feedparser.parse(payload)
    except Exception as exc:
        return {"ok": False, "error": f"Не удалось разобрать ленту: {exc}"}

    feed = parsed.feed or {}
    entries = list(getattr(parsed, "entries", []) or [])
    if not entries:
        return {
            "ok": False,
            "title": clean_text(feed.get("title", "")),
            "entries": 0,
            "error": "Записей не найдено. Похоже, это не RSS/Atom-лента.",
        }

    dated = sum(1 for entry in entries if entry_date(entry) is not None)
    linked = sum(
        1
        for entry in entries
        if str(getattr(entry, "link", "") or "").startswith(("http://", "https://"))
    )
    sample = [
        clean_text(getattr(entry, "title", ""))[:90] or "без заголовка"
        for entry in entries[:3]
    ]
    ok = dated > 0 and linked > 0
    error = ""
    if not dated:
        error = "У записей нет даты — такие материалы не пройдут фильтр свежести."
    elif not linked:
        error = "У записей нет ссылки — открыть источник будет нельзя."
    patterns = compile_keywords(keywords)
    matched = 0
    if patterns:
        for entry in entries:
            title = clean_text(getattr(entry, "title", ""))
            body = clean_text(
                getattr(entry, "summary", "")
                or getattr(entry, "description", "")
                or getattr(entry, "content", "")
            )
            if matches_text(patterns, title, body):
                matched += 1
    return {
        "ok": ok,
        "title": clean_text(feed.get("title", "")) or url,
        "entries": len(entries),
        "dated": dated,
        "linked": linked,
        "matched": matched if patterns else None,
        "keywords": keywords.strip(),
        "sample": sample,
        "error": error,
    }


async def check_source(url: str, keywords: str = "") -> dict[str, Any]:
    """Скачать и проверить ленту; при ключевых словах считает совпадения."""
    problem = validate_source_url(url)
    if problem:
        return {"ok": False, "error": problem}
    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers=_HEADERS,
        ) as client:
            response = await client.get(str(url).strip())
            response.raise_for_status()
            if validate_source_url(str(response.url)):
                return {"ok": False, "error": "Лента перенаправляет на локальный адрес."}
            payload = response.content[:MAX_FEED_BYTES]
    except Exception as exc:
        return {"ok": False, "error": f"Не удалось скачать ленту: {exc}"}
    return inspect_feed(payload, str(url), keywords)


async def fetch_custom_items(
    sources: list[dict[str, Any]],
    topics: dict[str, Any],
    max_age: Any,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Прочитать ленты пользователя по включённым темам и строгим ключам.

    Ленту читаем без грубого подстрочного фильтра ``fetch_rss``: иначе короткое
    слово ``евро`` пропускает «евротройку» и съедает лимит до того, как его
    отсечёт точный фильтр. Сначала берём достаточно записей, затем применяем
    свои шаблоны и только после этого ограничиваем число пунктов.
    """

    async def load(source: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        topic = str(source.get("topic") or "")
        items = await fetch_rss(
            str(source.get("url") or ""),
            limit=FETCH_LIMIT_PER_SOURCE,
            keywords=(),
            source=str(source.get("name") or "Мой источник"),
            max_age_days=max_age(topic),
            now=now,
        )
        return source, items

    selected = [source for source in sources if topics.get(str(source.get("topic") or ""), False)]
    if not selected:
        return []
    groups = await asyncio.gather(*(load(source) for source in selected))
    flat: list[dict[str, Any]] = []
    for source, items in groups:
        patterns = compile_keywords(str(source.get("keywords") or ""))
        if patterns:
            items = [
                item
                for item in items
                if matches_text(patterns, item.get("title"), item.get("text"))
            ]
        limit = normalize_custom_limit(source.get("max_items")) or DEFAULT_CUSTOM_LIMIT
        flat.extend(items[:limit])
    return dedupe(sort_by_date(flat), limit=MAX_CUSTOM_ITEMS)


__all__ = (
    "MAX_CUSTOM_ITEMS",
    "check_source",
    "compile_keywords",
    "fetch_custom_items",
    "inspect_feed",
    "matches_text",
    "validate_source_url",
)
