"""Кеш готовых блоков дайджеста.

Ключ привязан к дате: ``digest:v3:<topic>:YYYY-MM-DD``. Поэтому вчерашний
дайджест не может быть отдан сегодня даже при живом TTL. Основное хранилище —
Redis, резервное — память процесса (если Docker/Redis недоступны).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any

import redis.asyncio as aioredis

from sources.base import MOSCOW

logger = logging.getLogger(__name__)

TTL_SECONDS = 86_400
EMPTY_TTL_SECONDS = 1_800
DIGEST_VERSION = "v3"
_client: Any | None = None
_fallback: dict[str, tuple[str, float]] = {}
_warned = False


def _warn_redis(action: str, exc: Exception) -> None:
    """Не засорять лог одинаковыми ошибками Redis на каждой теме."""
    global _warned
    if not _warned:
        logger.warning("Redis недоступен (%s), работаю на памяти процесса: %s", action, exc)
        _warned = True


def _redis_url() -> str:
    return os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0").strip() or (
        "redis://127.0.0.1:6379/0"
    )


def digest_key(topic: str, day: datetime | None = None) -> str:
    """Ключ темы на конкретную московскую дату."""
    current = (day or datetime.now(MOSCOW)).astimezone(MOSCOW)
    return f"digest:{DIGEST_VERSION}:{topic}:{current.strftime('%Y-%m-%d')}"


def set_client(client: Any | None) -> None:
    """Подменить клиента (используется в тестах)."""
    global _client, _warned
    _client = client
    _warned = False
    _fallback.clear()


def clear_fallback() -> None:
    """Очистить резервный кеш процесса."""
    global _warned
    _warned = False
    _fallback.clear()


async def get_client() -> Any:
    global _client
    if _client is None:
        _client = aioredis.from_url(_redis_url(), decode_responses=True)
    return _client


async def get_digest(topic: str, day: datetime | None = None) -> str | None:
    """Прочитать готовый блок темы: сначала Redis, затем память процесса."""
    key = digest_key(topic, day)
    try:
        client = await get_client()
        value = await client.get(key)
        if value:
            return value
    except Exception as exc:
        _warn_redis(f"чтение темы {topic}", exc)

    entry = _fallback.get(key)
    if entry and time.time() < entry[1]:
        return entry[0]
    return None


async def set_digest(
    topic: str,
    text: str,
    ttl: int = TTL_SECONDS,
    day: datetime | None = None,
) -> bool:
    """Записать блок темы. Пустой блок кешируется короче, чтобы обновиться днём."""
    key = digest_key(topic, day)
    try:
        client = await get_client()
        await client.set(key, text, ex=ttl)
    except Exception as exc:
        _warn_redis(f"запись темы {topic}", exc)

    _fallback[key] = (text, time.time() + ttl)
    return True


__all__ = (
    "DIGEST_VERSION",
    "EMPTY_TTL_SECONDS",
    "TTL_SECONDS",
    "clear_fallback",
    "digest_key",
    "get_client",
    "get_digest",
    "set_client",
    "set_digest",
)
