"""Реализация Notifier поверх aiogram."""

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from app.bot.keyboards import task_keyboard
from app.config import get_settings
from app.logging import get_logger
from app.services.notifications import DeliveryResult

log = get_logger(__name__)


class TelegramNotifier:
    """Отправка сообщений через Telegram.

    Исключения не пробрасываются наверх: недоставленное сообщение — обычный
    рабочий исход (сотрудник не запускал бота или заблокировал его), и решение
    о том, что с этим делать, принимает вызывающий код, а не транспорт.
    """

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_text(
        self,
        chat_id: int,
        text: str,
        *,
        with_task_buttons: int | None = None,
        photo_file_id: str | None = None,
    ) -> DeliveryResult:
        markup = task_keyboard(with_task_buttons) if with_task_buttons else None
        try:
            if photo_file_id:
                message = await self._bot.send_photo(
                    chat_id, photo_file_id, caption=text, reply_markup=markup
                )
            else:
                message = await self._bot.send_message(chat_id, text, reply_markup=markup)
        except TelegramRetryAfter as exc:
            return DeliveryResult(
                delivered=False, error=f"flood control: retry after {exc.retry_after}s"
            )
        except TelegramAPIError as exc:
            return DeliveryResult(delivered=False, error=str(exc))
        return DeliveryResult(delivered=True, message_id=message.message_id)


async def build_notifier() -> TelegramNotifier:
    """Собрать Notifier с собственным экземпляром Bot."""
    token = get_settings().bot_token.get_secret_value()
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=None))
    return TelegramNotifier(bot)
