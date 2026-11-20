"""Кнопки: стабильные callback-и, переключение и отправка сообщений."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import handlers
from bot.keyboards import settings_keyboard
from bot.utils import SAFE_LIMIT, send_html, split_message


def _callbacks(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


def test_settings_keyboard_has_stable_callbacks():
    callbacks = _callbacks(settings_keyboard({"news": True}))
    assert callbacks == [
        "topic:news",
        "topic:jobs",
        "topic:tech",
        "topic:msfo",
        "save",
        "show:time",
        "show:sources",
        "show:help",
    ]
    text = settings_keyboard({"news": True}).inline_keyboard[0][0].text
    assert "✅" in text and "Новости" in text
    label = settings_keyboard({}).inline_keyboard[3][0].text
    assert "Аудит и МСФО" in label


def test_split_message_keeps_each_link_intact():
    link = '<a href="https://example.com/news/1">Открыть источник</a>'
    text = "\n".join([link] * 200)
    chunks = split_message(text)
    assert len(chunks) > 1
    assert all(len(chunk) <= SAFE_LIMIT for chunk in chunks)
    assert all("<a href=" in chunk and "</a>" in chunk for chunk in chunks)


def test_send_html_uses_html_parse_mode():
    bot = SimpleNamespace(send_message=AsyncMock())
    asyncio.run(send_html(bot, 1, "текст"))
    bot.send_message.assert_awaited_once_with(
        1,
        "текст",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


class FakeState:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.cleared = False

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def clear(self):
        self.cleared = True


class FakeMessage:
    def __init__(self):
        self.markup = None
        self.text = None

    async def edit_reply_markup(self, reply_markup):
        self.markup = reply_markup

    async def edit_text(self, text, reply_markup=None):
        self.text = text
        self.markup = reply_markup


class FakeCallback:
    def __init__(self, data):
        self.data = data
        self.from_user = SimpleNamespace(id=42, username="tester")
        self.message = FakeMessage()
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)


def test_toggle_topic_answers_and_updates_keyboard():
    state = FakeState(handlers._default_topics())
    callback = FakeCallback("topic:news")

    asyncio.run(handlers.toggle_topic(callback, state))

    assert state.data["topics"]["news"] is True
    assert callback.answers == [None]
    buttons = callback.message.markup.inline_keyboard
    assert buttons[0][0].text.startswith("✅")


def test_save_upserts_user_before_writing_topics(monkeypatch):
    calls = []
    monkeypatch.setattr(handlers, "upsert_user", lambda *args: calls.append("upsert"))
    monkeypatch.setattr(
        handlers,
        "set_topics",
        lambda user_id, topics: calls.append(("topics", user_id, topics)),
    )
    state = FakeState({"topics": {"news": True, "jobs": False, "tech": False, "msfo": True}})
    callback = FakeCallback("save")

    asyncio.run(handlers.save_topics(callback, state))

    assert calls[0] == "upsert"
    assert calls[1][1] == 42
    assert calls[1][2]["news"] is True
    assert state.cleared is True
    assert "сохранены" in callback.message.text
