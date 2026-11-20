"""Настройки времени, рассылка по часам и пользовательские источники."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import handlers
from bot.help import help_text
from bot.keyboards import MENU_UNSUBSCRIBE, main_keyboard, sources_keyboard, time_keyboard
from database.db import (
    MAX_CUSTOM_DIGEST_ITEMS,
    add_custom_source,
    delete_custom_source,
    get_custom_source,
    get_settings,
    get_users_for_delivery,
    list_custom_sources,
    mark_delivered,
    normalize_custom_limit,
    normalize_digest_time,
    set_digest_time,
    set_topics,
    unsubscribe,
    update_custom_source,
    upsert_user,
)
from services import digest as digest_service
from services.render import render_custom_block
from sources import custom as custom_sources
from sources.base import UTC

NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
ITEM = {
    "title": "Моя новость",
    "url": "https://example.com/post",
    "text": "Своя лента",
    "source": "Мой блог",
    "published_at": NOW,
}
RSS = (
    b'<?xml version="1.0"?><rss version="2.0"><channel><title>Test feed</title>'
    b"<item><title>One</title><link>https://example.com/1</link>"
    b"<pubDate>Thu, 19 Nov 2026 10:00:00 +0300</pubDate></item></channel></rss>"
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("14", "14:00"),
        ("14:00", "14:00"),
        ("7", "07:00"),
        ("off", ""),
        ("", ""),
        ("-", ""),
        ("14:30", None),
        ("24", None),
        ("abc", None),
    ],
)
def test_normalize_digest_time(raw, expected):
    assert normalize_digest_time(raw) == expected


def test_user_time_and_delivery_roundtrip():
    upsert_user(1, "tester")
    set_topics(1, {"news": True})
    assert set_digest_time(1, "14") == "14:00"

    settings = get_settings(1)
    assert settings["digest_time"] == "14:00"
    assert settings["delivery_enabled"] is True
    assert [user["user_id"] for user in get_users_for_delivery(14, "2026-11-20")] == [1]
    assert get_users_for_delivery(15, "2026-11-20") == []

    mark_delivered(1, "2026-11-20")
    assert get_users_for_delivery(14, "2026-11-20") == []

    assert set_digest_time(1, "off") == ""
    assert get_settings(1)["delivery_enabled"] is False
    assert get_users_for_delivery(14, "2026-11-21") == []


def test_unsubscribe_clears_topics_and_delivery():
    upsert_user(7, "tester")
    set_topics(7, {"news": True, "msfo": True, "jobs": False, "tech": False})
    set_digest_time(7, "14")
    add_custom_source(7, "news", "Мой блог", "https://example.com/rss")

    unsubscribe(7)

    settings = get_settings(7)
    assert not any(settings["topics"].values())
    assert settings["delivery_enabled"] is False
    assert settings["digest_time"] == "14:00"
    assert get_users_for_delivery(14, "2026-11-21") == []
    assert len(list_custom_sources(7)) == 1


def test_custom_sources_crud():
    upsert_user(1)
    first = add_custom_source(1, "news", "Мой блог", "https://example.com/rss", "аудит")
    add_custom_source(2, "news", "Чужой", "https://example.org/rss")

    assert len(list_custom_sources(1)) == 1
    assert delete_custom_source(2, first) is False
    assert delete_custom_source(1, first) is True
    assert list_custom_sources(1) == []


def test_normalize_custom_limit():
    assert normalize_custom_limit("2") == 2
    assert normalize_custom_limit("25") == 25
    assert normalize_custom_limit("0") is None
    assert normalize_custom_limit("26") is None
    assert normalize_custom_limit("abc") is None


def test_update_custom_source_edits_keywords_and_limit():
    upsert_user(11)
    source_id = add_custom_source(11, "news", "Мой блог", "https://example.com/rss", "аудит")
    assert get_custom_source(11, source_id)["max_items"] == 25

    assert update_custom_source(11, source_id, keywords="ставка", max_items="1") is True
    row = get_custom_source(11, source_id)
    assert row["keywords"] == "ставка"
    assert row["max_items"] == 1

    assert update_custom_source(12, source_id, max_items=2) is False
    assert update_custom_source(11, source_id, max_items=99) is False
    assert get_custom_source(11, source_id)["max_items"] == 1


def test_inspect_feed_reports_dates_and_links():
    report = custom_sources.inspect_feed(RSS, "https://example.com/rss")
    assert report["ok"] is True
    assert report["entries"] == 1
    assert report["dated"] == 1
    assert report["linked"] == 1
    assert "One" in report["sample"]

    no_date = RSS.replace(b"<pubDate>Thu, 19 Nov 2026 10:00:00 +0300</pubDate>", b"")
    assert custom_sources.inspect_feed(no_date)["ok"] is False


def test_keywords_match_words_not_substrings():
    patterns = custom_sources.compile_keywords("евро, ставка")
    assert custom_sources.matches_text(patterns, "Евротройка в России") is False
    assert custom_sources.matches_text(patterns, "Позиция по Европе") is False
    assert custom_sources.matches_text(patterns, "ставки по депозитам") is True
    assert custom_sources.matches_text(patterns, "ставший пионер регги") is False
    assert custom_sources.matches_text(patterns, "за рубежом") is False
    assert custom_sources.matches_text(patterns, "Новая котировка") is False
    assert custom_sources.matches_text(custom_sources.compile_keywords("евро*"), "евроинтеграция")


def test_fetch_custom_items_filters_before_taking_items(monkeypatch):
    fetched = [
        {**ITEM, "title": "Евротройка", "url": "https://example.com/1"},
        {**ITEM, "title": "Ставки выросли", "url": "https://example.com/2"},
        {**ITEM, "title": "Доллар укрепился", "url": "https://example.com/3"},
    ]
    monkeypatch.setattr(custom_sources, "fetch_rss", AsyncMock(return_value=fetched))
    source = {
        "topic": "news",
        "url": "https://example.com/rss",
        "name": "Тест",
        "keywords": "ставка",
    }

    result = asyncio.run(
        custom_sources.fetch_custom_items([source], {"news": True}, lambda topic: 3)
    )

    assert len(result) == 1
    assert result[0]["title"] == "Ставки выросли"


def test_fetch_custom_items_respects_source_limit(monkeypatch):
    fetched = [
        {**ITEM, "title": "Ставки выросли", "url": "https://example.com/1"},
        {**ITEM, "title": "Ставки упали", "url": "https://example.com/2"},
        {**ITEM, "title": "Ставки стабильны", "url": "https://example.com/3"},
    ]
    monkeypatch.setattr(custom_sources, "fetch_rss", AsyncMock(return_value=fetched))
    source = {
        "topic": "news",
        "url": "https://example.com/rss",
        "name": "Тест",
        "keywords": "ставка",
        "max_items": 2,
    }

    result = asyncio.run(
        custom_sources.fetch_custom_items([source], {"news": True}, lambda topic: 3)
    )

    assert [item["title"] for item in result] == ["Ставки выросли", "Ставки упали"]


def test_fetch_custom_items_caps_total_at_thirty(monkeypatch):
    first = [
        {**ITEM, "title": f"Первая {i}", "url": f"https://example.com/1/{i}"}
        for i in range(30)
    ]
    second = [
        {**ITEM, "title": f"Вторая {i}", "url": f"https://example.com/2/{i}"}
        for i in range(30)
    ]
    monkeypatch.setattr(custom_sources, "fetch_rss", AsyncMock(side_effect=[first, second]))
    sources = [
        {"topic": "news", "url": "https://example.com/1.rss", "name": "A", "max_items": 25},
        {"topic": "news", "url": "https://example.com/2.rss", "name": "B", "max_items": 25},
    ]

    result = asyncio.run(
        custom_sources.fetch_custom_items(sources, {"news": True}, lambda topic: 3)
    )

    assert len(result) == MAX_CUSTOM_DIGEST_ITEMS


def test_inspect_feed_counts_keyword_matches():
    report = custom_sources.inspect_feed(RSS, "https://example.com/rss", "one, ставка")
    assert report["matched"] == 1
    assert report["keywords"] == "one, ставка"


def test_check_source_rejects_local_address_without_network():
    report = asyncio.run(custom_sources.check_source("http://127.0.0.1/rss.xml"))
    assert report["ok"] is False
    assert "локальные" in report["error"].lower()


def test_help_contains_schedule_builtins_and_instructions():
    text = help_text()
    assert "08:30" in text
    assert "hh.ru" in text
    assert "Гарант" in text
    assert "/checksource" in text
    assert "pubDate" in text


def test_menu_and_time_keyboards_have_controls():
    buttons = [button.text for row in main_keyboard().keyboard for button in row]
    assert "📬 Дайджест" in buttons
    assert "🕐 Время" in buttons
    assert "❓ Помощь" in buttons
    assert MENU_UNSUBSCRIBE in buttons

    callbacks = [
        button.callback_data
        for row in time_keyboard("14:00").inline_keyboard
        for button in row
    ]
    assert "time:14" in callbacks
    assert "time:off" in callbacks

    source_callbacks = [
        button.callback_data
        for row in sources_keyboard(
            [{"id": 5, "name": "Мой блог", "topic": "news", "keywords": "", "max_items": 2}]
        ).inline_keyboard
        for button in row
    ]
    assert "keys:5" in source_callbacks
    assert "limit:5" in source_callbacks
    assert "del:5" in source_callbacks


def test_custom_block_has_date_and_link():
    text = render_custom_block([ITEM])
    assert "Мои источники" in text
    assert "19.11.2026" in text
    assert '<a href="https://example.com/post">' in text


class FakeState:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.cleared = False

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self.cleared = True


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.from_user = SimpleNamespace(id=42, username="tester")
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)

    async def edit_text(self, text, **kwargs):
        self.answers.append(text)


class FakeCallback:
    def __init__(self, data):
        self.data = data
        self.from_user = SimpleNamespace(id=42, username="tester")
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)


def test_time_callback_saves_hour():
    upsert_user(42)
    callback = FakeCallback("time:14")

    asyncio.run(handlers.choose_time(callback, FakeState()))

    assert get_settings(42)["digest_time"] == "14:00"
    assert "14:00" in callback.message.answers[-1]


def test_unsubscribe_command_keeps_sources_and_answers():
    upsert_user(42, "tester")
    set_topics(42, {"news": True})
    add_custom_source(42, "news", "Мой блог", "https://example.com/rss")
    message = FakeMessage()

    asyncio.run(handlers.cmd_unsubscribe(message, FakeState()))

    assert "Подписка отключена" in message.answers[-1]
    assert not any(get_settings(42)["topics"].values())
    assert get_settings(42)["delivery_enabled"] is False
    assert len(list_custom_sources(42)) == 1


def test_edit_keywords_flow_saves_new_keywords(monkeypatch):
    upsert_user(42)
    source_id = add_custom_source(42, "news", "Мой блог", "https://example.com/rss", "аудит")
    monkeypatch.setattr(
        handlers,
        "check_source",
        AsyncMock(return_value={"ok": True, "matched": 3}),
    )
    state = FakeState()

    asyncio.run(handlers.edit_source_keywords_callback(FakeCallback(f"keys:{source_id}"), state))
    message = FakeMessage("ставка")
    asyncio.run(handlers.edit_source_keywords_input(message, state))

    assert state.data["source_id"] == source_id
    assert get_custom_source(42, source_id)["keywords"] == "ставка"
    assert "обновлены" in message.answers[0]
    assert "3" in message.answers[0]


def test_edit_limit_flow_saves_per_source_limit():
    upsert_user(42)
    source_id = add_custom_source(42, "news", "Мой блог", "https://example.com/rss")
    state = FakeState()

    asyncio.run(handlers.edit_source_limit_callback(FakeCallback(f"limit:{source_id}"), state))
    message = FakeMessage("2")
    asyncio.run(handlers.edit_source_limit_input(message, state))

    assert get_custom_source(42, source_id)["max_items"] == 2
    assert "до 2 пунктов" in message.answers[0]


def test_add_source_flow_saves_verified_feed(monkeypatch):
    upsert_user(42)
    monkeypatch.setattr(
        handlers,
        "check_source",
        AsyncMock(return_value={"ok": True, "title": "Мой блог", "sample": ["One"]}),
    )
    monkeypatch.setattr(handlers, "fetch_custom_items", AsyncMock(return_value=[]))
    state = FakeState({"topic": "news"})

    asyncio.run(handlers.source_url_input(FakeMessage("https://example.com/rss"), state))
    assert state.data["url"] == "https://example.com/rss"
    asyncio.run(handlers.source_name_input(FakeMessage("-"), state))
    asyncio.run(handlers.source_keywords_input(FakeMessage("аудит"), state))

    rows = list_custom_sources(42)
    assert len(rows) == 1
    assert rows[0]["name"] == "Мой блог"
    assert rows[0]["keywords"] == "аудит"
    assert rows[0]["topic"] == "news"


def test_personal_digest_appends_custom_block(monkeypatch):
    monkeypatch.setattr(digest_service, "get_digest", AsyncMock(return_value="Общий блок"))
    monkeypatch.setattr(
        digest_service,
        "list_custom_sources",
        lambda user_id: [{"topic": "news", "url": "https://example.com/rss"}],
    )
    monkeypatch.setattr(
        digest_service,
        "fetch_custom_items",
        AsyncMock(return_value=[ITEM]),
    )

    text = asyncio.run(digest_service.render_user_digest({"news": True}, NOW, 7))

    assert "Общий блок" in text
    assert "🔌 Мои источники" in text
    assert "https://example.com/post" in text
