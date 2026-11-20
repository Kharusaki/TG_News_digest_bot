"""Проверка кеша, персональной сборки, рендера и защиты от выдумок LLM."""

import asyncio
from datetime import datetime

from services import cache, digest, llm
from services.render import render_topic_block
from sources.base import MOSCOW, UTC

DAY = datetime(2026, 11, 20, 12, 0, tzinfo=MOSCOW)
ITEM = {
    "title": "Свежая новость",
    "url": "https://example.com/news/1?utm_source=rss",
    "text": "Текст новости",
    "source": "КонсультантПлюс",
    "published_at": datetime(2026, 11, 19, 15, 20, tzinfo=UTC),
}


class FakeRedis:
    def __init__(self, data=None):
        self.data = dict(data or {})

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value
        self.data[("ex", key)] = ex


def teardown_function():
    cache.set_client(None)
    cache.clear_fallback()


def test_cache_key_is_bound_to_date():
    assert cache.digest_key("news", DAY) == "digest:v3:news:2026-11-20"
    assert cache.digest_key("news", DAY) != cache.digest_key(
        "news", datetime(2026, 11, 21, 12, 0, tzinfo=MOSCOW)
    )


def test_cache_survives_redis_outage():
    class BrokenRedis:
        async def get(self, key):
            raise ConnectionError("redis down")

        async def set(self, key, value, ex=None):
            raise ConnectionError("redis down")

    cache.set_client(BrokenRedis())
    asyncio.run(cache.set_digest("news", "ФОЛБЭК", day=DAY))
    assert asyncio.run(cache.get_digest("news", DAY)) == "ФОЛБЭК"


def test_cache_ttl_is_24_hours():
    fake = FakeRedis()
    cache.set_client(fake)
    asyncio.run(cache.set_digest("news", "ТЕКСТ", day=DAY))
    key = cache.digest_key("news", DAY)
    assert fake.data[key] == "ТЕКСТ"
    assert fake.data[("ex", key)] == 86_400
    assert asyncio.run(cache.get_digest("news", DAY)) == "ТЕКСТ"


def test_personal_render_uses_only_selected_topics():
    fake = FakeRedis(
        {
            cache.digest_key("news", DAY): "НОВОСТИ",
            cache.digest_key("msfo", DAY): "МСФО-БЛОК",
        }
    )
    cache.set_client(fake)
    text = asyncio.run(
        digest.render_user_digest(
            {"news": True, "jobs": True, "tech": False, "msfo": True},
            DAY,
        )
    )
    assert "НОВОСТИ" in text and "МСФО-БЛОК" in text
    assert "свежих вакансий не найдено" in text
    assert "Технологии" not in text
    assert asyncio.run(digest.render_user_digest({}, DAY)) is None


def test_render_has_date_source_and_link():
    block = render_topic_block("news", [ITEM], {"items": {1: "Короткая суть"}})
    assert "19.11.2026" in block
    assert "КонсультантПлюс" in block
    assert 'href="https://example.com/news/1?utm_source=rss"' in block
    assert "Короткая суть" in block
    assert "Открыть источник" in block


def test_render_escapes_html_and_skips_linkless_items():
    item = dict(ITEM, title="<script>alert(1)</script>")
    block = render_topic_block("news", [item], {"items": {}})
    assert "<script>" not in block
    broken = dict(ITEM, url="")
    assert "не найдено" in render_topic_block("news", [broken], {"items": {}})


def test_validate_summary_blocks_urls_and_invented_numbers():
    source = "Аудитор, зарплата 100000 рублей, Москва"
    assert llm.validate_summary("Аудитор с зарплатой 100000 рублей", source)
    assert llm.validate_summary("Подробности: https://evil.example", source) == ""
    assert llm.validate_summary("Зарплата 200000 рублей", source) == ""


def test_gemini_failure_falls_back_to_proxy(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(llm, "_gemini", _failing)
    monkeypatch.setattr(llm, "_proxy", _proxy_ok)
    result = asyncio.run(
        llm.summarize(
            "jobs",
            [{"title": "Аудитор", "text": "Зарплата 100000 рублей", "url": ""}],
        )
    )
    assert result["items"][1] == "Зарплата 100000 рублей"


def test_summarize_drops_unknown_ids(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "proxy")
    monkeypatch.setenv("PROXY_API_KEY", "test-key")

    async def wrong_ids(prompt):
        return '{"intro": "", "items": [{"id": 99, "summary": "Выдумка"}]}'

    monkeypatch.setattr(llm, "_proxy", wrong_ids)
    result = asyncio.run(
        llm.summarize("news", [{"title": "Новость", "text": "Текст", "url": ""}])
    )
    assert result["items"] == {}


def test_prompt_substitution_and_trim():
    prompt = llm.build_prompt("Вакансии", "СЫРЬЁ")
    assert "Вакансии" in prompt and "СЫРЬЁ" in prompt
    assert "JSON" in prompt
    block = llm.format_raw_items(
        [{"title": "Заголовок", "text": "x" * 900, "url": "https://example.com"}]
    )
    assert "Заголовок" in block and len(block) < 950
    assert "https://example.com" not in block


async def _failing(prompt):
    raise RuntimeError("геолокация не поддерживается")


async def _proxy_ok(prompt):
    return (
        '{"intro": "", "items": ['
        '{"id": 1, "summary": "Зарплата 100000 рублей"}]}'
    )
