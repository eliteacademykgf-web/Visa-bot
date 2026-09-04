"""Точка входа процесса бота."""

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.redis import RedisStorage

from app.bot.handlers import common, reactions
from app.bot.middlewares.context import DbSessionMiddleware, EmployeeMiddleware
from app.bot.notifier import TelegramNotifier
from app.config import get_settings
from app.db.session import dispose_engine
from app.logging import configure_logging, get_logger

log = get_logger(__name__)


def build_bot() -> Bot:
    """Экземпляр Bot с общими настройками."""
    return Bot(
        token=get_settings().bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=None),
    )


def build_dispatcher(bot: Bot, storage: RedisStorage) -> Dispatcher:
    """Диспетчер с мидлварями и роутерами.

    Порядок мидлварей важен: сессия создаётся раньше, чем по ней ищут
    сотрудника. Мидлвари вешаются и на сообщения, и на колбэки — задание
    закрывается кнопкой, и неизвестный пользователь не должен туда пройти.
    """
    dispatcher = Dispatcher(storage=storage)
    dispatcher["notifier"] = TelegramNotifier(bot)

    for observer in (dispatcher.message, dispatcher.callback_query):
        observer.middleware(DbSessionMiddleware())
        observer.middleware(EmployeeMiddleware())

    # common идёт первым: команды не должны перехватываться другими роутерами.
    dispatcher.include_router(common.router)
    dispatcher.include_router(reactions.router)
    return dispatcher


async def run() -> None:
    """Запустить бота на long polling."""
    configure_logging()
    settings = get_settings()

    bot = build_bot()
    storage = RedisStorage.from_url(settings.redis_url)
    dispatcher = build_dispatcher(bot, storage)

    log.info("bot.starting")
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(bot)
    finally:
        log.info("bot.stopping")
        await storage.close()
        await bot.session.close()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(run())
