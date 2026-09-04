"""Команды: /start, /status, /slots."""

import sqlalchemy as sa
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import alert_keyboard
from app.db.models import Alert, City, Employee, MonitorTarget, SlotEvent, SlotState
from app.domain.timeutils import format_for_city
from app.enums import CheckStatus

router = Router(name="common")

_STATUS_LABELS = {
    CheckStatus.NO_SLOTS: "слотов нет",
    CheckStatus.SLOT_AVAILABLE: "слот доступен",
    CheckStatus.SLOT_CHANGED: "дата изменилась",
    CheckStatus.SLOT_DISAPPEARED: "слот исчез",
    CheckStatus.AUTH_REQUIRED: "нужна авторизация",
    CheckStatus.CAPTCHA_REQUIRED: "запрошена CAPTCHA",
    CheckStatus.ACCESS_BLOCKED: "доступ ограничен",
    CheckStatus.SITE_CHANGED: "изменился сайт",
    CheckStatus.SYSTEM_ERROR: "ошибка мониторинга",
}


@router.message(Command("start"))
async def on_start(message: Message) -> None:
    """Приветствие."""
    await message.answer(texts.START)


@router.message(Command("status"))
async def on_status(message: Message, session: AsyncSession, employee: Employee) -> None:
    """Открытые уведомления, адресованные этому сотруднику."""
    rows = (
        await session.execute(
            sa.select(Alert, City, SlotEvent)
            .join(SlotEvent, SlotEvent.id == Alert.event_id)
            .join(MonitorTarget, MonitorTarget.id == SlotEvent.target_id)
            .join(City, City.id == MonitorTarget.city_id)
            .where(Alert.closed_at.is_(None), Alert.assignee_id == employee.id)
            .order_by(Alert.created_at)
        )
    ).all()

    if not rows:
        await message.answer(texts.NO_OPEN_ALERTS)
        return

    for alert, city, event in rows:
        when = format_for_city(alert.sent_at or alert.created_at, city.timezone)
        date_text = event.new_date.strftime("%d.%m.%Y") if event.new_date else "—"
        await message.answer(
            f"Уведомление №{alert.id} · {city.name}\n"
            f"Дата: {date_text}\nОтправлено: {when}",
            reply_markup=alert_keyboard(alert.id),
        )


@router.message(Command("slots"))
async def on_slots(message: Message, session: AsyncSession, employee: Employee) -> None:
    """Текущая картина по всем активным целям (ТЗ §12, таблица мониторинга)."""
    rows = (
        await session.execute(
            sa.select(City, SlotState)
            .join(MonitorTarget, MonitorTarget.city_id == City.id)
            .outerjoin(SlotState, SlotState.target_id == MonitorTarget.id)
            .where(MonitorTarget.is_active.is_(True), City.is_active.is_(True))
            .order_by(City.priority, City.name)
        )
    ).all()

    if not rows:
        await message.answer(texts.NO_TARGETS)
        return

    lines = []
    for city, state in rows:
        if state is None:
            lines.append(f"{city.name}: проверок ещё не было")
            continue
        status = _STATUS_LABELS.get(state.status, state.status.value)
        nearest = state.nearest_date.strftime("%d.%m.%Y") if state.nearest_date else "—"
        checked = format_for_city(state.last_check_at, city.timezone, "%H:%M")
        lines.append(f"{city.name}: {status}, ближайшая {nearest} (проверено {checked})")

    await message.answer("Текущая картина:\n" + "\n".join(lines))
