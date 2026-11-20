"""Хранилище подписок, времени рассылки и пользовательских источников.

SQLite в режиме WAL, без ORM: короткие транзакции, одно соединение на операцию.
Схема обновляется сама — старый ``bot.db`` продолжает работать после апгрейда.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

TOPICS: tuple[str, ...] = ("news", "jobs", "tech", "msfo")
_TOPIC_COLUMNS: dict[str, str] = {topic: f"topic_{topic}" for topic in TOPICS}
MAX_CUSTOM_SOURCES = 10
DEFAULT_CUSTOM_LIMIT = 25
MAX_CUSTOM_LIMIT = 25
MAX_CUSTOM_DIGEST_ITEMS = 30
LEGACY_CUSTOM_LIMIT = 3
DEFAULT_DIGEST_TIME = "09:00"


def _db_path() -> Path:
    raw = os.getenv("DB_PATH", "bot.db").strip() or "bot.db"
    path = Path(raw).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(_db_path(), timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode=WAL;")
        connection.execute("PRAGMA busy_timeout=5000;")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    name: str,
    definition: str,
) -> None:
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db() -> None:
    """Создать и при необходимости обновить схему при первом запуске."""
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                created_at  TEXT DEFAULT (datetime('now')),
                topic_news  INTEGER DEFAULT 0,
                topic_jobs  INTEGER DEFAULT 0,
                topic_tech  INTEGER DEFAULT 0,
                topic_msfo  INTEGER DEFAULT 0,
                digest_time TEXT DEFAULT '09:00'
            )
            """
        )
        # Доставка по часам появилась позже базовой схемы: добавляем колонки
        # только если их ещё нет, чтобы не ломать существующую базу.
        _ensure_column(connection, "users", "digest_enabled", "INTEGER DEFAULT 1")
        _ensure_column(connection, "users", "last_sent_date", "TEXT")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS custom_sources (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                topic      TEXT NOT NULL,
                name       TEXT NOT NULL,
                url        TEXT NOT NULL,
                keywords   TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        _ensure_column(
            connection,
            "custom_sources",
            "max_items",
            f"INTEGER DEFAULT {DEFAULT_CUSTOM_LIMIT}",
        )
        # Прежнее значение по умолчанию подтягиваем к новому; вручную выбранные
        # лимиты (не равные 3) остаются как есть.
        connection.execute(
            "UPDATE custom_sources SET max_items = ? WHERE max_items = ?",
            (DEFAULT_CUSTOM_LIMIT, LEGACY_CUSTOM_LIMIT),
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_custom_sources_user ON custom_sources(user_id)"
        )


def unsubscribe(user_id: int) -> None:
    """Полностью отключить рассылку, сохранив пользователя и его ленты."""
    assignments = ", ".join(f"{_TOPIC_COLUMNS[topic]} = 0" for topic in TOPICS)
    with _connect() as connection:
        connection.execute(
            f"""
            UPDATE users
            SET {assignments}, digest_enabled = 0, last_sent_date = NULL
            WHERE user_id = ?
            """,
            (int(user_id),),
        )


def normalize_digest_time(value: Any) -> str | None:
    """Привести ``14``/``14:00`` к ``14:00``; пустое значение выключает рассылку."""
    raw = str(value or "").strip().lower()
    if raw in {"", "-", "off", "выкл", "выключить", "нет"}:
        return ""
    if ":" in raw:
        hour, _, minute = raw.partition(":")
        minute = minute.strip() or "00"
    else:
        hour, minute = raw, "00"
    if minute not in {"00", "0"}:
        return None
    try:
        number = int(hour)
    except (TypeError, ValueError):
        return None
    if not 0 <= number <= 23:
        return None
    return f"{number:02d}:00"


def normalize_custom_limit(value: Any) -> int | None:
    """Число пунктов с одной ленты: ``1…MAX_CUSTOM_LIMIT`` или ``None``."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if 1 <= number <= MAX_CUSTOM_LIMIT else None


def upsert_user(user_id: int, username: str = "") -> None:
    """Создать или обновить имя пользователя, не трогая его подписки."""
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO users (user_id, username)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username = excluded.username
            """,
            (int(user_id), str(username or "")),
        )


def set_topics(user_id: int, topics: dict[str, bool]) -> None:
    """Сохранить флаги тем. Изменения вносятся только по белому списку."""
    assignments: list[str] = []
    values: list[Any] = []
    for topic in TOPICS:
        if topic in topics:
            assignments.append(f"{_TOPIC_COLUMNS[topic]} = ?")
            values.append(1 if topics[topic] else 0)
    assignments.append("last_sent_date = NULL")
    if not any(topic in topics for topic in TOPICS):
        with _connect() as connection:
            connection.execute(
                "UPDATE users SET last_sent_date = NULL WHERE user_id = ?",
                (int(user_id),),
            )
        return
    values.append(int(user_id))
    with _connect() as connection:
        connection.execute(
            f"UPDATE users SET {', '.join(assignments)} WHERE user_id = ?",
            values,
        )


def set_digest_time(user_id: int, value: Any) -> str | None:
    """Сохранить час доставки; ``None`` означает некорректный ввод."""
    normalized = normalize_digest_time(value)
    if normalized is None:
        return None
    with _connect() as connection:
        connection.execute(
            """
            UPDATE users
            SET digest_time = ?, digest_enabled = ?, last_sent_date = NULL
            WHERE user_id = ?
            """,
            (normalized or DEFAULT_DIGEST_TIME, 0 if not normalized else 1, int(user_id)),
        )
    return normalized


def mark_delivered(user_id: int, day: str) -> None:
    """Отметить рассылку за дату, чтобы не отправить её дважды."""
    with _connect() as connection:
        connection.execute(
            "UPDATE users SET last_sent_date = ? WHERE user_id = ?",
            (str(day), int(user_id)),
        )


