"""Хендлеры: меню, настройки, время рассылки, справка и свои источники.

Кнопки не привязаны ни к токену, ни к администратору. Обработчик-«ловушка» в
конце отвечает даже на устаревшие callback-и, чтобы у клиента не крутился
спиннер, а глобальный обработчик ошибок в ``main.py`` страхует хендлеры.
"""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.help import builtin_sources_text, help_text
from bot.keyboards import (
    MENU_ADD_SOURCE,
    MENU_DIGEST,
    MENU_HELP,
    MENU_SETTINGS,
    MENU_SOURCES,
    MENU_TIME,
    MENU_UNSUBSCRIBE,
    TOPIC_LABELS,
    help_keyboard,
    main_keyboard,
    settings_keyboard,
    source_topic_keyboard,
    sources_keyboard,
    time_keyboard,
)
from bot.utils import send_html
from database.db import (
    DEFAULT_CUSTOM_LIMIT,
    MAX_CUSTOM_DIGEST_ITEMS,
    MAX_CUSTOM_LIMIT,
    MAX_CUSTOM_SOURCES,
    TOPICS,
    add_custom_source,
    delete_custom_source,
    get_custom_source,
    get_settings,
    list_custom_sources,
    normalize_custom_limit,
    set_digest_time,
    set_topics,
    unsubscribe,
    update_custom_source,
    upsert_user,
)
from scheduler.jobs import build_digest_for
from sources import max_age_days
from sources.custom import check_source, fetch_custom_items

logger = logging.getLogger(__name__)
router = Router()


class SettingsStates(StatesGroup):
    editing = State()


class AddSourceStates(StatesGroup):
    topic = State()
    url = State()
    name = State()
    keywords = State()


class CheckStates(StatesGroup):
    url = State()


class EditSourceStates(StatesGroup):
    keywords = State()
    limit = State()


def _default_topics() -> dict[str, bool]:
    return {topic: False for topic in TOPICS}


def _stored_settings(user_id: int) -> tuple[dict[str, bool], str, bool]:
    stored = get_settings(user_id) or {}
    return (
        dict(stored.get("topics") or _default_topics()),
        str(stored.get("digest_time") or "09:00"),
        bool(stored.get("delivery_enabled", True)),
    )


async def _show_settings(message: Message, state: FSMContext) -> None:
    topics, digest_time, enabled = _stored_settings(message.from_user.id)
    await state.set_state(SettingsStates.editing)
    await state.update_data(topics=topics)
    await message.answer(
        "Выберите темы дайджеста и нажмите «Сохранить»:",
        reply_markup=settings_keyboard(topics, digest_time, enabled),
    )


async def _do_unsubscribe(message: Message, state: FSMContext | None = None) -> None:
    """Снять все подписки и остановить авторассылку, не удаляя данные."""
    upsert_user(message.from_user.id, message.from_user.username or "")
    unsubscribe(message.from_user.id)
    if state is not None:
        await state.clear()
    await message.answer(
        "Подписка отключена: темы сняты, авторассылка остановлена. "
        "Ваши источники и настройки времени сохранены. Вернуть можно через /settings.",
        reply_markup=main_keyboard(),
    )


async def _edit_or_send(callback: CallbackQuery, text: str, markup: object, **kwargs) -> None:
    """Обновить сообщение кнопки; если оно старое — отправить новое."""
    try:
        if callback.message is not None:
            await callback.message.edit_text(text, reply_markup=markup, **kwargs)
            return
    except Exception as exc:
        logger.debug("Не удалось изменить сообщение кнопки: %s", exc)
    await callback.bot.send_message(callback.from_user.id, text, reply_markup=markup, **kwargs)


async def _show_time(callback: CallbackQuery, state: FSMContext | None = None) -> None:
    _, digest_time, enabled = _stored_settings(callback.from_user.id)
    if state is not None:
        await state.clear()
    text = (
        "Выберите час, в который присылать дайджест из утреннего кеша. "
        "Рассылка приходит в начале выбранного часа."
    )
    await _edit_or_send(callback, text, time_keyboard(digest_time, enabled))


def _sources_text(user_id: int) -> str:
    sources = list_custom_sources(user_id)
    if not sources:
        return (
            "Своих источников пока нет. Пришлите /addsource, чтобы добавить RSS-ленту.\n"
            "Вшитые источники можно посмотреть в /help."
        )
    lines = [f"Ваши источники ({len(sources)} из {MAX_CUSTOM_SOURCES}):"]
    for source in sources:
        keywords = str(source.get("keywords") or "").strip() or "все записи"
        lines.append(
            f"• #{source['id']} {html.escape(source['name'])} "
            f"[{TOPIC_LABELS.get(source['topic'], source['topic'])}]\n"
            f"  {html.escape(source['url'])}\n"
            f"  Ключи: {html.escape(keywords)} · "
            f"Пунктов в дайджесте: {source.get('max_items', DEFAULT_CUSTOM_LIMIT)}"
        )
    lines.append("Изменить ключи — ✏️, число пунктов — 🔢, удалить — ❌.")
    return "\n".join(lines)


