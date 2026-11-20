"""Общая модель материала и работа с датами.

Все источники приводят записи к одному словарю:
``title``, ``url``, ``text``, ``source`` и ``published_at`` (aware datetime, UTC).
Дата здесь — не украшение: без неё материал нельзя проверить на свежесть,
поэтому записи без даты в новостные темы не попадают.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

UTC = timezone.utc
MOSCOW = timezone(timedelta(hours=3))
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_KEYS = {"yclid", "gclid", "fbclid", "from", "spm"}
_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")
_DATE_FORMATS = (
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


def clean_text(value: Any) -> str:
    """Убрать HTML-разметку, сущности и лишние пробелы."""
    if not value:
        return ""
    text = html.unescape(str(value))
    text = _TAG.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def normalize_url(value: Any) -> str:
    """Убрать якорь и utm-метки, чтобы один материал не дублировался."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    query = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if key not in _TRACKING_KEYS and not key.startswith(_TRACKING_PREFIXES)
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, urlencode(query), ""))


def parse_date(value: Any) -> datetime | None:
    """Разобрать дату RSS/JSON/HTML в aware datetime UTC.

    Поддерживаются RFC-822 (``Thu, 19 Nov 2026 18:20:01 +0300``), ISO-8601,
    ``dd.mm.yyyy`` и ``struct_time`` от feedparser.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, (tuple, list)) and len(value) >= 6:
        try:
            return datetime(*value[:6], tzinfo=UTC)
        except (TypeError, ValueError):
            return None

    raw = html.unescape(str(value)).strip()
    if not raw:
        return None

    # RFC-822: стандарт для RSS.
    try:
        from email.utils import parsedate_to_datetime

        parsed = parsedate_to_datetime(raw)
        if parsed is not None:
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        pass

    iso = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except ValueError:
        pass

    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, date_format).replace(tzinfo=MOSCOW).astimezone(UTC)
        except ValueError:
            continue
    return None


def is_fresh(
    published_at: datetime | None,
    max_age_days: int,
    now: datetime | None = None,
) -> bool:
    """Материал не старше ``max_age_days`` и не из будущего.

    Допускаем небольшой запас в сутки на расхождение часов и часовых поясов,
    но всё, что старше лимита, отбрасываем — включая записи без даты.
    """
    if published_at is None:
        return False
    current = now or datetime.now(UTC)
    age = current - published_at
    if age < timedelta(days=-1):
        return False
    return age <= timedelta(days=max_age_days)


def date_label(published_at: datetime | None) -> str:
    """Дата для сообщения в московском времени: ``19.11.2026``."""
    if published_at is None:
        return ""
    return published_at.astimezone(MOSCOW).strftime("%d.%m.%Y")


def make_item(
    title: Any,
    url: Any,
    text: Any,
    source: str,
    published_at: datetime | None = None,
) -> dict[str, Any] | None:
    """Собрать нормализованный материал или вернуть ``None`` для пустышки."""
    clean_title = clean_text(title)
    clean_body = clean_text(text)
    if not clean_title and not clean_body:
        return None
    return {
        "title": clean_title or clean_body[:120],
        "url": normalize_url(url),
        "text": clean_body,
        "source": str(source or "").strip(),
        "published_at": published_at,
    }


def sort_by_date(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Сначала свежие; материалы без даты уходят в конец."""
    earliest = datetime.min.replace(tzinfo=UTC)
    return sorted(items, key=lambda item: item.get("published_at") or earliest, reverse=True)


__all__ = (
    "MOSCOW",
    "UTC",
    "clean_text",
    "date_label",
    "is_fresh",
    "make_item",
    "normalize_url",
    "parse_date",
    "sort_by_date",
)