def get_settings(user_id: int) -> dict[str, Any] | None:
    """Вернуть темы, время рассылки и статус доставки пользователя."""
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT username, topic_news, topic_jobs, topic_tech, topic_msfo,
                   digest_time, digest_enabled, last_sent_date
            FROM users WHERE user_id = ?
            """,
            (int(user_id),),
        ).fetchone()
    if row is None:
        return None
    return {
        "username": row["username"] or "",
        "topics": {
            topic: bool(row[_TOPIC_COLUMNS[topic]]) for topic in TOPICS
        },
        "digest_time": row["digest_time"] or DEFAULT_DIGEST_TIME,
        "delivery_enabled": bool(row["digest_enabled"]),
        "last_sent_date": row["last_sent_date"] or "",
    }


def get_subscribers() -> list[dict[str, Any]]:
    """Все пользователи хотя бы с одной включённой темой."""
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT user_id, topic_news, topic_jobs, topic_tech, topic_msfo
            FROM users
            WHERE topic_news = 1 OR topic_jobs = 1 OR topic_tech = 1
               OR topic_msfo = 1
            """
        ).fetchall()
    return [
        {
            "user_id": row["user_id"],
            "topics": {
                topic: bool(row[_TOPIC_COLUMNS[topic]]) for topic in TOPICS
            },
        }
        for row in rows
    ]


def get_users_for_delivery(hour: int, day: str) -> list[dict[str, Any]]:
    """Подписчики, которым пора доставить дайджест в этом часу."""
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT user_id, topic_news, topic_jobs, topic_tech, topic_msfo
            FROM users
            WHERE digest_enabled = 1
              AND digest_time = ?
              AND (last_sent_date IS NULL OR last_sent_date <> ?)
              AND (topic_news = 1 OR topic_jobs = 1 OR topic_tech = 1
                   OR topic_msfo = 1)
            """,
            (f"{int(hour):02d}:00", str(day)),
        ).fetchall()
    return [
        {
            "user_id": row["user_id"],
            "topics": {
                topic: bool(row[_TOPIC_COLUMNS[topic]]) for topic in TOPICS
            },
        }
        for row in rows
    ]


def add_custom_source(
    user_id: int,
    topic: str,
    name: str,
    url: str,
    keywords: str = "",
    max_items: int = DEFAULT_CUSTOM_LIMIT,
) -> int | None:
    """Добавить RSS-источник пользователя. Возвращает id или ``None``."""
    if topic not in TOPICS:
        return None
    with _connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM custom_sources WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()[0]
        if count >= MAX_CUSTOM_SOURCES:
            return None
        cursor = connection.execute(
            """
            INSERT INTO custom_sources (user_id, topic, name, url, keywords, max_items)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                int(user_id),
                topic,
                str(name or "").strip()[:80],
                str(url or "").strip()[:500],
                str(keywords or "").strip()[:300],
                normalize_custom_limit(max_items) or DEFAULT_CUSTOM_LIMIT,
            ),
        )
    return int(cursor.lastrowid)


def list_custom_sources(user_id: int) -> list[dict[str, Any]]:
    """Источники пользователя в порядке добавления."""
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT id, topic, name, url, keywords, max_items
            FROM custom_sources WHERE user_id = ? ORDER BY id
            """,
            (int(user_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def get_custom_source(user_id: int, source_id: int) -> dict[str, Any] | None:
    """Найти источник по id, но только среди своих."""
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT id, topic, name, url, keywords, max_items
            FROM custom_sources WHERE id = ? AND user_id = ?
            """,
            (int(source_id), int(user_id)),
        ).fetchone()
    return dict(row) if row is not None else None


def update_custom_source(
    user_id: int,
    source_id: int,
    *,
    keywords: str | None = None,
    max_items: int | None = None,
) -> bool:
    """Изменить ключи и/или лимит своего источника. Вернуть ``True`` если обновлено."""
    assignments: list[str] = []
    values: list[Any] = []
    if keywords is not None:
        assignments.append("keywords = ?")
        values.append(str(keywords).strip()[:300])
    if max_items is not None:
        normalized = normalize_custom_limit(max_items)
        if normalized is None:
            return False
        assignments.append("max_items = ?")
        values.append(normalized)
    if not assignments:
        return False
    values.extend((int(source_id), int(user_id)))
    with _connect() as connection:
        cursor = connection.execute(
            f"UPDATE custom_sources SET {', '.join(assignments)} WHERE id = ? AND user_id = ?",
            values,
        )
    return cursor.rowcount > 0


def delete_custom_source(user_id: int, source_id: int) -> bool:
    """Удалить только свой источник: чужой id ошибкой не станет."""
    with _connect() as connection:
        cursor = connection.execute(
            "DELETE FROM custom_sources WHERE id = ? AND user_id = ?",
            (int(source_id), int(user_id)),
        )
    return cursor.rowcount > 0


__all__ = (
    "DEFAULT_CUSTOM_LIMIT",
    "DEFAULT_DIGEST_TIME",
    "LEGACY_CUSTOM_LIMIT",
    "MAX_CUSTOM_DIGEST_ITEMS",
    "MAX_CUSTOM_LIMIT",
    "MAX_CUSTOM_SOURCES",
    "TOPICS",
    "add_custom_source",
    "delete_custom_source",
    "get_custom_source",
    "get_settings",
    "get_subscribers",
    "get_users_for_delivery",
    "init_db",
    "list_custom_sources",
    "mark_delivered",
    "normalize_custom_limit",
    "normalize_digest_time",
    "set_digest_time",
    "set_topics",
    "unsubscribe",
    "update_custom_source",
    "upsert_user",
)