async def _send_sources(target: CallbackQuery | Message, state: FSMContext | None = None) -> None:
    """Показать список лент и кнопки редактирования из сообщения или колбэка."""
    user_id = target.from_user.id
    if state is not None:
        await state.clear()
    markup = sources_keyboard(list_custom_sources(user_id))
    text = _sources_text(user_id)
    if isinstance(target, CallbackQuery):
        await _edit_or_send(target, text, markup, parse_mode="HTML")
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=markup)


async def _show_sources(callback: CallbackQuery, state: FSMContext | None = None) -> None:
    await _send_sources(callback, state)


async def _check_report(target: Message, url: str, keywords: str = "") -> None:
    result = await check_source(url, keywords)
    if result.get("ok"):
        samples = "\n".join(f"• {item}" for item in result.get("sample", []))
        matched = ""
        if result.get("matched") is not None:
            matched = f", под ключи: {result['matched']}"
        text = (
            "✅ Лента подходит для дайджеста.\n"
            f"Название: {result.get('title', '—')}\n"
            f"Записей: {result.get('entries', 0)}, "
            f"с датой: {result.get('dated', 0)}, "
            f"со ссылкой: {result.get('linked', 0)}{matched}\n"
            f"Примеры заголовков:\n{samples}\n\n"
            "Добавить: /addsource"
        )
    else:
        text = f"❌ Лента не подойдёт: {result.get('error', 'неизвестная ошибка')}"
    await target.answer(text, reply_markup=main_keyboard())


# --- Команды (регистрируются раньше текстовых состояний) --------------------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    upsert_user(message.from_user.id, message.from_user.username or "")
    await message.answer(
        "Готово. Управление ботом — кнопками под полем ввода.",
        reply_markup=main_keyboard(),
    )
    await _show_settings(message, state)


@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext) -> None:
    upsert_user(message.from_user.id, message.from_user.username or "")
    await _show_settings(message, state)


@router.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message, state: FSMContext) -> None:
    await _do_unsubscribe(message, state)


