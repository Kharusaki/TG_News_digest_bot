"""Клавиатуры: главное меню, настройки, время и пользовательские источники."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from database.db import DEFAULT_CUSTOM_LIMIT, TOPICS, normalize_custom_limit

TOPIC_LABELS = {
    "news": "Новости отрасли",
    "jobs": "Вакансии",
    "tech": "Технологии",
    "msfo": "Аудит и МСФО",
}

MENU_DIGEST = "📬 Дайджест"
MENU_SETTINGS = "⚙️ Настройки"
MENU_TIME = "🕐 Время"
MENU_HELP = "❓ Помощь"
MENU_SOURCES = "🔌 Источники"
MENU_ADD_SOURCE = "➕ Добавить источник"
MENU_UNSUBSCRIBE = "🚫 Отписаться"


def main_keyboard() -> ReplyKeyboardMarkup:
    """Постоянные кнопки управления: не надо помнить команды."""
    builder = ReplyKeyboardBuilder()
    for label in (MENU_DIGEST, MENU_SETTINGS, MENU_TIME):
        builder.button(text=label)
    for label in (MENU_HELP, MENU_SOURCES, MENU_ADD_SOURCE):
        builder.button(text=label)
    builder.button(text=MENU_UNSUBSCRIBE)
    builder.adjust(3, 3, 1)
    return builder.as_markup(resize_keyboard=True, input_field_placeholder="Выберите действие")


def settings_keyboard(
    topics: dict[str, bool],
    digest_time: str = "09:00",
    delivery_enabled: bool = True,
) -> InlineKeyboardMarkup:
    """Переключатели тем, сохранение и быстрый переход к остальным настройкам."""
    builder = InlineKeyboardBuilder()
    for topic in TOPICS:
        mark = "✅" if topics.get(topic) else "⬜"
        builder.button(text=f"{mark} {TOPIC_LABELS[topic]}", callback_data=f"topic:{topic}")
    builder.button(text="💾 Сохранить", callback_data="save")
    if delivery_enabled:
        time_label = f"🕐 Рассылка в {digest_time}"
    else:
        time_label = "🕐 Рассылка выключена"
    builder.button(text=time_label, callback_data="show:time")
    builder.button(text="🔌 Мои источники", callback_data="show:sources")
    builder.button(text="❓ Помощь", callback_data="show:help")
    builder.adjust(1)
    return builder.as_markup()


def time_keyboard(current: str = "09:00", enabled: bool = True) -> InlineKeyboardMarkup:
    """Выбор часа доставки: с 09:00 до 23:00, текущий помечен галочкой."""
    builder = InlineKeyboardBuilder()
    for hour in range(9, 24):
        value = f"{hour:02d}:00"
        mark = "✅" if enabled and value == current else ""
        builder.button(text=f"{mark}{value}", callback_data=f"time:{hour:02d}")
    builder.button(
        text="🔕 Выключить рассылку" if enabled else "🔔 Включить рассылку",
        callback_data="time:off" if enabled else "time:on",
    )
    builder.button(text="◀️ Настройки", callback_data="back:settings")
    builder.adjust(5, 5, 5, 2)
    return builder.as_markup()


def source_topic_keyboard() -> InlineKeyboardMarkup:
    """Выбор темы для нового источника."""
    builder = InlineKeyboardBuilder()
    for topic in TOPICS:
        builder.button(text=TOPIC_LABELS[topic], callback_data=f"src_topic:{topic}")
    builder.button(text="Отмена", callback_data="src:cancel")
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def sources_keyboard(sources: list[dict]) -> InlineKeyboardMarkup:
    """Свои ленты: ключи и лимит пунктов редактируются отдельно по каждой."""
    builder = InlineKeyboardBuilder()
    for source in sources:
        source_id = source["id"]
        limit = normalize_custom_limit(source.get("max_items")) or DEFAULT_CUSTOM_LIMIT
        builder.button(text=f"✏️ Ключи #{source_id}", callback_data=f"keys:{source_id}")
        builder.button(text=f"🔢 Лимит #{source_id}: {limit}", callback_data=f"limit:{source_id}")
        builder.button(text=f"❌ #{source_id}", callback_data=f"del:{source_id}")
    builder.button(text="➕ Добавить", callback_data="src:add")
    builder.button(text="◀️ Настройки", callback_data="back:settings")
    builder.adjust(*([3] * len(sources) + [2]))
    return builder.as_markup()


def help_keyboard() -> InlineKeyboardMarkup:
    """Быстрые действия из справки."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⚙️ Настройки", callback_data="show:settings")
    builder.button(text="🕐 Время", callback_data="show:time")
    builder.button(text="🔌 Источники", callback_data="show:sources")
    builder.button(text="➕ Добавить", callback_data="src:add")
    builder.adjust(2)
    return builder.as_markup()


__all__ = (
    "MENU_ADD_SOURCE",
    "MENU_DIGEST",
    "MENU_HELP",
    "MENU_SETTINGS",
    "MENU_SOURCES",
    "MENU_TIME",
    "MENU_UNSUBSCRIBE",
    "TOPIC_LABELS",
    "help_keyboard",
    "main_keyboard",
    "settings_keyboard",
    "source_topic_keyboard",
    "sources_keyboard",
    "time_keyboard",
)
