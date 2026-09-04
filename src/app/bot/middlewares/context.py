"""Мидлвари: сессия БД и определение сотрудника."""

from collections.abc import Awaitable, Callable
from typing import Any

import sqlalchemy as sa
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from app.db.models import Employee
from app.db.session import get_sessionmaker
from app.logging import get_logger

log = get_logger(__name__)


class DbSessionMiddleware(BaseMiddleware):
    """Одна транзакция на апдейт: коммит при успехе, откат при исключении."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with get_sessionmaker()() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise


class EmployeeMiddleware(BaseMiddleware):
    """Подставляет сотрудника по telegram_id.

    Незарегистрированный или отключённый пользователь до хендлеров не доходит:
    отвечать на задания может только тот, кто заведён в системе.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None:
            return None

        session = data["session"]
        employee = (
            await session.execute(
                sa.select(Employee).where(Employee.telegram_id == user.id)
            )
        ).scalar_one_or_none()

        if employee is None or not employee.is_active:
            from app.bot import texts

            text = texts.UNKNOWN_USER if employee is None else texts.INACTIVE_USER
            if isinstance(event, Message):
                await event.answer(text)
            elif isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            log.info(
                "bot.rejected_user",
                telegram_id=user.id,
                known=employee is not None,
            )
            return None

        data["employee"] = employee
        return await handler(event, data)