@router.message(Command("time"))
async def cmd_time(message: Message, state: FSMContext) -> None:
    upsert_user(message.from_user.id, message.from_user.username or "")
    _, digest_time, enabled = _stored_settings(message.from_user.id)
    await state.clear()
    await message.answer(
        "Выберите час, в который присылать дайджест из утреннего кеша.",
        reply_markup=time_keyboard(digest_time, enabled),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(help_text(), parse_mode="HTML", reply_markup=help_keyboard())


@router.message(Command("sources"))
async def cmd_sources(message: Message, state: FSMContext) -> None:
    upsert_user(message.from_user.id, message.from_user.username or "")
    await state.clear()
    await message.answer(
        _sources_text(message.from_user.id),
        parse_mode="HTML",
        reply_markup=sources_keyboard(list_custom_sources(message.from_user.id)),
    )


@router.message(Command("helpsources"))
async def cmd_builtin_sources(message: Message) -> None:
    await message.answer(builtin_sources_text(), parse_mode="HTML")


@router.message(Command("addsource"))
async def cmd_addsource(message: Message, state: FSMContext) -> None:
    upsert_user(message.from_user.id, message.from_user.username or "")
    await state.set_state(AddSourceStates.topic)
    await message.answer("Для какой темы добавить источник?", reply_markup=source_topic_keyboard())


@router.message(Command("checksource"))
async def cmd_checksource(message: Message, state: FSMContext) -> None:
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await state.set_state(CheckStates.url)
        await message.answer("Пришлите ссылку на RSS/Atom-ленту для проверки.")
        return
    await state.clear()
    url = parts[1].strip()
    # Ключевые слова можно передать сразу: /checksource ССЫЛКА банки, ставка
    keywords = parts[2].strip() if len(parts) > 2 else ""
    await _check_report(message, url, keywords)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Ввод отменён.", reply_markup=main_keyboard())


@router.message(Command("digest_now"))
async def cmd_digest_now(message: Message) -> None:
    """Свежие блоки за сегодня: из кеша или собранные заново."""
    await message.answer(
        "Формирую дайджест… Свежие блоки за сегодня возьму из кеша, "
        "недостающие соберу заново."
    )
    try:
        text = await build_digest_for(
            {topic: True for topic in TOPICS},
            user_id=message.from_user.id,
        )
    except Exception as exc:
        logger.exception("Ошибка ручного дайджеста")
        await message.answer(f"Не удалось сформировать дайджест: {exc}")
        return
    if not text:
        await message.answer("Пока нет материалов: попробуйте позже.")
        return
    await send_html(message.bot, message.chat.id, text)


# --- Инлайн-кнопки ---------------------------------------------------------

@router.callback_query(F.data.startswith("topic:"))
async def toggle_topic(callback: CallbackQuery, state: FSMContext) -> None:
    # Сначала гасим спиннер: даже если сообщение старое, кнопка должна «отработать».
    await callback.answer()
    data = await state.get_data()
    topics = dict(data.get("topics") or _default_topics())
    topic = (callback.data or "").split(":", 1)[1]
    if topic in topics:
        topics[topic] = not topics[topic]
    await state.update_data(topics=topics)

    _, digest_time, enabled = _stored_settings(callback.from_user.id)
    markup = settings_keyboard(topics, digest_time, enabled)
    try:
        if callback.message is not None:
            await callback.message.edit_reply_markup(reply_markup=markup)
            return
    except Exception as exc:
        logger.debug("Не удалось обновить клавиатуру на месте: %s", exc)

    try:
        await callback.bot.send_message(
            callback.from_user.id,
            "Выберите темы дайджеста и нажмите «Сохранить»:",
            reply_markup=markup,
        )
    except Exception as exc:
        logger.warning("Не удалось отправить клавиатуру пользователю: %s", exc)


@router.callback_query(F.data == "save")
async def save_topics(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Сохраняю…")
    data = await state.get_data()
    topics = dict(data.get("topics") or _default_topics())
    # Строка могла ещё не существовать — upsert гарантирует, что UPDATE сохранит флаги.
    upsert_user(callback.from_user.id, callback.from_user.username or "")
    set_topics(callback.from_user.id, topics)
    await state.clear()

    _, digest_time, enabled = _stored_settings(callback.from_user.id)
    when = f"в {digest_time}" if enabled else "отключена"
    text = f"Настройки сохранены. Рассылка {when}."
    try:
        if callback.message is not None:
            await callback.message.edit_text(text, reply_markup=None)
            return
    except Exception as exc:
        logger.debug("Не удалось изменить сообщение настроек: %s", exc)
    await callback.bot.send_message(callback.from_user.id, text)


@router.callback_query(F.data.startswith("time:"))
async def choose_time(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    raw = (callback.data or "").split(":", 1)[1]
    if raw == "off":
        set_digest_time(callback.from_user.id, "")
        text = "Авторассылка выключена. /digest_now по-прежнему доступен."
    elif raw == "on":
        set_digest_time(callback.from_user.id, "09:00")
        text = "Авторассылка включена на 09:00."
    else:
        value = set_digest_time(callback.from_user.id, raw)
        if value is None:
            await callback.answer("Некорректное время", show_alert=True)
            return
        text = f"Рассылка перенесена на {value}. Данные будут взяты из утреннего кеша."
    await state.clear()
    _, digest_time, enabled = _stored_settings(callback.from_user.id)
    await _edit_or_send(callback, text, time_keyboard(digest_time, enabled))


@router.callback_query(F.data == "show:time")
async def show_time_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _show_time(callback, state)


@router.callback_query(F.data == "show:settings")
@router.callback_query(F.data == "back:settings")
async def show_settings_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    topics, digest_time, enabled = _stored_settings(callback.from_user.id)
    await state.set_state(SettingsStates.editing)
    await state.update_data(topics=topics)
    await _edit_or_send(
        callback,
        "Выберите темы дайджеста и нажмите «Сохранить»:",
        settings_keyboard(topics, digest_time, enabled),
    )


@router.callback_query(F.data == "show:sources")
async def show_sources_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _show_sources(callback, state)


@router.callback_query(F.data == "show:help")
async def show_help_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await _edit_or_send(callback, help_text(), help_keyboard(), parse_mode="HTML")


@router.callback_query(F.data.startswith("src_topic:"))
async def choose_source_topic(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    topic = (callback.data or "").split(":", 1)[1]
    if topic not in TOPICS:
        await callback.answer("Неизвестная тема", show_alert=True)
        return
    await state.set_state(AddSourceStates.url)
    await state.update_data(topic=topic)
    await _edit_or_send(
        callback,
        f"Тема: {TOPIC_LABELS[topic]}. Пришлите ссылку на RSS/Atom-ленту.",
        None,
    )


@router.callback_query(F.data == "src:add")
async def add_source_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AddSourceStates.topic)
    await _edit_or_send(callback, "Для какой темы добавить источник?", source_topic_keyboard())


@router.callback_query(F.data == "src:cancel")
async def cancel_source_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Отменено")
    await state.clear()
    await _edit_or_send(callback, "Ввод отменён.", main_keyboard())


@router.callback_query(F.data.startswith("keys:"))
async def edit_source_keywords_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        source_id = int((callback.data or "").split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный номер", show_alert=True)
        return
    source = get_custom_source(callback.from_user.id, source_id)
    if source is None:
        await callback.answer("Источник не найден", show_alert=True)
        return
    await state.set_state(EditSourceStates.keywords)
    await state.update_data(source_id=source_id)
    current = str(source.get("keywords") or "").strip() or "-"
    await _edit_or_send(
        callback,
        f"Источник #{source_id} «{source['name']}».\n"
        f"Текущие ключи: {current}\n\n"
        "Пришлите новые ключи через запятую или «-», чтобы брать все записи.",
        None,
    )


@router.callback_query(F.data.startswith("limit:"))
async def edit_source_limit_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        source_id = int((callback.data or "").split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный номер", show_alert=True)
        return
    source = get_custom_source(callback.from_user.id, source_id)
    if source is None:
        await callback.answer("Источник не найден", show_alert=True)
        return
    await state.set_state(EditSourceStates.limit)
    await state.update_data(source_id=source_id)
    await _edit_or_send(
        callback,
        f"Источник #{source_id} «{source['name']}».\n"
        f"Сейчас в дайджест берётся до "
        f"{source.get('max_items', DEFAULT_CUSTOM_LIMIT)} пунктов.\n\n"
        f"Пришлите число от 1 до {MAX_CUSTOM_LIMIT}.",
        None,
    )


@router.callback_query(F.data.startswith("del:"))
async def delete_source_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        source_id = int((callback.data or "").split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный номер", show_alert=True)
        return
    if delete_custom_source(callback.from_user.id, source_id):
        await callback.answer("Источник удалён")
    else:
        await callback.answer("Источник уже удалён")
    await _show_sources(callback, state)


# --- Пошаговый ввод (registration order важен) -----------------------------

@router.message(CheckStates.url)
async def check_url_input(message: Message, state: FSMContext) -> None:
    await _check_report(message, (message.text or "").strip())
    await state.clear()


@router.message(AddSourceStates.url)
async def source_url_input(message: Message, state: FSMContext) -> None:
    url = (message.text or "").strip()
    result = await check_source(url)
    if not result.get("ok"):
        await message.answer(
            f"❌ {result.get('error', 'лента не подошла')}\n"
            "Пришлите другую ссылку или напишите /cancel."
        )
        return
    await state.update_data(
        url=url,
        auto_name=str(result.get("title") or "Мой источник")[:80],
    )
    await state.set_state(AddSourceStates.name)
    samples = "\n".join(f"• {item}" for item in result.get("sample", []))
    await message.answer(
        "✅ Лента проверена:\n"
        f"{samples}\n\n"
        "Введите название для кнопки или «-», чтобы взять название из ленты."
    )


@router.message(AddSourceStates.name)
async def source_name_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    raw = (message.text or "").strip()
    name = str(data.get("auto_name") or "Мой источник") if raw in {"-", "—"} else raw[:80]
    if not name:
        name = "Мой источник"
    await state.update_data(name=name)
    await state.set_state(AddSourceStates.keywords)
    await message.answer(
        "Ключевые слова через запятую (например: аудит, ставка, евро) или «-», "
        "если нужны все записи. Короткие слова ищутся целиком, длинные — по "
        "основе (ставка = ставки). Приставка * включает все словоформы."
    )


@router.message(AddSourceStates.keywords)
async def source_keywords_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    raw = (message.text or "").strip()
    keywords = "" if raw in {"-", "—", "нет", "все"} else raw[:300]
    source_id = add_custom_source(
        message.from_user.id,
        str(data.get("topic") or ""),
        str(data.get("name") or "Мой источник"),
        str(data.get("url") or ""),
        keywords,
    )
    await state.clear()
    if source_id is None:
        await message.answer(
            f"Не добавил: лимит {MAX_CUSTOM_SOURCES} источников или неизвестная тема.",
            reply_markup=main_keyboard(),
        )
        return
    preview = await fetch_custom_items(
        [
            {
                "topic": str(data.get("topic") or ""),
                "name": str(data.get("name") or "Мой источник"),
                "url": str(data.get("url") or ""),
                "keywords": keywords,
            }
        ],
        {str(data.get("topic") or ""): True},
        max_age_days,
    )
    await message.answer(
        f"Источник «{html.escape(str(data.get('name') or ''))}» добавлен. "
        f"За последние {max_age_days(str(data.get('topic') or ''))} дн. под ваши ключи "
        f"подошло {len(preview)} записей — они попадут в блок «🔌 Мои источники». "
        f"По умолчанию берём до {DEFAULT_CUSTOM_LIMIT} пунктов; "
        "число можно поменять кнопкой 🔢.",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )
    await message.answer(
        _sources_text(message.from_user.id),
        parse_mode="HTML",
        reply_markup=sources_keyboard(list_custom_sources(message.from_user.id)),
    )


@router.message(EditSourceStates.keywords)
async def edit_source_keywords_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    source_id = int(data.get("source_id") or 0)
    source = get_custom_source(message.from_user.id, source_id)
    if source is None:
        await state.clear()
        await message.answer("Источник уже удалён.", reply_markup=main_keyboard())
        return
    raw = (message.text or "").strip()
    keywords = "" if raw in {"-", "—", "нет", "все"} else raw[:300]

    report = await check_source(str(source.get("url") or ""), keywords)
    matched = report.get("matched")
    if report.get("ok") and keywords and matched == 0:
        await message.answer(
            "Под эти ключи не подошло ни одной записи. Пришлите другие ключи "
            "или «-», чтобы брать все записи."
        )
        return
    if not update_custom_source(message.from_user.id, source_id, keywords=keywords):
        await state.clear()
        await message.answer("Не удалось сохранить ключи.", reply_markup=main_keyboard())
        return
    await state.clear()
    note = ""
    if report.get("ok") and matched is not None:
        note = f" Проверка: под ключи подошло {matched} записей."
    elif not report.get("ok"):
        note = " Сейчас проверить ленту не удалось, но настройка сохранена."
    await message.answer(
        f"Ключи источника #{source_id} обновлены.{note}",
        reply_markup=main_keyboard(),
    )
    await _send_sources(message)


@router.message(EditSourceStates.limit)
async def edit_source_limit_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    source_id = int(data.get("source_id") or 0)
    value = normalize_custom_limit((message.text or "").strip())
    if value is None:
        await message.answer(f"Нужно число от 1 до {MAX_CUSTOM_LIMIT}.")
        return
    if not update_custom_source(message.from_user.id, source_id, max_items=value):
        await state.clear()
        await message.answer("Источник уже удалён.", reply_markup=main_keyboard())
        return
    await state.clear()
    await message.answer(
        f"Лимит источника #{source_id}: до {value} пунктов. "
        f"Общий блок «Мои источники» ограничен {MAX_CUSTOM_DIGEST_ITEMS} пунктами.",
        reply_markup=main_keyboard(),
    )
    await _send_sources(message)


# --- Кнопки главного меню --------------------------------------------------

@router.message(F.text == MENU_DIGEST)
async def menu_digest(message: Message) -> None:
    await cmd_digest_now(message)


@router.message(F.text == MENU_SETTINGS)
async def menu_settings(message: Message, state: FSMContext) -> None:
    upsert_user(message.from_user.id, message.from_user.username or "")
    await _show_settings(message, state)


@router.message(F.text == MENU_TIME)
async def menu_time(message: Message, state: FSMContext) -> None:
    await cmd_time(message, state)


@router.message(F.text == MENU_HELP)
async def menu_help(message: Message) -> None:
    await cmd_help(message)


@router.message(F.text == MENU_SOURCES)
async def menu_sources(message: Message, state: FSMContext) -> None:
    await cmd_sources(message, state)


@router.message(F.text == MENU_ADD_SOURCE)
async def menu_add_source(message: Message, state: FSMContext) -> None:
    await cmd_addsource(message, state)


@router.message(F.text == MENU_UNSUBSCRIBE)
async def menu_unsubscribe(message: Message, state: FSMContext) -> None:
    await _do_unsubscribe(message, state)


@router.callback_query()
async def unknown_callback(callback: CallbackQuery) -> None:
    """Ловушка для кнопок от старых сообщений/версий — всегда отвечаем."""
    logger.info("Неизвестная кнопка %r от %s", callback.data, callback.from_user.id)
    await callback.answer(
        "Кнопка устарела. Отправьте /start, чтобы открыть настройки.",
        show_alert=True,
    )


__all__ = ("router",)
