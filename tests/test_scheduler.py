"""Инварианты планировщика: 4 вызова LLM при сборе и только кеш при рассылке."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

from scheduler import jobs
from services.cache import EMPTY_TTL_SECONDS
from sources.base import UTC

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
MATERIAL = {
    "title": "Материал",
    "url": "https://example.com/1",
    "text": "Текст",
    "source": "Источник",
    "published_at": NOW,
}


class FakeCache:
    def __init__(self):
        self.data = {}
        self.ttls = {}

    async def get_digest(self, topic):
        return self.data.get(topic)

    async def set_digest(self, topic, text, ttl=None):
        self.data[topic] = text
        self.ttls[topic] = ttl
        return True


def test_ensure_digests_calls_llm_four_times_and_caches(monkeypatch):
    cache = FakeCache()
    monkeypatch.setattr(jobs, "get_digest", cache.get_digest)
    monkeypatch.setattr(jobs, "set_digest", cache.set_digest)
    monkeypatch.setattr(
        jobs,
        "collect_topic_materials",
        AsyncMock(return_value=[MATERIAL]),
    )
    summarize = AsyncMock(return_value={"intro": "", "items": {1: "Суть"}})
    monkeypatch.setattr(jobs, "summarize", summarize)

    asyncio.run(jobs.ensure_digests())
    assert summarize.await_count == 4
    assert set(cache.data) == {"news", "jobs", "tech", "msfo"}
    assert "Открыть источник" in cache.data["news"]

    summarize.reset_mock()
    asyncio.run(jobs.ensure_digests())
    assert summarize.await_count == 0


def test_empty_topic_is_cached_with_text_and_short_ttl(monkeypatch):
    cache = FakeCache()
    monkeypatch.setattr(jobs, "get_digest", cache.get_digest)
    monkeypatch.setattr(jobs, "set_digest", cache.set_digest)
    monkeypatch.setattr(jobs, "collect_topic_materials", AsyncMock(return_value=[]))
    summarize = AsyncMock()
    monkeypatch.setattr(jobs, "summarize", summarize)

    asyncio.run(jobs.ensure_digests())
    assert summarize.await_count == 0
    assert "не найдено" in cache.data["jobs"]
    assert "14 дней" in cache.data["jobs"]
    assert cache.ttls["jobs"] == EMPTY_TTL_SECONDS


def test_collect_job_always_forces_refresh(monkeypatch):
    ensure = AsyncMock()
    monkeypatch.setattr(jobs, "ensure_digests", ensure)

    asyncio.run(jobs.collect_digests_job())

    ensure.assert_awaited_once_with(force=True)


def test_delivery_reads_only_cache_and_marks_date(monkeypatch):
    ensure = AsyncMock()
    monkeypatch.setattr(jobs, "ensure_digests", ensure)
    monkeypatch.setattr(jobs, "get_digest", AsyncMock(return_value="БЛОК"))
    topics = {"news": True, "jobs": False, "tech": False, "msfo": False}
    monkeypatch.setattr(
        jobs,
        "get_users_for_delivery",
        lambda hour, day: [{"user_id": 7, "topics": topics}],
    )
    render = AsyncMock(return_value="ГОТОВЫЙ ДАЙДЖЕСТ")
    monkeypatch.setattr(jobs, "render_user_digest", render)
    send_html = AsyncMock()
    monkeypatch.setattr(jobs, "send_html", send_html)
    marked = []
    monkeypatch.setattr(jobs, "mark_delivered", lambda user_id, day: marked.append((user_id, day)))

    # 11:00 UTC = 14:00 Москвы; внутри рассылки не должен вызываться сбор.
    asyncio.run(jobs.deliver_digests(SimpleNamespace(), now=NOW))

    ensure.assert_not_awaited()
    render.assert_awaited_once_with(topics, ANY, 7)
    send_html.assert_awaited_once_with(ANY, 7, "ГОТОВЫЙ ДАЙДЖЕСТ")
    assert marked == [(7, "2026-11-20")]


def test_delivery_is_skipped_until_morning_cache_exists(monkeypatch):
    monkeypatch.setattr(jobs, "get_digest", AsyncMock(return_value=None))
    topics = {"news": True, "jobs": False, "tech": False, "msfo": False}
    monkeypatch.setattr(
        jobs,
        "get_users_for_delivery",
        lambda hour, day: [{"user_id": 7, "topics": topics}],
    )
    render = AsyncMock()
    monkeypatch.setattr(jobs, "render_user_digest", render)

    asyncio.run(jobs.deliver_digests(SimpleNamespace(), now=NOW))

    render.assert_not_awaited()
