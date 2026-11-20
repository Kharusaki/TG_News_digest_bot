"""Точка входа Telegram-бота с фоновым планировщиком APScheduler."""

from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

from bot.handlers import router
from database.db import init_db
from scheduler.jobs import collect_digests_job, deliver_digests
from services.cache import get_client
from sources.base import MOSCOW

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def collect_time() -> tuple[int, int]:
    """Время планового сбора; по умолчанию 08:30 по Москве."""
    raw = os.getenv("DIGEST_COLLECT_TIME", "08:30").strip() or "08:30"
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (TypeError, ValueError):
        pass
    logger.warning("Некорректный DIGEST_COLLECT_TIME=%r, использую 08:30", raw)
    return 8, 30


def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    """Сбор раз в день и почасовая раздача по настройкам пользователей."""
    hour, minute = collect_time()
    scheduler = AsyncIOScheduler(timezone=MOSCOW)
    scheduler.add_job(
        collect_digests_job,
        trigger="cron",
        hour=hour,
        minute=minute,
        id="collect_digests",
        replace_existing=True,
    )
    scheduler.add_job(
        deliver_digests,
        trigger="cron",
        minute=0,
        args=[bot],
        id="deliver_digests",
        replace_existing=True,
    )
    return scheduler


async def main() -> None:
    init_db()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Не задан BOT_TOKEN — проверьте файл .env")

    proxy = os.getenv("TELEGRAM_PROXY", "").strip()
    session = AiohttpSession(proxy=proxy) if proxy else None
    bot = Bot(token=token, session=session)

    # Явно показываем, какой именно токен сейчас работает: при смене токена
    # в .env старый процесс нужно перезапустить, иначе кнопки будет обслуживать
    # другой бот.
    try:
        me = await bot.get_me()
    except Exception:
        logger.exception("Telegram не принял BOT_TOKEN — проверьте токен в .env")
        await bot.session.close()
        raise
    logger.info("Активный бот: @%s (id=%s)", me.username, me.id)

    # Снимаем возможный webhook другого запуска: иначе getUpdates молчит и
    # инлайн-кнопки выглядят «мёртвыми».
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as exc:
        logger.warning("Не удалось снять webhook: %s", exc)

    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)

    @dispatcher.errors()
    async def on_error(event: ErrorEvent) -> None:
        """Не оставлять пользователя со «мёртвой» кнопкой при сбое хендлера."""
        logger.error(
            "Ошибка обработки update: %s",
            event.exception,
            exc_info=(
                type(event.exception),
                event.exception,
                event.exception.__traceback__,
            ),
        )
        callback = getattr(event.update, "callback_query", None)
        if callback is not None:
            try:
                await callback.answer(
                    "Не получилось обработать кнопку. Отправьте /start.",
                    show_alert=True,
                )
            except Exception as exc:
                logger.debug("Не удалось ответить на callback: %s", exc)

    updates = dispatcher.resolve_used_update_types()

    scheduler = build_scheduler(bot)
    scheduler.start()
    collect_hour, collect_minute = collect_time()
    logger.info(
        "Планировщик запущен: сбор в %02d:%02d, рассылка каждый час по настройкам пользователей",
        collect_hour,
        collect_minute,
    )

    try:
        await dispatcher.start_polling(bot, allowed_updates=updates)
    finally:
        scheduler.shutdown(wait=False)
        try:
            client = await get_client()
            await client.aclose()
        except Exception as exc:
            logger.debug("Redis не закрылся штатно: %s", exc)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
