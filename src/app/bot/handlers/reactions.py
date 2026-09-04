"""Обработка нажатий на кнопки уведомления (ТЗ §11)."""

import contextlib

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import CALLBACK_PREFIX, alert_keyboard, parse_reaction_callback
from app.db.models import Employee
from app.enums import ReactionKind
from app.logging import get_logger
from app.services.alert_service import record_reaction

router = Router(name="reactions")
log = get_logger(__name__)


async def _refresh_keyboard(callback: CallbackQuery, alert_id: int, *, closed: bool) -> None:
    """Обновить клавиатуру после реакции.

    У закрытого уведомления кнопки убираются: они уже ничего не изменят,
    а их наличие провоцирует повторные нажатия. У открытого клавиатура
    остаётся — «Принял» не мешает потом отметить «Слот забронирован».

    InaccessibleMessage приходит для сообщений старше 48 часов; редактировать
    их нельзя, и для уведомления, провисевшего выходные, это нормальный исход.
    """
    if not isinstance(callback.message, Message):
        return
    markup = None if closed else alert_keyboard(alert_id)
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_reply_markup(reply_markup=markup)


@router.callback_query(F.data.startswith(f"{CALLBACK_PREFIX}:"))
async def on_reaction(
    callback: CallbackQuery,
    session: AsyncSession,
    employee: Employee,
) -> None:
    """Сотрудник нажал одну из шести кнопок."""
    parsed = parse_reaction_callback(callback.data or "")
    if parsed is None:
        await callback.answer(texts.STALE_CALLBACK, show_alert=True)
        return

    alert_id, kind = parsed
    result = await record_reaction(session, alert_id, employee, kind)

    if not result.accepted:
        # Устаревший колбэк объясняет, что произошло, а не молчит.
        await callback.answer(result.message, show_alert=True)
        await _refresh_keyboard(callback, alert_id, closed=True)
        return

    await callback.answer(result.message)
    closed = result.alert is not None and result.alert.closed_at is not None
    await _refresh_keyboard(callback, alert_id, closed=closed)

    if isinstance(callback.message, Message):
        await callback.message.answer(result.message)

    log.info(
        "bot.reaction_handled",
        alert_id=alert_id,
        kind=kind.value,
        employee_id=employee.id,
        closed=closed,
    )


@router.callback_query(F.data == "noop")
async def on_noop(callback: CallbackQuery) -> None:
    """Заглушка для декоративных кнопок."""
    await callback.answer()


__all__ = ["ReactionKind", "router"]
